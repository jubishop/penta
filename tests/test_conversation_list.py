"""Smoke tests for the ConversationListScreen modal."""

from __future__ import annotations

from datetime import datetime, timezone

from textual.app import App, ComposeResult
from textual.widgets import Static

from penta.models.conversation_info import ConversationInfo
from penta.widgets.conversation_list import (
    ConversationAction,
    ConversationListResult,
    ConversationListScreen,
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
