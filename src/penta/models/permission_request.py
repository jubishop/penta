from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID


class PermissionStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


@dataclass
class PermissionRequest:
    id: str
    agent_id: UUID
    tool_name: str
    tool_input: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: PermissionStatus = PermissionStatus.PENDING
