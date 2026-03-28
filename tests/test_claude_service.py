"""Tests for ClaudeService — spawn-per-turn with stream-json output."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from penta.services.agent_service import StreamEvent, StreamEventType
from penta.services.claude_service import ClaudeService


class TestClaudeArgBuilding:
    """Verify CLI args are constructed correctly."""

    def test_fresh_session_args(self):
        service = ClaudeService(executable="/usr/bin/claude")
        args = service._build_args("hello", session_id=None, system_prompt=None)
        assert "-p" in args
        assert "--verbose" in args
        assert "--output-format" in args
        assert "--include-partial-messages" in args
        assert args[-1] == "hello"

    def test_resume_session_args(self):
        service = ClaudeService(executable="/usr/bin/claude")
        args = service._build_args("follow up", session_id="sess-123", system_prompt=None)
        assert "--resume" in args
        assert args[args.index("--resume") + 1] == "sess-123"

    def test_system_prompt_uses_append_flag(self):
        service = ClaudeService(executable="/usr/bin/claude")
        args = service._build_args("hello", session_id=None, system_prompt="You are X.")
        assert "--append-system-prompt" in args
        assert args[args.index("--append-system-prompt") + 1] == "You are X."
        # System prompt should NOT be prepended to the user prompt
        assert args[-1] == "hello"

    def test_model_flag(self):
        service = ClaudeService(executable="/usr/bin/claude", model="opus")
        args = service._build_args("hello", session_id=None, system_prompt=None)
        assert "--model" in args
        assert args[args.index("--model") + 1] == "opus"

    def test_permission_server_settings(self):
        server = MagicMock()
        server.hook_settings_json = '{"hooks": {}}'
        service = ClaudeService(executable="/usr/bin/claude", permission_server=server)
        args = service._build_args("hello", session_id=None, system_prompt=None)
        assert "--settings" in args
        assert args[args.index("--settings") + 1] == '{"hooks": {}}'


class TestClaudeEventParsing:
    """Verify stream-json events are translated to StreamEvents."""

    @pytest.mark.asyncio
    async def test_system_init_yields_session(self):
        lines = [
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-abc"}),
            json.dumps({"type": "result", "result": "", "session_id": "sess-abc"}),
        ]
        events = await _run_with_lines(lines)

        session_events = [e for e in events if e.type == StreamEventType.SESSION_STARTED]
        # Should NOT duplicate — init captures it, result should skip
        assert len(session_events) == 1
        assert session_events[0].session_id == "sess-abc"

    @pytest.mark.asyncio
    async def test_session_from_result_when_no_init(self):
        """If system.init is missing, capture session_id from result."""
        lines = [
            json.dumps({"type": "result", "result": "done", "session_id": "sess-xyz"}),
        ]
        events = await _run_with_lines(lines)

        session_events = [e for e in events if e.type == StreamEventType.SESSION_STARTED]
        assert len(session_events) == 1
        assert session_events[0].session_id == "sess-xyz"

    @pytest.mark.asyncio
    async def test_text_delta_from_content_block(self):
        lines = [
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello "},
                },
            }),
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "world!"},
                },
            }),
        ]
        events = await _run_with_lines(lines)

        deltas = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
        assert len(deltas) == 2
        assert deltas[0].text == "Hello "
        assert deltas[1].text == "world!"

    @pytest.mark.asyncio
    async def test_tool_use_started(self):
        lines = [
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "content_block": {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Read",
                    },
                },
            }),
        ]
        events = await _run_with_lines(lines)

        tool_events = [e for e in events if e.type == StreamEventType.TOOL_USE_STARTED]
        assert len(tool_events) == 1
        assert tool_events[0].tool_name == "Read"
        assert tool_events[0].tool_id == "tu_1"

    @pytest.mark.asyncio
    async def test_text_block_separator(self):
        """Consecutive text blocks should get \\n\\n separator."""
        lines = [
            # First text block
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "first"},
                },
            }),
            # Second text block start (not tool_use)
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "content_block": {"type": "text"},
                },
            }),
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "second"},
                },
            }),
        ]
        events = await _run_with_lines(lines)

        deltas = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
        texts = [e.text for e in deltas]
        assert texts == ["first", "\n\n", "second"]

    @pytest.mark.asyncio
    async def test_no_separator_before_tool_use(self):
        """Tool use blocks should NOT get an extra \\n\\n from the service."""
        lines = [
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "thinking..."},
                },
            }),
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "content_block": {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Bash",
                    },
                },
            }),
        ]
        events = await _run_with_lines(lines)

        # Should have text delta + tool use, but NO separator delta
        types = [e.type for e in events if e.type != StreamEventType.DONE]
        assert StreamEventType.TEXT_DELTA in types
        assert StreamEventType.TOOL_USE_STARTED in types
        deltas = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
        assert len(deltas) == 1  # Only "thinking...", no "\n\n"

    @pytest.mark.asyncio
    async def test_result_error(self):
        lines = [
            json.dumps({"type": "result", "result": "rate limited", "is_error": True}),
        ]
        events = await _run_with_lines(lines)

        errors = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(errors) == 1
        assert errors[0].error == "rate limited"

    @pytest.mark.asyncio
    async def test_result_text_complete(self):
        lines = [
            json.dumps({"type": "result", "result": "Summary of work done."}),
        ]
        events = await _run_with_lines(lines)

        complete = [e for e in events if e.type == StreamEventType.TEXT_COMPLETE]
        assert len(complete) == 1
        assert complete[0].text == "Summary of work done."

    @pytest.mark.asyncio
    async def test_api_retry_yields_warning(self):
        lines = [
            json.dumps({
                "type": "system",
                "subtype": "api_retry",
                "attempt": 2,
                "retry_delay_ms": 1000,
                "error": "overloaded",
            }),
        ]
        events = await _run_with_lines(lines)

        warnings = [e for e in events if e.type == StreamEventType.WARNING]
        assert len(warnings) == 1
        assert "attempt 2" in warnings[0].error

    @pytest.mark.asyncio
    async def test_thinking_delta(self):
        lines = [
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "Let me consider..."},
                },
            }),
        ]
        events = await _run_with_lines(lines)

        thinking = [e for e in events if e.type == StreamEventType.THINKING]
        assert len(thinking) == 1
        assert thinking[0].text == "Let me consider..."

    @pytest.mark.asyncio
    async def test_rate_limit_warning(self):
        lines = [
            json.dumps({"type": "rate_limit_event", "status": "warning"}),
        ]
        events = await _run_with_lines(lines)

        warnings = [e for e in events if e.type == StreamEventType.WARNING]
        assert len(warnings) == 1
        assert "Rate limited" in warnings[0].error

    @pytest.mark.asyncio
    async def test_usage_from_result(self):
        lines = [
            json.dumps({
                "type": "result",
                "result": "",
                "cost_usd": 0.05,
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "duration_ms": 3000,
                "num_turns": 2,
            }),
        ]
        events = await _run_with_lines(lines)

        usage_events = [e for e in events if e.type == StreamEventType.USAGE]
        assert len(usage_events) == 1
        assert usage_events[0].usage["cost_usd"] == 0.05
        assert usage_events[0].usage["input_tokens"] == 100
        assert usage_events[0].usage["duration_ms"] == 3000

    @pytest.mark.asyncio
    async def test_done_always_emitted(self):
        events = await _run_with_lines([])
        assert events[-1].type == StreamEventType.DONE

    @pytest.mark.asyncio
    async def test_malformed_json_skipped(self):
        lines = [
            "not json",
            json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
        ]
        events = await _run_with_lines(lines)

        session_events = [e for e in events if e.type == StreamEventType.SESSION_STARTED]
        assert len(session_events) == 1

    @pytest.mark.asyncio
    async def test_parse_state_resets_between_sends(self):
        """_seen_session_id and _has_emitted_text should reset per send."""
        service = ClaudeService(executable="/usr/bin/claude")
        # Simulate state from a prior send
        service._seen_session_id = True
        service._has_emitted_text = True

        # After reset, state should be clean
        service._reset_parse_state()
        assert service._seen_session_id is False
        assert service._has_emitted_text is False


class TestClaudeCancel:
    @pytest.mark.asyncio
    async def test_cancel_terminates_process(self):
        service = ClaudeService(executable="/bin/false")
        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        service._current_process = proc

        await service.cancel()

        proc.terminate.assert_called_once()
        assert service._current_process is None


class TestClaudeNotFound:
    @pytest.mark.asyncio
    async def test_missing_executable_yields_error(self):
        service = ClaudeService(executable=None)
        service._executable = None

        events = []
        async for event in service.send("hello", None, Path("/tmp")):
            events.append(event)

        assert any(e.type == StreamEventType.ERROR for e in events)
        assert events[-1].type == StreamEventType.DONE


class TestClaudeStderrHandling:
    @pytest.mark.asyncio
    async def test_nonzero_exit_with_stderr_yields_error(self):
        lines = [
            json.dumps({"type": "result", "result": ""}),
        ]
        events = await _run_with_lines(lines, returncode=1, stderr=b"fatal: auth failed")

        errors = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(errors) == 1
        assert "auth failed" in errors[0].error


class TestClaudeFullTranscript:
    """End-to-end transcript simulation."""

    @pytest.mark.asyncio
    async def test_typical_tool_use_session(self):
        """Simulate: init, text, tool use, more text, result."""
        lines = [
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}),
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Let me check."},
                },
            }),
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "content_block": {"type": "tool_use", "id": "tu_1", "name": "Read"},
                },
            }),
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "content_block": {"type": "text"},
                },
            }),
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Here is what I found."},
                },
            }),
            json.dumps({
                "type": "result",
                "result": "Let me check.\n\nHere is what I found.",
                "session_id": "sess-1",
                "cost_usd": 0.01,
            }),
        ]
        events = await _run_with_lines(lines)

        types = [e.type for e in events]
        assert StreamEventType.SESSION_STARTED in types
        assert StreamEventType.TEXT_DELTA in types
        assert StreamEventType.TOOL_USE_STARTED in types
        assert StreamEventType.TEXT_COMPLETE in types
        assert StreamEventType.USAGE in types
        assert types[-1] == StreamEventType.DONE

        # Only one SESSION_STARTED (from init, not duplicated from result)
        assert types.count(StreamEventType.SESSION_STARTED) == 1


# -- Helpers ------------------------------------------------------------------

async def _run_with_lines(
    lines: list[str],
    returncode: int = 0,
    stderr: bytes = b"",
) -> list[StreamEvent]:
    """Run ClaudeService.send() with mocked subprocess outputting given lines."""
    service = ClaudeService(executable="/usr/bin/claude")

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
