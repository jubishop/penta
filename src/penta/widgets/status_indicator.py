from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static

from penta.models import AgentConfig, AgentStatus


class StatusIndicator(Static):
    """Shows agent name + colored status dot."""

    DEFAULT_CSS = """
    StatusIndicator {
        width: auto;
        margin: 0 1;
    }
    """

    status: reactive[AgentStatus] = reactive(AgentStatus.IDLE)

    def __init__(self, config: AgentConfig, **kwargs) -> None:
        super().__init__(**kwargs)
        self._config = config
        self.status = config.status

    def render(self) -> str:
        dot_map = {
            AgentStatus.IDLE: "[green]●[/]",
            AgentStatus.PROCESSING: "[yellow]●[/]",
            AgentStatus.DISCONNECTED: "[dim]○[/]",
            AgentStatus.ERROR: "[red]✗[/]",
        }
        dot = dot_map.get(self.status, "[dim]○[/]")
        return f"{self._config.name}{dot}"


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
