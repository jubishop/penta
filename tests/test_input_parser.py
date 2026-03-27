from uuid import uuid4

from penta.input_parser import ParsedChat, extract_mentions, parse
from penta.models import AgentConfig, AgentType


def _agents() -> list[AgentConfig]:
    return [
        AgentConfig(name="Claude", type=AgentType.CLAUDE, id=uuid4()),
        AgentConfig(name="Codex", type=AgentType.CODEX, id=uuid4()),
    ]


class TestParse:
    def test_chat_no_mentions(self):
        agents = _agents()
        result = parse("hello world", agents)
        assert isinstance(result, ParsedChat)
        assert result.text == "hello world"
        assert result.mentioned_ids == set()

    def test_chat_with_mention(self):
        agents = _agents()
        result = parse("@Claude explain this", agents)
        assert isinstance(result, ParsedChat)
        assert result.mentioned_ids == {agents[0].id}

    def test_chat_multiple_mentions(self):
        agents = _agents()
        result = parse("@Claude @Codex debate this", agents)
        assert isinstance(result, ParsedChat)
        assert result.mentioned_ids == {agents[0].id, agents[1].id}

    def test_chat_strips_whitespace(self):
        result = parse("  hello  ", [])
        assert isinstance(result, ParsedChat)
        assert result.text == "hello"


class TestExtractMentions:
    def test_case_insensitive(self):
        agents = _agents()
        assert extract_mentions("claude hi", agents) == {agents[0].id}
        assert extract_mentions("CLAUDE hi", agents) == {agents[0].id}
        assert extract_mentions("Claude hi", agents) == {agents[0].id}

    def test_bare_name(self):
        agents = _agents()
        assert extract_mentions("claude explain this", agents) == {agents[0].id}
        assert extract_mentions("hey codex help", agents) == {agents[1].id}

    def test_at_prefix_still_works(self):
        agents = _agents()
        assert extract_mentions("@claude hi", agents) == {agents[0].id}
        assert extract_mentions("@Codex help", agents) == {agents[1].id}

    def test_at_all(self):
        agents = _agents()
        result = extract_mentions("@all what do you think?", agents)
        assert result == {agents[0].id, agents[1].id}

    def test_at_everyone(self):
        agents = _agents()
        result = extract_mentions("@everyone hello", agents)
        assert result == {agents[0].id, agents[1].id}

    def test_no_mention(self):
        agents = _agents()
        assert extract_mentions("just a normal message", agents) == set()

    def test_mention_in_middle(self):
        agents = _agents()
        result = extract_mentions("hey codex can you help?", agents)
        assert result == {agents[1].id}

    def test_both_bare_names(self):
        agents = _agents()
        result = extract_mentions("claude and codex debate this", agents)
        assert result == {agents[0].id, agents[1].id}

    def test_empty_agents(self):
        assert extract_mentions("claude hi", []) == set()

    def test_no_substring_match_in_compound_word(self):
        agents = _agents()
        assert extract_mentions("recodex is great", agents) == set()

    def test_no_substring_match_in_file_path(self):
        agents = _agents()
        assert extract_mentions("src/penta/services/codex_service.py", agents) == set()

    def test_no_substring_match_with_suffix(self):
        agents = _agents()
        assert extract_mentions("claudette is here", agents) == set()
