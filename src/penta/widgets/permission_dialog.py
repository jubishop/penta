from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Static


class PermissionDialog(Vertical):
    """Inline permission approval widget shown within a chat message."""

    DEFAULT_CSS = """
    PermissionDialog {
        height: auto;
        padding: 1;
        margin: 0 2;
        border: tall $warning;
        background: $surface;
    }
    PermissionDialog .tool-name {
        text-style: bold;
        color: $warning;
    }
    PermissionDialog .tool-input {
        margin: 1 0;
        padding: 1;
        background: $panel;
    }
    PermissionDialog .buttons {
        height: 3;
        align: right middle;
    }
    PermissionDialog .buttons Button {
        margin-left: 1;
    }
    """

    class Approved(Message):
        def __init__(self, request_id: str) -> None:
            self.request_id = request_id
            super().__init__()

    class Denied(Message):
        def __init__(self, request_id: str) -> None:
            self.request_id = request_id
            super().__init__()

    def __init__(
        self, request_id: str, tool_name: str, tool_input: str, **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._request_id = request_id
        self._tool_name = tool_name
        self._tool_input = tool_input

    def compose(self) -> ComposeResult:
        yield Static(f"Wants to use: {self._tool_name}", classes="tool-name")
        yield Static(self._tool_input, classes="tool-input")
        with Horizontal(classes="buttons"):
            yield Button("Allow", variant="success", id="allow-btn")
            yield Button("Deny", variant="error", id="deny-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "allow-btn":
            self.post_message(self.Approved(self._request_id))
            self.remove()
        elif event.button.id == "deny-btn":
            self.post_message(self.Denied(self._request_id))
            self.remove()
