from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import UUID

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Static

from penta.input_parser import ParsedChat, parse
from penta.models import AgentStatus, AgentType, Message, PermissionRequest, AgentConfig
from penta.models.app_state import AppState
from penta.widgets.chat_message import ChatMessage
from penta.widgets.chat_room import ChatRoom
from penta.widgets.input_bar import InputBar
from penta.widgets.permission_dialog import PermissionDialog
from penta.widgets.status_indicator import ExternalIndicator, StatusIndicator

log = logging.getLogger(__name__)


class PentaApp(App):
    CSS_PATH = "penta.tcss"
    TITLE = "Penta"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+enter", "submit", "Send"),
    ]

    def __init__(self, directory: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self._directory = directory
        self._state: AppState | None = None
        self._message_widgets: dict[UUID, ChatMessage] = {}
        self._status_indicators: dict[UUID, StatusIndicator] = {}
        self._streaming_messages: dict[UUID, Message] = {}  # agent_id -> Message
        self._last_rendered_index: int = 0
        self._poll_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="header-bar"):
            yield Static(f"Penta -- {self._directory.name}", id="app-title")
            yield Horizontal(id="status-bar")
        yield ChatRoom(id="chat-room")
        yield InputBar()
        yield Footer()

    async def on_mount(self) -> None:
        state = AppState(self._directory)
        self._state = state
        loop = asyncio.get_running_loop()
        state.setup_permission_server(loop)

        # Wire callbacks
        state.on_text_delta = self._on_text_delta
        state.on_stream_complete = self._on_stream_complete
        state.on_permission_request = self._on_permission_request
        state.on_status_changed = self._on_status_changed
        state.on_external_message = self._on_external_message
        state.on_external_participant_joined = self._on_external_participant_joined

        # Seed agents
        claude = state.add_agent("claude", AgentType.CLAUDE)
        codex = state.add_agent("codex", AgentType.CODEX)
        gemini = state.add_agent("gemini", AgentType.GEMINI)

        # Add status indicators
        status_bar = self.query_one("#status-bar", Horizontal)
        for agent in state.agents:
            indicator = StatusIndicator(agent)
            self._status_indicators[agent.id] = indicator
            status_bar.mount(indicator)

        # Load history
        state.load_chat_history()
        chat_room = self.query_one("#chat-room", ChatRoom)
        for msg in state.conversation:
            name, agent_type = self._sender_info(msg)
            widget = chat_room.add_message(msg, name, agent_type)
            self._message_widgets[msg.id] = widget
        self._last_rendered_index = len(state.conversation)

        # Start external message polling
        self._poll_task = state.start_external_polling(
            lambda sender, text: self.call_from_thread(
                state.receive_external_message, sender, text
            )
        )

        # Compact on startup
        state.compact_history()

    async def on_unmount(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        if self._state:
            await self._state.shutdown()

    # -- Input handling --

    def action_submit(self) -> None:
        self.query_one(InputBar).action_submit()

    def on_input_bar_submitted(self, event: InputBar.Submitted) -> None:
        if not self._state:
            return
        parsed = parse(event.text, self._state.agents)

        self._state.send_user_message(parsed.text)
        self._render_new_messages()

    # -- Permission handling --

    def on_permission_dialog_approved(self, event: PermissionDialog.Approved) -> None:
        if self._state:
            self._state.approve_permission(event.request_id)

    def on_permission_dialog_denied(self, event: PermissionDialog.Denied) -> None:
        if self._state:
            self._state.deny_permission(event.request_id)

    # -- Callbacks from AppState --
    # These run on the same asyncio event loop as Textual, so call directly.

    def _on_text_delta(self, agent_id: UUID, delta: str) -> None:
        self._apply_text_delta(agent_id)

    def _on_stream_complete(self, message: Message, agent_id: UUID) -> None:
        self._apply_stream_complete(message)

    def _on_permission_request(self, request: PermissionRequest) -> None:
        self._show_permission_dialog(request)

    def _on_status_changed(self, agent_id: UUID, status: object) -> None:
        self._apply_status_change(agent_id, status)

    def _on_external_message(self, sender: str, text: str) -> None:
        self._render_new_messages()

    def _on_external_participant_joined(self, name: str) -> None:
        status_bar = self.query_one("#status-bar", Horizontal)
        status_bar.mount(ExternalIndicator(name))

    # -- UI updates --

    def _apply_text_delta(self, agent_id: UUID) -> None:
        self._render_new_messages()
        msg = self._streaming_messages.get(agent_id)
        if msg:
            widget = self._message_widgets.get(msg.id)
            if widget:
                widget.thinking_text = msg.thinking_text
                widget.body_text = msg.text
                widget.is_streaming = msg.is_streaming
        chat_room = self.query_one("#chat-room", ChatRoom)
        chat_room.scroll_end(animate=False)

    def _apply_stream_complete(self, message: Message) -> None:
        widget = self._message_widgets.get(message.id)
        if widget:
            widget.thinking_text = message.thinking_text
            widget.body_text = message.text
            widget.is_streaming = False
        if message.sender.is_agent and message.sender.agent_id:
            self._streaming_messages.pop(message.sender.agent_id, None)

    def _show_permission_dialog(self, request: PermissionRequest) -> None:
        # Find the streaming message widget for this agent and mount the dialog
        if not self._state:
            return
        for msg in reversed(self._state.conversation):
            if (
                msg.sender.is_agent
                and msg.sender.agent_id == request.agent_id
                and msg.is_streaming
            ):
                widget = self._message_widgets.get(msg.id)
                if widget:
                    dialog = PermissionDialog(
                        request_id=request.id,
                        tool_name=request.tool_name,
                        tool_input=request.tool_input,
                    )
                    widget.mount(dialog)
                    return

        # Fallback: mount at bottom of chat
        chat_room = self.query_one("#chat-room", ChatRoom)
        dialog = PermissionDialog(
            request_id=request.id,
            tool_name=request.tool_name,
            tool_input=request.tool_input,
        )
        chat_room.mount(dialog)

    def _apply_status_change(self, agent_id: UUID, status: object) -> None:
        indicator = self._status_indicators.get(agent_id)
        if indicator and isinstance(status, AgentStatus):
            indicator.status = status

    def _render_new_messages(self) -> None:
        """Mount widgets for any conversation messages not yet rendered."""
        if not self._state:
            return
        conversation = self._state.conversation
        chat_room = self.query_one("#chat-room", ChatRoom)
        for i in range(self._last_rendered_index, len(conversation)):
            msg = conversation[i]
            if msg.id not in self._message_widgets:
                name, agent_type = self._sender_info(msg)
                widget = chat_room.add_message(msg, name, agent_type)
                self._message_widgets[msg.id] = widget
                if msg.is_streaming and msg.sender.is_agent and msg.sender.agent_id:
                    self._streaming_messages[msg.sender.agent_id] = msg
        self._last_rendered_index = len(conversation)

    def _sender_info(self, message: Message) -> tuple[str, AgentType | None]:
        """Return (display_name, agent_type) for a message sender."""
        if message.sender.is_user:
            return "You", None
        if message.sender.is_external:
            return message.sender.name or "External", None
        if self._state and message.sender.agent_id:
            agent = self._state.agent_by_id(message.sender.agent_id)
            if agent:
                return agent.name, agent.type
        return "Agent", None
