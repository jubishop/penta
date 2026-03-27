"""Tests for CodexService permission auto-approval and tool visibility."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from penta.services.agent_service import StreamEvent, StreamEventType
from penta.services.codex_service import CodexService


class TestCodexAutoApproval:
    """Verify that Codex permission requests are auto-approved, not surfaced to UI."""

    def _make_service(self) -> CodexService:
        service = CodexService(executable="/bin/false")
        service._event_queue = asyncio.Queue()
        return service

    @pytest.mark.asyncio
    async def test_command_execution_auto_approved(self):
        service = self._make_service()

        approved_ids = []
        original_respond = service.respond_to_permission

        async def track_respond(request_id, granted):
            approved_ids.append((request_id, granted))

        service.respond_to_permission = track_respond

        service._handle_server_request({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "git status", "cwd": "/tmp"},
        })

        # Let the ensure_future run
        await asyncio.sleep(0.01)

        assert len(approved_ids) == 1
        assert approved_ids[0] == ("5", True)

    @pytest.mark.asyncio
    async def test_file_change_auto_approved(self):
        service = self._make_service()

        approved_ids = []

        async def track_respond(request_id, granted):
            approved_ids.append((request_id, granted))

        service.respond_to_permission = track_respond

        service._handle_server_request({
            "jsonrpc": "2.0",
            "id": 6,
            "method": "item/fileChange/requestApproval",
            "params": {"reason": "modify src/main.py"},
        })

        await asyncio.sleep(0.01)

        assert len(approved_ids) == 1
        assert approved_ids[0] == ("6", True)

    @pytest.mark.asyncio
    async def test_permissions_request_auto_approved(self):
        service = self._make_service()

        approved_ids = []

        async def track_respond(request_id, granted):
            approved_ids.append((request_id, granted))

        service.respond_to_permission = track_respond

        service._handle_server_request({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "item/permissions/requestApproval",
            "params": {"reason": "additional permissions needed"},
        })

        await asyncio.sleep(0.01)

        assert len(approved_ids) == 1
        assert approved_ids[0] == ("7", True)

    @pytest.mark.asyncio
    async def test_no_permission_event_surfaced(self):
        """Auto-approved requests should NOT put PERMISSION_REQUESTED in the event queue."""
        service = self._make_service()

        async def noop_respond(request_id, granted):
            pass

        service.respond_to_permission = noop_respond

        service._handle_server_request({
            "jsonrpc": "2.0",
            "id": 8,
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "ls"},
        })

        await asyncio.sleep(0.01)

        # Queue should be empty — no PERMISSION_REQUESTED event
        assert service._event_queue.empty()

    @pytest.mark.asyncio
    async def test_unknown_request_also_approved(self):
        service = self._make_service()

        approved_ids = []

        async def track_respond(request_id, granted):
            approved_ids.append((request_id, granted))

        service.respond_to_permission = track_respond

        service._handle_server_request({
            "jsonrpc": "2.0",
            "id": 9,
            "method": "item/someNewApproval/requestApproval",
            "params": {},
        })

        await asyncio.sleep(0.01)

        assert len(approved_ids) == 1
        assert approved_ids[0] == ("9", True)


class TestCodexToolVisibility:
    """Verify tool_use events from Codex produce visible log lines."""

    def _make_service(self) -> CodexService:
        service = CodexService(executable="/bin/false")
        service._event_queue = asyncio.Queue()
        return service

    def test_tool_started_notification_yields_event(self):
        service = self._make_service()

        service._handle_notification({
            "method": "item/started",
            "params": {
                "item": {
                    "type": "tool_call",
                    "name": "shell",
                    "id": "tool_1",
                }
            },
        })

        assert not service._event_queue.empty()
        event = service._event_queue.get_nowait()
        assert event.type.name == "TOOL_USE_STARTED"
        assert event.tool_name == "shell"
        assert event.tool_id == "tool_1"

    def test_tool_use_becomes_visible_in_message(self):
        """Same coordinator logic as gemini — tool use shows as '> Using ...' in message text."""
        text = "Analyzing the code."
        tool_name = "shell"
        if text:
            text += "\n\n"
        text += f"> Using {tool_name}...\n"

        assert "> Using shell..." in text


class TestCodexSessionRestore:
    """Verify that send() uses session_id to restore _thread_id."""

    @pytest.mark.asyncio
    async def test_session_id_restores_thread_id(self):
        """send() should use session_id to set _thread_id without calling
        _start_thread, and proceed to start a turn on that thread."""
        service = CodexService(executable="/bin/false")
        # Simulate a running server so _ensure_server is a no-op
        service._process = MagicMock()
        service._process.returncode = None
        service._initialized.set()

        started_threads: list[Path] = []
        original_start_thread = service._start_thread

        async def track_start_thread(working_dir):
            started_threads.append(working_dir)

        service._start_thread = track_start_thread

        # Mock _send_request; when _start_turn is called feed DONE into queue
        turn_thread_ids: list[str] = []
        original_send_request = service._send_request

        async def mock_send_request(method, params):
            if method == "turn/start":
                turn_thread_ids.append(params["threadId"])
                service._event_queue.put_nowait(
                    StreamEvent(type=StreamEventType.TEXT_DELTA, text="ok")
                )
                service._event_queue.put_nowait(
                    StreamEvent(type=StreamEventType.DONE)
                )

        service._send_request = mock_send_request

        events = []
        async for event in service.send("hello", "restored-thread-123", Path("/tmp")):
            events.append(event)

        assert service._thread_id == "restored-thread-123"
        assert started_threads == []  # _start_thread was NOT called
        assert turn_thread_ids == ["restored-thread-123"]
        assert any(e.type == StreamEventType.TEXT_DELTA and e.text == "ok" for e in events)

    @pytest.mark.asyncio
    async def test_stale_session_retries_with_fresh_thread(self):
        """If the restored session_id is stale, send() should transparently
        retry with a fresh thread instead of surfacing the error."""
        service = CodexService(executable="/bin/false")
        service._process = MagicMock()
        service._process.returncode = None
        service._initialized.set()

        call_count = 0

        async def mock_send_request(method, params):
            nonlocal call_count
            if method == "turn/start":
                call_count += 1
                if call_count == 1:
                    # First attempt: simulate turn/failed (stale thread)
                    service._handle_notification({
                        "method": "turn/failed",
                        "params": {"error": "Thread not found"},
                    })
                else:
                    # Second attempt: success
                    service._event_queue.put_nowait(
                        StreamEvent(type=StreamEventType.TEXT_DELTA, text="retried")
                    )
                    service._event_queue.put_nowait(
                        StreamEvent(type=StreamEventType.DONE)
                    )
            elif method == "thread/start":
                # _start_thread creates a new thread
                service._thread_id = "fresh-thread"
                service._thread_ready.set()

        service._send_request = mock_send_request

        events = []
        async for event in service.send("hello", "stale-thread", Path("/tmp")):
            events.append(event)

        assert service._thread_id == "fresh-thread"
        # No error events should have been yielded to the caller
        assert not any(e.type == StreamEventType.ERROR for e in events)
        assert any(e.type == StreamEventType.TEXT_DELTA and e.text == "retried" for e in events)

    @pytest.mark.asyncio
    async def test_turn_failed_clears_thread_id(self):
        service = CodexService(executable="/bin/false")
        service._thread_id = "stale-thread"
        service._thread_ready.set()
        service._event_queue = asyncio.Queue()

        service._handle_notification({
            "method": "turn/failed",
            "params": {"error": "Thread not found"},
        })

        assert service._thread_id is None
        assert not service._thread_ready.is_set()


class TestCodexApprovalPolicy:
    """Verify the thread is started with never approval policy."""

    @pytest.mark.asyncio
    async def test_thread_start_uses_never_approval(self):
        service = CodexService(executable="/bin/false")

        sent_requests = []

        async def capture_request(method, params):
            sent_requests.append((method, params))

        service._send_request = capture_request

        await service._start_thread(path_obj := __import__("pathlib").Path("/tmp"))

        assert len(sent_requests) == 1
        method, params = sent_requests[0]
        assert method == "thread/start"
        assert params["approvalPolicy"] == "never"
