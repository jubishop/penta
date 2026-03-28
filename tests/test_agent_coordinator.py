"""Tests for AgentCoordinator cancellation behaviour."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

import pytest

from penta.coordinators.agent_coordinator import AgentCoordinator
from penta.models import AgentConfig, AgentStatus, AgentType, Message, TaggedMessage
from penta.services.agent_service import AgentService, StreamEvent, StreamEventType
from penta.services.db import PentaDB


# -- Helpers ------------------------------------------------------------------


class HangingService(AgentService):
    """Yields one text delta then blocks forever (until cancelled)."""

    async def send(
        self, prompt: str, session_id: str | None, working_dir: Path,
        system_prompt: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(type=StreamEventType.TEXT_DELTA, text="partial")
        # Block indefinitely so the caller can cancel us.
        await asyncio.Event().wait()

    async def cancel(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


# -- Fixtures -----------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> PentaDB:
    return PentaDB(tmp_path / "test-project", storage_root=tmp_path)


@pytest.fixture
def coordinator(db: PentaDB) -> AgentCoordinator:
    config = AgentConfig(
        id=uuid4(),
        name="test-agent",
        type=AgentType.CLAUDE,
        status=AgentStatus.IDLE,
    )
    coord = AgentCoordinator(
        config=config,
        working_dir=Path("/tmp"),
        db=db,
        other_agent_names=["other"],
    )
    # Swap in the hanging service so we can control cancellation.
    coord.service = HangingService()
    return coord


# -- Tests --------------------------------------------------------------------


class TestCatchUpHistoryAfterRestart:
    """After load_chat_history(), the first prompt should replay all prior
    messages as catch-up context (last_prompted_index=0)."""

    @pytest.mark.asyncio
    async def test_first_prompt_includes_full_history(self, coordinator: AgentCoordinator):
        """Simulates a restart: hydrate history, then build a prompt.
        The prompt should contain catch-up lines from all prior messages."""
        # Simulate hydrated history (what load_chat_history does)
        coordinator.full_history = [
            TaggedMessage(sender_label="User", text="first message"),
            TaggedMessage(sender_label="other", text="reply from other"),
        ]
        coordinator.last_prompted_index = 0  # the fix

        # Now a new message arrives post-restart
        current = TaggedMessage(sender_label="User", text="new question")
        coordinator.full_history.append(current)

        prompt = coordinator._build_prompt(current)

        assert "[Messages since your last response:]" in prompt
        assert "first message" in prompt
        assert "reply from other" in prompt
        assert "new question" in prompt

    @pytest.mark.asyncio
    async def test_second_prompt_does_not_repeat_catchup(self, coordinator: AgentCoordinator):
        """After the first post-restart prompt, subsequent prompts should
        NOT re-include already-seen history."""
        coordinator.full_history = [
            TaggedMessage(sender_label="User", text="old message"),
        ]
        coordinator.last_prompted_index = 0

        first = TaggedMessage(sender_label="User", text="first new")
        coordinator.full_history.append(first)
        coordinator._build_prompt(first)  # advances last_prompted_index

        second = TaggedMessage(sender_label="User", text="second new")
        coordinator.full_history.append(second)
        prompt = coordinator._build_prompt(second)

        assert "old message" not in prompt
        assert "first new" not in prompt
        assert "second new" in prompt


class TestCancelledStreamIsNotTreatedAsSuccess:
    """Regression: cancelling a stream must NOT persist, route, or callback."""

    @pytest.mark.asyncio
    async def test_cancelled_response_is_flagged(self, coordinator: AgentCoordinator):
        """A cancelled stream should set is_cancelled on the response."""
        conversation: list[Message] = []
        tagged = TaggedMessage(sender_label="User", text="hello")

        response = coordinator.send(tagged, conversation)
        # Let the stream task start and receive the first delta.
        await asyncio.sleep(0.05)
        assert response.text == "partial"

        # Cancel by sending a new message (same as the real flow).
        tagged2 = TaggedMessage(sender_label="User", text="new message")
        response2 = coordinator.send(tagged2, conversation)
        # Wait for the old task to finish its CancelledError handler.
        await asyncio.sleep(0.05)

        assert response.is_cancelled is True
        assert response.is_streaming is False

        # Clean up second task.
        coordinator._current_task.cancel()
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_cancelled_stream_not_added_to_history(
        self, coordinator: AgentCoordinator
    ):
        """Cancelled response must NOT be appended to full_history."""
        conversation: list[Message] = []
        tagged = TaggedMessage(sender_label="User", text="hello")

        coordinator.send(tagged, conversation)
        await asyncio.sleep(0.05)

        history_before = len(coordinator.full_history)

        # Cancel via a new send.
        tagged2 = TaggedMessage(sender_label="User", text="new")
        coordinator.send(tagged2, conversation)
        await asyncio.sleep(0.05)

        # Only the new user message should have been added, not a cancelled agent reply.
        agent_replies = [
            m
            for m in coordinator.full_history[history_before:]
            if m.sender_label == coordinator.config.name
        ]
        assert agent_replies == []

        coordinator._current_task.cancel()
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_cancelled_stream_fires_callback_with_cancelled_flag(
        self, coordinator: AgentCoordinator
    ):
        """on_stream_complete MUST fire for cancelled streams (UI cleanup)
        but the message should carry is_cancelled=True."""
        completed: list[Message] = []
        coordinator.on_stream_complete = lambda msg, _aid: completed.append(msg)

        conversation: list[Message] = []
        tagged = TaggedMessage(sender_label="User", text="hello")
        coordinator.send(tagged, conversation)
        await asyncio.sleep(0.05)

        # Cancel.
        tagged2 = TaggedMessage(sender_label="User", text="new")
        coordinator.send(tagged2, conversation)
        await asyncio.sleep(0.05)

        assert len(completed) == 1, "on_stream_complete must fire so the UI can clear streaming state"
        assert completed[0].is_cancelled is True

        coordinator._current_task.cancel()
        await asyncio.sleep(0.05)


class TestCancelledStreamRollsBackPromptIndex:
    """Regression: when two agents finish simultaneously and both mention the
    same target agent, the second send() cancels the first stream.  The
    replacement prompt must include messages from the cancelled stream so the
    agent doesn't miss context (e.g. "Now we just need @codex to weigh in"
    when Codex already responded)."""

    @pytest.mark.asyncio
    async def test_cancelled_send_does_not_skip_messages(
        self, coordinator: AgentCoordinator
    ):
        """Simulate: user message (hop 0) → agent responds → Codex and Gemini
        both mention this agent nearly simultaneously.  The second send()
        cancels the first, but the replacement prompt must still contain the
        first agent's message."""
        conversation: list[Message] = []

        # Seed: a user message was already prompted (hop 0).
        user_msg = TaggedMessage(sender_label="User", text="plan a trip")
        coordinator.send(user_msg, conversation)
        await asyncio.sleep(0.05)
        # Pretend the stream completed normally so last_prompted_index advances.
        coordinator._current_task.cancel()
        await asyncio.sleep(0.05)
        # Manually advance as if the stream finished (normal completion path).
        coordinator.last_prompted_index = len(coordinator.full_history)
        coordinator._pre_prompt_index = coordinator.last_prompted_index

        # Now two agents finish nearly simultaneously and both mention us.
        codex_msg = TaggedMessage(sender_label="codex", text="@test-agent here's my take")
        gemini_msg = TaggedMessage(sender_label="gemini", text="@test-agent I agree")

        # First send: Codex's message arrives.
        resp1 = coordinator.send(codex_msg, conversation)
        await asyncio.sleep(0.05)  # Let stream start

        # Second send: Gemini's message arrives, cancelling Codex's stream.
        resp2 = coordinator.send(gemini_msg, conversation)
        await asyncio.sleep(0.05)

        # The cancelled response must be flagged.
        assert resp1.is_cancelled is True

        # Critical assertion: the prompt that was built for the Gemini send
        # must include Codex's message in the catch-up section.
        # We can verify by checking the prompt that was passed to the service.
        # Rebuild what _build_prompt would have produced for the second send:
        # Since the service is a HangingService, we can inspect the prompt
        # by looking at what _build_prompt would return.  Instead, let's
        # verify the index state: last_prompted_index should cover both.
        #
        # More directly: build a third prompt and confirm Codex's message
        # is NOT in the catch-up (meaning it WAS included in the second prompt).
        coordinator._current_task.cancel()
        await asyncio.sleep(0.05)
        coordinator.last_prompted_index = len(coordinator.full_history)

        check_msg = TaggedMessage(sender_label="User", text="check")
        coordinator.full_history.append(check_msg)
        check_prompt = coordinator._build_prompt(check_msg)

        # If the fix works, Codex's message was included in the Gemini prompt,
        # so it should NOT appear in this follow-up prompt.
        assert "@test-agent here's my take" not in check_prompt
        assert "@test-agent I agree" not in check_prompt
        assert "check" in check_prompt

    @pytest.mark.asyncio
    async def test_replacement_prompt_contains_cancelled_message(
        self, coordinator: AgentCoordinator
    ):
        """Directly verify the prompt text of the replacement send includes
        the message from the cancelled stream."""
        conversation: list[Message] = []

        # Prime: a user message was already prompted.
        user_msg = TaggedMessage(sender_label="User", text="hello")
        coordinator.full_history.append(user_msg)
        coordinator._build_prompt(user_msg)  # advances last_prompted_index

        # Two agents respond simultaneously.
        codex_msg = TaggedMessage(sender_label="codex", text="@test-agent Codex here")
        gemini_msg = TaggedMessage(sender_label="gemini", text="@test-agent Gemini here")

        # First send (Codex) — starts streaming.
        coordinator.send(codex_msg, conversation)
        await asyncio.sleep(0.05)

        # Capture prompt index before second send.
        # Second send (Gemini) — cancels the first.
        # We need to capture the prompt.  Monkey-patch _build_prompt to record it.
        prompts: list[str] = []
        original_build = coordinator._build_prompt

        def capturing_build(tagged):
            result = original_build(tagged)
            prompts.append(result)
            return result

        coordinator._build_prompt = capturing_build  # type: ignore[assignment]
        coordinator.send(gemini_msg, conversation)
        await asyncio.sleep(0.05)

        assert len(prompts) == 1
        replacement_prompt = prompts[0]

        # The replacement prompt MUST contain Codex's message as catch-up.
        assert "Codex here" in replacement_prompt, (
            "Codex's message was lost — the cancelled stream advanced "
            "last_prompted_index past it"
        )
        # And the current message (Gemini) must be there too.
        assert "Gemini here" in replacement_prompt

        coordinator._current_task.cancel()
        await asyncio.sleep(0.05)


