"""Textual pilot tests for QuestionPickerScreen."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Static

from penta.widgets.question_picker import QuestionPickerScreen


class _TestApp(App):
    def compose(self) -> ComposeResult:
        yield Static("base")


def _single_question() -> list[dict]:
    return [
        {
            "question": "Which approach?",
            "header": "Approach",
            "options": [
                {"label": "Option A", "description": "First approach"},
                {"label": "Option B", "description": "Second approach"},
            ],
            "multiSelect": False,
        }
    ]


def _multi_select_question() -> list[dict]:
    return [
        {
            "question": "Which features?",
            "header": "Features",
            "options": [
                {"label": "Auth", "description": "Authentication"},
                {"label": "Logging", "description": "Logging framework"},
            ],
            "multiSelect": True,
        }
    ]


async def test_escape_dismisses_with_none():
    """Pressing escape should dismiss the modal with None."""
    app = _TestApp()
    results: list[dict | None] = []

    async with app.run_test() as pilot:
        app.push_screen(
            QuestionPickerScreen("Claude", _single_question()),
            callback=lambda r: results.append(r),
        )
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert len(results) == 1
    assert results[0] is None


async def test_cancel_button_dismisses_with_none():
    """Clicking Cancel should dismiss with None."""
    app = _TestApp()
    results: list[dict | None] = []

    async with app.run_test() as pilot:
        modal = QuestionPickerScreen("Claude", _single_question())
        app.push_screen(modal, callback=lambda r: results.append(r))
        await pilot.pause()

        cancel = modal.query_one("#cancel-btn")
        await pilot.click(cancel)
        await pilot.pause()

    assert len(results) == 1
    assert results[0] is None


async def test_submit_with_selection_returns_answers():
    """Selecting an option and submitting should return answer dict."""
    app = _TestApp()
    results: list[dict | None] = []

    async with app.run_test() as pilot:
        modal = QuestionPickerScreen("Claude", _single_question())
        app.push_screen(modal, callback=lambda r: results.append(r))
        await pilot.pause()

        # Select Option A by pressing down (first radio) then enter
        from penta.widgets.question_picker import QuestionBlock
        block = modal.query_one(QuestionBlock)
        radio_set = block.query_one("RadioSet")
        radio_set.focus()
        await pilot.pause()
        # Press space/enter to select first option
        await pilot.press("enter")
        await pilot.pause()

        # Click submit
        submit = modal.query_one("#submit-btn")
        await pilot.click(submit)
        await pilot.pause()

    assert len(results) == 1
    assert results[0] is not None
    assert "Which approach?" in results[0]
    assert results[0]["Which approach?"] == "Option A"


async def test_submit_without_selection_blocked():
    """Submitting without selecting anything should not dismiss."""
    app = _TestApp()
    results: list[dict | None] = []

    async with app.run_test() as pilot:
        modal = QuestionPickerScreen("Claude", _single_question())
        app.push_screen(modal, callback=lambda r: results.append(r))
        await pilot.pause()

        # Click submit without selecting
        submit = modal.query_one("#submit-btn")
        await pilot.click(submit)
        await pilot.pause()

    # Should NOT have dismissed — modal still open, no result
    assert len(results) == 0


async def test_renders_question_text():
    """The question text should be visible in the modal."""
    app = _TestApp()
    async with app.run_test() as pilot:
        modal = QuestionPickerScreen("Claude", _single_question())
        app.push_screen(modal)
        await pilot.pause()

        from penta.widgets.question_picker import QuestionBlock
        blocks = modal.query(QuestionBlock)
        assert len(blocks) == 1
