"""Tests for MessageRouter routing logic."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from penta.coordinators.agent_coordinator import AgentCoordinator
from penta.models.agent_config import AgentConfig
from penta.models.agent_status import AgentStatus
from penta.models.agent_type import AgentType
from penta.models.message import Message
from penta.models.message_sender import MessageSender
from penta.models.tagged_message import TaggedMessage
from penta.routing import MessageRouter, RouteMode
from penta.services.db import PentaDB

from .fakes import FakeAgentService


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
def agents() -> list[AgentConfig]:
    return []


@pytest.fixture
def coordinators() -> dict[UUID, MagicMock]:
    return {}


@pytest.fixture
def conversation() -> list[Message]:
    return []


@pytest.fixture
def agents_by_id() -> dict[UUID, AgentConfig]:
    return {}


@pytest.fixture
def router(agents, agents_by_id, coordinators, conversation, memory_db) -> MessageRouter:
    return MessageRouter(agents, agents_by_id, coordinators, conversation, memory_db)


def _register(
    agents: list[AgentConfig],
    coordinators: dict,
    agent: AgentConfig,
    agents_by_id: dict[UUID, AgentConfig] | None = None,
) -> MagicMock:
    """Register an agent and its mock coordinator."""
    agents.append(agent)
    if agents_by_id is not None:
        agents_by_id[agent.id] = agent
    coord = _make_mock_coordinator()
    coordinators[agent.id] = coord
    return coord


class TestUserMessageRouting:
    async def test_message_appended_to_conversation(self, router, conversation):
        await router.send_user_message("hello")
        assert len(conversation) == 1
        assert conversation[0].sender.is_user
        assert conversation[0].text == "hello"

    async def test_message_persisted_to_db(self, router, memory_db):
        await router.send_user_message("hello")
        rows = await memory_db.get_messages()
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

    async def test_external_at_all_routes_to_all(
        self, router, agents, coordinators,
    ):
        claude = _make_agent("claude", AgentType.CLAUDE)
        codex = _make_agent("codex", AgentType.CODEX)

        claude_coord = _register(agents, coordinators, claude)
        codex_coord = _register(agents, coordinators, codex)

        router.receive_external_message("Alice", "@all what do you think?")
        claude_coord.send.assert_called_once()
        codex_coord.send.assert_called_once()

    async def test_external_at_everyone_routes_to_all(
        self, router, agents, coordinators,
    ):
        claude = _make_agent("claude", AgentType.CLAUDE)
        codex = _make_agent("codex", AgentType.CODEX)

        claude_coord = _register(agents, coordinators, claude)
        codex_coord = _register(agents, coordinators, codex)

        router.receive_external_message("Alice", "@everyone hello")
        claude_coord.send.assert_called_once()
        codex_coord.send.assert_called_once()


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
        self, router, agents, agents_by_id, coordinators, memory_db,
    ):
        claude = _make_agent("claude", AgentType.CLAUDE)
        _register(agents, coordinators, claude, agents_by_id)

        msg = Message(
            sender=MessageSender.agent(claude.id),
            text="here is my answer",
        )
        await router._await_completion(msg, claude.id)

        rows = await memory_db.get_messages()
        assert len(rows) == 1
        assert rows[0][1] == "claude"
        assert rows[0][2] == "here is my answer"

    async def test_cancelled_message_not_persisted(
        self, router, agents, agents_by_id, coordinators, memory_db,
    ):
        claude = _make_agent("claude", AgentType.CLAUDE)
        _register(agents, coordinators, claude, agents_by_id)

        msg = Message(
            sender=MessageSender.agent(claude.id),
            text="partial",
            is_streaming=True,
        )
        msg.is_cancelled = True
        msg.mark_complete()

        await router._await_completion(msg, claude.id)
        rows = await memory_db.get_messages()
        assert len(rows) == 0

    async def test_completed_message_triggers_mention_rerouting(
        self, router, agents, agents_by_id, coordinators,
    ):
        claude = _make_agent("claude", AgentType.CLAUDE)
        codex = _make_agent("codex", AgentType.CODEX)
        _register(agents, coordinators, claude, agents_by_id)
        codex_coord = _register(agents, coordinators, codex, agents_by_id)

        msg = Message(
            sender=MessageSender.agent(claude.id),
            text="@codex please help",
        )
        await router._await_completion(msg, claude.id, hops=0)

        # Codex should have received a routed send
        codex_coord.send.assert_called_once()

    async def test_cancelled_message_not_routed_onward(
        self, router, agents, agents_by_id, coordinators,
    ):
        """A cancelled message mentioning another agent must NOT trigger
        routing to that agent."""
        claude = _make_agent("claude", AgentType.CLAUDE)
        codex = _make_agent("codex", AgentType.CODEX)
        _register(agents, coordinators, claude, agents_by_id)
        codex_coord = _register(agents, coordinators, codex, agents_by_id)

        msg = Message(
            sender=MessageSender.agent(claude.id),
            text="@codex please help",
            is_streaming=True,
        )
        msg.is_cancelled = True
        msg.mark_complete()

        await router._await_completion(msg, claude.id, hops=0)

        codex_coord.send.assert_not_called()
        codex_coord.inject_context.assert_not_called()


# -- Helpers for real-coordinator router tests --------------------------------


def _make_real_coordinator(
    config: AgentConfig,
    db: PentaDB,
    fake: FakeAgentService,
    other_names: list[str],
) -> AgentCoordinator:
    return AgentCoordinator(
        config=config,
        working_dir=Path("/tmp"),
        db=db,
        other_agent_names=other_names,
        service=fake,
    )


def _make_router_with_real_coordinators(
    agents: list[AgentConfig],
    fakes: dict[UUID, FakeAgentService],
    db: PentaDB,
) -> tuple[MessageRouter, list[Message], dict[UUID, AgentCoordinator]]:
    """Wire up a MessageRouter with real AgentCoordinators backed by fakes."""
    agents_by_id = {a.id: a for a in agents}
    other_names = [a.name for a in agents]
    coordinators: dict[UUID, AgentCoordinator] = {}
    for agent in agents:
        coordinators[agent.id] = _make_real_coordinator(
            agent, db, fakes[agent.id],
            [n for n in other_names if n != agent.name],
        )
    conversation: list[Message] = []
    router = MessageRouter(agents, agents_by_id, coordinators, conversation, db)
    return router, conversation, coordinators


class TestRouterCancelCoalesceEndToEnd:
    """End-to-end: router.route() twice for the same agent before the first
    finishes — verify cancel → coalesce → only final response persisted."""

    async def test_second_route_cancels_first_and_only_final_persisted(
        self, memory_db: PentaDB,
    ):
        target = _make_agent("claude", AgentType.CLAUDE)
        target_fake = FakeAgentService()
        target_fake.enqueue_hang()                     # first route — hangs
        target_fake.enqueue_text("final answer")       # second route — completes

        fakes = {target.id: target_fake}
        router, conversation, coordinators = _make_router_with_real_coordinators(
            [target], fakes, memory_db,
        )

        # First route: user says hello — routes to claude, starts streaming.
        tagged1 = TaggedMessage(sender_label="User", text="hello")
        router.route(
            tagged1, excluding=None, mentioned={target.id},
            mode=RouteMode.MENTIONED_ONLY,
        )
        await asyncio.sleep(0)  # let first stream start

        # Second route: another message arrives before first finishes.
        tagged2 = TaggedMessage(sender_label="User", text="actually do this instead")
        router.route(
            tagged2, excluding=None, mentioned={target.id},
            mode=RouteMode.MENTIONED_ONLY,
        )

        await router.drain()

        # Only the final response should be persisted.
        rows = await memory_db.get_messages()
        assert len(rows) == 1
        assert rows[0][1] == "claude"
        assert rows[0][2] == "final answer"

    async def test_cancelled_response_mention_does_not_cascade(
        self, memory_db: PentaDB,
    ):
        """If the cancelled stream's partial text mentions another agent,
        that mention must NOT trigger cascading routing."""
        claude = _make_agent("claude", AgentType.CLAUDE)
        codex = _make_agent("codex", AgentType.CODEX)

        claude_fake = FakeAgentService()
        # First stream: partial text mentions @codex, then hangs.
        claude_fake.enqueue_hang(prefix_text="@codex help me")
        # Second stream: clean response, no mentions.
        claude_fake.enqueue_text("done, no mentions")

        codex_fake = FakeAgentService()
        # Codex should NOT be called at all — but enqueue a response
        # just in case, so we don't get IndexError on a spurious send.
        codex_fake.enqueue_text("codex was called unexpectedly")

        fakes = {claude.id: claude_fake, codex.id: codex_fake}
        router, conversation, coordinators = _make_router_with_real_coordinators(
            [claude, codex], fakes, memory_db,
        )

        # Route to claude only.
        tagged1 = TaggedMessage(sender_label="User", text="@claude go")
        router.route(
            tagged1, excluding=None, mentioned={claude.id},
            mode=RouteMode.MENTIONED_ONLY,
        )
        await asyncio.sleep(0)

        # Second route cancels the first.
        tagged2 = TaggedMessage(sender_label="User", text="@claude retry")
        router.route(
            tagged2, excluding=None, mentioned={claude.id},
            mode=RouteMode.MENTIONED_ONLY,
        )

        await router.drain()

        # Codex should never have been sent anything.
        assert len(codex_fake.calls) == 0

        # DB should only have claude's final response.
        rows = await memory_db.get_messages()
        assert len(rows) == 1
        assert rows[0][1] == "claude"
        assert rows[0][2] == "done, no mentions"

    async def test_triple_route_only_final_persisted(
        self, memory_db: PentaDB,
    ):
        """Three rapid routes to the same agent: only the third response
        is persisted and the first two are discarded."""
        target = _make_agent("claude", AgentType.CLAUDE)
        target_fake = FakeAgentService()
        target_fake.enqueue_hang()                # first — cancelled
        target_fake.enqueue_hang()                # second — cancelled
        target_fake.enqueue_text("third answer")  # third — completes

        fakes = {target.id: target_fake}
        router, conversation, coordinators = _make_router_with_real_coordinators(
            [target], fakes, memory_db,
        )

        for i, text in enumerate(["first", "second", "third"]):
            tagged = TaggedMessage(sender_label="User", text=text)
            router.route(
                tagged, excluding=None, mentioned={target.id},
                mode=RouteMode.MENTIONED_ONLY,
            )
            if i < 2:
                await asyncio.sleep(0)  # let stream start before next cancel

        await router.drain()

        rows = await memory_db.get_messages()
        assert len(rows) == 1
        assert rows[0][2] == "third answer"
