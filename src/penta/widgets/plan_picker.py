"""Small modal for choosing which pending plan to use when multiple exist."""

from __future__ import annotations

from uuid import UUID

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RadioButton, RadioSet

from penta.models.pending_plan import PendingPlan


def _time_ago(plan: PendingPlan) -> str:
    from datetime import datetime

    delta = datetime.now() - plan.timestamp
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    return f"{mins}m ago"


class PlanPickerScreen(ModalScreen[UUID | None]):
    """Pick one pending plan from multiple agents."""

    DEFAULT_CSS = """
    PlanPickerScreen {
        align: center middle;
    }
    PlanPickerScreen #dialog {
        width: 50%;
        max-height: 60%;
        background: $surface;
        border: thick $accent;
        padding: 1 2;
    }
    PlanPickerScreen #dialog-title {
        text-style: bold;
        margin: 0 0 1 0;
    }
    PlanPickerScreen #button-bar {
        height: auto;
        margin: 1 0 0 0;
        align: right middle;
    }
    PlanPickerScreen #button-bar Button {
        margin: 0 0 0 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, plans: dict[UUID, PendingPlan], **kwargs) -> None:
        super().__init__(**kwargs)
        self._plans = plans
        # Ordered list for index-based lookup
        self._plan_list = list(plans.values())

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Which plan?", id="dialog-title")
            with RadioSet(id="plan-radio"):
                for plan in self._plan_list:
                    yield RadioButton(
                        f"{plan.agent_name}'s plan ({_time_ago(plan)})"
                    )
            with Horizontal(id="button-bar"):
                yield Button("Cancel", id="cancel-btn")
                yield Button("Select", variant="primary", id="select-btn")

    def on_mount(self) -> None:
        self.query_one(RadioSet).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "select-btn":
            self._select()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _select(self) -> None:
        radio_set = self.query_one(RadioSet)
        idx = radio_set.pressed_index
        if idx >= 0 and idx < len(self._plan_list):
            self.dismiss(self._plan_list[idx].agent_id)
        else:
            self.dismiss(None)
