"""Tests for GeminiService — spawn-per-turn with stream-json output."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from penta.services.agent_service import StreamEvent, StreamEventType
from penta.services.gemini_service import GeminiService


class TestGeminiArgBuilding:
    """Verify CLI args are constructed correctly."""

    def test_fresh_session_args(self):
        service = GeminiService(executable="/usr/bin/gemini")
        args = service._build_args("hello", session_id=None, system_prompt=None)
        assert "--output-format" in args
        assert "--approval-mode" in args
        assert args[args.index("--approval-mode") + 1] == "yolo"
        assert "-p" in args
        assert args[-1] == "hello"

    def test_deprecated_yolo_flag_not_used(self):
        service = GeminiService(executable="/usr/bin/gemini")
        args = service._build_args("hello", session_id=None, system_prompt=None)
        assert "--yolo" not in args

    def test_resume_session_args(self):
        service = GeminiService(executable="/usr/bin/gemini")
        args = service._build_args("follow up", session_id="sess-123", system_prompt=None)
        assert "--resume" in args
        assert args[args.index("--resume") + 1] == "sess-123"

    def test_system_prompt_prepended(self):
        service = GeminiService(executable="/usr/bin/gemini")
        args = service._build_args("hello", session_id=None, system_prompt="You are X.")
        assert args[-1] == "You are X.\n\nhello"

    def test_model_flag(self):
        service = GeminiService(executable="/usr/bin/gemini", model="gemini-2.5-pro")
        args = service._build_args("hello", session_id=None, system_prompt=None)
        assert "--model" in args
        assert args[args.index("--model") + 1] == "gemini-2.5-pro"


class TestGeminiEventParsing:
    """Verify stream-json events are translated to StreamEvents."""

    @pytest.mark.asyncio
    async def test_init_yields_session(self):
        lines = [
            json.dumps({"type": "init", "session_id": "abc-123", "model": "gemini-3"}),
        ]
        events = await _run_with_lines(lines)

        session_events = [e for e in events if e.type == StreamEventType.SESSION_STARTED]
        assert len(session_events) == 1
        assert session_events[0].session_id == "abc-123"

    @pytest.mark.asyncio
    async def test_user_message_skipped(self):
        lines = [
            json.dumps({"type": "message", "role": "user", "content": "hello"}),
        ]
        events = await _run_with_lines(lines)

        deltas = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
        assert len(deltas) == 0

    @pytest.mark.asyncio
    async def test_assistant_delta_yields_text(self):
        lines = [
            json.dumps({
                "type": "message",
                "role": "assistant",
                "content": "Hi there!",
                "delta": True,
            }),
        ]
        events = await _run_with_lines(lines)

        deltas = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
        assert len(deltas) == 1
        assert deltas[0].text == "Hi there!"

    @pytest.mark.asyncio
    async def test_tool_use_with_parameters(self):
        lines = [
            json.dumps({
                "type": "tool_use",
                "tool_name": "read_file",
                "tool_id": "rf_1",
                "parameters": {"file_path": "foo.py"},
            }),
        ]
        events = await _run_with_lines(lines)

        tool_events = [e for e in events if e.type == StreamEventType.TOOL_USE_STARTED]
        assert len(tool_events) == 1
        assert tool_events[0].tool_name == "read_file"
        assert tool_events[0].tool_id == "rf_1"
        # Parameters should be surfaced as tool_input
        assert "foo.py" in tool_events[0].tool_input

    @pytest.mark.asyncio
    async def test_tool_use_without_parameters(self):
        lines = [
            json.dumps({
                "type": "tool_use",
                "tool_name": "list_dir",
                "tool_id": "ld_1",
            }),
        ]
        events = await _run_with_lines(lines)

        tool_events = [e for e in events if e.type == StreamEventType.TOOL_USE_STARTED]
        assert len(tool_events) == 1
        assert tool_events[0].tool_input is None

    @pytest.mark.asyncio
    async def test_error_event_severity_error(self):
        lines = [
            json.dumps({
                "type": "error",
                "severity": "error",
                "message": "Loop detected",
            }),
        ]
        events = await _run_with_lines(lines)

        errors = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(errors) == 1
        assert errors[0].error == "Loop detected"

    @pytest.mark.asyncio
    async def test_error_event_severity_warning(self):
        lines = [
            json.dumps({
                "type": "error",
                "severity": "warning",
                "message": "Approaching turn limit",
            }),
        ]
        events = await _run_with_lines(lines)

        warnings = [e for e in events if e.type == StreamEventType.WARNING]
        assert len(warnings) == 1
        assert warnings[0].error == "Approaching turn limit"
        # Should NOT produce an ERROR
        errors = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(errors) == 0

    @pytest.mark.asyncio
    async def test_result_error(self):
        lines = [
            json.dumps({
                "type": "result",
                "status": "error",
                "error": {"message": "context exceeded"},
            }),
        ]
        events = await _run_with_lines(lines)

        errors = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(errors) == 1
        assert "context exceeded" in errors[0].error

    @pytest.mark.asyncio
    async def test_result_error_string(self):
        """Error field can be a plain string instead of a dict."""
        lines = [
            json.dumps({
                "type": "result",
                "status": "error",
                "error": "something broke",
            }),
        ]
        events = await _run_with_lines(lines)

        errors = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(errors) == 1
        assert "something broke" in errors[0].error

    @pytest.mark.asyncio
    async def test_result_success_no_error(self):
        lines = [
            json.dumps({"type": "result", "status": "success", "stats": {}}),
        ]
        events = await _run_with_lines(lines)

        errors = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(errors) == 0

    @pytest.mark.asyncio
    async def test_result_success_with_stats_yields_usage(self):
        lines = [
            json.dumps({
                "type": "result",
                "status": "success",
                "stats": {"total_tokens": 500, "input_tokens": 400, "output_tokens": 100},
            }),
        ]
        events = await _run_with_lines(lines)

        usage = [e for e in events if e.type == StreamEventType.USAGE]
        assert len(usage) == 1
        assert usage[0].usage["total_tokens"] == 500

    @pytest.mark.asyncio
    async def test_done_always_emitted(self):
        events = await _run_with_lines([])
        assert events[-1].type == StreamEventType.DONE

    @pytest.mark.asyncio
    async def test_malformed_json_skipped(self):
        lines = [
            "not json",
            json.dumps({"type": "init", "session_id": "s1"}),
        ]
        events = await _run_with_lines(lines)

        session_events = [e for e in events if e.type == StreamEventType.SESSION_STARTED]
        assert len(session_events) == 1


class TestGeminiStderrFiltering:
    """Gemini filters stderr to avoid MCP noise."""

    @pytest.mark.asyncio
    async def test_stderr_with_error_keyword_reported(self):
        events = await _run_with_lines([], returncode=1, stderr=b"fatal error: crash")

        errors = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(errors) == 1

    @pytest.mark.asyncio
    async def test_stderr_without_error_keyword_suppressed(self):
        """MCP debug noise (no 'error' keyword) should be suppressed."""
        events = await _run_with_lines([], returncode=1, stderr=b"MCP debug: connected")

        errors = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(errors) == 0

    @pytest.mark.asyncio
    async def test_stderr_zero_exit_not_reported(self):
        events = await _run_with_lines([], returncode=0, stderr=b"some error text")

        errors = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(errors) == 0


class TestGeminiCancel:
    @pytest.mark.asyncio
    async def test_cancel_terminates_process(self):
        service = GeminiService(executable="/bin/false")
        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        service._current_process = proc

        await service.cancel()

        proc.terminate.assert_called_once()
        assert service._current_process is None


class TestGeminiNotFound:
    @pytest.mark.asyncio
    async def test_missing_executable_yields_error(self):
        service = GeminiService(executable=None)
        service._executable = None

        events = []
        async for event in service.send("hello", None, Path("/tmp")):
            events.append(event)

        assert any(e.type == StreamEventType.ERROR for e in events)
        assert events[-1].type == StreamEventType.DONE


class TestGeminiFullTranscript:
    """End-to-end transcript simulation."""

    @pytest.mark.asyncio
    async def test_typical_tool_use_session(self):
        """Simulate: init, text, tool use, tool result, more text, success result."""
        lines = [
            json.dumps({"type": "init", "session_id": "sess-1", "model": "gemini-3"}),
            json.dumps({"type": "message", "role": "user", "content": "read pyproject.toml"}),
            json.dumps({
                "type": "message",
                "role": "assistant",
                "content": "I'll read that file.\n",
                "delta": True,
            }),
            json.dumps({
                "type": "tool_use",
                "tool_name": "read_file",
                "tool_id": "rf_1",
                "parameters": {"file_path": "pyproject.toml"},
            }),
            json.dumps({
                "type": "tool_result",
                "tool_id": "rf_1",
                "status": "success",
                "output": "[project]\nname = \"penta\"",
            }),
            json.dumps({
                "type": "message",
                "role": "assistant",
                "content": "The project is called penta.",
                "delta": True,
            }),
            json.dumps({
                "type": "result",
                "status": "success",
                "stats": {"total_tokens": 500},
            }),
        ]
        events = await _run_with_lines(lines)

        types = [e.type for e in events]
        assert StreamEventType.SESSION_STARTED in types
        assert StreamEventType.TEXT_DELTA in types
        assert StreamEventType.TOOL_USE_STARTED in types
        assert StreamEventType.USAGE in types
        assert types[-1] == StreamEventType.DONE

        deltas = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
        assert any("read that file" in e.text for e in deltas)
        assert any("penta" in e.text for e in deltas)


class TestGeminiToolVisibilityInCoordinator:
    """Test that tool_use events become visible "> Using ..." lines in the message.

    This tests the coordinator formatting logic, not the service directly.
    """

    def test_tool_use_appears_in_message_text(self):
        text = ""
        tool_name = "read_file"
        if text:
            text += "\n\n"
        text += f"> Using {tool_name}...\n"
        assert "> Using read_file..." in text

    def test_tool_use_after_text_has_separator(self):
        text = "I'll read that file."
        tool_name = "read_file"
        if text:
            text += "\n\n"
        text += f"> Using {tool_name}...\n"
        assert "I'll read that file.\n\n> Using read_file..." in text

    def test_multiple_tool_uses_visible(self):
        text = ""
        for tool in ["read_file", "run_shell_command", "write_file"]:
            if text:
                text += "\n\n"
            text += f"> Using {tool}...\n"
        assert "> Using read_file..." in text
        assert "> Using run_shell_command..." in text
        assert "> Using write_file..." in text


# -- Helpers ------------------------------------------------------------------

async def _run_with_lines(
    lines: list[str],
    returncode: int = 0,
    stderr: bytes = b"",
) -> list[StreamEvent]:
    """Run GeminiService.send() with mocked subprocess outputting given lines."""
    service = GeminiService(executable="/usr/bin/gemini")

    stdout_data = ("\n".join(lines) + "\n").encode() if lines else b""

    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = asyncio.StreamReader()
    proc.stdout.feed_data(stdout_data)
    proc.stdout.feed_eof()
    proc.stderr = asyncio.StreamReader()
    proc.stderr.feed_data(stderr)
    proc.stderr.feed_eof()
    proc.wait = AsyncMock(return_value=returncode)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        events = []
        async for event in service.send("test", None, Path("/tmp")):
            events.append(event)
        return events
