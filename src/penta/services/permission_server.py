from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

log = logging.getLogger(__name__)


class PermissionServer:
    """Localhost HTTP server that handles Claude CLI PreToolUse hook requests.

    Claude CLI POSTs tool info to us; we bridge to the asyncio event loop
    where the TUI shows a permission dialog; the user's decision is sent
    back as the HTTP response.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._on_request: Callable[[str, str, str], None] | None = None

        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}

                tool_name = body.get("tool_name", "unknown")
                tool_input = body.get("tool_input", {})
                tool_use_id = body.get("tool_use_id", "")

                if isinstance(tool_input, dict):
                    tool_input = json.dumps(tool_input, indent=2)

                future = asyncio.run_coroutine_threadsafe(
                    server_ref._request_permission(tool_use_id, tool_name, tool_input),
                    server_ref._loop,
                )

                try:
                    granted = future.result(timeout=300)
                except Exception:
                    granted = False

                decision = "allow" if granted else "deny"
                resp_body = json.dumps({
                    "hookSpecificOutput": {"permissionDecision": decision}
                })
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body.encode())

            def log_message(self, format: str, *args: object) -> None:
                log.debug("PermissionServer: %s", format % args)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread: threading.Thread | None = None

    def set_request_callback(
        self, callback: Callable[[str, str, str], None]
    ) -> None:
        """Set callback invoked on asyncio loop when a permission request arrives.

        callback(tool_use_id, tool_name, tool_input_str)
        """
        self._on_request = callback

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        log.info("Permission server started on port %d", self.port)

    def stop(self) -> None:
        self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=2)
        log.info("Permission server stopped")

    def resolve_permission(self, tool_use_id: str, granted: bool) -> None:
        """Called by the TUI when the user clicks Allow/Deny."""
        future = self._pending.pop(tool_use_id, None)
        if future and not future.done():
            future.set_result(granted)

    @property
    def hook_settings_json(self) -> str:
        """JSON string to pass as Claude CLI's --settings flag."""
        return json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "",
                    "hooks": [{
                        "type": "http",
                        "url": f"http://127.0.0.1:{self.port}/permission",
                    }],
                }]
            }
        })

    async def _request_permission(
        self, tool_use_id: str, tool_name: str, tool_input: str
    ) -> bool:
        future = self._loop.create_future()
        self._pending[tool_use_id] = future
        if self._on_request:
            self._on_request(tool_use_id, tool_name, tool_input)
        return await future
