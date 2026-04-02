from uuid import uuid4

from penta.input_parser import (
    extract_mentions,
    has_agent_mention,
    has_broadcast_mention,
    strip_code_blocks,
)
from penta.models import AgentConfig, AgentType


def _agents() -> list[AgentConfig]:
    return [
        AgentConfig(name="claude", type=AgentType.CLAUDE, id=uuid4()),
        AgentConfig(name="codex", type=AgentType.CODEX, id=uuid4()),
    ]


class TestExtractMentions:
    def test_at_prefix_required(self):
        agents = _agents()
        assert extract_mentions("@claude hi", agents) == {agents[0].id}
        assert extract_mentions("@codex help", agents) == {agents[1].id}

    def test_case_insensitive(self):
        agents = _agents()
        assert extract_mentions("@claude hi", agents) == {agents[0].id}
        assert extract_mentions("@CLAUDE hi", agents) == {agents[0].id}
        assert extract_mentions("@Claude hi", agents) == {agents[0].id}

    def test_bare_name_does_not_match(self):
        agents = _agents()
        assert extract_mentions("claude explain this", agents) == set()
        assert extract_mentions("hey codex help", agents) == set()
        assert extract_mentions("claude and codex debate this", agents) == set()

    def test_at_all(self):
        agents = _agents()
        result = extract_mentions("@all what do you think?", agents)
        assert result == {agents[0].id, agents[1].id}

    def test_at_everyone(self):
        agents = _agents()
        result = extract_mentions("@everyone hello", agents)
        assert result == {agents[0].id, agents[1].id}

    def test_bare_all_does_not_match(self):
        agents = _agents()
        assert extract_mentions("all", agents) == set()

    def test_no_mention(self):
        agents = _agents()
        assert extract_mentions("just a normal message", agents) == set()

    def test_mention_in_middle(self):
        agents = _agents()
        result = extract_mentions("hey @codex can you help?", agents)
        assert result == {agents[1].id}

    def test_both_at_mentions(self):
        agents = _agents()
        result = extract_mentions("@claude and @codex debate this", agents)
        assert result == {agents[0].id, agents[1].id}

    def test_empty_agents(self):
        assert extract_mentions("@claude hi", []) == set()

    def test_no_match_without_at(self):
        agents = _agents()
        assert extract_mentions("recodex is great", agents) == set()
        assert extract_mentions("src/penta/services/codex_service.py", agents) == set()
        assert extract_mentions("claudette is here", agents) == set()

    def test_no_match_email_like(self):
        agents = _agents()
        assert extract_mentions("user@claude.com", agents) == set()


class TestStripCodeBlocks:
    def test_strips_fenced_block(self):
        assert "@claude" not in strip_code_blocks("before ```@claude``` after")

    def test_strips_inline_code(self):
        assert "@claude" not in strip_code_blocks("before `@claude` after")

    def test_preserves_text_outside_code(self):
        assert "@claude" in strip_code_blocks("@claude `other`")


class TestHasBroadcastMention:
    def test_at_all(self):
        assert has_broadcast_mention("@all hello")

    def test_at_everyone(self):
        assert has_broadcast_mention("@everyone hello")

    def test_case_insensitive(self):
        assert has_broadcast_mention("@ALL hello")

    def test_no_broadcast(self):
        assert not has_broadcast_mention("hello @claude")

    def test_broadcast_in_code_block_ignored(self):
        assert not has_broadcast_mention("`@all` hello")


class TestHasAgentMention:
    def test_finds_mention(self):
        assert has_agent_mention("@claude hi", "claude")

    def test_case_insensitive(self):
        assert has_agent_mention("@CLAUDE hi", "claude")

    def test_no_mention(self):
        assert not has_agent_mention("hello", "claude")

    def test_mention_in_code_block_ignored(self):
        assert not has_agent_mention("`@claude` hello", "claude")

    def test_bare_name_not_matched(self):
        assert not has_agent_mention("claude hello", "claude")
