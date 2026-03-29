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

    def compact(self, limit: int) -> None:
        """Trim full_history to *limit* entries and adjust indices."""
        trimmed = max(0, len(self.full_history) - limit)
        if trimmed:
            del self.full_history[:trimmed]
            self.last_prompted_index = max(0, self.last_prompted_index - trimmed)
            self._pre_prompt_index = max(0, self._pre_prompt_index - trimmed)

    def inject_context(self, tagged: TaggedMessage) -> None:
        """Record a message for catch-up delivery on next send()."""
        self.full_history.append(tagged)

    def send(
        self, tagged: TaggedMessage, conversation: list[Message]
    ) -> Message:
        """Send a message and return the streaming response Message."""
        cancelled_task: asyncio.Task | None = None
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            cancelled_task = self._current_task
            # Roll back last_prompted_index so the next prompt re-includes
            # messages that were part of the cancelled stream.
            self.last_prompted_index = self._pre_prompt_index

        response = Message(
            sender=MessageSender.agent(self.config.id),
            text="",
            is_streaming=True,
        )
        conversation.append(response)
        self.set_status(AgentStatus.PROCESSING)

        system_prompt = self._get_system_prompt()
        self._pre_prompt_index = self.last_prompted_index
        prompt = self._build_prompt(tagged)
        self.full_history.append(tagged)
        self.last_prompted_index = len(self.full_history)
        self._current_task = asyncio.create_task(
            self._stream_response(
                prompt, response, system_prompt,
                wait_for=cancelled_task,
            )
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

        missed = self.full_history[self.last_prompted_index:]
        if missed:
            parts.append("[Messages since your last response:]")
            parts.extend(msg.formatted for msg in missed)
            parts.append("")
            parts.append("[New message:]")

        parts.append(current.formatted)
        return "\n".join(parts)

    def _identity_preamble(self) -> str:
        others = ", ".join(self._other_names) if self._other_names else "other agents"
        return (
            f'You are "{self.config.name}" in a multi-agent group chat called Penta.\n'
            f"Working directory: {self._working_dir}\n"
            f"Other participants: {others}, User.\n"
            f"Messages tagged [Group - <name>] are from the group chat.\n"
            f"Use @name to address other participants."
        )

    # -- Streaming --

    async def _stream_response(
        self,
        prompt: str,
        response: Message,
        system_prompt: str | None,
        wait_for: asyncio.Task | None = None,
    ) -> None:
        if wait_for is not None:
            log.debug("[%s] Waiting for cancelled stream to clean up", self.config.name)
            try:
                await wait_for
            except asyncio.CancelledError:
                pass
            log.debug("[%s] Cancelled stream cleaned up, proceeding", self.config.name)

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
                        log.info(
                            "[%s] Tool use: %s (received_text=%s)",
                            self.config.name, event.tool_name, received_text,
                        )
                        tool_line = f"> Using {event.tool_name}...\n"
                        if received_text:
                            # Already in the response body — append there.
                            response.text += "\n\n" + tool_line
                        else:
                            # Still in thinking territory — keep body clean.
                            response.thinking_text += tool_line
                            if self.on_text_delta:
                                self.on_text_delta(self.config.id, "")
                        self.set_status(AgentStatus.PROCESSING)

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
                        log.debug("[%s] Stream DONE event received", self.config.name)
                        break

        except asyncio.CancelledError:
            log.info(
                "[%s] Stream cancelled (body_len=%d, thinking_len=%d, received_text=%s)",
                self.config.name, len(response.text),
                len(response.thinking_text), received_text,
            )
            response.is_cancelled = True
            response.mark_complete()
            self.set_status(AgentStatus.IDLE)
            if self.on_stream_complete:
                self.on_stream_complete(response, self.config.id)
            return

        except Exception:
            log.exception(
                "[%s] Stream failed (body_len=%d, received_text=%s)",
                self.config.name, len(response.text), received_text,
            )
            if response.text:
                response.text += "\n\n[Stream failed unexpectedly]"
            else:
                response.text = "[Stream failed unexpectedly]"
            response.is_error = True
            response.mark_complete()
            self.set_status(AgentStatus.IDLE)
            if self.on_stream_complete:
                self.on_stream_complete(response, self.config.id)
            return

        log.info(
            "[%s] Stream completed normally (body_len=%d)",
            self.config.name, len(response.text),
        )
        response.mark_complete()
        self.set_status(AgentStatus.IDLE)

        # Record agent's response in history
        self.full_history.append(
            TaggedMessage(sender_label=self.config.name, text=response.text)
        )

        if self.on_stream_complete:
            self.on_stream_complete(response, self.config.id)

    # -- Helpers --

    def set_status(self, status: AgentStatus) -> None:
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
