from __future__ import annotations

from textual.containers import VerticalScroll

from penta.models import Message
from penta.widgets.chat_message import ChatMessage


class ChatRoom(VerticalScroll):
    """Scrollable chat message list with auto-scroll."""

    DEFAULT_CSS = """
    ChatRoom {
        height: 1fr;
    }
    """

    def add_message(
        self, message: Message, sender_name: str
    ) -> ChatMessage:
        widget = ChatMessage(
            message=message,
            sender_name=sender_name,
            id=f"msg-{message.id}",
        )
        self.mount(widget)
        self.scroll_end(animate=False)
        return widget

    def get_message_widget(self, message_id: str) -> ChatMessage | None:
        try:
            return self.query_one(f"#msg-{message_id}", ChatMessage)
        except Exception:
            return None
