from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Button, Static, TextArea

from penta.input_parser import has_agent_mention, has_broadcast_mention
from penta.models import AgentConfig


class AgentToggle(Static):
    """Pill-shaped toggle button for an agent."""

    active: reactive[bool] = reactive(False)

    def __init__(self, agent: AgentConfig) -> None:
        super().__init__(f"@{agent.name}")
        self._agent = agent

    @property
    def agent_name(self) -> str:
        return self._agent.name

    def on_click(self) -> None:
        self.active = not self.active

    def watch_active(self, value: bool) -> None:
        self.set_class(value, "active")
        if value:
            self.styles.background = self._agent.type.color
        else:
            self.styles.background = "transparent"


class InputBar(Vertical):
    """Chat input bar with agent toggle pills. Ctrl+Enter sends, Enter for newlines."""

    DEFAULT_CSS = """
    InputBar {
        height: auto;
        max-height: 10;
        dock: bottom;
        padding: 1;
        background: $surface;
    }
    InputBar #agent-toggles {
        height: auto;
    }
    InputBar AgentToggle {
        width: auto;
        height: 1;
        padding: 0 1;
        margin: 0 1 0 0;
        color: gray;
    }
    InputBar AgentToggle.active {
        color: black;
        text-style: bold;
    }
    InputBar #input-row {
        height: auto;
    }
    InputBar #input-row TextArea {
        width: 1fr;
        min-height: 1;
        max-height: 5;
        border: round $accent;
    }
    InputBar #input-row Button {
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
        with Horizontal(id="input-row"):
            yield TextArea(id="input-text")
            yield Button("Send", variant="primary", id="send-btn")

    def on_mount(self) -> None:
        ta = self.query_one("#input-text", TextArea)
        ta.show_line_numbers = False
        ta.focus()

    def set_agents(self, agents: list[AgentConfig]) -> None:
        """Populate agent toggle pills above the input (idempotent)."""
        results = self.query("#agent-toggles")
        if results:
            container = results.first()
            container.remove_children()
        else:
            container = Horizontal(id="agent-toggles")
            self.mount(container, before="#input-row")
        for agent in agents:
            container.mount(AgentToggle(agent))

    def _active_toggles(self) -> list[AgentToggle]:
        return [t for t in self.query(AgentToggle) if t.active]

    def save_toggle_state(self) -> set[str]:
        """Return names of currently active toggles."""
        return {t.agent_name for t in self._active_toggles()}

    def restore_toggle_state(self, active_names: set[str]) -> None:
        """Restore toggle state from a set of agent names."""
        for toggle in self.query(AgentToggle):
            toggle.active = toggle.agent_name in active_names

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

        # Don't prepend mentions to slash commands or broadcast messages
        active = self._active_toggles()
        if active and not text.startswith("/") and not has_broadcast_mention(text):
            prefixes = []
            for toggle in active:
                if not has_agent_mention(text, toggle.agent_name):
                    prefixes.append(f"@{toggle.agent_name}")
            if prefixes:
                text = " ".join(prefixes) + " " + text

        ta.clear()
        self.post_message(self.Submitted(text))
        ta.focus()
