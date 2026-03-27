from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID


@dataclass
class PermissionRequest:
    id: str
    agent_id: UUID
    tool_name: str
    tool_input: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
