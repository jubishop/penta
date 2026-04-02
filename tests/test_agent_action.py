"""Textual pilot tests for AgentActionScreen."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Static

from penta.widgets.agent_action import AgentAction, AgentActionScreen


class _TestApp(App):
    def compose(self) -> ComposeResult:
        yield Static("base")


async def test_escape_dismisses_with_none():
    app = _TestApp()
    results: list[AgentAction | None] = []

    async with app.run_test() as pilot:
        app.push_screen(
            AgentActionScreen("Claude"),
            callback=lambda r: results.append(r),
        )
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert results == [None]


async def test_cancel_button_dismisses_with_none():
    app = _TestApp()
    results: list[AgentAction | None] = []

    async with app.run_test() as pilot:
        modal = AgentActionScreen("Claude")
        app.push_screen(modal, callback=lambda r: results.append(r))
        await pilot.pause()

        cancel = modal.query_one("#cancel-btn")
        await pilot.click(cancel)
        await pilot.pause()

    assert results == [None]


async def test_approve_returns_approve():
    app = _TestApp()
    results: list[AgentAction | None] = []

    async with app.run_test() as pilot:
        modal = AgentActionScreen("Claude")
        app.push_screen(modal, callback=lambda r: results.append(r))
        await pilot.pause()

        btn = modal.query_one("#approve-btn")
        await pilot.click(btn)
        await pilot.pause()

    assert results == [AgentAction.APPROVE]


async def test_revise_returns_revise():
    app = _TestApp()
    results: list[AgentAction | None] = []

    async with app.run_test() as pilot:
        modal = AgentActionScreen("Claude")
        app.push_screen(modal, callback=lambda r: results.append(r))
        await pilot.pause()

        btn = modal.query_one("#revise-btn")
        await pilot.click(btn)
        await pilot.pause()

    assert results == [AgentAction.REVISE]


async def test_stop_returns_stop():
    app = _TestApp()
    results: list[AgentAction | None] = []

    async with app.run_test() as pilot:
        modal = AgentActionScreen("Claude")
        app.push_screen(modal, callback=lambda r: results.append(r))
        await pilot.pause()

        btn = modal.query_one("#stop-btn")
        await pilot.click(btn)
        await pilot.pause()

    assert results == [AgentAction.STOP]


async def test_title_shows_agent_name():
    app = _TestApp()
    async with app.run_test() as pilot:
        modal = AgentActionScreen("Codex")
        app.push_screen(modal)
        await pilot.pause()

        from textual.widgets import Label
        title = modal.query_one("#dialog-title", Label)
        assert "Codex" in str(title.render())
