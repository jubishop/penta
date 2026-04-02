"""Behavioral tests exercising AppState through its public API.

These tests use per-agent FakeAgentService instances to control responses
and verify prompts at the behavioral boundary.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Footer

from penta.app import PentaApp
from penta.app_state import AppState
from penta.models.agent_status import AgentStatus
from penta.models.agent_type import AgentType


async def _drain(app: AppState) -> None:
    """Wait until all routing tasks (including cascaded ones) are done."""
    await app.router.drain()


class TestUserMessageRouting:
    async def test_message_reaches_both_agents(self, multi_agent_state):
        app, services = multi_agent_state

        await app.add_agent("claude", AgentType.CLAUDE)
        await app.add_agent("codex", AgentType.CODEX)
        services["claude"].enqueue_text("claude says hi")
        services["codex"].enqueue_text("codex says hi")

        await app.send_user_message("hello everyone")
        await _drain(app)

        assert len(services["claude"].calls) == 1
        assert len(services["codex"].calls) == 1
        assert "hello everyone" in services["claude"].calls[0].prompt
        assert "hello everyone" in services["codex"].calls[0].prompt

    async def test_mention_routes_to_mentioned_only(self, multi_agent_state):
        app, services = multi_agent_state

        await app.add_agent("claude", AgentType.CLAUDE)
        await app.add_agent("codex", AgentType.CODEX)
        services["claude"].enqueue_text("on it")

        await app.send_user_message("@claude help me")
        await _drain(app)

        assert len(services["claude"].calls) == 1
        assert len(services["codex"].calls) == 0

    async def test_responses_appear_in_conversation(self, multi_agent_state):
        app, services = multi_agent_state

        await app.add_agent("claude", AgentType.CLAUDE)
        services["claude"].enqueue_text("hello back")

        await app.send_user_message("@claude hi")
        await _drain(app)

        texts = [m.text for m in app.conversation]
        assert "@claude hi" in texts
        assert "hello back" in texts

    async def test_responses_persisted_to_db(self, multi_agent_state, memory_db):
        app, services = multi_agent_state

        await app.add_agent("claude", AgentType.CLAUDE)
        services["claude"].enqueue_text("saved response")

        await app.send_user_message("@claude persist this")
        await _drain(app)

        rows = await memory_db.get_messages()
        senders = [r[1] for r in rows]
        texts = [r[2] for r in rows]
        assert "User" in senders
        assert "claude" in senders
        assert "saved response" in texts


class TestSessionManagement:
    async def test_session_id_persisted_on_first_turn(self, multi_agent_state, memory_db):
        app, services = multi_agent_state

        await app.add_agent("claude", AgentType.CLAUDE)
        services["claude"].enqueue_text("hi", session_id="sess-abc")

        await app.send_user_message("@claude hello")
        await _drain(app)

        stored = await memory_db.load_session("claude")
        assert stored == "sess-abc"

    async def test_session_id_passed_on_subsequent_turns(self, multi_agent_state):
        app, services = multi_agent_state

        await app.add_agent("claude", AgentType.CLAUDE)
        services["claude"].enqueue_text("first", session_id="sess-123")
        services["claude"].enqueue_text("second")

        await app.send_user_message("@claude turn 1")
        await _drain(app)

        await app.send_user_message("@claude turn 2")
        await _drain(app)

        # Second call should have received the session_id
        assert services["claude"].calls[1].session_id == "sess-123"

    async def test_system_prompt_only_on_first_turn(self, multi_agent_state):
        app, services = multi_agent_state

        await app.add_agent("claude", AgentType.CLAUDE)
        services["claude"].enqueue_text("first", session_id="sess-1")
        services["claude"].enqueue_text("second")

        await app.send_user_message("@claude turn 1")
        await _drain(app)

        await app.send_user_message("@claude turn 2")
        await _drain(app)

        # First turn gets system prompt (identity preamble)
        assert services["claude"].calls[0].system_prompt is not None
        assert "group chat" in services["claude"].calls[0].system_prompt.lower()
        # Second turn (with session) should not
        assert services["claude"].calls[1].system_prompt is None


class TestCrossAgentMentionRouting:
    async def test_agent_response_mentioning_other_triggers_routing(self, multi_agent_state):
        app, services = multi_agent_state

        await app.add_agent("claude", AgentType.CLAUDE)
        await app.add_agent("codex", AgentType.CODEX)
        # Claude's response mentions codex
        services["claude"].enqueue_text("@codex can you help?")
        services["codex"].enqueue_text("sure thing")

        await app.send_user_message("@claude start")
        await _drain(app)

        # Codex should have been triggered by Claude's mention
        assert len(services["codex"].calls) == 1
        assert "@codex can you help?" in services["codex"].calls[0].prompt

    async def test_non_mentioned_agent_receives_context_on_next_prompt(self, multi_agent_state):
        """When an agent is not mentioned, it should still receive the missed
        messages as catch-up context when it is next prompted."""
        app, services = multi_agent_state

        await app.add_agent("claude", AgentType.CLAUDE)
        await app.add_agent("codex", AgentType.CODEX)
        services["claude"].enqueue_text("done")

        await app.send_user_message("@claude only you")
        await _drain(app)

        # Codex was not called
        assert len(services["codex"].calls) == 0

        # Now prompt codex — its prompt should contain the missed context
        services["codex"].enqueue_text("got it")
        await app.send_user_message("@codex your turn")
        await _drain(app)

        codex_prompt = services["codex"].calls[0].prompt
        assert "only you" in codex_prompt


class TestErrorHandling:
    async def test_agent_error_does_not_block_conversation(self, multi_agent_state):
        app, services = multi_agent_state

        await app.add_agent("claude", AgentType.CLAUDE)
        services["claude"].enqueue_error("something broke")

        await app.send_user_message("@claude hi")
        await _drain(app)

        # Error response should be in conversation
        error_msgs = [m for m in app.conversation if m.is_error]
        assert len(error_msgs) == 1
        assert "something broke" in error_msgs[0].text

        # Agent should be back to IDLE (observable via public API)
        claude = app.agent_by_name("claude")
        assert claude.status.name == "IDLE"


class TestCancelAgent:
    async def test_cancel_specific_agent(self, multi_agent_state):
        app, services = multi_agent_state

        claude = await app.add_agent("claude", AgentType.CLAUDE)
        codex = await app.add_agent("codex", AgentType.CODEX)
        services["claude"].enqueue_hang()
        services["codex"].enqueue_hang()

        await app.send_user_message("hello everyone")
        await asyncio.sleep(0)

        cancelled = app.cancel_agent(claude.id)
        assert cancelled is True

        # Codex should still be processing
        assert codex.status == AgentStatus.PROCESSING

    async def test_cancel_all_busy(self, multi_agent_state):
        app, services = multi_agent_state

        await app.add_agent("claude", AgentType.CLAUDE)
        await app.add_agent("codex", AgentType.CODEX)
        services["claude"].enqueue_hang()
        services["codex"].enqueue_hang()

        await app.send_user_message("hello everyone")
        await asyncio.sleep(0)

        count = app.cancel_all_busy()
        assert count == 2

    async def test_cancel_agent_when_idle(self, multi_agent_state):
        app, services = multi_agent_state

        claude = await app.add_agent("claude", AgentType.CLAUDE)
        cancelled = app.cancel_agent(claude.id)
        assert cancelled is False


class _FooterTestApp(App):
    """Minimal app that reuses PentaApp's BINDINGS and check_action."""

    BINDINGS = PentaApp.BINDINGS

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self._state = state

    def compose(self) -> ComposeResult:
        yield Footer()

    check_action = PentaApp.check_action


