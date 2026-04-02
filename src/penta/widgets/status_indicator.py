from __future__ import annotations

from uuid import UUID

from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from penta.models import AgentConfig, AgentStatus


class StatusIndicator(Static):
    """Shows agent name + colored status dot. Click while busy to act."""

    DEFAULT_CSS = """
    StatusIndicator {
        width: auto;
        margin: 0 1;
    }
    StatusIndicator.clickable {
        text-style: underline;
    }
    """

    class Clicked(Message):
        """Posted when user clicks a busy indicator."""

        def __init__(self, agent_id: UUID) -> None:
            super().__init__()
            self.agent_id = agent_id

    status: reactive[AgentStatus] = reactive(AgentStatus.IDLE)

    def __init__(self, config: AgentConfig, **kwargs) -> None:
        super().__init__(**kwargs)
        self._config = config
        self.status = config.status

    def render(self) -> str:
        dot_map = {
            AgentStatus.IDLE: "[green]●[/]",
            AgentStatus.PROCESSING: "[yellow]●[/]",
            AgentStatus.WAITING_FOR_USER: "[blue]●[/]",
            AgentStatus.DISCONNECTED: "[dim]○[/]",
            AgentStatus.ERROR: "[red]✗[/]",
        }
        dot = dot_map.get(self.status, "[dim]○[/]")
        return f"{self._config.name}{dot}"

    def watch_status(self, value: AgentStatus) -> None:
        self.set_class(value.is_busy, "clickable")

    def on_click(self) -> None:
        if self.status.is_busy:
            self.post_message(self.Clicked(self._config.id))


class ExternalIndicator(Static):
    """Shows an external participant name with an orange dot."""

    DEFAULT_CSS = """
    ExternalIndicator {
        width: auto;
        margin: 0 1;
    }
    """

    def __init__(self, name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._name = name

    def render(self) -> str:
        return f"{self._name}[orange1]●[/]"
