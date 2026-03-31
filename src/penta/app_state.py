from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import UUID

from penta.coordinators.agent_coordinator import AgentCoordinator
from penta.models.agent_config import AgentConfig
from penta.models.agent_status import AgentStatus
from penta.models.agent_type import AgentType
from penta.models.conversation_info import ConversationInfo
from penta.models.message import Message
from penta.models.message_sender import MessageSender
from penta.models.pending_plan import PendingPlan
from penta.models.tagged_message import TaggedMessage
from penta.routing import MessageRouter
from penta.services.agent_service import AgentService
from penta.services.db import PentaDB
from penta.services.permission_server import PermissionServer

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
        self._permission_server: PermissionServer | None = None

        self.router = MessageRouter(
            self.agents, self._agents_by_id, self.coordinators, self.conversation, self.db,
        )

        # Conversation state
        self.current_conversation_id: int = 1
        self.current_conversation_title: str = "Default"

        # Pending plans awaiting user approval
        self.pending_plans: dict[UUID, PendingPlan] = {}

        # Callbacks for the TUI layer
        self.on_text_delta: Callable[[UUID, str], None] | None = None
        self.on_stream_complete: Callable[[Message, UUID], None] | None = None
        self.on_status_changed: Callable[[UUID, AgentStatus], None] | None = None
        self.on_conversation_switched: Callable[[], None] | None = None
        self.on_question_asked: Callable[[UUID, list[dict], str], None] | None = None
        self.on_plan_review: Callable[[UUID, str, str], None] | None = None

    async def connect(self) -> None:
        await self.db.connect()
        self.current_conversation_id = self.db.conversation_id

        # Load the title of the active conversation
        rows = await self.db.list_conversations()
        for cid, title, _, _ in rows:
            if cid == self.current_conversation_id:
                self.current_conversation_title = title
                break

        # Start permission server for hook-based plan review
        if not self._service_factory:  # Skip in tests with fake services
            self._start_permission_server()

    def _start_permission_server(self) -> None:
        loop = asyncio.get_running_loop()
        server = PermissionServer(loop)
        server.set_plan_review_callback(self._on_hook_plan_review)
        server.set_question_callback(self._on_hook_question)
        if server.start():
            self._permission_server = server
            log.info("Permission server started on port %d", server.port)
        else:
            log.warning("Permission server failed to start — falling back to auto-approve")

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

        hook_settings = (
            self._permission_server.hook_settings_json
            if self._permission_server and agent_type == AgentType.CLAUDE
            else None
        )
        coordinator = AgentCoordinator(
            config=config,
            working_dir=self.directory,
            db=self.db,
            executable=executable,
            other_agent_names=other_names,
            session_id=session_id,
            service=service,
            hook_settings=hook_settings,
        )

        # Wire callbacks through to TUI
        self._wire_coordinator_callbacks(coordinator)

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

    def cancel_agent(self, agent_id: UUID) -> bool:
        """Cancel a specific agent's current stream. Returns True if was streaming."""
        coord = self.coordinators.get(agent_id)
        if coord and coord.config.status.is_busy:
            coord.cancel()
            self.pending_plans.pop(agent_id, None)
            # Only resolve hooks for agents that use them (Claude)
            if self._permission_server and coord.config.type == AgentType.CLAUDE:
                self._permission_server.resolve_all_pending()
            return True
        return False

    def cancel_all_busy(self) -> int:
        """Cancel all busy agents (streaming or waiting for user). Returns count cancelled."""
        count = 0
        claude_cancelled = False
        for coord in self.coordinators.values():
            if coord.config.status.is_busy:
                coord.cancel()
                self.pending_plans.pop(coord.config.id, None)
                if coord.config.type == AgentType.CLAUDE:
                    claude_cancelled = True
                count += 1
        if claude_cancelled and self._permission_server:
            self._permission_server.resolve_all_pending()
        return count

    @property
    def directory_name(self) -> str:
        return self.directory.name

    @property
    def external_participants(self) -> set[str]:
        return self.router.external_participants

    # -- Delegated to router --

    _PLAN_TOKEN_RE = re.compile(r"/plan\b", re.IGNORECASE)

    async def send_user_message(
        self, text: str, resolved_plan_id: UUID | None = None,
    ) -> None:
        """Send a user message, interpolating /plan if present.

        If *resolved_plan_id* is provided, that specific plan is used for
        interpolation.  Otherwise, when there is exactly one pending plan it
        is used automatically.  When multiple plans exist and none is
        specified, the original text is sent without interpolation (the TUI
        should show the picker first and call again with *resolved_plan_id*).
        """
        routed_text: str | None = None
        if self._PLAN_TOKEN_RE.search(text):
            plan = self._resolve_plan_for_interpolation(resolved_plan_id)
            if plan:
                routed_text = self._PLAN_TOKEN_RE.sub(
                    f"\n\n{plan.plan_text}\n\n", text,
                )
        await self.router.send_user_message(text, routed_text=routed_text)

    def _resolve_plan_for_interpolation(
        self, explicit_id: UUID | None = None,
    ) -> PendingPlan | None:
        if explicit_id is not None:
            return self.pending_plans.get(explicit_id)
        if len(self.pending_plans) == 1:
            return next(iter(self.pending_plans.values()))
        return None

    def receive_external_message(self, sender_name: str, text: str) -> None:
        self.router.receive_external_message(sender_name, text)

    # -- Plan responses (via permission server hook) --

    def approve_plan(self, agent_id: UUID) -> None:
        plan = self.pending_plans.pop(agent_id, None)
        if not plan:
            return
        agent = self.agent_by_id(agent_id)
        if agent:
            agent.status = AgentStatus.PROCESSING
            if self.on_status_changed:
                self.on_status_changed(agent_id, AgentStatus.PROCESSING)
        if self._permission_server:
            self._permission_server.resolve_plan_review(plan.tool_use_id, True)
            log.info("[%s] Plan approved", plan.agent_name)

    def reject_plan(self, agent_id: UUID) -> None:
        """Reject a pending plan (deny via hook). Feedback delivery is the caller's job."""
        plan = self.pending_plans.pop(agent_id, None)
        if not plan:
            return
        agent = self.agent_by_id(agent_id)
        if agent:
            agent.status = AgentStatus.PROCESSING
            if self.on_status_changed:
                self.on_status_changed(agent_id, AgentStatus.PROCESSING)
        if self._permission_server:
            self._permission_server.resolve_plan_review(plan.tool_use_id, False)
            log.info("[%s] Plan rejected", plan.agent_name)

    # -- Conversation management --

    async def create_conversation(self, title: str) -> ConversationInfo:
        cid = await self.db.create_conversation(title)
        rows = await self.db.list_conversations()
        for row_id, row_title, created, updated in rows:
            if row_id == cid:
                return ConversationInfo(
                    id=cid,
                    title=row_title,
                    created_at=datetime.fromisoformat(created),
                    updated_at=datetime.fromisoformat(updated),
                )
        # Shouldn't happen, but satisfy the return type
        raise RuntimeError(f"Conversation {cid} not found after creation")

    async def list_conversations(self) -> list[ConversationInfo]:
        rows = await self.db.list_conversations()
        return [
            ConversationInfo(
                id=cid,
                title=title,
                created_at=datetime.fromisoformat(created),
                updated_at=datetime.fromisoformat(updated),
            )
            for cid, title, created, updated in rows
        ]

    async def delete_conversation(self, conversation_id: int) -> bool:
        """Delete a conversation. Returns False if it's the active or sole conversation.

        Raises ValueError if the conversation does not exist.
        """
        if not await self.db._conversation_exists(conversation_id):
            raise ValueError(f"Conversation {conversation_id} does not exist")
        if conversation_id == self.current_conversation_id:
            return False
        rows = await self.db.list_conversations()
        if len(rows) <= 1:
            return False
        await self.db.delete_conversation(conversation_id)
        return True

    async def rename_conversation(self, conversation_id: int, title: str) -> None:
        await self.db.rename_conversation(conversation_id, title)
        if conversation_id == self.current_conversation_id:
            self.current_conversation_title = title

    async def switch_conversation(self, conversation_id: int) -> None:
        """Tear down current agent state and rebuild for the target conversation."""
        if conversation_id == self.current_conversation_id:
            return

        # 0. Validate target exists before tearing anything down
        if not await self.db._conversation_exists(conversation_id):
            raise ValueError(f"Conversation {conversation_id} does not exist")

        # 1. Pause external polling so it can't check or deliver during switch
        self.db.pause_polling()

        try:
            # 2. Cancel active streams and unblock pending hooks
            for coord in self.coordinators.values():
                coord.cancel()
            if self._permission_server:
                self._permission_server.resolve_all_pending()
            self.pending_plans.clear()

            # 3. Wait for pending routing tasks to persist to the current conversation
            await self.router.drain()

            # 4. Shut down coordinators (cancels processes, shuts down services)
            for coord in self.coordinators.values():
                await coord.shutdown()

            # 5. Switch DB context (resets _last_seen_id for the new conversation)
            await self.db.set_conversation(conversation_id)

            # 6. Clear in-memory state (same list/dict objects — router refs stay valid)
            self.conversation.clear()
            self.router.external_participants.clear()

            # 7. Rebuild coordinators with new conversation's sessions
            await self._rebuild_coordinators()

            # 8. Load new conversation's history
            await self.load_chat_history()

            # 9. Update conversation tracking
            self.current_conversation_id = conversation_id
            rows = await self.db.list_conversations()
            for cid, title, _, _ in rows:
                if cid == conversation_id:
                    self.current_conversation_title = title
                    break

        finally:
            # 10. Resume external polling
            self.db.resume_polling()

        # 11. Notify UI
        if self.on_conversation_switched:
            self.on_conversation_switched()

    async def _rebuild_coordinators(self) -> None:
        """Create fresh coordinators for all registered agents using the current conversation's sessions."""
        self.coordinators.clear()
        other_names_map = {
            config.id: [a.name for a in self.agents if a.id != config.id]
            for config in self.agents
        }

        for config in self.agents:
            session_id = await self.db.load_session(config.name)

            if self._service_factory:
                service = self._service_factory(config)
                executable = None
            else:
                executable = config.type.find_executable()
                if not executable:
                    config.status = AgentStatus.DISCONNECTED
                else:
                    config.status = AgentStatus.IDLE
                service = None

            hook_settings = (
                self._permission_server.hook_settings_json
                if self._permission_server and config.type == AgentType.CLAUDE
                else None
            )
            coordinator = AgentCoordinator(
                config=config,
                working_dir=self.directory,
                db=self.db,
                executable=executable,
                other_agent_names=other_names_map[config.id],
                session_id=session_id,
                service=service,
                hook_settings=hook_settings,
            )

            self._wire_coordinator_callbacks(coordinator)

            self.coordinators[config.id] = coordinator

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
        if self._permission_server:
            await self._permission_server.stop()
        for coord in self.coordinators.values():
            await coord.shutdown()
        await self.db.close()

    # -- Callback relays --

    def _wire_coordinator_callbacks(self, coord: AgentCoordinator) -> None:
        coord.on_text_delta = self._relay_text_delta
        coord.on_stream_complete = self._relay_stream_complete
        coord.on_status_changed = self._relay_status_changed

    def _relay_text_delta(self, agent_id: UUID, delta: str) -> None:
        if self.on_text_delta:
            self.on_text_delta(agent_id, delta)

    def _relay_stream_complete(self, message: Message, agent_id: UUID) -> None:
        # Clean up pending plan if the stream ended (cancelled, errored, etc.)
        self.pending_plans.pop(agent_id, None)
        if self.on_stream_complete:
            self.on_stream_complete(message, agent_id)

    def _relay_status_changed(self, agent_id: UUID, status: AgentStatus) -> None:
        if self.on_status_changed:
            self.on_status_changed(agent_id, status)

    def _on_hook_question(
        self, tool_use_id: str, questions: list[dict],
    ) -> None:
        """Called by the permission server when AskUserQuestion hook fires.

        Surfaces the structured questions to the TUI.  The hook HTTP
        response is blocked until the user answers (via resolve_question).
        The answers are injected via updatedInput so Claude receives them
        directly as the AskUserQuestion tool result.
        """
        agent = next(
            (a for a in self.agents if a.type == AgentType.CLAUDE), None,
        )
        if not agent:
            return
        agent.status = AgentStatus.WAITING_FOR_USER
        if self.on_status_changed:
            self.on_status_changed(agent.id, AgentStatus.WAITING_FOR_USER)
        log.info("[%s] Question intercepted (%d questions)", agent.name, len(questions))
        if self.on_question_asked:
            self.on_question_asked(agent.id, questions, tool_use_id)

    def _on_hook_plan_review(
        self, tool_use_id: str, plan_text: str, full_input: dict,
    ) -> None:
        """Called by the permission server when ExitPlanMode hook fires.

        Identifies the Claude agent (only Claude uses hooks) and stores
        the pending plan.  Runs on the asyncio event loop thread.
        """
        # Find the Claude agent — currently only Claude uses hooks
        agent = next(
            (a for a in self.agents if a.type == AgentType.CLAUDE), None,
        )
        if not agent:
            log.warning("ExitPlanMode hook fired but no Claude agent found")
            return

        agent.status = AgentStatus.WAITING_FOR_USER
        if self.on_status_changed:
            self.on_status_changed(agent.id, AgentStatus.WAITING_FOR_USER)
        agent_name = agent.name
        self.pending_plans[agent.id] = PendingPlan(
            agent_id=agent.id,
            agent_name=agent_name,
            tool_use_id=tool_use_id,
            plan_text=plan_text,
        )
        log.info("[%s] Plan review pending: %s", agent_name, plan_text[:100])
        if self.on_plan_review:
            self.on_plan_review(agent.id, plan_text, tool_use_id)
