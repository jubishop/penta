from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from .agent_status import AgentStatus
from .agent_type import AgentType


@dataclass
class AgentConfig:
    name: str
    type: AgentType
    id: UUID = field(default_factory=uuid4)
    status: AgentStatus = AgentStatus.IDLE

    @property
    def mention_handle(self) -> str:
        return f"@{self.name}"
