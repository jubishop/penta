from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID


class MessageSenderKind(Enum):
    USER = "user"
    AGENT = "agent"


@dataclass(frozen=True)
class MessageSender:
    kind: MessageSenderKind
    agent_id: UUID | None = None

    @classmethod
    def user(cls) -> MessageSender:
        return cls(kind=MessageSenderKind.USER)

    @classmethod
    def agent(cls, agent_id: UUID) -> MessageSender:
        return cls(kind=MessageSenderKind.AGENT, agent_id=agent_id)

    @property
    def is_user(self) -> bool:
        return self.kind == MessageSenderKind.USER

    @property
    def is_agent(self) -> bool:
        return self.kind == MessageSenderKind.AGENT
