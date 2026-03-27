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
        self, prompt: str, session_id: str | None, working_dir: Path
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
    return PentaDB(tmp_path / "test-project")


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
    async def test_cancelled_stream_does_not_fire_callback(
        self, coordinator: AgentCoordinator
    ):
        """on_stream_complete must NOT fire for a cancelled stream."""
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

        assert len(completed) == 0, "on_stream_complete should not fire for cancelled stream"

        coordinator._current_task.cancel()
        await asyncio.sleep(0.05)
