from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable
from uuid import UUID

from penta.models import (
    AgentConfig,
    AgentStatus,
    AgentType,
    Message,
    MessageSender,
    PermissionRequest,
    TaggedMessage,
)
from penta.services.agent_service import AgentService, StreamEventType
from penta.services.claude_service import ClaudeService
from penta.services.codex_service import CodexService
from penta.services.gemini_service import GeminiService
from penta.services.db import PentaDB
from penta.services.permission_server import PermissionServer

log = logging.getLogger(__name__)


class AgentCoordinator:
    def __init__(
        self,
        config: AgentConfig,
        working_dir: Path,
        db: PentaDB,
        permission_server: PermissionServer | None = None,
        other_agent_names: list[str] | None = None,
    ) -> None:
        self.config = config
        self._permission_server = permission_server
        self.service: AgentService = self._create_service()
        self.session_id: str | None = db.load_session(config.name)
        self.full_history: list[TaggedMessage] = []
        self.last_prompted_index: int = 0
        self._working_dir = working_dir
        self._db = db
        self._other_names: list[str] = other_agent_names or []
        self._current_task: asyncio.Task | None = None

        # Callbacks set by AppState / App
        self.on_text_delta: Callable[[UUID, str], None] | None = None
        self.on_stream_complete: Callable[[Message, UUID], None] | None = None
        self.on_permission_request: Callable[[PermissionRequest], None] | None = None
        self.on_status_changed: Callable[[UUID, AgentStatus], None] | None = None

    def set_other_agent_names(self, names: list[str]) -> None:
        self._other_names = names

    def inject_context(self, tagged: TaggedMessage) -> None:
        """Record a message for catch-up delivery on next send()."""
        self.full_history.append(tagged)

    def send(
        self, tagged: TaggedMessage, conversation: list[Message]
    ) -> Message:
        """Send a message and return the streaming response Message."""
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

        self.full_history.append(tagged)
        response = Message(
            sender=MessageSender.agent(self.config.id),
            text="",
            is_streaming=True,
        )
        conversation.append(response)
        self._set_status(AgentStatus.PROCESSING)

        prompt = self._build_prompt(tagged)
        self._current_task = asyncio.create_task(
            self._stream_response(prompt, response)
        )
        return response

    def resolve_permission(self, request_id: str, granted: bool) -> None:
        """Resolve a pending permission request through the appropriate channel."""
        if self.config.type == AgentType.CLAUDE:
            if self._permission_server:
                self._permission_server.resolve_permission(request_id, granted)
        else:
            asyncio.create_task(
                self.service.respond_to_permission(request_id, granted)
            )

    def cancel(self) -> None:
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
        asyncio.create_task(self.service.cancel())

    async def shutdown(self) -> None:
        self.cancel()
        await self.service.shutdown()

    # -- Prompt construction --

    def _build_prompt(self, current: TaggedMessage) -> str:
        parts: list[str] = []

        if self.session_id is None:
            parts.append(self._identity_preamble())
            parts.append("")

        # Catch-up: messages since last prompt (excluding current, which is last)
        missed = self.full_history[self.last_prompted_index:-1]
        if missed:
            parts.append("[Messages since your last response:]")
            parts.extend(msg.formatted for msg in missed)
            parts.append("")
            parts.append("[New message:]")

        parts.append(current.formatted)
        self.last_prompted_index = len(self.full_history)
        return "\n".join(parts)

    def _identity_preamble(self) -> str:
        others = ", ".join(self._other_names) if self._other_names else "other agents"
        return (
            f'You are "{self.config.name}" in a multi-agent group chat called Penta.\n'
            f"Working directory: {self._working_dir}\n"
            f"Other participants: {others}, User.\n"
            f"Messages tagged [Group - <name>] are from the group chat visible to all.\n"
            f"Use @name to address other participants. Respond naturally and concisely."
        )

    # -- Streaming --

    async def _stream_response(self, prompt: str, response: Message) -> None:
        received_text = False

        try:
            async for event in self.service.send(
                prompt, self.session_id, self._working_dir
            ):
                match event.type:
                    case StreamEventType.SESSION_STARTED:
                        self.session_id = event.session_id
                        self._db.save_session(self.config.name, event.session_id)
                        log.info(
                            "[%s] Session: %s", self.config.name, event.session_id
                        )

                    case StreamEventType.TEXT_DELTA:
                        response.text += event.text
                        received_text = True
                        if self.on_text_delta:
                            self.on_text_delta(self.config.id, event.text)

                    case StreamEventType.TEXT_COMPLETE:
                        if not received_text:
                            response.text = event.text

                    case StreamEventType.TOOL_USE_STARTED:
                        if response.text:
                            response.text += "\n\n"
                        response.text += f"> Using {event.tool_name}...\n"
                        received_text = True
                        self._set_status(AgentStatus.PROCESSING)

                    case StreamEventType.PERMISSION_REQUESTED:
                        self._set_status(AgentStatus.AWAITING_PERMISSION)
                        if self.on_permission_request:
                            self.on_permission_request(PermissionRequest(
                                id=event.request_id or "",
                                agent_id=self.config.id,
                                tool_name=event.tool_name or "unknown",
                                tool_input=event.tool_input or "",
                            ))

                    case StreamEventType.ERROR:
                        if response.text:
                            response.text += f"\n\n[Error: {event.error}]"
                        else:
                            response.text = event.error or "Unknown error"
                        response.is_error = True

                    case StreamEventType.DONE:
                        break

        except asyncio.CancelledError:
            log.info("[%s] Stream cancelled", self.config.name)
            response.is_cancelled = True
            response.mark_complete()
            self._set_status(AgentStatus.IDLE)
            # Still fire on_stream_complete so the UI clears the streaming
            # widget state.  _await_completion guards on is_cancelled to
            # prevent persistence / routing.
            if self.on_stream_complete:
                self.on_stream_complete(response, self.config.id)
            return

        response.mark_complete()
        self._set_status(AgentStatus.IDLE)

        # Record agent's response in history
        self.full_history.append(
            TaggedMessage(sender_label=self.config.name, text=response.text)
        )

        if self.on_stream_complete:
            self.on_stream_complete(response, self.config.id)

    # -- Helpers --

    def _set_status(self, status: AgentStatus) -> None:
        self.config.status = status
        if self.on_status_changed:
            self.on_status_changed(self.config.id, status)

    def _create_service(self) -> AgentService:
        if self.config.type == AgentType.CLAUDE:
            return ClaudeService(
                executable=self.config.type.find_executable(),
                permission_server=self._permission_server,
            )
        elif self.config.type == AgentType.GEMINI:
            return GeminiService(
                executable=self.config.type.find_executable(),
            )
        else:
            return CodexService(
                executable=self.config.type.find_executable(),
            )
