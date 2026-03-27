from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import AsyncIterator


class StreamEventType(Enum):
    SESSION_STARTED = auto()
    TEXT_DELTA = auto()
    TEXT_COMPLETE = auto()
    TOOL_USE_STARTED = auto()
    PERMISSION_REQUESTED = auto()
    ERROR = auto()
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


class AgentService(ABC):
    @abstractmethod
    async def send(
        self, prompt: str, session_id: str | None, working_dir: Path
    ) -> AsyncIterator[StreamEvent]: ...

    @abstractmethod
    async def respond_to_permission(
        self, request_id: str, granted: bool
    ) -> None: ...

    @abstractmethod
    async def cancel(self) -> None: ...

    @abstractmethod
    async def shutdown(self) -> None: ...
