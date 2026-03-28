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
    TaggedMessage,
    group_tag_prefix,
)
from penta.services.agent_service import AgentService, StreamEventType
from penta.services.claude_service import ClaudeService
from penta.services.codex_service import CodexService
from penta.services.db import PentaDB
from penta.services.permission_server import PermissionServer
from penta.utils import log_task_error

log = logging.getLogger(__name__)


class AgentCoordinator:
    def __init__(
        self,
        config: AgentConfig,
        working_dir: Path,
        db: PentaDB,
        executable: str | None = None,
        permission_server: PermissionServer | None = None,
        other_agent_names: list[str] | None = None,
        session_id: str | None = None,
    ) -> None:
        self.config = config
        self._permission_server = permission_server
        self.service: AgentService = self._create_service(executable)
        self.session_id = session_id
        self.full_history: list[TaggedMessage] = []
        self.last_prompted_index: int = 0
        self._pre_prompt_index: int = 0
        self._working_dir = working_dir
        self._db = db
        self._other_names: list[str] = other_agent_names or []
        self._current_task: asyncio.Task | None = None
        self._cancel_task: asyncio.Task | None = None

        # Callbacks set by AppState / App
        self.on_text_delta: Callable[[UUID, str], None] | None = None
        self.on_stream_complete: Callable[[Message, UUID], None] | None = None
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
            # Roll back last_prompted_index so the next prompt re-includes
            # messages that were part of the cancelled stream.
            self.last_prompted_index = self._pre_prompt_index

        self.full_history.append(tagged)
        response = Message(
            sender=MessageSender.agent(self.config.id),
            text="",
            is_streaming=True,
        )
        conversation.append(response)
        self._set_status(AgentStatus.PROCESSING)

        system_prompt = self._get_system_prompt()
        self._pre_prompt_index = self.last_prompted_index
        prompt = self._build_prompt(tagged)
        self._current_task = asyncio.create_task(
            self._stream_response(prompt, response, system_prompt)
        )
        return response

    def resolve_permission(self, request_id: str, granted: bool) -> None:
        """Resolve a pending permission request via the HTTP bridge.

        Only Claude uses interactive permissions (via PermissionServer).
        Codex auto-approves all tool use at the CLI level.
        """
        if self._permission_server:
            self._permission_server.resolve_permission(request_id, granted)

    def cancel(self) -> None:
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
        self._cancel_task = asyncio.create_task(self.service.cancel())
        self._cancel_task.add_done_callback(log_task_error)

    async def shutdown(self) -> None:
        self.cancel()
        if self._cancel_task:
            await self._cancel_task
        await self.service.shutdown()

    # -- Prompt construction --

    def _get_system_prompt(self) -> str | None:
        """Return identity preamble on the first turn only."""
        if self.session_id is None:
            return self._identity_preamble()
        return None

    def _build_prompt(self, current: TaggedMessage) -> str:
        parts: list[str] = []

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
        prefix = group_tag_prefix(self.config.name)
        return (
            f'You are "{self.config.name}" in a multi-agent group chat called Penta.\n'
            f"Working directory: {self._working_dir}\n"
            f"Other participants: {others}, User.\n"
            f"Messages tagged [Group - <name>] are from the group chat visible to all.\n"
            f"Always prefix your response with {prefix} "
            f"(this tag is required so the chat system can display your message).\n"
            f"Use @name to address other participants. Respond naturally and concisely."
        )

    # -- Streaming --

    async def _stream_response(
        self, prompt: str, response: Message, system_prompt: str | None,
    ) -> None:
        received_text = False

        try:
            async for event in self.service.send(
                prompt, self.session_id, self._working_dir, system_prompt,
            ):
                match event.type:
                    case StreamEventType.SESSION_STARTED:
                        self.session_id = event.session_id
                        await self._db.save_session(self.config.name, event.session_id)
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
                        tool_line = f"> Using {event.tool_name}...\n"
                        if received_text:
                            # Already in the response body — append there.
                            response.text += "\n\n" + tool_line
                        else:
                            # Still in thinking territory — keep body clean.
                            response.thinking_text += tool_line
                            if self.on_text_delta:
                                self.on_text_delta(self.config.id, "")
                        self._set_status(AgentStatus.PROCESSING)

                    case StreamEventType.THINKING:
                        response.thinking_text += event.text or ""
                        if self.on_text_delta:
                            self.on_text_delta(self.config.id, "")
                        log.debug(
                            "[%s] Thinking: %s",
                            self.config.name, (event.text or "")[:100],
                        )

                    case StreamEventType.WARNING:
                        log.warning(
                            "[%s] %s", self.config.name, event.error,
                        )

                    case StreamEventType.ERROR:
                        if response.text:
                            response.text += f"\n\n[Error: {event.error}]"
                        else:
                            response.text = event.error or "Unknown error"
                        response.is_error = True

                    case StreamEventType.USAGE:
                        log.info(
                            "[%s] Usage: %s", self.config.name, event.usage
                        )

                    case StreamEventType.DONE:
                        break

        except asyncio.CancelledError:
            log.info("[%s] Stream cancelled", self.config.name)
            response.is_cancelled = True
            response.mark_complete()
            self._set_status(AgentStatus.IDLE)
            if self.on_stream_complete:
                self.on_stream_complete(response, self.config.id)
            return

        except Exception:
            log.exception("[%s] Stream failed", self.config.name)
            if response.text:
                response.text += "\n\n[Stream failed unexpectedly]"
            else:
                response.text = "[Stream failed unexpectedly]"
            response.is_error = True
            response.mark_complete()
            self._set_status(AgentStatus.IDLE)
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

    def _create_service(self, executable: str | None) -> AgentService:
        if self.config.type == AgentType.CLAUDE:
            return ClaudeService(
                executable=executable,
                model=self.config.model,
                permission_server=self._permission_server,
            )
        else:
            return CodexService(
                executable=executable,
                model=self.config.model,
            )
