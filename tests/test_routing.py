"""Tests for MessageRouter routing logic."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from penta.models.agent_config import AgentConfig
from penta.models.agent_status import AgentStatus
from penta.models.agent_type import AgentType
from penta.models.message import Message
from penta.models.message_sender import MessageSender
from penta.models.tagged_message import TaggedMessage
from penta.routing import MessageRouter, RouteMode
from penta.services.db import PentaDB


def _make_agent(
    name: str, agent_type: AgentType, status: AgentStatus = AgentStatus.IDLE,
) -> AgentConfig:
    return AgentConfig(id=uuid4(), name=name, type=agent_type, status=status)


def _make_mock_coordinator() -> MagicMock:
    """Create a mock coordinator that records send/inject_context calls."""
    coord = MagicMock()
    # send() should return a Message that completes immediately (non-streaming)
    coord.send.return_value = Message(
        sender=MessageSender.user(), text="mock response",
    )
    return coord


@pytest.fixture
async def db(tmp_path: Path) -> PentaDB:
    db = PentaDB(tmp_path / "test-project", storage_root=tmp_path)
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
def agents() -> list[AgentConfig]:
    return []


@pytest.fixture
def coordinators() -> dict[UUID, MagicMock]:
    return {}


@pytest.fixture
def conversation() -> list[Message]:
    return []


@pytest.fixture
def router(agents, coordinators, conversation, db) -> MessageRouter:
    return MessageRouter(agents, coordinators, conversation, db)


def _register(
    agents: list[AgentConfig],
    coordinators: dict,
    agent: AgentConfig,
) -> MagicMock:
    """Register an agent and its mock coordinator."""
    agents.append(agent)
    coord = _make_mock_coordinator()
    coordinators[agent.id] = coord
    return coord


class TestUserMessageRouting:
    async def test_message_appended_to_conversation(self, router, conversation):
        await router.send_user_message("hello")
        assert len(conversation) == 1
        assert conversation[0].sender.is_user
        assert conversation[0].text == "hello"

    async def test_message_persisted_to_db(self, router, db):
        await router.send_user_message("hello")
        rows = await db.get_messages()
        assert len(rows) == 1
        assert rows[0][1] == "User"
        assert rows[0][2] == "hello"

    async def test_no_mentions_routes_to_all_connected(
        self, router, agents, coordinators,
    ):
        claude = _make_agent("claude", AgentType.CLAUDE)
        codex = _make_agent("codex", AgentType.CODEX)
        disconnected = _make_agent("offline", AgentType.CODEX, AgentStatus.DISCONNECTED)

        claude_coord = _register(agents, coordinators, claude)
        codex_coord = _register(agents, coordinators, codex)
        _register(agents, coordinators, disconnected)

        await router.send_user_message("hello everyone")

        claude_coord.send.assert_called_once()
        codex_coord.send.assert_called_once()
        # Disconnected agent should not receive anything
        coordinators[disconnected.id].send.assert_not_called()
        coordinators[disconnected.id].inject_context.assert_not_called()

    async def test_mention_routes_to_mentioned_only(
        self, router, agents, coordinators,
    ):
        claude = _make_agent("claude", AgentType.CLAUDE)
        codex = _make_agent("codex", AgentType.CODEX)

        claude_coord = _register(agents, coordinators, claude)
        codex_coord = _register(agents, coordinators, codex)

        await router.send_user_message("@claude help me")

        claude_coord.send.assert_called_once()
        codex_coord.send.assert_not_called()
        codex_coord.inject_context.assert_called_once()


class TestExternalMessageRouting:
    async def test_external_message_added_to_conversation(
        self, router, conversation,
    ):
        router.receive_external_message("Alice", "hi")
        assert len(conversation) == 1
        assert conversation[0].sender.is_external
        assert conversation[0].text == "hi"

    async def test_sender_name_sanitized(self, router, agents, coordinators):
        claude = _make_agent("claude", AgentType.CLAUDE)
        _register(agents, coordinators, claude)

        router.receive_external_message("claude", "impersonating")
        assert router._conversation[0].sender.name == "claude (external)"

    async def test_new_participant_tracked(self, router):
        joined: list[str] = []
        router.on_external_participant_joined = lambda n: joined.append(n)
        router.receive_external_message("Alice", "hi")
        assert joined == ["Alice"]

    async def test_repeat_participant_not_re_announced(self, router):
        joined: list[str] = []
        router.on_external_participant_joined = lambda n: joined.append(n)
        router.receive_external_message("Alice", "hi")
        router.receive_external_message("Alice", "hello again")
        assert joined == ["Alice"]

    async def test_external_uses_mentioned_only_mode(
        self, router, agents, coordinators,
    ):
        claude = _make_agent("claude", AgentType.CLAUDE)
        codex = _make_agent("codex", AgentType.CODEX)

        claude_coord = _register(agents, coordinators, claude)
        codex_coord = _register(agents, coordinators, codex)

        # External message with no mentions — should NOT route to all
        router.receive_external_message("Alice", "hello everyone")
        claude_coord.send.assert_not_called()
        codex_coord.send.assert_not_called()


class TestRoutingDepthLimit:
    async def test_stops_at_max_hops(self, router, agents, coordinators):
        claude = _make_agent("claude", AgentType.CLAUDE)
        coord = _register(agents, coordinators, claude)

        tagged = TaggedMessage(sender_label="User", text="hello")
        router.route(
            tagged, excluding=None, mentioned={claude.id},
            mode=RouteMode.MENTIONED_ONLY, hops=3,
        )
        coord.send.assert_not_called()

    async def test_under_limit_routes_normally(self, router, agents, coordinators):
        claude = _make_agent("claude", AgentType.CLAUDE)
        coord = _register(agents, coordinators, claude)

        tagged = TaggedMessage(sender_label="User", text="hello")
        router.route(
            tagged, excluding=None, mentioned={claude.id},
            mode=RouteMode.MENTIONED_ONLY, hops=2,
        )
        coord.send.assert_called_once()


class TestAwaitCompletion:
    async def test_completed_message_persisted_to_db(
        self, router, agents, coordinators, db,
    ):
        claude = _make_agent("claude", AgentType.CLAUDE)
        _register(agents, coordinators, claude)

        msg = Message(
            sender=MessageSender.agent(claude.id),
            text="here is my answer",
        )
        await router._await_completion(msg, claude.id)

        rows = await db.get_messages()
        assert len(rows) == 1
        assert rows[0][1] == "claude"
        assert rows[0][2] == "here is my answer"

    async def test_cancelled_message_not_persisted(
        self, router, agents, coordinators, db,
    ):
        claude = _make_agent("claude", AgentType.CLAUDE)
        _register(agents, coordinators, claude)

        msg = Message(
            sender=MessageSender.agent(claude.id),
            text="partial",
            is_streaming=True,
        )
        msg.is_cancelled = True
        msg.mark_complete()

        await router._await_completion(msg, claude.id)
        rows = await db.get_messages()
        assert len(rows) == 0

    async def test_completed_message_triggers_mention_rerouting(
        self, router, agents, coordinators,
    ):
        claude = _make_agent("claude", AgentType.CLAUDE)
        codex = _make_agent("codex", AgentType.CODEX)
        _register(agents, coordinators, claude)
        codex_coord = _register(agents, coordinators, codex)

        msg = Message(
            sender=MessageSender.agent(claude.id),
            text="@codex please help",
        )
        await router._await_completion(msg, claude.id, hops=0)

        # Codex should have received a routed send
        codex_coord.send.assert_called_once()
