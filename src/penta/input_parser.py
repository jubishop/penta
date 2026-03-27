from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from penta.models import AgentConfig


@dataclass(frozen=True)
class ParsedChat:
    text: str
    mentioned_ids: set[UUID]


@dataclass(frozen=True)
class ParsedShell:
    command: str


ParsedInput = ParsedChat | ParsedShell


def parse(raw: str, agents: list[AgentConfig]) -> ParsedInput:
    trimmed = raw.strip()

    if trimmed.startswith("$"):
        return ParsedShell(command=trimmed[1:].strip())

    mentioned = extract_mentions(trimmed, agents)
    return ParsedChat(text=trimmed, mentioned_ids=mentioned)


def extract_mentions(text: str, agents: list[AgentConfig]) -> set[UUID]:
    lower = text.lower()

    if "all" == lower or "@all" in lower or "@everyone" in lower:
        return {a.id for a in agents}

    mentioned: set[UUID] = set()
    for agent in agents:
        name_lower = agent.name.lower()
        # Match bare name ("claude") or @name ("@claude")
        if name_lower in lower or f"@{name_lower}" in lower:
            mentioned.add(agent.id)
    return mentioned
