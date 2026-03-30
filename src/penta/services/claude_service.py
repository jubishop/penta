from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import AsyncIterator

from penta.services.agent_service import CliAgentService, StreamEvent, StreamEventType

log = logging.getLogger(__name__)


class ClaudeService(CliAgentService):
    """Claude CLI agent — thin wrapper over CliAgentService."""

    _needs_stdin: bool = True

    def __init__(
        self,
        executable: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(
            agent_name="Claude",
            executable=executable,
            model=model,
        )
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
        # Prompt is delivered via stdin, not as a CLI arg, because
        # --input-format stream-json ignores CLI arg prompts.
        args = ["-p", "--verbose", "--output-format", "stream-json",
                "--input-format", "stream-json",
                "--include-partial-messages",
                "--permission-prompt-tool", "stdio"]

        if self._model:
            args += ["--model", self._model]

        if system_prompt:
            args += ["--append-system-prompt", system_prompt]

        if session_id:
            args += ["--resume", session_id]

        return args

    async def _on_process_started(
        self, proc: asyncio.subprocess.Process, prompt: str,
    ) -> None:
        """Send the initialize handshake and prompt via stdin."""
        if not proc.stdin:
            return

        # 1. Initialize control handshake (as the Agent SDK does)
        init_req = json.dumps({
            "type": "control_request",
            "request_id": f"req_init_{uuid.uuid4().hex[:8]}",
            "request": {"subtype": "initialize", "hooks": None},
        })
        proc.stdin.write(init_req.encode() + b"\n")

        # 2. Send the user prompt
        user_msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": prompt},
        })
        proc.stdin.write(user_msg.encode() + b"\n")
        await proc.stdin.drain()

    async def _auto_approve(self, request_id: str) -> None:
        """Auto-approve a control_request by writing to stdin."""
        proc = self._current_process
        if proc and proc.stdin:
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": {"behavior": "allow"},
                },
            }
            line = json.dumps(response).encode() + b"\n"
            proc.stdin.write(line)
            await proc.stdin.drain()

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

        elif msg_type == "control_request":
            # SDK format: {request_id, request: {subtype, tool_name, input}}
            # Possible flat format: {id, subtype, tool: {name, input}}
            req = data.get("request", {})
            subtype = req.get("subtype") or data.get("subtype")
            if subtype == "can_use_tool":
                tool_name = req.get("tool_name") or data.get("tool", {}).get("name", "")
                request_id = data.get("request_id") or data.get("id", "")
                tool_input = req.get("input") or data.get("tool", {}).get("input", {})
                tool_id = req.get("tool_use_id") or data.get("tool", {}).get("id", "")

                if tool_name == "AskUserQuestion":
                    questions = tool_input.get("questions", [])
                    yield StreamEvent(
                        type=StreamEventType.QUESTION,
                        questions=questions,
                        control_request_id=request_id,
                        tool_name=tool_name,
                    )
                elif tool_name == "ExitPlanMode":
                    plan = tool_input.get("plan", "")
                    yield StreamEvent(
                        type=StreamEventType.PLAN_REVIEW,
                        plan_text=plan,
                        control_request_id=request_id,
                        tool_name=tool_name,
                    )
                else:
                    # Auto-approve all other tools
                    await self._auto_approve(request_id)
                    yield StreamEvent(
                        type=StreamEventType.TOOL_USE_STARTED,
                        tool_name=tool_name,
                        tool_id=tool_id,
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
