"""Smoke tests for the ConversationListScreen modal."""

from __future__ import annotations

from datetime import datetime, timezone

from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from penta.models.conversation_info import ConversationInfo
from penta.widgets.conversation_list import (
    ConversationAction,
    ConversationListResult,
    ConversationListScreen,
    RenameScreen,
)


def _make_convos() -> list[ConversationInfo]:
    now = datetime.now(timezone.utc)
    return [
        ConversationInfo(id=1, title="Default", created_at=now, updated_at=now),
        ConversationInfo(id=2, title="Second Chat", created_at=now, updated_at=now),
    ]


class _TestApp(App):
    """Minimal app for mounting the modal."""

    def compose(self) -> ComposeResult:
        yield Static("base")


async def test_modal_lists_conversations():
    """The modal should render all conversations."""
    app = _TestApp()
    async with app.run_test() as pilot:
        conversations = _make_convos()
        modal = ConversationListScreen(conversations, current_id=1)
        app.push_screen(modal)
        await pilot.pause()

        # Verify the title is rendered
        title = modal.query_one("#conversation-list-title", Static)
        assert title is not None

        # Verify both conversations appear as list items
        from penta.widgets.conversation_list import ConversationItem

        items = modal.query(ConversationItem)
        assert len(items) == 2


async def test_modal_escape_dismisses():
    """Pressing escape should dismiss the modal with None."""
    app = _TestApp()
    results: list[ConversationListResult | None] = []

    async with app.run_test() as pilot:
        app.push_screen(
            ConversationListScreen(_make_convos(), current_id=1),
            callback=lambda r: results.append(r),
        )
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert len(results) == 1
    assert results[0] is None


async def test_modal_enter_on_current_dismisses():
    """Pressing enter on the already-active conversation should dismiss with None."""
    app = _TestApp()
    results: list[ConversationListResult | None] = []

    async with app.run_test() as pilot:
        app.push_screen(
            ConversationListScreen(_make_convos(), current_id=1),
            callback=lambda r: results.append(r),
        )
        await pilot.pause()
        # First item (id=1) is highlighted by default and is the current conversation
        await pilot.press("enter")
        await pilot.pause()

    assert len(results) == 1
    assert results[0] is None


async def test_modal_enter_on_other_switches():
    """Pressing enter on a different conversation should dismiss with SWITCH."""
    app = _TestApp()
    results: list[ConversationListResult | None] = []

    async with app.run_test() as pilot:
        app.push_screen(
            ConversationListScreen(_make_convos(), current_id=1),
            callback=lambda r: results.append(r),
        )
        await pilot.pause()
        # Move to second item (id=2)
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

    assert len(results) == 1
    assert results[0].action is ConversationAction.SWITCH
    assert results[0].conversation_id == 2


async def test_modal_new_returns_new_action():
    """Pressing 'n' should dismiss with a NEW action."""
    app = _TestApp()
    results: list[ConversationListResult | None] = []

    async with app.run_test() as pilot:
        app.push_screen(
            ConversationListScreen(_make_convos(), current_id=1),
            callback=lambda r: results.append(r),
        )
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()

    assert len(results) == 1
    assert results[0].action is ConversationAction.NEW


# ── RenameScreen tests ────────────────────────────────────────────


async def test_rename_input_receives_focus_and_shows_current_title():
    """The input should auto-focus and display the current title."""
    app = _TestApp()
    async with app.run_test() as pilot:
        modal = RenameScreen("Old Title")
        app.push_screen(modal)
        await pilot.pause()

        inp = modal.query_one("#rename-input", Input)
        assert inp.has_focus
        assert inp.value == "Old Title"


async def test_rename_input_displays_typed_text():
    """Text typed into the rename input should appear in the widget value."""
    app = _TestApp()
    async with app.run_test() as pilot:
        modal = RenameScreen("")
        app.push_screen(modal)
        await pilot.pause()

        await pilot.press(*"New Name")
        await pilot.pause()

        inp = modal.query_one("#rename-input", Input)
        assert inp.value == "New Name"


async def test_rename_submit_returns_new_title():
    """Pressing enter should dismiss with the entered title."""
    app = _TestApp()
    results: list[str | None] = []

    async with app.run_test() as pilot:
        app.push_screen(
            RenameScreen("Old"),
            callback=lambda r: results.append(r),
        )
        await pilot.pause()

        # Clear existing text and type new title
        inp = app.screen.query_one("#rename-input", Input)
        inp.value = ""
        await pilot.press(*"Renamed")
        await pilot.press("enter")
        await pilot.pause()

    assert len(results) == 1
    assert results[0] == "Renamed"


async def test_rename_submit_empty_returns_none():
    """Submitting an empty (or whitespace-only) title should dismiss with None."""
    app = _TestApp()
    results: list[str | None] = []

    async with app.run_test() as pilot:
        app.push_screen(
            RenameScreen(""),
            callback=lambda r: results.append(r),
        )
        await pilot.pause()

        await pilot.press("space", "space", "space")
        await pilot.press("enter")
        await pilot.pause()

    assert len(results) == 1
    assert results[0] is None


async def test_rename_escape_cancels():
    """Pressing escape should dismiss with None without renaming."""
    app = _TestApp()
    results: list[str | None] = []

    async with app.run_test() as pilot:
        app.push_screen(
            RenameScreen("Keep This"),
            callback=lambda r: results.append(r),
        )
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

    assert len(results) == 1
    assert results[0] is None


async def test_rename_updates_list_item_and_stays_open():
    """After renaming, the list should stay open with the updated title visible."""
    app = _TestApp()
    rename_events: list[ConversationListScreen.RenameRequested] = []
    dismiss_results: list[ConversationListResult | None] = []

    class _CaptureApp(App):
        def compose(self) -> ComposeResult:
            yield Static("base")

        def on_conversation_list_screen_rename_requested(
            self, event: ConversationListScreen.RenameRequested,
        ) -> None:
            rename_events.append(event)

    app = _CaptureApp()
    async with app.run_test() as pilot:
        app.push_screen(
            ConversationListScreen(_make_convos(), current_id=1),
            callback=lambda r: dismiss_results.append(r),
        )
        await pilot.pause()

        # Press 'r' to open rename modal
        await pilot.press("r")
        await pilot.pause()

        # Clear the pre-filled title and type a new one
        inp = app.screen.query_one("#rename-input", Input)
        inp.value = ""
        await pilot.press(*"Better")
        await pilot.press("enter")
        await pilot.pause()

        # The list should still be showing (not dismissed)
        assert len(dismiss_results) == 0
        assert isinstance(app.screen, ConversationListScreen)

        # The RenameRequested message should have been posted
        assert len(rename_events) == 1
        assert rename_events[0].conversation_id == 1
        assert rename_events[0].title == "Better"

        # The list item label should reflect the new title
        from penta.widgets.conversation_list import ConversationItem
        items = app.screen.query(ConversationItem)
        assert items[0].info.title == "Better"
