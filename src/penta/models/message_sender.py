from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID


RESERVED_SENDER_NAMES = frozenset({"user", "shell", "system"})

EXTERNAL_SUFFIX = " (external)"


def sanitize_external_name(
    name: str, agent_names: frozenset[str],
) -> str:
    """Ensure an external sender name doesn't collide with built-in names.

    Idempotent — already-suffixed names are returned unchanged.
    """
    if name.endswith(EXTERNAL_SUFFIX):
        return name
    if name.lower() in RESERVED_SENDER_NAMES | agent_names:
        return f"{name}{EXTERNAL_SUFFIX}"
    return name


class MessageSenderKind(Enum):
    USER = "user"
    AGENT = "agent"
    EXTERNAL = "external"


@dataclass(frozen=True)
class MessageSender:
    kind: MessageSenderKind
    agent_id: UUID | None = None
    name: str | None = None

    @classmethod
    def user(cls) -> MessageSender:
        return cls(kind=MessageSenderKind.USER)

    @classmethod
    def agent(cls, agent_id: UUID) -> MessageSender:
        return cls(kind=MessageSenderKind.AGENT, agent_id=agent_id)

    @classmethod
    def external(cls, name: str) -> MessageSender:
        return cls(kind=MessageSenderKind.EXTERNAL, name=name)

    @property
    def is_user(self) -> bool:
        return self.kind == MessageSenderKind.USER

    @property
    def is_agent(self) -> bool:
        return self.kind == MessageSenderKind.AGENT

    @property
    def is_external(self) -> bool:
        return self.kind == MessageSenderKind.EXTERNAL
