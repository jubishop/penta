from __future__ import annotations

import logging
from typing import AsyncIterator

from penta.models import AgentType
from penta.services.agent_service import CliAgentService, StreamEvent, StreamEventType
from penta.services.permission_server import PermissionServer

log = logging.getLogger(__name__)


class ClaudeService(CliAgentService):
    """Claude CLI agent — thin wrapper over CliAgentService."""

    def __init__(
        self,
        executable: str | None = None,
        model: str | None = None,
        permission_server: PermissionServer | None = None,
    ) -> None:
        super().__init__(
            agent_name="Claude",
            executable=executable or AgentType.CLAUDE.find_executable(),
            model=model,
        )
        self._permission_server = permission_server

    def _build_args(
        self,
        prompt: str,
        session_id: str | None,
        system_prompt: str | None,
    ) -> list[str]:
        args = ["-p", "--verbose", "--output-format", "stream-json"]

        if self._model:
            args += ["--model", self._model]

        if system_prompt:
            args += ["--append-system-prompt", system_prompt]

        if self._permission_server:
            args += ["--settings", self._permission_server.hook_settings_json]

        if session_id:
            args += ["--resume", session_id]

        args.append(prompt)
        return args

    async def _parse_line(self, data: dict) -> AsyncIterator[StreamEvent]:
        msg_type = data.get("type")

        if msg_type == "system":
            if data.get("subtype") == "init":
                sid = data.get("session_id")
                if sid:
                    log.info("[Claude] Session started: %s", sid)
                    yield StreamEvent(
                        type=StreamEventType.SESSION_STARTED, session_id=sid,
                    )

        elif msg_type == "stream_event":
            event = data.get("event", {})
            event_type = event.get("type")

            if event_type == "content_block_start":
                content_block = event.get("content_block", {})
                if content_block.get("type") == "tool_use":
                    yield StreamEvent(
                        type=StreamEventType.TOOL_USE_STARTED,
                        tool_id=content_block.get("id", ""),
                        tool_name=content_block.get("name", ""),
                    )

            elif event_type == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        yield StreamEvent(
                            type=StreamEventType.TEXT_DELTA, text=text,
                        )

        elif msg_type == "result":
            result_text = data.get("result", "")
            if data.get("is_error"):
                log.error("[Claude] API error: %s", result_text)
                yield StreamEvent(
                    type=StreamEventType.ERROR, error=result_text,
                )
            elif result_text:
                log.info("[Claude] Result received, len=%d", len(result_text))
                yield StreamEvent(
                    type=StreamEventType.TEXT_COMPLETE, text=result_text,
                )

            # Capture session_id from result if not yet seen
            sid = data.get("session_id")
            if sid:
                yield StreamEvent(
                    type=StreamEventType.SESSION_STARTED, session_id=sid,
                )

            # Token usage from result
            cost = data.get("cost_usd") or data.get("cost")
            usage_stats = data.get("usage") or data.get("stats")
            if cost is not None or usage_stats:
                yield StreamEvent(
                    type=StreamEventType.USAGE,
                    usage={"cost_usd": cost, **(usage_stats or {})},
                )
