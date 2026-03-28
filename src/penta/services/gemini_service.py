from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from penta.models import AgentType
from penta.services.agent_service import CliAgentService, StreamEvent, StreamEventType

log = logging.getLogger(__name__)


class GeminiService(CliAgentService):
    """Gemini CLI agent — thin wrapper over CliAgentService."""

    def __init__(
        self,
        executable: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(
            agent_name="Gemini",
            executable=executable or AgentType.GEMINI.find_executable(),
            model=model,
        )

    def _should_report_stderr(self, stderr_text: str, returncode: int) -> bool:
        # Gemini dumps MCP debug noise to stderr — only report real errors
        return returncode != 0 and "error" in stderr_text.lower()

    def _build_args(
        self,
        prompt: str,
        session_id: str | None,
        system_prompt: str | None,
    ) -> list[str]:
        effective_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt

        args = ["--output-format", "stream-json", "--approval-mode", "yolo"]

        if self._model:
            args += ["--model", self._model]

        if session_id:
            args += ["--resume", session_id]

        args += ["-p", effective_prompt]
        return args

    async def _parse_line(self, data: dict) -> AsyncIterator[StreamEvent]:
        msg_type = data.get("type")

        if msg_type == "init":
            sid = data.get("session_id")
            if sid:
                log.info("[Gemini] Session started: %s", sid)
                yield StreamEvent(
                    type=StreamEventType.SESSION_STARTED, session_id=sid,
                )

        elif msg_type == "message":
            role = data.get("role")
            if role == "user":
                return
            if role == "assistant" and data.get("delta"):
                text = data.get("content", "")
                if text:
                    yield StreamEvent(
                        type=StreamEventType.TEXT_DELTA, text=text,
                    )

        elif msg_type == "tool_use":
            # Surface tool arguments when available
            params = data.get("parameters")
            tool_input = json.dumps(params, indent=2) if params else None
            yield StreamEvent(
                type=StreamEventType.TOOL_USE_STARTED,
                tool_id=data.get("tool_id", ""),
                tool_name=data.get("tool_name", ""),
                tool_input=tool_input,
            )

        elif msg_type == "error":
            severity = data.get("severity", "error")
            message = data.get("message", "Unknown error")
            if severity == "error":
                log.error("[Gemini] Error event: %s", message)
                yield StreamEvent(
                    type=StreamEventType.ERROR, error=message,
                )
            else:
                log.warning("[Gemini] Warning: %s", message)
                yield StreamEvent(
                    type=StreamEventType.WARNING, error=message,
                )

        elif msg_type == "result":
            status = data.get("status", "")
            if status != "success":
                error_msg = data.get("error", "Gemini turn failed")
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                yield StreamEvent(
                    type=StreamEventType.ERROR, error=str(error_msg),
                )
            # Capture token usage from result stats (both success and error)
            stats = data.get("stats")
            if stats:
                yield StreamEvent(
                    type=StreamEventType.USAGE, usage=stats,
                )
