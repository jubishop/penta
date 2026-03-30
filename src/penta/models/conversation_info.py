from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class ConversationInfo:
    id: int
    title: str
    created_at: datetime
    updated_at: datetime