def _footer_shows_stop(app: App) -> bool:
    """Return True if 'stop_agents' appears in the screen's active bindings."""
    return "escape" in app.screen.active_bindings


class TestStopAgentsFooter:
    """The 'Stop agents' escape binding should only appear in the footer
    when at least one agent is busy."""

    async def test_footer_hidden_when_idle(self, multi_agent_state):
        app_state, services = multi_agent_state
        await app_state.add_agent("claude", AgentType.CLAUDE)

        async with _FooterTestApp(app_state).run_test() as pilot:
            await pilot.pause()
            assert not _footer_shows_stop(pilot.app)

    async def test_footer_visible_when_processing(self, multi_agent_state):
        app_state, services = multi_agent_state
        await app_state.add_agent("claude", AgentType.CLAUDE)
        services["claude"].enqueue_hang()

        await app_state.send_user_message("@claude hello")
        await asyncio.sleep(0)

        async with _FooterTestApp(app_state).run_test() as pilot:
            await pilot.pause()
            assert _footer_shows_stop(pilot.app)

    async def test_footer_hidden_after_finish(self, multi_agent_state):
        app_state, services = multi_agent_state
        await app_state.add_agent("claude", AgentType.CLAUDE)
        services["claude"].enqueue_text("done")

        await app_state.send_user_message("@claude hello")
        await app_state.router.drain()

        async with _FooterTestApp(app_state).run_test() as pilot:
            await pilot.pause()
            assert not _footer_shows_stop(pilot.app)
