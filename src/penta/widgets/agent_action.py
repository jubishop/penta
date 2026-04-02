"""Small modal for choosing an action on an agent with a pending plan."""

from __future__ import annotations

from enum import Enum

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class AgentAction(Enum):
    APPROVE = "approve"
    REVISE = "revise"
    STOP = "stop"


class AgentActionScreen(ModalScreen[AgentAction | None]):
    """Pick an action for an agent with a pending plan."""

    DEFAULT_CSS = """
    AgentActionScreen {
        align: center middle;
    }
    AgentActionScreen #dialog {
        width: 30;
        background: $surface;
        border: thick $accent;
        padding: 1 2;
    }
    AgentActionScreen #dialog-title {
        text-style: bold;
        margin: 0 0 1 0;
    }
    AgentActionScreen Button {
        width: 100%;
        margin: 0 0 1 0;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, agent_name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._agent_name = agent_name

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"{self._agent_name}'s plan", id="dialog-title")
            yield Button("Approve", variant="success", id="approve-btn")
            yield Button("Revise", variant="warning", id="revise-btn")
            yield Button("Stop", variant="error", id="stop-btn")

    def on_mount(self) -> None:
        self.query_one("#approve-btn").focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action_map: dict[str, AgentAction] = {
            "approve-btn": AgentAction.APPROVE,
            "revise-btn": AgentAction.REVISE,
            "stop-btn": AgentAction.STOP,
        }
        btn_id = event.button.id or ""
        self.dismiss(action_map.get(btn_id))

    def action_cancel(self) -> None:
        self.dismiss(None)
