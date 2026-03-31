from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import AsyncIterator

from penta.services.cli_env import build_cli_env
from penta.services.stream_parser import async_lines

log = logging.getLogger(__name__)


async def terminate_process(
    proc: asyncio.subprocess.Process, timeout: float = 5,
) -> None:
    """Gracefully terminate a subprocess, falling back to kill."""
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()


class StreamEventType(Enum):
    SESSION_STARTED = auto()
    TEXT_DELTA = auto()
    TEXT_COMPLETE = auto()
    TOOL_USE_STARTED = auto()
    THINKING = auto()
    WARNING = auto()
    ERROR = auto()
    USAGE = auto()
    DONE = auto()


@dataclass
class StreamEvent:
    type: StreamEventType
    session_id: str | None = None
    text: str | None = None
    tool_id: str | None = None
    tool_name: str | None = None
    tool_input: str | None = None
    request_id: str | None = None
    error: str | None = None
    usage: dict | None = None


class AgentService(ABC):
    """Pure interface — test doubles inherit from this directly.

    Claude uses ``--output-format stream-json`` with HTTP hook-based
    permission handling for plan review and question answering.
    Codex uses ``-a never`` to auto-approve at the CLI level.
    """

    @abstractmethod
    async def send(
        self,
        prompt: str,
        session_id: str | None,
        working_dir: Path,
        system_prompt: str | None = None,
    ) -> AsyncIterator[StreamEvent]: ...

    @abstractmethod
    async def cancel(self) -> None: ...

    @abstractmethod
    async def shutdown(self) -> None: ...

class CliAgentService(AgentService):
    """Shared subprocess lifecycle for CLI-based agents.

    Subclasses only need to implement ``_build_args`` and ``_parse_line``.
    """

    def __init__(
        self,
        agent_name: str,
        executable: str | None,
        model: str | None = None,
    ) -> None:
        self._executable = executable
        self._agent_name = agent_name
        self._model = model
        self._current_process: asyncio.subprocess.Process | None = None
        self._streaming = False

    # -- Abstract: subclasses must implement ---------------------------------

    @abstractmethod
    def _build_args(
        self,
        prompt: str,
        session_id: str | None,
        system_prompt: str | None,
    ) -> list[str]: ...

    @abstractmethod
    async def _parse_line(self, data: dict) -> AsyncIterator[StreamEvent]: ...

    # -- Overridable hooks ---------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        return build_cli_env()

    def _should_report_stderr(self, stderr_text: str, returncode: int) -> bool:
        return returncode != 0 and bool(stderr_text)

    def _reset_parse_state(self) -> None:
        """Reset per-send parser state. Subclasses override if stateful."""

    def _effective_prompt(self, prompt: str, system_prompt: str | None) -> str:
        """Prepend system prompt for CLIs without a dedicated system-prompt flag."""
        return f"{system_prompt}\n\n{prompt}" if system_prompt else prompt

    # -- Shared lifecycle ----------------------------------------------------

    async def send(
        self,
        prompt: str,
        session_id: str | None,
        working_dir: Path,
        system_prompt: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        await self.cancel()
        if self._streaming:
            raise RuntimeError(
                f"{self._agent_name}: send() called while already streaming"
            )
        self._streaming = True
        self._reset_parse_state()

        if not self._executable:
            self._streaming = False
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error=f"{self._agent_name} CLI not found. "
                      f"Set PENTA_{self._agent_name.upper()}_PATH or install {self._agent_name.lower()}.",
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return

        args = self._build_args(prompt, session_id, system_prompt)
        env = self._build_env()

        log.info("[%s] Launching: %s %s", self._agent_name, self._executable, " ".join(args))
        log.info("[%s] cwd: %s", self._agent_name, working_dir)

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

        _completed = False
        try:
            async for line in async_lines(proc.stdout):
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                log.debug("[%s] raw (len=%d): %s", self._agent_name, len(line), line[:2000])
                async for event in self._parse_line(data):
                    yield event
            _completed = True
        finally:
            if not _completed:
                # Caller abandoned iteration (CancelledError / GeneratorExit)
                # — clean up the subprocess so it doesn't leak.
                if proc.returncode is None:
                    await terminate_process(proc)
                stderr_task.cancel()
                self._current_process = None
                self._streaming = False

        # Normal completion — wait for stderr and process exit.
        log.info("[%s] stdout stream ended", self._agent_name)

        stderr_data = await stderr_task
        returncode = await proc.wait()
        self._current_process = None

        if stderr_data:
            stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
            if self._should_report_stderr(stderr_text, returncode):
                log.error("[%s] stderr: %s", self._agent_name, stderr_text)
                yield StreamEvent(type=StreamEventType.ERROR, error=stderr_text)

        self._streaming = False
        yield StreamEvent(type=StreamEventType.DONE)

    async def cancel(self) -> None:
        proc = self._current_process
        self._current_process = None
        if proc:
            log.info("[%s] Cancelling process pid=%d", self._agent_name, proc.pid)
            await terminate_process(proc)

    async def shutdown(self) -> None:
        await self.cancel()