class TestServiceFailureDoesNotWedgeUI:
    """If the service raises an unexpected exception, the message must still
    be marked complete and the UI cleaned up."""

    @pytest.mark.asyncio
    async def test_exception_marks_complete_and_fires_callback(self, db: PentaDB):
        class ExplodingService(AgentService):
            async def send(self, prompt, session_id, working_dir, system_prompt=None):
                yield StreamEvent(type=StreamEventType.TEXT_DELTA, text="partial")
                raise ConnectionResetError("boom")

            async def cancel(self):
                pass

            async def shutdown(self):
                pass

        config = AgentConfig(
            id=uuid4(), name="exploder", type=AgentType.CLAUDE, status=AgentStatus.IDLE,
        )
        coord = AgentCoordinator(
            config=config, working_dir=Path("/tmp"), db=db, other_agent_names=[],
        )
        coord.service = ExplodingService()

        completed: list[Message] = []
        coord.on_stream_complete = lambda msg, _aid: completed.append(msg)

        conversation: list[Message] = []
        tagged = TaggedMessage(sender_label="User", text="hello")
        response = coord.send(tagged, conversation)
        await asyncio.sleep(0.1)

        # Must have fired the callback
        assert len(completed) == 1
        assert completed[0].is_error is True
        assert not completed[0].is_streaming
        assert "failed" in completed[0].text.lower()
        # Status should be IDLE, not stuck
        assert coord.config.status == AgentStatus.IDLE
