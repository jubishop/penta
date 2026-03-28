from __future__ import annotations

import asyncio
import logging
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


class RouteMode(Enum):
    ALL_IF_NO_MENTIONS = auto()
    MENTIONED_ONLY = auto()


class MessageRouter:
    _MAX_ROUTING_HOPS = 3

    def __init__(
        self,
        agents: list[AgentConfig],
        coordinators: dict[UUID, AgentCoordinator],
        conversation: list[Message],
        db: PentaDB,
    ) -> None:
        self._agents = agents
        self._coordinators = coordinators
        self._conversation = conversation
        self._db = db
        self.external_participants: set[str] = set()

        # Callbacks for the TUI layer
        self.on_external_message: Callable[[str, str], None] | None = None
        self.on_external_participant_joined: Callable[[str], None] | None = None

    async def send_user_message(self, text: str) -> None:
        self._conversation.append(Message(sender=MessageSender.user(), text=text))
        await self._db.append_message("User", text)
        tagged = TaggedMessage(sender_label="User", text=text)
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
        if hops >= self._MAX_ROUTING_HOPS:
            log.warning("Routing depth limit reached (%d hops), stopping propagation", hops)
            return

        if mode == RouteMode.ALL_IF_NO_MENTIONS and not mentioned:
            responding = {
                a.id for a in self._agents
                if a.status != AgentStatus.DISCONNECTED
            }
        else:
            responding = mentioned

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

    def _agent_by_id(self, agent_id: UUID) -> AgentConfig | None:
        return next((a for a in self._agents if a.id == agent_id), None)
