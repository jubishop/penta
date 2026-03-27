from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Callable
from uuid import UUID

from penta.coordinators.agent_coordinator import AgentCoordinator
from penta.input_parser import extract_mentions
from penta.models.agent_config import AgentConfig
from penta.models.agent_status import AgentStatus
from penta.models.agent_type import AgentType
from penta.models.message import Message
from penta.models.message_sender import MessageSender
from penta.models.permission_request import PermissionRequest
from penta.models.tagged_message import TaggedMessage
from penta.services.db import PentaDB
from penta.services.permission_server import PermissionServer

log = logging.getLogger(__name__)


class RouteMode(Enum):
    ALL_IF_NO_MENTIONS = auto()
    MENTIONED_ONLY = auto()


class AppState:
    _MAX_ROUTING_HOPS = 3

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.agents: list[AgentConfig] = []
        self.coordinators: dict[UUID, AgentCoordinator] = {}
        self.conversation: list[Message] = []
        self.pending_permissions: list[PermissionRequest] = []
        self.db = PentaDB(self.directory)
        self.permission_server: PermissionServer | None = None
        self.external_participants: set[str] = set()

        # Callbacks for the TUI layer
        self.on_text_delta: Callable[[UUID, str], None] | None = None
        self.on_stream_complete: Callable[[Message, UUID], None] | None = None
        self.on_permission_request: Callable[[PermissionRequest], None] | None = None
        self.on_status_changed: Callable[[UUID, object], None] | None = None
        self.on_external_message: Callable[[str, str], None] | None = None
        self.on_external_participant_joined: Callable[[str], None] | None = None

    def setup_permission_server(self, loop: asyncio.AbstractEventLoop) -> None:
        self.permission_server = PermissionServer(loop)
        self.permission_server.set_request_callback(self._on_http_permission_request)
        self.permission_server.start()

    def add_agent(self, name: str, agent_type: AgentType) -> AgentConfig:
        config = AgentConfig(name=name, type=agent_type)
        if not agent_type.find_executable():
            config.status = AgentStatus.DISCONNECTED
            log.warning("Agent %s: executable not found, marked DISCONNECTED", name)
        self.agents.append(config)

        other_names = [a.name for a in self.agents if a.id != config.id]
        coordinator = AgentCoordinator(
            config=config,
            working_dir=self.directory,
            db=self.db,
            permission_server=self.permission_server,
            other_agent_names=other_names,
        )

        # Wire callbacks through to TUI
        coordinator.on_text_delta = self._relay_text_delta
        coordinator.on_stream_complete = self._relay_stream_complete
        coordinator.on_permission_request = self._relay_permission_request
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
        return next((a for a in self.agents if a.id == agent_id), None)

    def agent_by_name(self, name: str) -> AgentConfig | None:
        lower = name.lower()
        return next((a for a in self.agents if a.name.lower() == lower), None)

    @property
    def directory_name(self) -> str:
        return self.directory.name

    # -- User actions --

    def send_user_message(self, text: str) -> None:
        self.conversation.append(Message(sender=MessageSender.user(), text=text))
        self.db.append_message("User", text)
        tagged = TaggedMessage(sender_label="User", text=text)
        mentioned = extract_mentions(text, self.agents)
        self._route(
            tagged, excluding=None, mentioned=mentioned,
            mode=RouteMode.ALL_IF_NO_MENTIONS,
        )

    _RESERVED_NAMES = {"user", "shell", "system"}

    def receive_external_message(self, sender_name: str, text: str) -> None:
        # External writers may not impersonate built-in agents or reserved
        # names — always treat them as external senders.
        if self.agent_by_name(sender_name) or sender_name.lower() in self._RESERVED_NAMES:
            sender_name = f"{sender_name} (external)"

        msg_sender = MessageSender.external(sender_name)
        self.conversation.append(Message(sender=msg_sender, text=text))

        if sender_name not in self.external_participants:
            self.external_participants.add(sender_name)
            if self.on_external_participant_joined:
                self.on_external_participant_joined(sender_name)

        if self.on_external_message:
            self.on_external_message(sender_name, text)

        tagged = TaggedMessage(sender_label=sender_name, text=text)
        mentioned = extract_mentions(text, self.agents)
        self._route(
            tagged, excluding=None, mentioned=mentioned,
            mode=RouteMode.MENTIONED_ONLY,
        )

    # -- Permissions --

    def approve_permission(self, request_id: str) -> None:
        request = self._pop_permission(request_id)
        if not request:
            return
        self._resolve_permission(request, granted=True)

    def deny_permission(self, request_id: str) -> None:
        request = self._pop_permission(request_id)
        if not request:
            return
        self._resolve_permission(request, granted=False)

    def pending_permission_for(self, agent_id: UUID) -> PermissionRequest | None:
        return next(
            (p for p in self.pending_permissions if p.agent_id == agent_id), None
        )

    # -- Lifecycle --

    def load_chat_history(self) -> None:
        rows = self.db.get_messages()
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

        # Hydrate coordinator catch-up history so context works even if
        # session resume fails after a restart.  last_prompted_index stays
        # at 0 so the first post-restart prompt replays all prior history
        # as catch-up context.  For agents that successfully resume their
        # session natively (Claude --resume, Codex exec resume) this is
        # redundant but harmless.
        history = [
            TaggedMessage(sender_label=sender, text=text)
            for _, sender, text, _ in rows
        ]
        for coord in self.coordinators.values():
            coord.full_history = list(history)
            coord.last_prompted_index = 0

    def start_external_polling(
        self, relay: Callable[[str, str], None],
    ) -> asyncio.Task:
        """Begin polling for messages written by external processes."""
        self.db.set_external_message_callback(relay)
        return asyncio.create_task(self.db.poll_external_messages())

    def compact_history(self) -> None:
        self.db.compact()

    async def shutdown(self) -> None:
        for coord in self.coordinators.values():
            await coord.shutdown()
        if self.permission_server:
            self.permission_server.stop()
        self.db.close()

    # -- Internal routing --

    def _route(
        self,
        tagged: TaggedMessage,
        excluding: UUID | None,
        mentioned: set[UUID],
        mode: RouteMode,
        hops: int = 0,
    ) -> None:
        if hops >= self._MAX_ROUTING_HOPS:
            log.warning("Routing depth limit reached (%d hops), stopping propagation", hops)
            return

        if mode == RouteMode.ALL_IF_NO_MENTIONS and not mentioned:
            responding = {
                a.id for a in self.agents
                if a.status != AgentStatus.DISCONNECTED
            }
        else:
            responding = mentioned

        for agent in self.agents:
            if agent.id == excluding:
                continue
            if agent.status == AgentStatus.DISCONNECTED:
                continue
            coord = self.coordinators.get(agent.id)
            if not coord:
                continue

            if agent.id in responding:
                msg = coord.send(tagged, self.conversation)
                asyncio.create_task(self._await_completion(msg, agent.id, hops))
            else:
                coord.inject_context(tagged)

    async def _await_completion(self, message: Message, agent_id: UUID, hops: int = 0) -> None:
        await message.wait_for_completion()
        if message.is_cancelled:
            return
        agent = self.agent_by_id(agent_id)
        if not agent:
            return

        self.db.append_message(agent.name, message.text)

        tagged = TaggedMessage(sender_label=agent.name, text=message.text)
        mentioned = extract_mentions(message.text, self.agents) - {agent_id}
        self._route(
            tagged, excluding=agent_id, mentioned=mentioned,
            mode=RouteMode.MENTIONED_ONLY, hops=hops + 1,
        )

    # -- Permission helpers --

    def _on_http_permission_request(
        self, tool_use_id: str, tool_name: str, tool_input: str
    ) -> None:
        """Called by PermissionServer when Claude POSTs a permission request."""
        claude_agent = next(
            (a for a in self.agents if a.type == AgentType.CLAUDE), None
        )
        agent_id = claude_agent.id if claude_agent else self.agents[0].id
        request = PermissionRequest(
            id=tool_use_id,
            agent_id=agent_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        self.pending_permissions.append(request)
        if self.on_permission_request:
            self.on_permission_request(request)

    def _pop_permission(self, request_id: str) -> PermissionRequest | None:
        for i, req in enumerate(self.pending_permissions):
            if req.id == request_id:
                return self.pending_permissions.pop(i)
        return None

    def _resolve_permission(
        self, request: PermissionRequest, granted: bool
    ) -> None:
        coord = self.coordinators.get(request.agent_id)
        if not coord:
            return
        coord.resolve_permission(request.id, granted)

    # -- Callback relays --

    def _relay_text_delta(self, agent_id: UUID, delta: str) -> None:
        if self.on_text_delta:
            self.on_text_delta(agent_id, delta)

    def _relay_stream_complete(self, message: Message, agent_id: UUID) -> None:
        if self.on_stream_complete:
            self.on_stream_complete(message, agent_id)

    def _relay_permission_request(self, request: PermissionRequest) -> None:
        self.pending_permissions.append(request)
        if self.on_permission_request:
            self.on_permission_request(request)

    def _relay_status_changed(self, agent_id: UUID, status: object) -> None:
        if self.on_status_changed:
            self.on_status_changed(agent_id, status)
