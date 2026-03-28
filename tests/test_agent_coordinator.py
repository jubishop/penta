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

    async def respond_to_permission(self, request_id: str, granted: bool) -> None:
        pass

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


class TestServiceFailureDoesNotWedgeUI:
    """If the service raises an unexpected exception, the message must still
    be marked complete and the UI cleaned up."""

    @pytest.mark.asyncio
    async def test_exception_marks_complete_and_fires_callback(self, db: PentaDB):
        class ExplodingService(AgentService):
            async def send(self, prompt, session_id, working_dir, system_prompt=None):
                yield StreamEvent(type=StreamEventType.TEXT_DELTA, text="partial")
                raise ConnectionResetError("boom")

            async def respond_to_permission(self, request_id, granted):
                pass

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
