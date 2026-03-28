from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from penta.models import AgentConfig


@dataclass(frozen=True)
class ParsedChat:
    text: str
    mentioned_ids: set[UUID]


def parse(raw: str, agents: list[AgentConfig]) -> ParsedChat:
    trimmed = raw.strip()
    mentioned = extract_mentions(trimmed, agents)
    return ParsedChat(text=trimmed, mentioned_ids=mentioned)


def extract_mentions(text: str, agents: list[AgentConfig]) -> set[UUID]:
    lower = text.lower()

    if re.search(r"(?<!\w)@all\b", lower) or re.search(r"(?<!\w)@everyone\b", lower):
        return {a.id for a in agents}

    mentioned: set[UUID] = set()
    for agent in agents:
        name_lower = agent.name.lower()
        # Require explicit @mention — bare names don't trigger routing.
        # Negative lookbehind ensures we don't match inside emails like user@claude.com.
        if re.search(rf"(?<!\w)@{re.escape(name_lower)}\b", lower):
            mentioned.add(agent.id)
    return mentioned
