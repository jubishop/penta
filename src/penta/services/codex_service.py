from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncIterator

from penta.models import AgentType
from penta.services.agent_service import AgentService, StreamEvent, StreamEventType, terminate_process
from penta.services.cli_env import build_cli_env
from penta.services.stream_parser import async_lines

log = logging.getLogger(__name__)


class CodexService(AgentService):
    """Codex CLI agent — spawn-per-turn with JSON Lines output."""

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable or AgentType.CODEX.find_executable()
        self._current_process: asyncio.subprocess.Process | None = None

    async def send(
        self, prompt: str, session_id: str | None, working_dir: Path
    ) -> AsyncIterator[StreamEvent]:
        await self.cancel()

        if not self._executable:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error="Codex CLI not found. Set PENTA_CODEX_PATH or install codex.",
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return

        args = self._build_args(prompt, session_id)

        log.info("[Codex] Launching: %s %s", self._executable, " ".join(args))
        log.info("[Codex] cwd: %s", working_dir)

        proc = await asyncio.create_subprocess_exec(
            self._executable,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=build_cli_env(),
        )
        self._current_process = proc

        # Read stderr concurrently so it doesn't block
        stderr_task = asyncio.create_task(proc.stderr.read())

        async for line in async_lines(proc.stdout):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = data.get("type", "")

            if event_type == "thread.started":
                thread_id = data.get("thread_id", "")
                if thread_id:
                    log.info("[Codex] Session started: %s", thread_id)
                    yield StreamEvent(
                        type=StreamEventType.SESSION_STARTED,
                        session_id=thread_id,
                    )

            elif event_type == "item.started":
                item = data.get("item", {})
                item_type = item.get("type", "")
                if item_type == "command_execution":
                    command = item.get("command", "")
                    tool_id = item.get("id", "")
                    yield StreamEvent(
                        type=StreamEventType.TOOL_USE_STARTED,
                        tool_id=tool_id,
                        tool_name=command,
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

            elif event_type == "error":
                message = data.get("message", "Unknown error")
                log.error("[Codex] Error: %s", message)
                yield StreamEvent(
                    type=StreamEventType.ERROR, error=message,
                )

        log.info("[Codex] stdout stream ended")

        # Wait for stderr and process exit
        stderr_data = await stderr_task
        returncode = await proc.wait()
        self._current_process = None

        if returncode != 0 and stderr_data:
            stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
            if stderr_text:
                log.error("[Codex] stderr: %s", stderr_text)
                yield StreamEvent(
                    type=StreamEventType.ERROR, error=stderr_text,
                )

        yield StreamEvent(type=StreamEventType.DONE)

    async def respond_to_permission(
        self, request_id: str, granted: bool
    ) -> None:
        # Codex runs with --full-auto, no permission flow
        pass

    async def cancel(self) -> None:
        proc = self._current_process
        self._current_process = None
        if proc:
            log.info("[Codex] Cancelling process pid=%d", proc.pid)
            await terminate_process(proc)

    async def shutdown(self) -> None:
        await self.cancel()

    def _build_args(self, prompt: str, session_id: str | None) -> list[str]:
        if session_id:
            return [
                "exec", "resume", session_id,
                "--json", "--full-auto",
                prompt,
            ]
        return [
            "exec",
            "--json", "--full-auto",
            prompt,
        ]
