from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class TaggedMessage:
    sender_label: str
    text: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def formatted(self) -> str:
        return f"[Group - {self.sender_label}]: {self.text}"
