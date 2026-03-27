from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncIterator

from penta.models import AgentType
from penta.services.agent_service import AgentService, StreamEvent, StreamEventType
from penta.services.stream_parser import async_lines

log = logging.getLogger(__name__)


class GeminiService(AgentService):
    """Gemini CLI agent — spawn-per-turn with stream-json, --yolo for permissions."""

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable or AgentType.GEMINI.find_executable()
        self._current_process: asyncio.subprocess.Process | None = None

    async def send(
        self, prompt: str, session_id: str | None, working_dir: Path
    ) -> AsyncIterator[StreamEvent]:
        await self.cancel()

        if not self._executable:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error="Gemini CLI not found. Set PENTA_GEMINI_PATH or install gemini.",
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return

        args = self._build_args(prompt, session_id)
        env = self._build_env()

        log.info("[Gemini] Launching: %s %s", self._executable, " ".join(args))
        log.info("[Gemini] cwd: %s", working_dir)

        proc = await asyncio.create_subprocess_exec(
            self._executable,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=env,
        )
        self._current_process = proc

        stderr_task = asyncio.create_task(proc.stderr.read())

        captured_session_id: str | None = None
        received_text = False

        async for line in async_lines(proc.stdout):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            if msg_type == "init":
                sid = data.get("session_id")
                if sid:
                    captured_session_id = sid
                    log.info("[Gemini] Session started: %s", sid)
                    yield StreamEvent(
                        type=StreamEventType.SESSION_STARTED,
                        session_id=sid,
                    )

            elif msg_type == "message":
                role = data.get("role")
                if role == "user":
                    continue  # Skip user echo
                if role == "assistant" and data.get("delta"):
                    text = data.get("content", "")
                    if text:
                        received_text = True
                        yield StreamEvent(
                            type=StreamEventType.TEXT_DELTA, text=text
                        )

            elif msg_type == "tool_use":
                tool_name = data.get("tool_name", "")
                tool_id = data.get("tool_id", "")
                yield StreamEvent(
                    type=StreamEventType.TOOL_USE_STARTED,
                    tool_id=tool_id,
                    tool_name=tool_name,
                )

            elif msg_type == "tool_result":
                # Tool results are handled by Gemini internally (--yolo)
                pass

            elif msg_type == "result":
                status = data.get("status", "")
                if status != "success":
                    error_msg = data.get("error", "Gemini turn failed")
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get("message", str(error_msg))
                    yield StreamEvent(
                        type=StreamEventType.ERROR, error=str(error_msg)
                    )

        log.info("[Gemini] stdout stream ended")

        stderr_data = await stderr_task
        returncode = await proc.wait()
        self._current_process = None

        if returncode != 0 and stderr_data:
            stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
            # Gemini dumps MCP noise to stderr — only report real errors
            if stderr_text and "error" in stderr_text.lower():
                log.error("[Gemini] stderr: %s", stderr_text)
                yield StreamEvent(
                    type=StreamEventType.ERROR, error=stderr_text
                )

        yield StreamEvent(type=StreamEventType.DONE)

    async def respond_to_permission(
        self, request_id: str, granted: bool
    ) -> None:
        # Gemini runs with --yolo, no permission flow
        pass

    async def cancel(self) -> None:
        proc = self._current_process
        self._current_process = None
        if proc and proc.returncode is None:
            log.info("[Gemini] Cancelling process pid=%d", proc.pid)
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

    async def shutdown(self) -> None:
        await self.cancel()

    def _build_args(self, prompt: str, session_id: str | None) -> list[str]:
        args = [
            "--output-format", "stream-json",
            "--yolo",
        ]

        if session_id:
            args += ["--resume", session_id]

        args += ["-p", prompt]
        return args

    def _build_env(self) -> dict[str, str] | None:
        import os

        env = os.environ.copy()
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
