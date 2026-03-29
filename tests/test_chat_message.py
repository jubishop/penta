"""Tests for ChatMessage widget visibility."""

from __future__ import annotations

from uuid import uuid4

from penta.models import Message, MessageSender
from penta.widgets.chat_message import ChatMessage


def test_historical_message_is_visible():
    """Non-streaming messages with text must not be hidden on creation.

    Regression: Textual 8.x fires reactive watchers during __init__ with
    default values.  watch_is_streaming(False) ran before body_text was set,
    saw empty text, and set display=False — hiding every message.
    """
    msg = Message(sender=MessageSender.user(), text="hello world")
    widget = ChatMessage(msg, "You", None)
    assert widget.display is True


def test_streaming_message_is_visible():
    """In-flight streaming messages must be visible even with empty text."""
    msg = Message(
        sender=MessageSender.agent(uuid4()), text="", is_streaming=True,
    )
    widget = ChatMessage(msg, "Claude", None)
    assert widget.display is True
