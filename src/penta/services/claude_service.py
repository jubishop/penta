from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncIterator

from penta.models import AgentType
from penta.services.agent_service import AgentService, StreamEvent, StreamEventType
from penta.services.permission_server import PermissionServer
from penta.services.stream_parser import async_lines

log = logging.getLogger(__name__)


class ClaudeService(AgentService):
    def __init__(
        self,
        executable: str | None = None,
        permission_server: PermissionServer | None = None,
    ) -> None:
        self._executable = executable or AgentType.CLAUDE.find_executable()
        self._permission_server = permission_server
        self._current_process: asyncio.subprocess.Process | None = None

    async def send(
        self, prompt: str, session_id: str | None, working_dir: Path
    ) -> AsyncIterator[StreamEvent]:
        await self.cancel()

        if not self._executable:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error="Claude CLI not found. Set PENTA_CLAUDE_PATH or install claude.",
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return

        args = self._build_args(prompt, session_id)
        env = self._build_env()

        log.info("[Claude] Launching: %s %s", self._executable, " ".join(args))
        log.info("[Claude] cwd: %s", working_dir)

        proc = await asyncio.create_subprocess_exec(
            self._executable,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=env,
        )
        self._current_process = proc

        # Read stderr concurrently so it doesn't block
        stderr_task = asyncio.create_task(proc.stderr.read())

        captured_session_id: str | None = None
        full_text = ""
        received_text = False

        async for line in async_lines(proc.stdout):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            if msg_type == "system":
                if data.get("subtype") == "init":
                    sid = data.get("session_id")
                    if sid:
                        captured_session_id = sid
                        log.info("[Claude] Session started: %s", sid)
                        yield StreamEvent(
                            type=StreamEventType.SESSION_STARTED,
                            session_id=sid,
                        )

            elif msg_type == "stream_event":
                event = data.get("event", {})
                event_type = event.get("type")

                if event_type == "content_block_start":
                    content_block = event.get("content_block", {})
                    if content_block.get("type") == "tool_use":
                        tool_id = content_block.get("id", "")
                        tool_name = content_block.get("name", "")
                        yield StreamEvent(
                            type=StreamEventType.TOOL_USE_STARTED,
                            tool_id=tool_id,
                            tool_name=tool_name,
                        )
                    if full_text:
                        full_text += "\n\n"
                        yield StreamEvent(
                            type=StreamEventType.TEXT_DELTA, text="\n\n"
                        )

                elif event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            full_text += text
                            received_text = True
                            yield StreamEvent(
                                type=StreamEventType.TEXT_DELTA, text=text
                            )

            elif msg_type == "result":
                result_text = data.get("result", "")
                if data.get("is_error"):
                    log.error("[Claude] API error: %s", result_text)
                    yield StreamEvent(
                        type=StreamEventType.ERROR, error=result_text
                    )
                elif result_text:
                    log.info("[Claude] Result received, len=%d", len(result_text))
                    yield StreamEvent(
                        type=StreamEventType.TEXT_COMPLETE, text=result_text
                    )

                sid = data.get("session_id")
                if sid and not captured_session_id:
                    captured_session_id = sid
                    yield StreamEvent(
                        type=StreamEventType.SESSION_STARTED,
                        session_id=sid,
                    )

        log.info("[Claude] stdout stream ended")

        # Wait for stderr and process exit
        stderr_data = await stderr_task
        returncode = await proc.wait()
        self._current_process = None

        if returncode != 0 and stderr_data:
            stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
            if stderr_text:
                log.error("[Claude] stderr: %s", stderr_text)
                yield StreamEvent(
                    type=StreamEventType.ERROR, error=stderr_text
                )

        yield StreamEvent(type=StreamEventType.DONE)

    async def respond_to_permission(
        self, request_id: str, granted: bool
    ) -> None:
        # Claude permissions go through HTTP hooks, not process stdin.
        # The PermissionServer handles this directly.
        pass

    async def cancel(self) -> None:
        proc = self._current_process
        self._current_process = None
        if proc and proc.returncode is None:
            log.info("[Claude] Cancelling process pid=%d", proc.pid)
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

    async def shutdown(self) -> None:
        await self.cancel()

    def _build_args(self, prompt: str, session_id: str | None) -> list[str]:
        args = [
            "-p",
            "--verbose",
            "--output-format", "stream-json",
        ]

        if self._permission_server:
            args += ["--settings", self._permission_server.hook_settings_json]

        if session_id:
            args += ["--resume", session_id]

        args.append(prompt)
        return args

    def _build_env(self) -> dict[str, str] | None:
        import os

        env = os.environ.copy()
        # Ensure common install locations are on PATH
        extra_paths = [
            str(Path.home() / ".local" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
        ]
        existing = env.get("PATH", "")
        for p in extra_paths:
            if p not in existing:
                existing = f"{p}:{existing}"
        env["PATH"] = existing
        return env
