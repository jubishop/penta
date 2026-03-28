"""Tests for compact_history trimming in-memory state to match the DB."""

from __future__ import annotations

from pathlib import Path

import pytest

from penta.app_state import AppState
from penta.models.agent_type import AgentType
from penta.models.message import Message
from penta.models.message_sender import MessageSender
from penta.models.tagged_message import TaggedMessage


@pytest.fixture
async def state(tmp_path: Path) -> AppState:
    state = AppState(tmp_path / "project")
    await state.connect()
    await state.add_agent("claude", AgentType.CLAUDE)
    yield state
    await state.db.close()


async def _seed_messages(state: AppState, count: int) -> None:
    """Add messages directly to conversation + DB, bypassing routing."""
    for i in range(count):
        state.conversation.append(
            Message(sender=MessageSender.user(), text=f"msg-{i}")
        )
        await state.db.append_message("User", f"msg-{i}")


class TestCompactTrimsInMemory:
    async def test_conversation_trimmed_to_max(self, state: AppState):
        limit = 5
        state.db.MAX_MESSAGES = limit
        await _seed_messages(state, 10)

        assert len(state.conversation) == 10
        trimmed = await state.compact_history()
        assert trimmed == 5
        assert len(state.conversation) == 5
        # Should keep the most recent
        assert state.conversation[-1].text == "msg-9"
        assert state.conversation[0].text == "msg-5"

    async def test_no_trim_when_under_limit(self, state: AppState):
        await _seed_messages(state, 1)
        trimmed = await state.compact_history()
        assert trimmed == 0
        assert len(state.conversation) == 1

    async def test_coordinator_history_trimmed(self, state: AppState):
        limit = 5
        state.db.MAX_MESSAGES = limit
        coord = list(state.coordinators.values())[0]

        for i in range(10):
            coord.full_history.append(
                TaggedMessage(sender_label="User", text=f"msg-{i}")
            )
        coord.last_prompted_index = 8
        coord._pre_prompt_index = 6

        await _seed_messages(state, 10)
        await state.compact_history()

        assert len(coord.full_history) == 5
        assert coord.last_prompted_index == 3  # 8 - 5
        assert coord._pre_prompt_index == 1  # 6 - 5

    async def test_coordinator_indices_floor_at_zero(self, state: AppState):
        limit = 3
        state.db.MAX_MESSAGES = limit
        coord = list(state.coordinators.values())[0]

        for i in range(10):
            coord.full_history.append(
                TaggedMessage(sender_label="User", text=f"msg-{i}")
            )
        coord.last_prompted_index = 2
        coord._pre_prompt_index = 1

        await _seed_messages(state, 10)
        await state.compact_history()

        assert coord.last_prompted_index == 0
        assert coord._pre_prompt_index == 0

    async def test_compact_preserves_list_identity(self, state: AppState):
        """Conversation list must be mutated in place, not replaced."""
        state.db.MAX_MESSAGES = 3
        await _seed_messages(state, 5)
        original_list = state.conversation
        await state.compact_history()
        assert state.conversation is original_list
