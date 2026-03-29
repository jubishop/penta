from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import UUID

from penta.coordinators.agent_coordinator import AgentCoordinator
from penta.models.agent_config import AgentConfig
from penta.models.agent_status import AgentStatus
from penta.models.agent_type import AgentType
from penta.models.message import Message
from penta.models.message_sender import MessageSender
from penta.models.tagged_message import TaggedMessage
from penta.routing import MessageRouter
from penta.services.agent_service import AgentService
from penta.services.db import PentaDB

log = logging.getLogger(__name__)


class AppState:

    def __init__(
        self,
        directory: Path,
        storage_root: Path | None = None,
        db: PentaDB | None = None,
        service_factory: Callable[[AgentConfig], AgentService] | None = None,
    ) -> None:
        self.directory = directory.resolve()
        self.agents: list[AgentConfig] = []
        self._agents_by_id: dict[UUID, AgentConfig] = {}
        self.coordinators: dict[UUID, AgentCoordinator] = {}
        self.conversation: list[Message] = []
        self.db = db or PentaDB(self.directory, storage_root=storage_root)
        self._service_factory = service_factory
        self._poll_task: asyncio.Task | None = None

        self.router = MessageRouter(
            self.agents, self._agents_by_id, self.coordinators, self.conversation, self.db,
        )

        # Callbacks for the TUI layer
        self.on_text_delta: Callable[[UUID, str], None] | None = None
        self.on_stream_complete: Callable[[Message, UUID], None] | None = None
        self.on_status_changed: Callable[[UUID, AgentStatus], None] | None = None

    async def connect(self) -> None:
        await self.db.connect()

    # -- Agent management --

    async def add_agent(
        self, name: str, agent_type: AgentType, model: str | None = None,
    ) -> AgentConfig:
        config = AgentConfig(name=name, type=agent_type, model=model)
        self.agents.append(config)
        self._agents_by_id[config.id] = config

        session_id = await self.db.load_session(config.name)
        other_names = [a.name for a in self.agents if a.id != config.id]

        if self._service_factory:
            service = self._service_factory(config)
            executable = None
        else:
            executable = agent_type.find_executable()
            if not executable:
                config.status = AgentStatus.DISCONNECTED
                log.warning("Agent %s: executable not found, marked DISCONNECTED", name)
            service = None

        coordinator = AgentCoordinator(
            config=config,
            working_dir=self.directory,
            db=self.db,
            executable=executable,
            other_agent_names=other_names,
            session_id=session_id,
            service=service,
        )

        # Wire callbacks through to TUI
        coordinator.on_text_delta = self._relay_text_delta
        coordinator.on_stream_complete = self._relay_stream_complete
        coordinator.on_status_changed = self._relay_status_changed

        self.coordinators[config.id] = coordinator

        # Update other coordinators' awareness of this new agent
        for agent in self.agents:
            if agent.id != config.id:
                coord = self.coordinators.get(agent.id)
                if coord:
                    coord.set_other_agent_names(
                        [a.name for a in self.agents if a.id != agent.id]
                    )

        return config

    def agent_by_id(self, agent_id: UUID) -> AgentConfig | None:
        return self._agents_by_id.get(agent_id)

    def agent_by_name(self, name: str) -> AgentConfig | None:
        lower = name.lower()
        return next((a for a in self.agents if a.name.lower() == lower), None)

    @property
    def directory_name(self) -> str:
        return self.directory.name

    @property
    def external_participants(self) -> set[str]:
        return self.router.external_participants

    # -- Delegated to router --

    async def send_user_message(self, text: str) -> None:
        await self.router.send_user_message(text)

    def receive_external_message(self, sender_name: str, text: str) -> None:
        self.router.receive_external_message(sender_name, text)

    # -- Lifecycle --

    async def load_chat_history(self) -> None:
        rows = await self.db.get_messages()
        for row_id, sender, text, ts in rows:
            agent = self.agent_by_name(sender)
            if sender == "User":
                msg_sender = MessageSender.user()
            elif agent:
                msg_sender = MessageSender.agent(agent.id)
            else:
                msg_sender = MessageSender.external(sender)
            stored_ts = datetime.fromisoformat(ts)
            self.conversation.append(
                Message(sender=msg_sender, text=text, timestamp=stored_ts)
            )

        history = [
            TaggedMessage(sender_label=sender, text=text)
            for _, sender, text, _ in rows
        ]
        for coord in self.coordinators.values():
            coord.full_history = list(history)
            if coord.session_id is not None:
                # Resumed session already has this context
                coord.last_prompted_index = len(history)
            else:
                coord.last_prompted_index = 0

    def start_external_polling(
        self, relay: Callable[[str, str], None],
    ) -> asyncio.Task:
        """Begin polling for messages written by external processes."""
        self.db.set_external_message_callback(relay)
        self._poll_task = asyncio.create_task(self.db.poll_external_messages())
        return self._poll_task

    async def compact_history(self) -> int:
        """Compact DB and trim in-memory lists to match. Returns count trimmed."""
        await self.db.compact()
        limit = self.db.MAX_MESSAGES
        trimmed = max(0, len(self.conversation) - limit)
        if trimmed:
            del self.conversation[:trimmed]
            for coord in self.coordinators.values():
                coord.compact(limit)
        return trimmed

    async def shutdown(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        for coord in self.coordinators.values():
            await coord.shutdown()
        await self.db.close()

    # -- Callback relays --

    def _relay_text_delta(self, agent_id: UUID, delta: str) -> None:
        if self.on_text_delta:
            self.on_text_delta(agent_id, delta)

    def _relay_stream_complete(self, message: Message, agent_id: UUID) -> None:
        if self.on_stream_complete:
            self.on_stream_complete(message, agent_id)

    def _relay_status_changed(self, agent_id: UUID, status: AgentStatus) -> None:
        if self.on_status_changed:
            self.on_status_changed(agent_id, status)
