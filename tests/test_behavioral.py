"""Behavioral tests exercising AppState through its public API.

These tests use per-agent FakeAgentService instances to control responses
and verify prompts at the behavioral boundary.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from textual.app import App, ComposeResult
from textual.widgets import Footer
from textual.widgets._footer import FooterKey

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

    async def test_cancel_by_name(self, multi_agent_state):
        """cancel_agent works via agent_by_name (the /stop AgentName path)."""
        app, services = multi_agent_state

        await app.add_agent("claude", AgentType.CLAUDE)
        await app.add_agent("codex", AgentType.CODEX)
        services["claude"].enqueue_hang()
        services["codex"].enqueue_hang()

        await app.send_user_message("hello everyone")
        await asyncio.sleep(0)

        agent = app.agent_by_name("claude")
        assert agent is not None
        cancelled = app.cancel_agent(agent.id)
        assert cancelled is True

        codex = app.agent_by_name("codex")
        assert codex is not None
        assert codex.status == AgentStatus.PROCESSING

    async def test_cancel_by_name_case_insensitive(self, multi_agent_state):
        app, services = multi_agent_state

        await app.add_agent("Claude", AgentType.CLAUDE)
        services["Claude"].enqueue_hang()

        await app.send_user_message("@Claude hello")
        await asyncio.sleep(0)

        # agent_by_name is case-insensitive
        agent = app.agent_by_name("claude")
        assert agent is not None
        assert app.cancel_agent(agent.id) is True


class _FooterTestApp(App):
    """Minimal app that reuses PentaApp's BINDINGS and check_action logic."""

    BINDINGS = PentaApp.BINDINGS

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self._state = state
        # Wire the same callback PentaApp uses so status changes refresh the footer
        state.on_status_changed = self._on_status_changed

    def _on_status_changed(self, agent_id: UUID, status: AgentStatus) -> None:
        if self.is_running:
            self.refresh_bindings()

    def compose(self) -> ComposeResult:
        yield Footer()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "stop_agents":
            if not self._state:
                return False
            return any(
                c.config.status.is_busy for c in self._state.coordinators.values()
            )
        return super().check_action(action, parameters)


def _has_stop_footer_key(app: App) -> bool:
    """Return True if the rendered Footer contains a FooterKey for stop_agents."""
    return any(k.action == "stop_agents" for k in app.query(FooterKey))


class TestStopAgentsFooter:
    """The 'Stop agents' escape binding should only appear in the footer
    when at least one agent is busy."""

    async def test_footer_hidden_when_idle(self, multi_agent_state):
        app_state, _ = multi_agent_state
        await app_state.add_agent("claude", AgentType.CLAUDE)

        async with _FooterTestApp(app_state).run_test() as pilot:
            await pilot.pause()
            assert not _has_stop_footer_key(pilot.app)

    async def test_footer_appears_when_agent_starts(self, multi_agent_state):
        """Start idle, trigger processing mid-session, verify footer updates."""
        app_state, services = multi_agent_state
        await app_state.add_agent("claude", AgentType.CLAUDE)
        services["claude"].enqueue_hang()

        async with _FooterTestApp(app_state).run_test() as pilot:
            await pilot.pause()
            assert not _has_stop_footer_key(pilot.app)

            await app_state.send_user_message("@claude hello")
            await asyncio.sleep(0)
            await pilot.pause()

            assert _has_stop_footer_key(pilot.app)

    async def test_footer_visible_when_waiting_for_user(self, multi_agent_state):
        """WAITING_FOR_USER is also is_busy — footer should show stop binding."""
        app_state, _ = multi_agent_state
        claude = await app_state.add_agent("claude", AgentType.CLAUDE)

        async with _FooterTestApp(app_state).run_test() as pilot:
            await pilot.pause()
            assert not _has_stop_footer_key(pilot.app)

            # Simulate agent entering WAITING_FOR_USER (e.g. plan review)
            claude.status = AgentStatus.WAITING_FOR_USER
            pilot.app.refresh_bindings()
            await pilot.pause()

            assert _has_stop_footer_key(pilot.app)

    async def test_footer_disappears_when_agent_finishes(self, multi_agent_state):
        """Start with a busy agent, cancel it, verify footer updates."""
        app_state, services = multi_agent_state
        claude = await app_state.add_agent("claude", AgentType.CLAUDE)
        services["claude"].enqueue_hang()

        await app_state.send_user_message("@claude hello")
        await asyncio.sleep(0)

        async with _FooterTestApp(app_state).run_test() as pilot:
            await pilot.pause()
            assert _has_stop_footer_key(pilot.app)

            app_state.cancel_agent(claude.id)
            await app_state.router.drain()
            await pilot.pause()

            assert not _has_stop_footer_key(pilot.app)
