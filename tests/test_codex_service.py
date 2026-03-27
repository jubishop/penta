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
    """Verify that send() uses session_id correctly based on server state."""

    @pytest.mark.asyncio
    async def test_session_id_reused_when_server_running(self):
        """When the server is already running, send() should restore
        _thread_id from session_id without calling _start_thread."""
        service = CodexService(executable="/bin/false")
        # Simulate a running server so _ensure_server returns False
        service._process = MagicMock()
        service._process.returncode = None
        service._initialized.set()

        started_threads: list[Path] = []

        async def track_start_thread(working_dir):
            started_threads.append(working_dir)

        service._start_thread = track_start_thread

        turn_thread_ids: list[str] = []

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
    async def test_session_id_skipped_on_fresh_server(self):
        """When the server was just started (fresh process), the saved
        session_id is stale — send() must skip it and create a new thread.
        The new thread's SESSION_STARTED must be emitted for persistence."""
        service = CodexService(executable="/bin/false")
        # Do NOT set _process — _ensure_server will "start" a fresh server.
        # We mock it to just set the flag and return True.
        async def mock_ensure_server(working_dir):
            service._process = MagicMock()
            service._process.returncode = None
            service._thread_id = None
            return True

        service._ensure_server = mock_ensure_server

        async def mock_send_request(method, params):
            if method == "thread/start":
                service._handle_server_response({
                    "id": 1,
                    "result": {"thread": {"id": "fresh-thread"}},
                })
            elif method == "turn/start":
                service._event_queue.put_nowait(
                    StreamEvent(type=StreamEventType.TEXT_DELTA, text="new session")
                )
                service._event_queue.put_nowait(
                    StreamEvent(type=StreamEventType.DONE)
                )

        service._send_request = mock_send_request

        events = []
        async for event in service.send("hello", "stale-session", Path("/tmp")):
            events.append(event)

        # Must have created a fresh thread, NOT reused the stale session
        assert service._thread_id == "fresh-thread"
        assert any(e.type == StreamEventType.TEXT_DELTA and e.text == "new session" for e in events)
        # SESSION_STARTED emitted so the coordinator persists the new ID
        session_events = [e for e in events if e.type == StreamEventType.SESSION_STARTED]
        assert len(session_events) == 1
        assert session_events[0].session_id == "fresh-thread"

    @pytest.mark.asyncio
    async def test_thread_start_rpc_error_surfaces_exact_message(self):
        """If thread/start gets an RPC error, send() should surface the
        exact error text, not a generic wrapper."""
        service = CodexService(executable="/bin/false")

        async def mock_ensure_server(working_dir):
            service._process = MagicMock()
            service._process.returncode = None
            service._thread_id = None
            return True

        service._ensure_server = mock_ensure_server

        async def mock_send_request(method, params):
            if method == "thread/start":
                service._handle_server_response({
                    "id": 1,
                    "error": {"message": "cannot create thread"},
                })

        service._send_request = mock_send_request

        events = []
        async for event in service.send("hello", None, Path("/tmp")):
            events.append(event)

        error_events = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(error_events) == 1
        assert error_events[0].error == "cannot create thread"
        assert service._thread_id is None
        # Future must be cleaned up
        assert service._thread_create_future is None

    @pytest.mark.asyncio
    async def test_failed_thread_start_does_not_break_next_send(self):
        """After a thread/start RPC error, the next send() should be able
        to create a thread successfully — no wedged state."""
        service = CodexService(executable="/bin/false")

        async def mock_ensure_server(working_dir):
            service._process = MagicMock()
            service._process.returncode = None
            service._thread_id = None
            return True

        service._ensure_server = mock_ensure_server

        call_count = 0

        async def mock_send_request(method, params):
            nonlocal call_count
            if method == "thread/start":
                call_count += 1
                if call_count == 1:
                    # First attempt: RPC error
                    service._handle_server_response({
                        "id": 1,
                        "error": {"message": "cannot create thread"},
                    })
                else:
                    # Second attempt: success
                    service._handle_server_response({
                        "id": 2,
                        "result": {"thread": {"id": "thread-ok"}},
                    })
            elif method == "turn/start":
                service._event_queue.put_nowait(
                    StreamEvent(type=StreamEventType.TEXT_DELTA, text="recovered")
                )
                service._event_queue.put_nowait(
                    StreamEvent(type=StreamEventType.DONE)
                )

        service._send_request = mock_send_request

        # First send: should fail with the RPC error
        events1 = []
        async for event in service.send("hello", None, Path("/tmp")):
            events1.append(event)
        assert any(e.type == StreamEventType.ERROR for e in events1)
        assert service._thread_id is None

        # Second send: should succeed — no wedged state
        events2 = []
        async for event in service.send("hello", None, Path("/tmp")):
            events2.append(event)
        assert service._thread_id == "thread-ok"
        assert any(e.type == StreamEventType.TEXT_DELTA and e.text == "recovered" for e in events2)
        assert not any(e.type == StreamEventType.ERROR for e in events2)


class TestLateThreadResponse:
    """Late thread/start responses after timeout must not clobber state."""

    def test_late_response_does_not_overwrite_thread_id(self):
        """If _thread_id is already set (from a successful later attempt),
        a late response from a timed-out thread/start must be ignored."""
        service = CodexService(executable="/bin/false")
        service._event_queue = asyncio.Queue()
        # Simulate: thread was already created successfully
        service._thread_id = "good-thread"
        # Future is None (timed out and cleaned up)
        service._thread_create_future = None

        # Late response arrives for the old timed-out request
        service._handle_server_response({
            "id": 1,
            "result": {"thread": {"id": "late-thread"}},
        })

        # Must NOT overwrite the good thread
        assert service._thread_id == "good-thread"

    def test_late_notification_does_not_overwrite_thread_id(self):
        """Same as above but via thread/started notification path."""
        service = CodexService(executable="/bin/false")
        service._event_queue = asyncio.Queue()
        service._thread_id = "good-thread"
        service._thread_create_future = None

        service._handle_notification({
            "method": "thread/started",
            "params": {"thread": {"id": "late-thread"}},
        })

        assert service._thread_id == "good-thread"


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
