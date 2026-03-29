from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import UUID

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Static

from penta.models import AgentStatus, AgentType, Message, PermissionRequest, AgentConfig
from penta.app_state import AppState
from penta.widgets.chat_message import ChatMessage
from penta.widgets.chat_room import ChatRoom, NewContentIndicator
from penta.widgets.input_bar import InputBar
from penta.widgets.permission_dialog import PermissionDialog
from penta.widgets.status_indicator import ExternalIndicator, StatusIndicator

log = logging.getLogger(__name__)


class _MessageTracker:
    """Owns widget registry, streaming state, and render progress."""

    def __init__(self) -> None:
        self.widgets: dict[UUID, ChatMessage] = {}
        self.streaming: dict[UUID, Message] = {}  # agent_id -> Message
        self.rendered_up_to: int = 0

    def register(self, msg: Message, widget: ChatMessage) -> None:
        self.widgets[msg.id] = widget
        if msg.is_streaming and msg.sender.is_agent and msg.sender.agent_id:
            self.streaming[msg.sender.agent_id] = msg

    def complete_stream(self, message: Message) -> None:
        if message.sender.is_agent and message.sender.agent_id:
            self.streaming.pop(message.sender.agent_id, None)


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
        self._messages = _MessageTracker()
        self._status_indicators: dict[UUID, StatusIndicator] = {}
        self._poll_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="header-bar"):
            yield Static(f"Penta -- {self._directory.name}", id="app-title")
            yield Horizontal(id="status-bar")
        yield ChatRoom(id="chat-room")
        yield NewContentIndicator()
        yield InputBar()
        yield Footer()

    async def on_mount(self) -> None:
        state = AppState(self._directory)
        self._state = state
        await state.connect()
        loop = asyncio.get_running_loop()
        state.setup_permission_server(loop)

        # Wire callbacks
        state.on_text_delta = self._on_text_delta
        state.on_stream_complete = self._on_stream_complete
        state.on_status_changed = self._on_status_changed
        state.permissions.on_permission_request = self._on_permission_request
        state.router.on_external_message = self._on_external_message
        state.router.on_external_participant_joined = self._on_external_participant_joined

        # Seed agents
        claude = await state.add_agent(AgentType.CLAUDE.default_name, AgentType.CLAUDE)
        codex = await state.add_agent(AgentType.CODEX.default_name, AgentType.CODEX)

        # Add status indicators
        status_bar = self.query_one("#status-bar", Horizontal)
        for agent in state.agents:
            indicator = StatusIndicator(agent)
            self._status_indicators[agent.id] = indicator
            status_bar.mount(indicator)

        # Load history
        await state.load_chat_history()
        chat_room = self.query_one("#chat-room", ChatRoom)
        for msg in state.conversation:
            name, agent_type = self._sender_info(msg)
            widget = chat_room.add_message(msg, name, agent_type)
            self._messages.register(msg, widget)
        self._messages.rendered_up_to = len(state.conversation)

        # Start external message polling
        self._poll_task = state.start_external_polling(
            state.receive_external_message,
        )

        # Compact on startup
        trimmed = await state.compact_history()
        if trimmed:
            self._messages.rendered_up_to = len(state.conversation)

    async def on_unmount(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        if self._state:
            await self._state.shutdown()

    # -- Input handling --

    def action_submit(self) -> None:
        self.query_one(InputBar).action_submit()

    async def on_input_bar_submitted(self, event: InputBar.Submitted) -> None:
        if not self._state:
            return
        await self._state.send_user_message(event.text.strip())
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

    def _on_status_changed(self, agent_id: UUID, status: AgentStatus) -> None:
        self._apply_status_change(agent_id, status)

    def _on_external_message(self, sender: str, text: str) -> None:
        self._render_new_messages()

    def on_chat_room_new_content_changed(
        self, event: ChatRoom.NewContentChanged,
    ) -> None:
        indicator = self.query_one("#new-content-indicator", NewContentIndicator)
        indicator.display = event.has_new

    def _on_external_participant_joined(self, name: str) -> None:
        status_bar = self.query_one("#status-bar", Horizontal)
        status_bar.mount(ExternalIndicator(name))

    # -- UI updates --

    def _apply_text_delta(self, agent_id: UUID) -> None:
        self._render_new_messages()
        msg = self._messages.streaming.get(agent_id)
        if msg:
            widget = self._messages.widgets.get(msg.id)
            if widget:
                widget.thinking_text = msg.thinking_text
                widget.body_text = msg.text
                widget.is_streaming = msg.is_streaming
        chat_room = self.query_one("#chat-room", ChatRoom)
        if msg and msg.text:
            chat_room.scroll_if_at_bottom()
        elif chat_room.is_at_bottom:
            chat_room.scroll_end(animate=False)

    def _apply_stream_complete(self, message: Message) -> None:
        widget = self._messages.widgets.get(message.id)
        if widget:
            widget.thinking_text = message.thinking_text
            widget.body_text = message.text
            widget.is_cancelled = message.is_cancelled
            widget.is_streaming = False
        self._messages.complete_stream(message)
        chat_room = self.query_one("#chat-room", ChatRoom)
        chat_room.scroll_if_at_bottom()

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
                widget = self._messages.widgets.get(msg.id)
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

    def _apply_status_change(self, agent_id: UUID, status: AgentStatus) -> None:
        indicator = self._status_indicators.get(agent_id)
        if indicator:
            indicator.status = status

    def _render_new_messages(self) -> None:
        """Mount widgets for any conversation messages not yet rendered."""
        if not self._state:
            return
        conversation = self._state.conversation
        chat_room = self.query_one("#chat-room", ChatRoom)
        for i in range(self._messages.rendered_up_to, len(conversation)):
            msg = conversation[i]
            if msg.id not in self._messages.widgets:
                name, agent_type = self._sender_info(msg)
                widget = chat_room.add_message(msg, name, agent_type)
                self._messages.register(msg, widget)
        self._messages.rendered_up_to = len(conversation)

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
