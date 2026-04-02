"""Textual pilot tests for RoundLimitScreen."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from penta.widgets.round_limit_screen import RoundLimitScreen


class _TestApp(App):
    def compose(self) -> ComposeResult:
        yield Static("base")


async def test_escape_dismisses_with_none():
    app = _TestApp()
    results: list[int | None] = []

    async with app.run_test() as pilot:
        app.push_screen(
            RoundLimitScreen(3),
            callback=lambda r: results.append(r),
        )
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert results == [None]


async def test_cancel_button_dismisses_with_none():
    app = _TestApp()
    results: list[int | None] = []

    async with app.run_test() as pilot:
        modal = RoundLimitScreen(3)
        app.push_screen(modal, callback=lambda r: results.append(r))
        await pilot.pause()

        cancel = modal.query_one("#cancel-btn")
        await pilot.click(cancel)
        await pilot.pause()

    assert results == [None]


async def test_save_returns_entered_value():
    app = _TestApp()
    results: list[int | None] = []

    async with app.run_test() as pilot:
        modal = RoundLimitScreen(3)
        app.push_screen(modal, callback=lambda r: results.append(r))
        await pilot.pause()

        inp = modal.query_one("#limit-input", Input)
        inp.clear()
        inp.insert_text_at_cursor("5")

        save = modal.query_one("#save-btn")
        await pilot.click(save)
        await pilot.pause()

    assert results == [5]


async def test_enter_submits_value():
    app = _TestApp()
    results: list[int | None] = []

    async with app.run_test() as pilot:
        modal = RoundLimitScreen(3)
        app.push_screen(modal, callback=lambda r: results.append(r))
        await pilot.pause()

        inp = modal.query_one("#limit-input", Input)
        inp.clear()
        inp.insert_text_at_cursor("7")
        await pilot.press("enter")
        await pilot.pause()

    assert results == [7]


async def test_value_below_one_rejected():
    """Entering 0 should show a warning and not dismiss."""
    app = _TestApp()
    results: list[int | None] = []

    async with app.run_test() as pilot:
        modal = RoundLimitScreen(3)
        app.push_screen(modal, callback=lambda r: results.append(r))
        await pilot.pause()

        inp = modal.query_one("#limit-input", Input)
        inp.clear()
        inp.insert_text_at_cursor("0")

        save = modal.query_one("#save-btn")
        await pilot.click(save)
        await pilot.pause()

    # Should not have dismissed
    assert results == []


async def test_renders_current_limit():
    """The input should show the current limit value."""
    app = _TestApp()

    async with app.run_test() as pilot:
        modal = RoundLimitScreen(42)
        app.push_screen(modal)
        await pilot.pause()

        inp = modal.query_one("#limit-input", Input)
        assert inp.value == "42"
