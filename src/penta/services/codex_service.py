from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncIterator

from penta.models import AgentType
from penta.services.agent_service import AgentService, StreamEvent, StreamEventType, terminate_process
from penta.services.cli_env import build_cli_env
from penta.services.stream_parser import async_lines

log = logging.getLogger(__name__)


class CodexService(AgentService):
    """Long-lived Codex app-server with JSON-RPC protocol."""

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable or AgentType.CODEX.find_executable()
        self._process: asyncio.subprocess.Process | None = None
        self._thread_id: str | None = None
        self._next_request_id = 1
        self._initialized = asyncio.Event()
        self._thread_create_future: asyncio.Future[str] | None = None
        self._event_queue: asyncio.Queue[StreamEvent] | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None

    async def send(
        self, prompt: str, session_id: str | None, working_dir: Path
    ) -> AsyncIterator[StreamEvent]:
        if not self._executable:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error="Codex CLI not found. Set PENTA_CODEX_PATH or install codex.",
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return

        fresh_server = await self._ensure_server(working_dir)

        if not self._thread_id and session_id and not fresh_server:
            # Server is still running — the saved thread is valid.
            self._thread_id = session_id

        if not self._thread_id:
            # Need a new thread: either the server just started (saved
            # session_id is stale) or there was no prior session at all.
            # Set up the event queue first so SESSION_STARTED is captured.
            self._event_queue = asyncio.Queue()
            try:
                self._thread_id = await self._create_thread(working_dir)
            except asyncio.TimeoutError:
                self._event_queue = None
                yield StreamEvent(
                    type=StreamEventType.ERROR,
                    error="Timed out waiting for Codex thread creation",
                )
                yield StreamEvent(type=StreamEventType.DONE)
                return
            except Exception as e:
                self._event_queue = None
                yield StreamEvent(
                    type=StreamEventType.ERROR, error=str(e),
                )
                yield StreamEvent(type=StreamEventType.DONE)
                return

        if not self._event_queue:
            self._event_queue = asyncio.Queue()
        queue = self._event_queue  # local ref survives handler nulling self._event_queue
        await self._start_turn(self._thread_id, prompt)

        while True:
            event = await queue.get()
            yield event
            if event.type == StreamEventType.DONE:
                break

    async def respond_to_permission(
        self, request_id: str, granted: bool
    ) -> None:
        decision = "accept" if granted else "decline"
        # Request ID may have been an integer from the server
        try:
            id_value: int | str = int(request_id)
        except ValueError:
            id_value = request_id

        response = {
            "jsonrpc": "2.0",
            "id": id_value,
            "result": {"decision": decision},
        }
        await self._write_stdin(response)

    async def cancel(self) -> None:
        if self._event_queue:
            await self._event_queue.put(StreamEvent(type=StreamEventType.DONE))
            self._event_queue = None

    async def shutdown(self) -> None:
        await self.cancel()
        proc = self._process
        self._process = None
        if proc:
            log.info("[Codex] Terminating app-server pid=%d", proc.pid)
            await terminate_process(proc)

        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()

        self._thread_id = None
        self._thread_create_future = None
        self._initialized.clear()

    # -- Server lifecycle --

    async def _ensure_server(self, working_dir: Path) -> bool:
        """Start the app-server if not running.  Returns True when a fresh
        server was launched (all prior thread IDs are invalid)."""
        if self._process and self._process.returncode is None:
            return False

        # Reset state for fresh server
        self._initialized.clear()
        self._thread_id = None
        self._thread_create_future = None
        self._next_request_id = 1

        log.info("[Codex] Launching app-server: %s", self._executable)

        self._process = await asyncio.create_subprocess_exec(
            self._executable,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=build_cli_env(),
        )

        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

        await self._send_initialize()
        try:
            await asyncio.wait_for(self._initialized.wait(), timeout=10)
        except asyncio.TimeoutError:
            log.error("[Codex] Initialization timed out")

        return True

    async def _send_initialize(self) -> None:
        await self._send_request("initialize", {
            "clientInfo": {"name": "Penta", "version": "0.1.0"},
        })

    async def _create_thread(self, working_dir: Path) -> str:
        """Request-scoped thread creation.  Returns the new thread ID.
        Raises on RPC error or timeout (caught by send())."""
        loop = asyncio.get_running_loop()
        self._thread_create_future = loop.create_future()
        try:
            await self._start_thread(working_dir)
            return await asyncio.wait_for(
                self._thread_create_future, timeout=10,
            )
        finally:
            self._thread_create_future = None

    async def _start_thread(self, working_dir: Path) -> None:
        await self._send_request("thread/start", {
            "cwd": str(working_dir),
            "approvalPolicy": "never",
        })

    async def _start_turn(self, thread_id: str, prompt: str) -> None:
        await self._send_request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        })

    async def _send_request(
        self, method: str, params: dict
    ) -> None:
        request_id = self._next_request_id
        self._next_request_id += 1

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        log.info("[Codex] Sending: %s id=%d", method, request_id)
        await self._write_stdin(request)

    async def _write_stdin(self, obj: dict) -> None:
        if not self._process or not self._process.stdin:
            return
        line = json.dumps(obj) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()

    # -- Reading --

    async def _read_stdout(self) -> None:
        if not self._process or not self._process.stdout:
            return
        try:
            async for line in async_lines(self._process.stdout):
                log.debug("[Codex] stdout: %s", line[:300])
                self._handle_message(line)
        except asyncio.CancelledError:
            pass
        log.info("[Codex] stdout stream ended")

        # Server died — notify any waiting consumer
        if self._event_queue:
            await self._event_queue.put(StreamEvent(type=StreamEventType.DONE))

    async def _read_stderr(self) -> None:
        if not self._process or not self._process.stderr:
            return
        try:
            async for line in async_lines(self._process.stderr):
                log.debug("[Codex] stderr: %s", line)
        except asyncio.CancelledError:
            pass

    def _handle_message(self, line: str) -> None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return

        has_id = "id" in data
        has_method = "method" in data
        has_result = "result" in data or "error" in data

        if has_id and has_method:
            self._handle_server_request(data)
        elif has_id and has_result:
            self._handle_server_response(data)
        elif has_method:
            self._handle_notification(data)

    @staticmethod
    def _extract_thread_id(data: dict) -> str | None:
        """Pull thread ID from a response or notification payload."""
        thread_obj = data.get("thread", {})
        thread_id = thread_obj.get("id") if isinstance(thread_obj, dict) else None
        return thread_id or data.get("threadId")

    def _accept_thread_id(self, thread_id: str, label: str) -> bool:
        """Try to deliver a thread ID to the waiting future.

        Returns True if accepted, False if late/stale.
        """
        if not (self._thread_create_future and not self._thread_create_future.done()):
            log.warning("[Codex] Ignoring late thread %s: %s", label, thread_id)
            return False
        self._thread_create_future.set_result(thread_id)
        log.info("[Codex] Thread %s: %s", label, thread_id)
        if self._event_queue:
            self._event_queue.put_nowait(
                StreamEvent(
                    type=StreamEventType.SESSION_STARTED,
                    session_id=thread_id,
                )
            )
        return True

    def _handle_server_response(self, data: dict) -> None:
        # Handle JSON-RPC error responses
        error = data.get("error")
        if error:
            message = error.get("message", "Unknown error") if isinstance(error, dict) else str(error)
            log.error("[Codex] RPC error: %s", message)

            # If we're waiting for a thread, reject the future with the
            # real error message so send() can surface it.
            if self._thread_create_future and not self._thread_create_future.done():
                self._thread_create_future.set_exception(RuntimeError(message))
            elif self._event_queue:
                self._event_queue.put_nowait(
                    StreamEvent(type=StreamEventType.ERROR, error=message)
                )

            # Unblock initialize waiter so it doesn't time out silently
            if not self._initialized.is_set():
                self._initialized.set()
            return

        result = data.get("result", {})
        if not isinstance(result, dict):
            return

        # initialize response — Codex returns userAgent/platformFamily
        if result.get("userAgent") or result.get("serverInfo") or result.get("capabilities"):
            self._initialized.set()
            log.info("[Codex] Initialized")

        thread_id = self._extract_thread_id(result)
        if thread_id:
            self._accept_thread_id(thread_id, "created")

    def _handle_server_request(self, data: dict) -> None:
        method = data.get("method", "")
        params = data.get("params", {})

        raw_id = data.get("id")
        request_id = str(raw_id) if raw_id is not None else ""

        if method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        ):
            # Auto-approve and log as tool use for visibility
            detail = params.get("command", "") or params.get("reason", method)
            log.info("[Codex] Auto-approving: %s", detail)
            asyncio.create_task(
                self.respond_to_permission(request_id, True)
            )

        else:
            # Unknown request — accept so server doesn't hang
            log.info("[Codex] Unknown request: %s, accepting", method)
            asyncio.create_task(
                self.respond_to_permission(request_id, True)
            )

    def _handle_notification(self, data: dict) -> None:
        method = data.get("method", "")
        params = data.get("params", {})
        queue = self._event_queue  # local ref survives handler nulling self._event_queue

        if method == "thread/started":
            thread_id = self._extract_thread_id(params)
            if thread_id:
                self._accept_thread_id(thread_id, "started")

        elif method == "item/agentMessage/delta":
            # Streaming text delta
            item = params.get("item", {})
            delta = item.get("delta", "")
            if delta and queue:
                queue.put_nowait(StreamEvent(
                    type=StreamEventType.TEXT_DELTA, text=delta,
                ))

        elif method == "item/completed":
            item = params.get("item", {})
            item_type = item.get("type", "")
            # agentMessage (Codex 0.116+) or agent_message (older)
            if item_type in ("agentMessage", "agent_message"):
                text = item.get("text", "")
                if text and queue:
                    queue.put_nowait(StreamEvent(
                        type=StreamEventType.TEXT_COMPLETE, text=text,
                    ))

        elif method == "item/started":
            item = params.get("item", {})
            item_type = item.get("type", "")
            if item_type in ("tool_call", "toolCall"):
                tool_name = item.get("name", "")
                tool_id = item.get("id", "")
                if queue:
                    queue.put_nowait(StreamEvent(
                        type=StreamEventType.TOOL_USE_STARTED,
                        tool_id=tool_id,
                        tool_name=tool_name,
                    ))

        elif method == "turn/completed":
            log.info("[Codex] Turn completed")
            if queue:
                queue.put_nowait(StreamEvent(type=StreamEventType.DONE))
            self._event_queue = None

        elif method == "turn/failed":
            error = params.get("error", "Turn failed")
            log.error("[Codex] Turn failed: %s", error)
            if queue:
                queue.put_nowait(
                    StreamEvent(type=StreamEventType.ERROR, error=error)
                )
                queue.put_nowait(StreamEvent(type=StreamEventType.DONE))
            self._event_queue = None

        elif method == "error":
            message = params.get("message", "Unknown error")
            log.error("[Codex] Error: %s", message)
            if queue:
                queue.put_nowait(
                    StreamEvent(type=StreamEventType.ERROR, error=message)
                )
