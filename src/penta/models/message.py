from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from .message_sender import MessageSender


@dataclass
class Message:
    sender: MessageSender
    text: str
    id: UUID = field(default_factory=uuid4)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_streaming: bool = False
    is_error: bool = False
    is_cancelled: bool = False
    thinking_text: str = ""
    _done: asyncio.Event | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.is_streaming:
            self._ensure_done().set()

    def _ensure_done(self) -> asyncio.Event:
        if self._done is None:
            self._done = asyncio.Event()
        return self._done

    async def wait_for_completion(self) -> None:
        await self._ensure_done().wait()

    def mark_complete(self) -> None:
        self.is_streaming = False
        self._ensure_done().set()
