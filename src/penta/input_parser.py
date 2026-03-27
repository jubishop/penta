from __future__ import annotations

import re
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

    if lower == "all" or re.search(r"@all\b", lower) or re.search(r"@everyone\b", lower):
        return {a.id for a in agents}

    mentioned: set[UUID] = set()
    for agent in agents:
        name_lower = agent.name.lower()
        # Word-boundary match to avoid false positives on substrings
        # like "recodex", "claudette", or file paths containing agent names.
        if re.search(rf"\b{re.escape(name_lower)}\b", lower):
            mentioned.add(agent.id)
    return mentioned
