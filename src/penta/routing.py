from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable
from uuid import UUID

from penta.coordinators.agent_coordinator import AgentCoordinator
from penta.input_parser import extract_mentions
from penta.models.agent_config import AgentConfig
from penta.models.agent_status import AgentStatus
from penta.models.message import Message
from penta.models.message_sender import MessageSender, sanitize_external_name
from penta.models.tagged_message import TaggedMessage
from penta.services.db import PentaDB
from penta.utils import log_task_error

log = logging.getLogger(__name__)

DEFAULT_ROUND_LIMIT = 3


class RouteMode(Enum):
    ALL_IF_NO_MENTIONS = auto()
    MENTIONED_ONLY = auto()


@dataclass(frozen=True)
class _StalledRoute:
    tagged: TaggedMessage
    excluding: UUID | None
    mentioned: frozenset[UUID]
    mode: RouteMode


class MessageRouter:

    def __init__(
        self,
        agents: list[AgentConfig],
        agents_by_id: dict[UUID, AgentConfig],
        coordinators: dict[UUID, AgentCoordinator],
        conversation: list[Message],
        db: PentaDB,
    ) -> None:
        self._agents = agents
        self._agents_by_id = agents_by_id
        self._coordinators = coordinators
        self._conversation = conversation
        self._db = db
        self._round_limit = DEFAULT_ROUND_LIMIT
        self._stalled: list[_StalledRoute] = []
        self.external_participants: set[str] = set()
        self._pending_tasks: set[asyncio.Task] = set()

        # Callbacks for the TUI layer
        self.on_external_message: Callable[[str, str], None] | None = None
        self.on_external_participant_joined: Callable[[str], None] | None = None
        self.on_routing_stalled: Callable[[], None] | None = None

    @property
    def round_limit(self) -> int:
        return self._round_limit

    @round_limit.setter
    def round_limit(self, value: int) -> None:
        self._round_limit = max(1, value)

    @property
    def is_stalled(self) -> bool:
        return bool(self._stalled)

    def clear_stalled(self) -> None:
        """Discard all stalled routes (e.g. on conversation switch)."""
        self._stalled.clear()

    def continue_routing(self) -> None:
        """Resume routing from the point where the round limit stopped it."""
        stalled = list(self._stalled)
        self._stalled.clear()
        for sr in stalled:
            self.route(
                sr.tagged, excluding=sr.excluding,
                mentioned=set(sr.mentioned),
                mode=sr.mode,
            )

    async def send_user_message(
        self, text: str, routed_text: str | None = None,
    ) -> None:
        self._stalled.clear()
        self._conversation.append(Message(sender=MessageSender.user(), text=text))
        await self._db.append_message("User", text)
        delivered = routed_text if routed_text is not None else text
        tagged = TaggedMessage(sender_label="User", text=delivered)
        # Extract mentions from the original text, not the interpolated version,
        # so plan content containing @mentions can't hijack routing.
        mentioned = extract_mentions(text, self._agents)
        self.route(
            tagged, excluding=None, mentioned=mentioned,
            mode=RouteMode.ALL_IF_NO_MENTIONS,
        )

    def receive_external_message(self, sender_name: str, text: str) -> None:
        agent_names = frozenset(a.name.lower() for a in self._agents)
        sender_name = sanitize_external_name(sender_name, agent_names)

        msg_sender = MessageSender.external(sender_name)
        self._conversation.append(Message(sender=msg_sender, text=text))

        if sender_name not in self.external_participants:
            self.external_participants.add(sender_name)
            if self.on_external_participant_joined:
                self.on_external_participant_joined(sender_name)

        if self.on_external_message:
            self.on_external_message(sender_name, text)

        tagged = TaggedMessage(sender_label=sender_name, text=text)
        mentioned = extract_mentions(text, self._agents)
        self.route(
            tagged, excluding=None, mentioned=mentioned,
            mode=RouteMode.MENTIONED_ONLY,
        )

    def route(
        self,
        tagged: TaggedMessage,
        excluding: UUID | None,
        mentioned: set[UUID],
        mode: RouteMode,
        hops: int = 0,
    ) -> None:
        asyncio.get_running_loop()  # fail fast if called outside an event loop

        if mode == RouteMode.ALL_IF_NO_MENTIONS and not mentioned:
            responding = {
                a.id for a in self._agents
                if a.status != AgentStatus.DISCONNECTED
            }
        else:
            responding = mentioned

        if hops >= self._round_limit and responding:
            log.warning("Round limit reached (%d hops), stopping propagation", hops)
            self._stalled.append(_StalledRoute(tagged, excluding, frozenset(mentioned), mode))
            if self.on_routing_stalled:
                self.on_routing_stalled()
            return

        for agent in self._agents:
            if agent.id == excluding:
                continue
            if agent.status == AgentStatus.DISCONNECTED:
                continue
            coord = self._coordinators.get(agent.id)
            if not coord:
                continue

            if agent.id in responding:
                msg = coord.send(tagged, self._conversation)
                task = asyncio.create_task(
                    self._await_completion(msg, agent.id, hops)
                )
                task.add_done_callback(log_task_error)
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)
            else:
                coord.inject_context(tagged)

    async def _await_completion(self, message: Message, agent_id: UUID, hops: int = 0) -> None:
        await message.wait_for_completion()
        if message.is_cancelled:
            return
        agent = self._agent_by_id(agent_id)
        if not agent:
            return

        await self._db.append_message(agent.name, message.text)

        tagged = TaggedMessage(sender_label=agent.name, text=message.text)
        mentioned = extract_mentions(message.text, self._agents) - {agent_id}
        self.route(
            tagged, excluding=agent_id, mentioned=mentioned,
            mode=RouteMode.MENTIONED_ONLY, hops=hops + 1,
        )

    async def drain(self) -> None:
        """Wait until all routing tasks (including cascaded ones) are done."""
        while self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)

    def _agent_by_id(self, agent_id: UUID) -> AgentConfig | None:
        return self._agents_by_id.get(agent_id)
