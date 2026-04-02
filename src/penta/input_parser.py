from __future__ import annotations

import re
from uuid import UUID

from penta.models import AgentConfig

_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`]+`")
_BROADCAST_RE = re.compile(r"(?<!\w)@(?:all|everyone)\b")


def strip_code_blocks(text: str) -> str:
    """Remove fenced and inline code blocks from text."""
    stripped = _CODE_BLOCK_RE.sub("", text)
    return _INLINE_CODE_RE.sub("", stripped)


def has_broadcast_mention(text: str) -> bool:
    """Return True if text contains @all or @everyone outside code blocks."""
    return bool(_BROADCAST_RE.search(strip_code_blocks(text.lower())))


def has_agent_mention(text: str, agent_name: str) -> bool:
    """Return True if text contains @agent_name outside code blocks."""
    pattern = rf"(?<!\w)@{re.escape(agent_name.lower())}\b"
    return bool(re.search(pattern, strip_code_blocks(text.lower())))


def extract_mentions(text: str, agents: list[AgentConfig]) -> set[UUID]:
    stripped = strip_code_blocks(text.lower())

    if _BROADCAST_RE.search(stripped):
        return {a.id for a in agents}

    mentioned: set[UUID] = set()
    for agent in agents:
        name_lower = agent.name.lower()
        # Require explicit @mention — bare names don't trigger routing.
        # Negative lookbehind ensures we don't match inside emails like user@claude.com.
        if re.search(rf"(?<!\w)@{re.escape(name_lower)}\b", stripped):
            mentioned.add(agent.id)
    return mentioned
