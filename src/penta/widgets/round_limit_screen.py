"""Modal screen for configuring the agent-to-agent round limit."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


class RoundLimitScreen(ModalScreen[int | None]):
    """Small modal to set the maximum number of agent-to-agent routing rounds."""

    DEFAULT_CSS = """
    RoundLimitScreen {
        align: center middle;
    }
    RoundLimitScreen #dialog {
        width: 40;
        height: auto;
        background: $surface;
        border: thick $accent;
        padding: 1 2;
    }
    RoundLimitScreen #dialog-title {
        text-style: bold;
        margin: 0 0 1 0;
    }
    RoundLimitScreen #hint {
        color: $text-muted;
        margin: 0 0 1 0;
    }
    RoundLimitScreen #button-bar {
        height: auto;
        margin: 1 0 0 0;
        align: right middle;
    }
    RoundLimitScreen #button-bar Button {
        margin: 0 0 0 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, current_limit: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current_limit = current_limit

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Round limit", id="dialog-title")
            yield Label(
                "Max agent-to-agent exchanges per message",
                id="hint",
            )
            yield Input(
                value=str(self._current_limit),
                type="integer",
                id="limit-input",
            )
            with Horizontal(id="button-bar"):
                yield Button("Cancel", id="cancel-btn")
                yield Button("Save", variant="primary", id="save-btn")

    def on_mount(self) -> None:
        inp = self.query_one("#limit-input", Input)
        inp.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            self._save()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        text = self.query_one("#limit-input", Input).value.strip()
        try:
            value = int(text)
        except ValueError:
            self.notify("Enter a whole number", severity="warning")
            return
        if value < 1:
            self.notify("Round limit must be at least 1", severity="warning")
            return
        self.dismiss(value)
