from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Shared tag format — used by coordinator prompts and response parsing.
GROUP_TAG_RE = re.compile(r"^\[Group - [^\]]+\]:\s*")


def group_tag_prefix(name: str) -> str:
    """Return the ``[Group - <name>]:`` prefix for *name*."""
    return f"[Group - {name}]:"


@dataclass(frozen=True)
class TaggedMessage:
    sender_label: str
    text: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def formatted(self) -> str:
        return f"{group_tag_prefix(self.sender_label)} {self.text}"
