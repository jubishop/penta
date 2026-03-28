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
        # Per-send parser state
        self._seen_session_id = False
        self._has_emitted_text = False

    def _reset_parse_state(self) -> None:
        self._seen_session_id = False
        self._has_emitted_text = False

    def _build_args(
        self,
        prompt: str,
        session_id: str | None,
        system_prompt: str | None,
    ) -> list[str]:
        args = ["-p", "--verbose", "--output-format", "stream-json", "--include-partial-messages"]

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
            subtype = data.get("subtype")
            if subtype == "init":
                sid = data.get("session_id")
                if sid:
                    self._seen_session_id = True
                    log.info("[Claude] Session started: %s", sid)
                    yield StreamEvent(
                        type=StreamEventType.SESSION_STARTED, session_id=sid,
                    )
            elif subtype == "api_retry":
                attempt = data.get("attempt", "?")
                delay = data.get("retry_delay_ms", 0)
                error = data.get("error", "")
                log.warning(
                    "[Claude] API retry attempt=%s delay=%sms: %s",
                    attempt, delay, error,
                )
                yield StreamEvent(
                    type=StreamEventType.WARNING,
                    error=f"Retrying (attempt {attempt})...",
                )

        elif msg_type == "stream_event":
            event = data.get("event", {})
            event_type = event.get("type")

            if event_type == "content_block_start":
                content_block = event.get("content_block", {})
                block_type = content_block.get("type")
                if block_type == "tool_use":
                    yield StreamEvent(
                        type=StreamEventType.TOOL_USE_STARTED,
                        tool_id=content_block.get("id", ""),
                        tool_name=content_block.get("name", ""),
                    )
                # Separate consecutive text blocks with whitespace.
                # Skip for tool_use — the coordinator adds its own spacing.
                elif self._has_emitted_text:
                    yield StreamEvent(
                        type=StreamEventType.TEXT_DELTA, text="\n\n",
                    )

            elif event_type == "content_block_delta":
                delta = event.get("delta", {})
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        self._has_emitted_text = True
                        yield StreamEvent(
                            type=StreamEventType.TEXT_DELTA, text=text,
                        )
                elif delta_type == "thinking_delta":
                    text = delta.get("thinking", "")
                    if text:
                        yield StreamEvent(
                            type=StreamEventType.THINKING, text=text,
                        )

        elif msg_type == "tool_progress":
            # Heartbeat while a tool is running — log for liveness
            log.debug("[Claude] Tool progress heartbeat")

        elif msg_type == "rate_limit_event":
            status = data.get("status", "")
            if status in ("warning", "rejected"):
                log.warning("[Claude] Rate limit: %s", status)
                yield StreamEvent(
                    type=StreamEventType.WARNING,
                    error=f"Rate limited ({status})",
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

            # Capture session_id from result only if not already seen from init
            sid = data.get("session_id")
            if sid and not self._seen_session_id:
                self._seen_session_id = True
                yield StreamEvent(
                    type=StreamEventType.SESSION_STARTED, session_id=sid,
                )

            # Token usage from result
            cost = data.get("cost_usd") or data.get("total_cost_usd")
            usage_stats = data.get("usage") or data.get("model_usage")
            duration = data.get("duration_ms")
            num_turns = data.get("num_turns")
            if cost is not None or usage_stats:
                usage: dict = {"cost_usd": cost, **(usage_stats or {})}
                if duration is not None:
                    usage["duration_ms"] = duration
                if num_turns is not None:
                    usage["num_turns"] = num_turns
                yield StreamEvent(
                    type=StreamEventType.USAGE, usage=usage,
                )
