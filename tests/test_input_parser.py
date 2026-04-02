from uuid import uuid4

from penta.input_parser import extract_mentions
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
