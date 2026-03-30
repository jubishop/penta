from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass
class PendingPlan:
    agent_id: UUID
    agent_name: str
    tool_use_id: str
    plan_text: str
    timestamp: datetime = field(default_factory=datetime.now)
