from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from rich.markup import escape as rich_escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from penta.models.conversation_info import ConversationInfo


class ConversationAction(Enum):
    SWITCH = auto()
    DELETE = auto()
    NEW = auto()
    RENAME = auto()


@dataclass
class ConversationListResult:
    action: ConversationAction
    conversation_id: int = 0
    title: str = ""


class ConversationItem(ListItem):
    """A list item representing a single conversation."""

    def __init__(self, info: ConversationInfo, is_current: bool) -> None:
        super().__init__()
        self.info = info
        self.is_current = is_current

    def compose(self) -> ComposeResult:
        yield Static(self._render_label(), classes="conversation-item-text")

    def refresh_label(self) -> None:
        self.query_one(".conversation-item-text", Static).update(self._render_label())

    def _render_label(self) -> str:
        marker = " *" if self.is_current else ""
        updated = self.info.updated_at.astimezone().strftime("%Y-%m-%d %H:%M")
        safe_title = rich_escape(self.info.title)
        return f"{safe_title}{marker}  [dim]{updated}[/]"


class RenameScreen(ModalScreen[str | None]):
    """Small modal for renaming a conversation."""

    DEFAULT_CSS = """
    RenameScreen {
        align: center middle;
    }
    #rename-container {
        width: 50;
        height: auto;
        max-height: 10;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #rename-label {
        height: 1;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, current_title: str) -> None:
        super().__init__()
        self._current_title = current_title

    def compose(self) -> ComposeResult:
        with Vertical(id="rename-container"):
            yield Label("Rename conversation", id="rename-label")
            yield Input(value=self._current_title, id="rename-input")

    def on_mount(self) -> None:
        self.query_one("#rename-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value if value else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConversationListScreen(ModalScreen[ConversationListResult | None]):
    """Modal for browsing and managing conversations."""

    class RenameRequested(Message):
        """Posted when a conversation is renamed so the app can persist it."""

        def __init__(self, conversation_id: int, title: str) -> None:
            super().__init__()
            self.conversation_id = conversation_id
            self.title = title

    DEFAULT_CSS = """
    ConversationListScreen {
        align: center middle;
    }
    #conversation-list-container {
        width: 60;
        height: 70%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #conversation-list-title {
        text-style: bold;
        text-align: center;
        height: 1;
        margin-bottom: 1;
    }
    #conversation-list-hint {
        height: 1;
        margin-top: 1;
        text-align: center;
        color: $text-muted;
    }
    .conversation-item-text {
        height: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("n", "new", "New", show=True),
        Binding("d", "delete", "Delete", show=True),
        Binding("r", "rename", "Rename", show=True),
    ]

    def __init__(
        self,
        conversations: list[ConversationInfo],
        current_id: int,
    ) -> None:
        super().__init__()
        self._conversations = conversations
        self._current_id = current_id

    def compose(self) -> ComposeResult:
        with Vertical(id="conversation-list-container"):
            yield Static("Conversations", id="conversation-list-title")
            lv = ListView(id="conversation-list-view")
            for conv in self._conversations:
                lv.compose_add_child(
                    ConversationItem(conv, is_current=(conv.id == self._current_id))
                )
            yield lv
            yield Static(
                "[b]enter[/] switch  [b]n[/] new  [b]d[/] delete  [b]r[/] rename  [b]esc[/] close",
                id="conversation-list-hint",
            )

    def _selected_item(self) -> ConversationItem | None:
        lv = self.query_one("#conversation-list-view", ListView)
        if lv.highlighted_child and isinstance(lv.highlighted_child, ConversationItem):
            return lv.highlighted_child
        return None

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = self._selected_item()
        if not item or item.is_current:
            self.dismiss(None)
            return
        self.dismiss(ConversationListResult(
            action=ConversationAction.SWITCH,
            conversation_id=item.info.id,
            title=item.info.title,
        ))

    def action_new(self) -> None:
        self.dismiss(ConversationListResult(action=ConversationAction.NEW))

    def action_delete(self) -> None:
        item = self._selected_item()
        if not item:
            return
        if item.is_current:
            self.notify("Cannot delete the active conversation", severity="warning")
            return
        self.dismiss(ConversationListResult(
            action=ConversationAction.DELETE,
            conversation_id=item.info.id,
        ))

    def action_rename(self) -> None:
        item = self._selected_item()
        if not item:
            return

        def on_rename(new_title: str | None) -> None:
            if not new_title:
                return
            item.info.title = new_title
            item.refresh_label()
            self.post_message(self.RenameRequested(item.info.id, new_title))

        self.app.push_screen(RenameScreen(item.info.title), callback=on_rename)
