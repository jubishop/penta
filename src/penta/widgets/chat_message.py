from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import Collapsible, Markdown, Static

from penta.models import Message
from penta.models.agent_type import AgentType

log = logging.getLogger(__name__)


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
    ChatMessage .sender-external {
        color: magenta;
    }
    ChatMessage .message-body, ChatMessage .message-body-stream {
        padding: 0 0 0 2;
    }
    ChatMessage .thinking-fold {
        padding: 0 0 0 2;
        margin: 0 0 1 0;
    }
    ChatMessage .thinking-fold > CollapsibleTitle {
        color: $text-muted;
        text-style: dim italic;
        padding: 0;
        height: 1;
    }
    ChatMessage .thinking-fold > Contents {
        padding: 0 0 0 2;
    }
    ChatMessage .thinking-text {
        color: $text-muted;
        text-style: dim italic;
    }
    ChatMessage .streaming-cursor {
        color: yellow;
    }
    ChatMessage .error-text {
        color: red;
    }
    """

    thinking_text: reactive[str] = reactive("")
    body_text: reactive[str] = reactive("")
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
        self.thinking_text = message.thinking_text
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
        # Start expanded while streaming so the user sees thinking in
        # real-time; start collapsed for completed messages (history).
        thinking_fold = Collapsible(
            Static(self.thinking_text, classes="thinking-text"),
            title="Thinking",
            collapsed=not self.is_streaming,
            classes="thinking-fold",
        )
        thinking_fold.display = bool(self.thinking_text)
        yield thinking_fold

        # During streaming, use a cheap Static widget for text updates.
        # On completion, swap to Markdown for rich rendering (one parse).
        stream_body = Static(
            self.body_text or "...", classes="message-body-stream",
        )
        stream_body.display = self.is_streaming
        yield stream_body

        md = Markdown(
            self.body_text or "", classes="message-body",
        )
        md.display = not self.is_streaming
        yield md

    def watch_thinking_text(self, value: str) -> None:
        try:
            fold = self.query_one(".thinking-fold", Collapsible)
            if value:
                self.query_one(".thinking-text", Static).update(value)
                fold.display = True
            else:
                fold.display = False
        except NoMatches:
            log.debug("watch_thinking_text: widget not ready")

    def watch_body_text(self, value: str) -> None:
        try:
            if self.is_streaming:
                w = self.query_one(".message-body-stream", Static)
                w.update(value or "...")
            else:
                md = self.query_one(".message-body", Markdown)
                md.update(value or "")
        except NoMatches:
            log.debug("watch_body_text: widget not ready")

    def watch_is_streaming(self, value: bool) -> None:
        if not value:
            if not self.body_text:
                # Hide the entire widget when streaming ends with no content
                # (e.g. cancelled responses).
                self.display = False
                return
            # Streaming finished — swap to rendered Markdown.
            try:
                self.query_one(".message-body-stream", Static).display = False
                md = self.query_one(".message-body", Markdown)
                md.update(self.body_text or "")
                md.display = True
            except NoMatches:
                log.debug("watch_is_streaming: widget not ready")
            # Collapse thinking now that the response is complete.
            try:
                fold = self.query_one(".thinking-fold", Collapsible)
                if self.thinking_text:
                    fold.collapsed = True
            except NoMatches:
                pass
