from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Markdown, Static

from penta.models import Message
from penta.models.agent_type import AgentType


class ChatMessage(Vertical):
    """A single chat message bubble."""

    DEFAULT_CSS = """
    ChatMessage {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    ChatMessage .sender-label {
        text-style: bold;
        height: 1;
        margin-bottom: 0;
    }
    ChatMessage .sender-user {
        color: $accent;
    }
    ChatMessage .sender-claude {
        color: orange;
    }
    ChatMessage .sender-codex {
        color: green;
    }
    ChatMessage .sender-gemini {
        color: dodgerblue;
    }
    ChatMessage .sender-external {
        color: magenta;
    }
    ChatMessage .message-body {
        padding: 0 0 0 2;
    }
    ChatMessage .streaming-cursor {
        color: yellow;
    }
    ChatMessage .error-text {
        color: red;
    }
    """

    body_text: reactive[str] = reactive("", layout=True)
    is_streaming: reactive[bool] = reactive(False)

    def __init__(
        self,
        message: Message,
        sender_name: str,
        sender_type: AgentType | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._message = message
        self._sender_name = sender_name
        self._sender_type = sender_type
        self.body_text = message.text
        self.is_streaming = message.is_streaming

    def compose(self) -> ComposeResult:
        sender_class = "sender-user"
        label = "You"
        if not self._message.sender.is_user:
            label = self._sender_name
            if self._sender_type:
                sender_class = f"sender-{self._sender_type.value}"
            elif self._message.sender.is_external:
                sender_class = "sender-external"

        yield Static(label, classes=f"sender-label {sender_class}")
        yield Markdown(self.body_text or ("..." if self.is_streaming else ""), classes="message-body")

    def watch_body_text(self, value: str) -> None:
        try:
            md = self.query_one(".message-body", Markdown)
            display = value or ("..." if self.is_streaming else "")
            md.update(display)
        except Exception:
            pass

    def watch_is_streaming(self, value: bool) -> None:
        if not value and not self.body_text:
            try:
                md = self.query_one(".message-body", Markdown)
                md.update(self.body_text or "")
            except Exception:
                pass
