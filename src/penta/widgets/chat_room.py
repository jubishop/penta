from __future__ import annotations

from textual.containers import VerticalScroll
from textual.message import Message as TextualMessage
from textual.reactive import reactive
from textual.widgets import Static

from penta.models import Message
from penta.models.agent_type import AgentType
from penta.widgets.chat_message import ChatMessage


class NewContentIndicator(Static):
    """Subtle indicator shown when new messages arrive while scrolled up."""

    DEFAULT_CSS = """
    NewContentIndicator {
        height: 1;
        width: 100%;
        text-align: center;
        background: $primary-background;
        color: $accent;
        text-style: bold;
        display: none;
    }
    """

    def __init__(self) -> None:
        super().__init__("▼ New messages below ▼", id="new-content-indicator")

    def on_click(self) -> None:
        chat_room = self.screen.query_one("#chat-room", ChatRoom)
        chat_room.scroll_to_bottom()


class ChatRoom(VerticalScroll):
    """Scrollable chat message list with smart auto-scroll."""

    DEFAULT_CSS = """
    ChatRoom {
        height: 1fr;
    }
    """

    has_new_content: reactive[bool] = reactive(False)

    _SCROLL_TOLERANCE = 2

    class NewContentChanged(TextualMessage):
        """Posted when the new-content indicator state changes."""

        def __init__(self, has_new: bool) -> None:
            super().__init__()
            self.has_new = has_new

    @property
    def is_at_bottom(self) -> bool:
        return self.scroll_y >= self.max_scroll_y - self._SCROLL_TOLERANCE

    def watch_has_new_content(self, value: bool) -> None:
        self.post_message(self.NewContentChanged(value))

    def add_message(
        self, message: Message, sender_name: str, sender_type: AgentType | None = None,
    ) -> ChatMessage:
        was_at_bottom = self.is_at_bottom
        widget = ChatMessage(
            message=message,
            sender_name=sender_name,
            sender_type=sender_type,
            id=f"msg-{message.id}",
        )
        self.mount(widget)
        if was_at_bottom:
            self.scroll_end(animate=False)
        else:
            self.has_new_content = True
        return widget

    def scroll_if_at_bottom(self) -> None:
        """Scroll to end only if user hasn't scrolled up."""
        if self.is_at_bottom:
            self.scroll_end(animate=False)
        else:
            self.has_new_content = True

    def scroll_to_bottom(self) -> None:
        """Unconditionally scroll to bottom and clear indicator."""
        self.scroll_end(animate=False)
        self.has_new_content = False

    def watch_scroll_y(self, value: float) -> None:
        """Clear new-content flag when user scrolls back to bottom."""
        if self.is_at_bottom and self.has_new_content:
            self.has_new_content = False
