from enum import Enum


class AgentStatus(Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    PROCESSING = "processing"
    AWAITING_PERMISSION = "awaiting_permission"
    ERROR = "error"

    @property
    def is_busy(self) -> bool:
        return self in (AgentStatus.PROCESSING, AgentStatus.AWAITING_PERMISSION)
