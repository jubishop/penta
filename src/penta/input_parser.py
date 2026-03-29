from __future__ import annotations

import re
from uuid import UUID

from penta.models import AgentConfig


def extract_mentions(text: str, agents: list[AgentConfig]) -> set[UUID]:
    lower = text.lower()

    # Strip code blocks so mentions inside code aren't matched.
    stripped = re.sub(r"```[\s\S]*?```", "", lower)
    stripped = re.sub(r"`[^`]+`", "", stripped)

    if re.search(r"(?<!\w)@all\b", stripped) or re.search(r"(?<!\w)@everyone\b", stripped):
        return {a.id for a in agents}

    mentioned: set[UUID] = set()
    for agent in agents:
        name_lower = agent.name.lower()
        # Require explicit @mention — bare names don't trigger routing.
        # Negative lookbehind ensures we don't match inside emails like user@claude.com.
        if re.search(rf"(?<!\w)@{re.escape(name_lower)}\b", stripped):
            mentioned.add(agent.id)
    return mentioned
