"""Tests for InputBar agent toggle pills and auto-prepend behavior."""

from __future__ import annotations

from uuid import uuid4

from textual.app import App, ComposeResult
from textual.widgets import TextArea

from penta.models.agent_config import AgentConfig
from penta.models.agent_type import AgentType
from penta.widgets.input_bar import AgentToggle, InputBar


def _agents() -> list[AgentConfig]:
    return [
        AgentConfig(name="claude", type=AgentType.CLAUDE, id=uuid4()),
        AgentConfig(name="codex", type=AgentType.CODEX, id=uuid4()),
    ]


class _TestApp(App):
    """Minimal app that hosts an InputBar for pilot testing."""

    def __init__(self, agents: list[AgentConfig] | None = None) -> None:
        super().__init__()
        self._agents = agents or []
        self.submitted: list[str] = []

    def compose(self) -> ComposeResult:
        yield InputBar()

    def on_mount(self) -> None:
        if self._agents:
            self.query_one(InputBar).set_agents(self._agents)

    def on_input_bar_submitted(self, event: InputBar.Submitted) -> None:
        self.submitted.append(event.text)


def _type_and_submit(app: App, text: str) -> None:
    """Insert text into the input and submit via the public action."""
    ta = app.query_one("#input-text", TextArea)
    ta.insert(text)
    app.query_one(InputBar).action_submit()


# -- Toggle rendering and state --


async def test_set_agents_creates_toggles():
    agents = _agents()
    app = _TestApp(agents)
    async with app.run_test() as pilot:
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        assert len(toggles) == 2
        assert toggles[0].agent_name == "claude"
        assert toggles[1].agent_name == "codex"


async def test_set_agents_is_idempotent():
    agents = _agents()
    app = _TestApp(agents)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Call set_agents again — should replace, not duplicate
        app.query_one(InputBar).set_agents(agents)
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        assert len(toggles) == 2


async def test_toggle_click_activates():
    agents = _agents()
    app = _TestApp(agents)
    async with app.run_test() as pilot:
        await pilot.pause()
        toggle = app.query_one(InputBar).query(AgentToggle)[0]
        assert not toggle.active
        await pilot.click(toggle)
        assert toggle.active


async def test_toggle_click_deactivates():
    agents = _agents()
    app = _TestApp(agents)
    async with app.run_test() as pilot:
        await pilot.pause()
        toggle = app.query_one(InputBar).query(AgentToggle)[0]
        await pilot.click(toggle)
        assert toggle.active
        await pilot.click(toggle)
        assert not toggle.active


async def test_toggles_are_independent():
    agents = _agents()
    app = _TestApp(agents)
    async with app.run_test() as pilot:
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        await pilot.click(toggles[0])
        assert toggles[0].active
        assert not toggles[1].active


# -- Auto-prepend behavior --


async def test_prepend_single_active_toggle():
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        await pilot.click(toggles[0])

        _type_and_submit(app, "hello")
        await pilot.pause()

    assert len(app.submitted) == 1
    assert app.submitted[0] == "@claude hello"


async def test_prepend_multiple_active_toggles():
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        await pilot.click(toggles[0])
        await pilot.click(toggles[1])

        _type_and_submit(app, "hello")
        await pilot.pause()

    assert len(app.submitted) == 1
    assert app.submitted[0] == "@claude @codex hello"


async def test_no_prepend_when_no_toggles_active():
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        _type_and_submit(app, "hello")
        await pilot.pause()

    assert len(app.submitted) == 1
    assert app.submitted[0] == "hello"


async def test_no_double_prepend_when_already_mentioned():
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        await pilot.click(toggles[0])

        _type_and_submit(app, "@claude explain this")
        await pilot.pause()

    assert len(app.submitted) == 1
    assert app.submitted[0] == "@claude explain this"


async def test_no_double_prepend_case_insensitive():
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        await pilot.click(toggles[0])

        _type_and_submit(app, "@CLAUDE explain this")
        await pilot.pause()

    assert len(app.submitted) == 1
    assert app.submitted[0] == "@CLAUDE explain this"


async def test_prepend_skips_mention_in_inline_code():
    """A mention inside inline code shouldn't prevent prepending."""
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        await pilot.click(toggles[0])

        _type_and_submit(app, "check this `@claude` snippet")
        await pilot.pause()

    assert len(app.submitted) == 1
    assert app.submitted[0] == "@claude check this `@claude` snippet"


async def test_prepend_skips_mention_in_fenced_code_block():
    """A mention inside a fenced code block shouldn't prevent prepending."""
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        await pilot.click(toggles[0])

        _type_and_submit(app, "review this\n```\n@claude do stuff\n```")
        await pilot.pause()

    assert len(app.submitted) == 1
    assert app.submitted[0] == "@claude review this\n```\n@claude do stuff\n```"


async def test_partial_prepend_when_one_already_mentioned():
    """Only missing agents get prepended."""
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        await pilot.click(toggles[0])
        await pilot.click(toggles[1])

        _type_and_submit(app, "@claude explain this")
        await pilot.pause()

    assert len(app.submitted) == 1
    assert app.submitted[0] == "@codex @claude explain this"


async def test_toggle_state_persists_across_submits():
    """Toggle stays active after submitting — sticky state."""
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        await pilot.click(toggles[0])

        _type_and_submit(app, "first message")
        await pilot.pause()

        _type_and_submit(app, "second message")
        await pilot.pause()

    assert len(app.submitted) == 2
    assert app.submitted[0] == "@claude first message"
    assert app.submitted[1] == "@claude second message"


# -- Slash command guard --


async def test_no_prepend_for_slash_commands():
    """Slash commands like /approve should never get mentions prepended."""
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        await pilot.click(toggles[0])

        _type_and_submit(app, "/approve Claude")
        await pilot.pause()

    assert len(app.submitted) == 1
    assert app.submitted[0] == "/approve Claude"


async def test_no_prepend_for_revise_command():
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        await pilot.click(toggles[0])

        _type_and_submit(app, "/revise Claude make it better")
        await pilot.pause()

    assert len(app.submitted) == 1
    assert app.submitted[0] == "/revise Claude make it better"


# -- Save / restore toggle state --


async def test_save_and_restore_toggle_state():
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        toggles = input_bar.query(AgentToggle)
        await pilot.click(toggles[0])

        saved = input_bar.save_toggle_state()
        assert saved == {"claude"}

        # Deactivate, then restore
        await pilot.click(toggles[0])
        assert not toggles[0].active

        input_bar.restore_toggle_state(saved)
        assert toggles[0].active
        assert not toggles[1].active


async def test_restore_empty_state_clears_toggles():
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        toggles = input_bar.query(AgentToggle)
        await pilot.click(toggles[0])
        await pilot.click(toggles[1])

        input_bar.restore_toggle_state(set())
        assert not toggles[0].active
        assert not toggles[1].active
