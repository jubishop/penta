from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Button, TextArea


class InputBar(Horizontal):
    """Chat input bar. Ctrl+Enter sends, Enter for newlines."""

    DEFAULT_CSS = """
    InputBar {
        height: auto;
        max-height: 7;
        dock: bottom;
        padding: 1;
        background: $surface;
    }
    InputBar TextArea {
        width: 1fr;
        min-height: 1;
        max-height: 5;
        border: round $accent;
    }
    InputBar Button {
        width: 8;
        margin-left: 1;
    }
    """

    BINDINGS = []

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    def compose(self) -> ComposeResult:
        yield TextArea(id="input-text")
        yield Button("Send", variant="primary", id="send-btn")

    def on_mount(self) -> None:
        ta = self.query_one("#input-text", TextArea)
        ta.show_line_numbers = False
        ta.focus()

    def action_submit(self) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send-btn":
            self._submit()

    def _submit(self) -> None:
        ta = self.query_one("#input-text", TextArea)
        text = ta.text.strip()
        if not text:
            return
        ta.clear()
        self.post_message(self.Submitted(text))
        ta.focus()
