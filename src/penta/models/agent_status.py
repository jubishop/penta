from enum import Enum


class AgentStatus(Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    PROCESSING = "processing"
    WAITING_FOR_USER = "waiting_for_user"
    ERROR = "error"

    @property
    def is_busy(self) -> bool:
        return self in (AgentStatus.PROCESSING, AgentStatus.WAITING_FOR_USER)
