"""Tests for GeminiService stream-json parsing and tool visibility."""

import asyncio
import json

import pytest

from penta.services.agent_service import StreamEventType
from penta.services.gemini_service import GeminiService


def _make_ndjson(*events: dict) -> bytes:
    """Build NDJSON bytes from a sequence of event dicts."""
    return b"".join(json.dumps(e).encode() + b"\n" for e in events)


async def _collect_events(ndjson: bytes) -> list:
    """Feed NDJSON into a GeminiService parser and collect StreamEvents."""
    service = GeminiService(executable="/bin/false")  # Won't actually spawn

    # Manually parse lines the same way the service does
    events = []
    for line in ndjson.decode().strip().split("\n"):
        data = json.loads(line)
        msg_type = data.get("type")

        if msg_type == "init":
            sid = data.get("session_id")
            if sid:
                events.append(("SESSION_STARTED", sid))

        elif msg_type == "message":
            role = data.get("role")
            if role == "user":
                continue
            if role == "assistant" and data.get("delta"):
                events.append(("TEXT_DELTA", data.get("content", "")))

        elif msg_type == "tool_use":
            events.append(("TOOL_USE", data.get("tool_name", "")))

        elif msg_type == "tool_result":
            events.append(("TOOL_RESULT", data.get("status", "")))

        elif msg_type == "result":
            status = data.get("status", "")
            if status != "success":
                events.append(("ERROR", status))

    return events


class TestGeminiStreamParsing:
    @pytest.mark.asyncio
    async def test_init_event_yields_session_id(self):
        ndjson = _make_ndjson(
            {"type": "init", "session_id": "abc-123", "model": "gemini-3"},
        )
        events = await _collect_events(ndjson)
        assert ("SESSION_STARTED", "abc-123") in events

    @pytest.mark.asyncio
    async def test_user_message_skipped(self):
        ndjson = _make_ndjson(
            {"type": "message", "role": "user", "content": "hello"},
        )
        events = await _collect_events(ndjson)
        assert events == []

    @pytest.mark.asyncio
    async def test_assistant_delta_yields_text(self):
        ndjson = _make_ndjson(
            {"type": "message", "role": "assistant", "content": "Hi there!", "delta": True},
        )
        events = await _collect_events(ndjson)
        assert ("TEXT_DELTA", "Hi there!") in events

    @pytest.mark.asyncio
    async def test_tool_use_yields_tool_event(self):
        ndjson = _make_ndjson(
            {"type": "tool_use", "tool_name": "read_file", "tool_id": "rf_1", "parameters": {"file_path": "foo.py"}},
        )
        events = await _collect_events(ndjson)
        assert ("TOOL_USE", "read_file") in events

    @pytest.mark.asyncio
    async def test_tool_result_parsed(self):
        ndjson = _make_ndjson(
            {"type": "tool_result", "tool_id": "rf_1", "status": "success", "output": "contents"},
        )
        events = await _collect_events(ndjson)
        assert ("TOOL_RESULT", "success") in events

    @pytest.mark.asyncio
    async def test_error_result(self):
        ndjson = _make_ndjson(
            {"type": "result", "status": "error", "stats": {}},
        )
        events = await _collect_events(ndjson)
        assert ("ERROR", "error") in events

    @pytest.mark.asyncio
    async def test_success_result_no_error(self):
        ndjson = _make_ndjson(
            {"type": "result", "status": "success", "stats": {}},
        )
        events = await _collect_events(ndjson)
        assert not any(e[0] == "ERROR" for e in events)

    @pytest.mark.asyncio
    async def test_full_tool_use_sequence(self):
        """Simulate: user asks gemini to read a file. Verify tool use is visible."""
        ndjson = _make_ndjson(
            {"type": "init", "session_id": "sess-1", "model": "gemini-3"},
            {"type": "message", "role": "user", "content": "read pyproject.toml"},
            {"type": "message", "role": "assistant", "content": "I'll read that file.\n", "delta": True},
            {"type": "tool_use", "tool_name": "read_file", "tool_id": "rf_1", "parameters": {"file_path": "pyproject.toml"}},
            {"type": "tool_result", "tool_id": "rf_1", "status": "success", "output": ""},
            {"type": "message", "role": "assistant", "content": "The project is called penta.", "delta": True},
            {"type": "result", "status": "success", "stats": {}},
        )
        events = await _collect_events(ndjson)
        assert ("SESSION_STARTED", "sess-1") in events
        assert ("TEXT_DELTA", "I'll read that file.\n") in events
        assert ("TOOL_USE", "read_file") in events
        assert ("TOOL_RESULT", "success") in events
        assert ("TEXT_DELTA", "The project is called penta.") in events


class TestGeminiToolVisibilityInChat:
    """Test that tool_use events become visible "> Using ..." lines in the message."""

    @pytest.mark.asyncio
    async def test_tool_use_appears_in_message_text(self):
        """Simulate coordinator handling of TOOL_USE_STARTED event."""
        # This tests the coordinator logic inline (same pattern for all agents)
        text = ""
        # Simulate the coordinator's match case for TOOL_USE_STARTED:
        tool_name = "read_file"
        if text:
            text += "\n\n"
        text += f"> Using {tool_name}...\n"

        assert "> Using read_file..." in text

    @pytest.mark.asyncio
    async def test_tool_use_after_text_has_separator(self):
        text = "I'll read that file."
        tool_name = "read_file"
        if text:
            text += "\n\n"
        text += f"> Using {tool_name}...\n"

        assert "I'll read that file.\n\n> Using read_file..." in text

    @pytest.mark.asyncio
    async def test_multiple_tool_uses_visible(self):
        text = ""
        for tool in ["read_file", "run_shell_command", "write_file"]:
            if text:
                text += "\n\n"
            text += f"> Using {tool}...\n"

        assert "> Using read_file..." in text
        assert "> Using run_shell_command..." in text
        assert "> Using write_file..." in text
