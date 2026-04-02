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

        ta = app.query_one("#input-text", TextArea)
        ta.insert("hello")
        app.query_one(InputBar)._submit()
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

        ta = app.query_one("#input-text", TextArea)
        ta.insert("hello")
        app.query_one(InputBar)._submit()
        await pilot.pause()

    assert len(app.submitted) == 1
    assert app.submitted[0] == "@claude @codex hello"


async def test_no_prepend_when_no_toggles_active():
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        ta = app.query_one("#input-text", TextArea)
        ta.insert("hello")
        app.query_one(InputBar)._submit()
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

        ta = app.query_one("#input-text", TextArea)
        ta.insert("@claude explain this")
        app.query_one(InputBar)._submit()
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

        ta = app.query_one("#input-text", TextArea)
        ta.insert("@CLAUDE explain this")
        app.query_one(InputBar)._submit()
        await pilot.pause()

    assert len(app.submitted) == 1
    assert app.submitted[0] == "@CLAUDE explain this"


async def test_prepend_skips_mention_in_code_block():
    """A mention inside a code block shouldn't prevent prepending."""
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        await pilot.click(toggles[0])

        ta = app.query_one("#input-text", TextArea)
        ta.insert("check this `@claude` snippet")
        app.query_one(InputBar)._submit()
        await pilot.pause()

    assert len(app.submitted) == 1
    assert app.submitted[0] == "@claude check this `@claude` snippet"


async def test_partial_prepend_when_one_already_mentioned():
    """Only missing agents get prepended."""
    agents = _agents()
    app = _TestApp(agents)

    async with app.run_test() as pilot:
        await pilot.pause()
        toggles = app.query_one(InputBar).query(AgentToggle)
        await pilot.click(toggles[0])
        await pilot.click(toggles[1])

        ta = app.query_one("#input-text", TextArea)
        ta.insert("@claude explain this")
        app.query_one(InputBar)._submit()
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

        ta = app.query_one("#input-text", TextArea)
        ta.insert("first message")
        app.query_one(InputBar)._submit()
        await pilot.pause()

        ta.insert("second message")
        app.query_one(InputBar)._submit()
        await pilot.pause()

    assert len(app.submitted) == 2
    assert app.submitted[0] == "@claude first message"
    assert app.submitted[1] == "@claude second message"
