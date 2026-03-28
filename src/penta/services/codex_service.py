from __future__ import annotations

import logging
from typing import AsyncIterator

from penta.models import AgentType
from penta.services.agent_service import CliAgentService, StreamEvent, StreamEventType

log = logging.getLogger(__name__)


class CodexService(CliAgentService):
    """Codex CLI agent — thin wrapper over CliAgentService."""

    def __init__(
        self,
        executable: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(
            agent_name="Codex",
            executable=executable or AgentType.CODEX.find_executable(),
            model=model,
        )

    def _build_args(
        self,
        prompt: str,
        session_id: str | None,
        system_prompt: str | None,
    ) -> list[str]:
        effective_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt

        if session_id:
            args = ["exec", "resume", session_id]
        else:
            args = ["exec"]

        if self._model:
            args += ["--model", self._model]

        args += [
            "--json",
            "--full-auto",
            "--ask-for-approval", "never",
            "--skip-git-repo-check",
            effective_prompt,
        ]
        return args

    async def _parse_line(self, data: dict) -> AsyncIterator[StreamEvent]:
        event_type = data.get("type", "")

        if event_type == "thread.started":
            thread_id = data.get("thread_id", "")
            if thread_id:
                log.info("[Codex] Session started: %s", thread_id)
                yield StreamEvent(
                    type=StreamEventType.SESSION_STARTED, session_id=thread_id,
                )

        elif event_type == "item.started":
            item = data.get("item", {})
            item_type = item.get("type", "")
            if item_type == "command_execution":
                yield StreamEvent(
                    type=StreamEventType.TOOL_USE_STARTED,
                    tool_id=item.get("id", ""),
                    tool_name=item.get("command", ""),
                )
            elif item_type == "file_change":
                changes = item.get("changes", [])
                summary = ", ".join(
                    f"{c.get('kind', '?')} {c.get('path', '?')}" for c in changes
                ) or "file changes"
                yield StreamEvent(
                    type=StreamEventType.TOOL_USE_STARTED,
                    tool_id=item.get("id", ""),
                    tool_name=summary,
                )
            elif item_type == "mcp_tool_call":
                server = item.get("server", "")
                tool = item.get("tool", "")
                yield StreamEvent(
                    type=StreamEventType.TOOL_USE_STARTED,
                    tool_id=item.get("id", ""),
                    tool_name=f"{server}:{tool}" if server else tool,
                )
            elif item_type == "web_search":
                query = item.get("query", "web search")
                yield StreamEvent(
                    type=StreamEventType.TOOL_USE_STARTED,
                    tool_id=item.get("id", ""),
                    tool_name=f"web_search: {query}",
                )

        elif event_type == "item.updated":
            item = data.get("item", {})
            item_type = item.get("type", "")
            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    yield StreamEvent(
                        type=StreamEventType.TEXT_DELTA, text=text,
                    )

        elif event_type == "item.completed":
            item = data.get("item", {})
            item_type = item.get("type", "")
            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    yield StreamEvent(
                        type=StreamEventType.TEXT_COMPLETE, text=text,
                    )
            elif item_type == "reasoning":
                text = item.get("text", "")
                if text:
                    yield StreamEvent(
                        type=StreamEventType.THINKING, text=text,
                    )
            elif item_type == "command_execution":
                exit_code = item.get("exit_code")
                output = item.get("aggregated_output", "")
                if exit_code and exit_code != 0 and output:
                    # Surface failed command output as a warning
                    log.warning(
                        "[Codex] Command failed (exit %d): %s",
                        exit_code, output[:200],
                    )
            elif item_type == "todo_list":
                items = item.get("items", [])
                if items:
                    lines = []
                    for t in items:
                        check = "x" if t.get("completed") else " "
                        lines.append(f"  [{check}] {t.get('text', '')}")
                    yield StreamEvent(
                        type=StreamEventType.TEXT_DELTA,
                        text="\n".join(lines) + "\n",
                    )

        elif event_type == "turn.completed":
            usage = data.get("usage")
            if usage:
                yield StreamEvent(
                    type=StreamEventType.USAGE, usage=usage,
                )

        elif event_type == "turn.failed":
            error = data.get("error", {})
            message = error.get("message") if isinstance(error, dict) else str(error)
            if not message:
                message = data.get("message", "Turn failed")
            log.error("[Codex] Turn failed: %s", message)
            yield StreamEvent(
                type=StreamEventType.ERROR, error=str(message),
            )

        elif event_type == "error":
            message = data.get("message", "Unknown error")
            log.error("[Codex] Error: %s", message)
            yield StreamEvent(
                type=StreamEventType.ERROR, error=message,
            )
