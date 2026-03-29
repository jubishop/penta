from enum import Enum


class AgentStatus(Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    PROCESSING = "processing"
    ERROR = "error"

    @property
    def is_busy(self) -> bool:
        return self is AgentStatus.PROCESSING
