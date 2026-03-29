"""Tests for CodexService — spawn-per-turn with codex exec --json."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from penta.services.agent_service import StreamEvent, StreamEventType
from penta.services.codex_service import CodexService


class TestCodexArgBuilding:
    """Verify CLI args are constructed correctly."""

    def test_fresh_session_args(self):
        service = CodexService(executable="/usr/bin/codex")
        args = service._build_args("hello world", session_id=None, system_prompt=None)
        assert args == [
            "--dangerously-bypass-approvals-and-sandbox",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "hello world",
        ]

    def test_resume_session_args(self):
        service = CodexService(executable="/usr/bin/codex")
        args = service._build_args("follow up", session_id="thread-123", system_prompt=None)
        assert args == [
            "--dangerously-bypass-approvals-and-sandbox",
            "exec", "resume", "thread-123",
            "--json",
            "--skip-git-repo-check",
            "follow up",
        ]

    def test_system_prompt_prepended(self):
        service = CodexService(executable="/usr/bin/codex")
        args = service._build_args("hello", session_id=None, system_prompt="You are X.")
        assert args[-1] == "You are X.\n\nhello"

    def test_model_flag(self):
        service = CodexService(executable="/usr/bin/codex", model="o3")
        args = service._build_args("hello", session_id=None, system_prompt=None)
        assert "--model" in args
        assert args[args.index("--model") + 1] == "o3"


class TestCodexEventParsing:
    """Verify JSON Lines events are translated to StreamEvents."""

    @pytest.mark.asyncio
    async def test_thread_started_yields_session(self):
        """thread.started event should emit SESSION_STARTED."""
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "thr_abc"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        events = await _run_with_lines(lines)

        session_events = [e for e in events if e.type == StreamEventType.SESSION_STARTED]
        assert len(session_events) == 1
        assert session_events[0].session_id == "thr_abc"

    @pytest.mark.asyncio
    async def test_agent_message_yields_text_complete(self):
        """item.completed with agent_message should emit TEXT_COMPLETE."""
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "thr_1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "Hello!"},
            }),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        events = await _run_with_lines(lines)

        text_events = [e for e in events if e.type == StreamEventType.TEXT_COMPLETE]
        assert len(text_events) == 1
        assert text_events[0].text == "Hello!"

    @pytest.mark.asyncio
    async def test_command_execution_yields_tool_use(self):
        """item.started with command_execution should emit TOOL_USE_STARTED."""
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "thr_1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({
                "type": "item.started",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": "/bin/zsh -lc ls",
                    "status": "in_progress",
                },
            }),
            json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": "/bin/zsh -lc ls",
                    "exit_code": 0,
                    "status": "completed",
                },
            }),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        events = await _run_with_lines(lines)

        tool_events = [e for e in events if e.type == StreamEventType.TOOL_USE_STARTED]
        assert len(tool_events) == 1
        assert tool_events[0].tool_name == "/bin/zsh -lc ls"
        assert tool_events[0].tool_id == "item_1"

    @pytest.mark.asyncio
    async def test_error_event_yields_error(self):
        """error events should emit ERROR."""
        lines = [
            json.dumps({"type": "error", "message": "something broke"}),
        ]
        events = await _run_with_lines(lines)

        error_events = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(error_events) == 1
        assert error_events[0].error == "something broke"

    @pytest.mark.asyncio
    async def test_done_always_emitted(self):
        """DONE should always be the last event."""
        lines = [
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        events = await _run_with_lines(lines)

        assert events[-1].type == StreamEventType.DONE

    @pytest.mark.asyncio
    async def test_malformed_json_skipped(self):
        """Non-JSON lines should be silently skipped."""
        lines = [
            "this is not json",
            json.dumps({"type": "thread.started", "thread_id": "thr_1"}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        events = await _run_with_lines(lines)

        session_events = [e for e in events if e.type == StreamEventType.SESSION_STARTED]
        assert len(session_events) == 1


    @pytest.mark.asyncio
    async def test_web_search_yields_tool_use(self):
        """item.started with web_search should emit TOOL_USE_STARTED."""
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "thr_1"}),
            json.dumps({
                "type": "item.started",
                "item": {"id": "ws_1", "type": "web_search", "query": "python asyncio"},
            }),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        events = await _run_with_lines(lines)

        tool_events = [e for e in events if e.type == StreamEventType.TOOL_USE_STARTED]
        assert len(tool_events) == 1
        assert "python asyncio" in tool_events[0].tool_name

    @pytest.mark.asyncio
    async def test_todo_list_yields_thinking(self):
        """item.completed with todo_list should emit THINKING with checklist."""
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "thr_1"}),
            json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "td_1",
                    "type": "todo_list",
                    "items": [
                        {"text": "Read file", "completed": True},
                        {"text": "Write test", "completed": False},
                    ],
                },
            }),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        events = await _run_with_lines(lines)

        thinking_events = [e for e in events if e.type == StreamEventType.THINKING]
        assert len(thinking_events) == 1
        assert "[x] Read file" in thinking_events[0].text
        assert "[ ] Write test" in thinking_events[0].text

    @pytest.mark.asyncio
    async def test_reasoning_yields_thinking(self):
        """item.completed with reasoning should emit THINKING."""
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "thr_1"}),
            json.dumps({
                "type": "item.completed",
                "item": {"id": "r_1", "type": "reasoning", "text": "Let me think..."},
            }),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        events = await _run_with_lines(lines)

        thinking_events = [e for e in events if e.type == StreamEventType.THINKING]
        assert len(thinking_events) == 1
        assert thinking_events[0].text == "Let me think..."

    @pytest.mark.asyncio
    async def test_file_change_yields_tool_use(self):
        """item.started with file_change should emit TOOL_USE_STARTED."""
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "thr_1"}),
            json.dumps({
                "type": "item.started",
                "item": {
                    "id": "fc_1",
                    "type": "file_change",
                    "changes": [
                        {"path": "src/main.py", "kind": "update"},
                        {"path": "src/util.py", "kind": "add"},
                    ],
                },
            }),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        events = await _run_with_lines(lines)

        tool_events = [e for e in events if e.type == StreamEventType.TOOL_USE_STARTED]
        assert len(tool_events) == 1
        assert "update src/main.py" in tool_events[0].tool_name
        assert "add src/util.py" in tool_events[0].tool_name

    @pytest.mark.asyncio
    async def test_turn_failed_yields_error(self):
        """turn.failed should emit ERROR."""
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "thr_1"}),
            json.dumps({
                "type": "turn.failed",
                "error": {"message": "context window exceeded"},
            }),
        ]
        events = await _run_with_lines(lines)

        error_events = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(error_events) == 1
        assert "context window exceeded" in error_events[0].error

    @pytest.mark.asyncio
    async def test_turn_completed_yields_usage(self):
        """turn.completed with usage should emit USAGE."""
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "thr_1"}),
            json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }),
        ]
        events = await _run_with_lines(lines)

        usage_events = [e for e in events if e.type == StreamEventType.USAGE]
        assert len(usage_events) == 1
        assert usage_events[0].usage["input_tokens"] == 100


class TestCodexCancel:
    """Verify cancel kills the process."""

    @pytest.mark.asyncio
    async def test_cancel_terminates_process(self):
        service = CodexService(executable="/bin/false")
        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        service._current_process = proc

        await service.cancel()

        proc.terminate.assert_called_once()
        assert service._current_process is None

    @pytest.mark.asyncio
    async def test_cancel_noop_without_process(self):
        service = CodexService(executable="/bin/false")
        await service.cancel()  # should not raise


class TestCodexNotFound:
    """Verify missing executable is handled."""

    @pytest.mark.asyncio
    async def test_missing_executable_yields_error(self):
        service = CodexService(executable=None)
        # Force executable to None (find_executable may return something)
        service._executable = None

        events = []
        async for event in service.send("hello", None, Path("/tmp")):
            events.append(event)

        assert any(e.type == StreamEventType.ERROR for e in events)
        assert events[-1].type == StreamEventType.DONE


class TestCodexStderrHandling:
    """Verify stderr from failed processes surfaces as errors."""

    @pytest.mark.asyncio
    async def test_nonzero_exit_with_stderr_yields_error(self):
        lines = [
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        events = await _run_with_lines(lines, returncode=1, stderr=b"fatal: bad config")

        error_events = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(error_events) == 1
        assert "fatal: bad config" in error_events[0].error


# -- Helpers ------------------------------------------------------------------

async def _run_with_lines(
    lines: list[str],
    returncode: int = 0,
    stderr: bytes = b"",
) -> list[StreamEvent]:
    """Run CodexService.send() with mocked subprocess outputting given lines."""
    service = CodexService(executable="/usr/bin/codex")

    stdout_data = ("\n".join(lines) + "\n").encode()

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
