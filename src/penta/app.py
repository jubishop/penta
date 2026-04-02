from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from uuid import UUID

from rich.markup import escape as rich_escape
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Static

from penta.models import AgentStatus, AgentType, Message, AgentConfig, MessageSender
from penta.app_state import AppState
from penta.utils import log_task_error
from penta.widgets.chat_message import ChatMessage
from penta.widgets.chat_room import ChatRoom, NewContentIndicator
from penta.widgets.conversation_list import ConversationAction, ConversationListResult, ConversationListScreen
from penta.widgets.input_bar import InputBar
from penta.widgets.plan_picker import PlanPickerScreen
from penta.widgets.question_picker import QuestionPickerScreen
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
        ("escape", "stop_agents", "Stop agents"),
        ("ctrl+enter", "submit", "Send"),
        ("ctrl+b", "scroll_to_new", "Jump to new"),
        ("ctrl+n", "new_conversation", "New chat"),
        ("ctrl+l", "list_conversations", "Chats"),
    ]

    async def action_quit(self) -> None:
        log.info("action_quit triggered")
        self.exit()

    def action_stop_agents(self) -> None:
        """Stop all currently streaming agents."""
        if self._state:
            self._state.cancel_all_busy()

    def on_status_indicator_stop_requested(
        self, event: StatusIndicator.StopRequested,
    ) -> None:
        """Handle click-to-stop on a processing StatusIndicator."""
        if self._state:
            self._state.cancel_agent(event.agent_id)

    def _handle_exception(self, error: Exception) -> None:
        log.exception("Unhandled exception — app will exit: %s", error)
        super()._handle_exception(error)

    def __init__(self, directory: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self._directory = directory
        self._state: AppState | None = None
        self._messages = _MessageTracker()
        self._status_indicators: dict[UUID, StatusIndicator] = {}
        self._poll_task: asyncio.Task | None = None
        self._switching: bool = False
        self._toggle_state: dict[int, set[str]] = {}  # conversation_id -> active agent names

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

        # Wire callbacks
        state.on_text_delta = self._on_text_delta
        state.on_stream_complete = self._on_stream_complete
        state.on_status_changed = self._on_status_changed
        state.on_conversation_switched = self._on_conversation_switched
        state.on_question_asked = self._on_question_asked
        state.on_plan_review = self._on_plan_review
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

        # Add agent toggle pills to input bar
        self.query_one(InputBar).set_agents(state.agents)

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

        # Warn if permission server failed (plan review/questions disabled)
        if state._permission_server is None:
            self.notify(
                "Permission server unavailable — plans and questions will be auto-approved",
                severity="warning",
            )

        # Update title with conversation name
        self._update_title()

    async def on_unmount(self) -> None:
        import traceback
        log.info("on_unmount called — stack:\n%s", "".join(traceback.format_stack()))
        if self._poll_task:
            self._poll_task.cancel()
        if self._state:
            await self._state.shutdown()

    # -- Input handling --

    def action_submit(self) -> None:
        self.query_one(InputBar).action_submit()

    def action_scroll_to_new(self) -> None:
        indicator = self.query_one("#new-content-indicator", NewContentIndicator)
        if indicator.display:
            chat_room = self.query_one("#chat-room", ChatRoom)
            chat_room.scroll_to_bottom()

    async def on_input_bar_submitted(self, event: InputBar.Submitted) -> None:
        if not self._state or self._switching:
            return
        text = event.text.strip()

        # /approve [AgentName] — approve a pending plan
        lower = text.lower()
        if lower == "/approve" or lower.startswith("/approve "):
            self._handle_approve(text)
            return

        # /revise [AgentName] <feedback> — reject a plan with feedback
        if lower == "/revise" or lower.startswith("/revise "):
            await self._handle_revise(text)
            return

        # If message contains /plan and multiple plans exist, show picker
        if self._state._PLAN_TOKEN_RE.search(text) and len(self._state.pending_plans) > 1:
            def on_pick(agent_id: UUID | None) -> None:
                if agent_id is not None:
                    async def _send_and_render():
                        assert self._state is not None
                        await self._state.send_user_message(text, resolved_plan_id=agent_id)
                        self._render_new_messages()
                    task = asyncio.create_task(_send_and_render())
                    task.add_done_callback(log_task_error)

            self.push_screen(
                PlanPickerScreen(self._state.pending_plans), callback=on_pick,
            )
            return

        await self._state.send_user_message(text)
        self._render_new_messages()

    # -- Conversation management --

    def _conversation_task(self, coro) -> None:
        """Create a tracked async task with error logging."""
        task = asyncio.create_task(coro)
        task.add_done_callback(log_task_error)

    async def action_new_conversation(self) -> None:
        if not self._state or self._switching:
            return
        self._switching = True
        try:
            self._save_toggle_state()
            title = f"Chat {datetime.now():%Y-%m-%d %H:%M:%S}"
            info = await self._state.create_conversation(title)
            await self._state.switch_conversation(info.id)
        finally:
            self._switching = False

    async def action_list_conversations(self) -> None:
        if not self._state or self._switching:
            return
        conversations = await self._state.list_conversations()
        current_id = self._state.current_conversation_id

        def handle_result(result: ConversationListResult | None) -> None:
            if result is None:
                return
            if result.action is ConversationAction.SWITCH:
                self._conversation_task(self._handle_switch(result.conversation_id))
            elif result.action is ConversationAction.DELETE:
                self._conversation_task(self._handle_delete(result.conversation_id))
            elif result.action is ConversationAction.NEW:
                self._conversation_task(self.action_new_conversation())
        self.push_screen(
            ConversationListScreen(conversations, current_id),
            callback=handle_result,
        )

    async def _handle_switch(self, conversation_id: int) -> None:
        state = self._state
        assert state is not None
        if self._switching:
            return
        self._switching = True
        try:
            self._save_toggle_state()
            await state.switch_conversation(conversation_id)
        finally:
            self._switching = False

    async def _handle_delete(self, conversation_id: int) -> None:
        assert self._state is not None
        deleted = await self._state.delete_conversation(conversation_id)
        if not deleted:
            self.notify("Cannot delete the active or only conversation", severity="warning")

    def on_conversation_list_screen_rename_requested(
        self, event: ConversationListScreen.RenameRequested,
    ) -> None:
        self._conversation_task(self._handle_rename(event.conversation_id, event.title))

    async def _handle_rename(self, conversation_id: int, title: str) -> None:
        assert self._state is not None
        await self._state.rename_conversation(conversation_id, title)
        self._update_title()

    def _on_conversation_switched(self) -> None:
        """Rebuild the chat room UI after a conversation switch."""
        state = self._state
        assert state is not None
        chat_room = self.query_one("#chat-room", ChatRoom)
        chat_room.remove_children()
        self._messages = _MessageTracker()

        # Clear stale external participant indicators
        status_bar = self.query_one("#status-bar", Horizontal)
        for widget in status_bar.query(ExternalIndicator):
            widget.remove()

        # Re-render loaded history and rebuild external indicators
        for msg in state.conversation:
            name, agent_type = self._sender_info(msg)
            widget = chat_room.add_message(msg, name, agent_type)
            self._messages.register(msg, widget)
            # Rebuild external participant indicators from history
            if msg.sender.is_external and msg.sender.name:
                ext_name = msg.sender.name
                if ext_name not in state.external_participants:
                    state.router.external_participants.add(ext_name)
                    status_bar.mount(ExternalIndicator(ext_name))
        self._messages.rendered_up_to = len(state.conversation)

        # Restore per-conversation toggle state
        input_bar = self.query_one(InputBar)
        saved = self._toggle_state.get(state.current_conversation_id, set())
        input_bar.restore_toggle_state(saved)

        self._update_title()

    def _save_toggle_state(self) -> None:
        """Save current toggle state for the active conversation."""
        if not self._state:
            return
        input_bar = self.query_one(InputBar)
        self._toggle_state[self._state.current_conversation_id] = input_bar.save_toggle_state()

    def _update_title(self) -> None:
        if not self._state:
            return
        title_widget = self.query_one("#app-title", Static)
        safe_title = rich_escape(self._state.current_conversation_title)
        title_widget.update(
            f"Penta -- {self._directory.name} / {safe_title}"
        )

    # -- Plan / Question handling --

    def _handle_approve(self, text: str) -> None:
        """Handle /approve [AgentName]."""
        state = self._state
        assert state is not None
        parts = text.split(maxsplit=1)
        agent_name = parts[1].strip() if len(parts) > 1 else None
        agent_id = self._resolve_plan_agent(agent_name)
        if agent_id is None:
            return
        # Add plan text as a visible message in conversation
        plan = state.pending_plans.get(agent_id)
        if plan:
            state.conversation.append(
                Message(
                    sender=MessageSender.agent(agent_id),
                    text=f"**Approved plan:**\n\n{plan.plan_text}",
                )
            )
        state.approve_plan(agent_id)
        self._render_new_messages()

    async def _handle_revise(self, text: str) -> None:
        """Handle /revise [AgentName] <feedback>."""
        state = self._state
        assert state is not None
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            self.notify("Usage: /revise [AgentName] <feedback>", severity="warning")
            return

        # Try to parse as /revise AgentName feedback
        candidate_name = parts[1]
        agent_id = self._resolve_plan_agent_by_name(candidate_name)

        if agent_id is not None:
            feedback = parts[2] if len(parts) > 2 else ""
        else:
            # No agent name match — treat all after /revise as feedback
            agent_id = self._resolve_plan_agent(None)
            feedback = text[len("/revise"):].strip()

        if agent_id is None:
            return
        if not feedback:
            self.notify("Please provide feedback for the revision", severity="warning")
            return

        agent = state.agent_by_id(agent_id)
        agent_name = agent.name if agent else "Agent"
        state.reject_plan(agent_id)
        # Route feedback directly — bypass /plan interpolation so "/plan"
        # in user feedback doesn't accidentally inject another agent's plan.
        await state.router.send_user_message(f"@{agent_name} Please revise your plan: {feedback}")
        self._render_new_messages()

    def _resolve_plan_agent(self, agent_name: str | None) -> UUID | None:
        """Resolve which pending plan to act on."""
        assert self._state is not None
        plans = self._state.pending_plans
        if not plans:
            self.notify("No plans pending", severity="warning")
            return None
        if agent_name:
            agent_id = self._resolve_plan_agent_by_name(agent_name)
            if agent_id is None:
                self.notify(f"No pending plan from '{agent_name}'", severity="warning")
            return agent_id
        if len(plans) == 1:
            return next(iter(plans))
        # Multiple plans — prompt user to specify agent name
        self.notify(
            "Multiple plans pending. Specify an agent name, e.g. /approve Claude",
            severity="warning",
        )
        return None

    def _resolve_plan_agent_by_name(self, name: str | None) -> UUID | None:
        if not name or not self._state:
            return None
        agent = self._state.agent_by_name(name)
        if agent and agent.id in self._state.pending_plans:
            return agent.id
        return None

    def _on_question_asked(
        self, agent_id: UUID, questions: list[dict], tool_use_id: str,
    ) -> None:
        """Show the question picker when Claude uses AskUserQuestion.

        The hook HTTP response is blocked until the user answers.
        Answers are injected via updatedInput so Claude receives them
        directly as the AskUserQuestion tool result.
        """
        if not self._state:
            return
        agent = self._state.agent_by_id(agent_id)
        agent_name = agent.name if agent else "Agent"

        def on_answers(answers: dict[str, str] | None) -> None:
            if not self._state:
                return
            if answers is None:
                # User cancelled — cancel the agent's stream
                self._state.cancel_agent(agent_id)
                return
            if not self._state._permission_server:
                return
            self._state._permission_server.resolve_question(tool_use_id, answers)
            # Resume processing status
            coord = self._state.coordinators.get(agent_id)
            if coord:
                coord.set_status(AgentStatus.PROCESSING)

        self.push_screen(
            QuestionPickerScreen(agent_name, questions),
            callback=on_answers,
        )

    def _on_plan_review(
        self, agent_id: UUID, plan_text: str, tool_use_id: str,
    ) -> None:
        """Show the plan as an inline message and notify the user."""
        if not self._state:
            return
        agent = self._state.agent_by_id(agent_id)
        agent_name = agent.name if agent else "Agent"

        # Add plan as a message in the chat
        self._state.conversation.append(
            Message(
                sender=MessageSender.agent(agent_id),
                text=f"**Plan awaiting approval:**\n\n{plan_text}",
            )
        )
        self._render_new_messages()
        self.notify(
            f"Plan from {agent_name} — /approve, /revise <feedback>, "
            f"or share with /plan",
            severity="information",
        )

    # -- Callbacks from AppState --
    # These run on the same asyncio event loop as Textual, so call directly.

    def _on_text_delta(self, agent_id: UUID, delta: str) -> None:
        self._apply_text_delta(agent_id)

    def _on_stream_complete(self, message: Message, agent_id: UUID) -> None:
        self._apply_stream_complete(message)

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
        log.info(
            "Stream complete: msg=%s, cancelled=%s, error=%s, body_len=%d, widget_found=%s",
            message.id, message.is_cancelled, message.is_error,
            len(message.text), widget is not None,
        )
        if widget:
            widget.thinking_text = message.thinking_text
            widget.body_text = message.text
            widget.is_cancelled = message.is_cancelled
            widget.is_streaming = False
        self._messages.complete_stream(message)
        chat_room = self.query_one("#chat-room", ChatRoom)
        chat_room.scroll_if_at_bottom()

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
                log.debug(
                    "Mounting new message widget: msg=%s, sender=%s, streaming=%s",
                    msg.id, name, msg.is_streaming,
                )
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
