"""Localhost HTTP server handling Claude CLI PreToolUse hook requests.

Auto-approves all tools except:
- ExitPlanMode: pauses for user review via an asyncio future
- AskUserQuestion: denies (Claude falls back to plain text questions),
  but surfaces the structured questions to the TUI first
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

log = logging.getLogger(__name__)


class PermissionServer:
    """HTTP server for Claude CLI PreToolUse hooks.

    Runs on a daemon thread; bridges blocking HTTP handlers to the
    asyncio event loop for interactive decisions (plan review, questions).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._on_plan_review: Callable[[str, str, dict], None] | None = None
        self._on_question: Callable[[str, list[dict]], None] | None = None
        self._server: HTTPServer | None = None
        self.port: int | None = None
        self._thread: threading.Thread | None = None

    def set_plan_review_callback(
        self, callback: Callable[[str, str, dict], None],
    ) -> None:
        """Set callback for ExitPlanMode interception.

        callback(tool_use_id, plan_text, full_input)
        """
        self._on_plan_review = callback

    def set_question_callback(
        self, callback: Callable[[str, list[dict]], None],
    ) -> None:
        """Set callback for AskUserQuestion interception.

        callback(tool_use_id, questions)
        Called on the asyncio loop when Claude tries to ask a question.
        The tool is denied so Claude falls back to text, but the TUI
        can show the structured questions for a better UX.
        """
        self._on_question = callback

    def start(self) -> bool:
        """Bind and start the HTTP server. Returns False if bind fails."""
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length)) if length else {}
                except (json.JSONDecodeError, ValueError):
                    self.send_response(400)
                    self.end_headers()
                    return

                tool_name = body.get("tool_name", "unknown")
                tool_input = body.get("tool_input", {})
                tool_use_id = body.get("tool_use_id", "")

                if tool_name == "ExitPlanMode":
                    log.info("ExitPlanMode hook — pausing for review")
                    plan_text = ""
                    if isinstance(tool_input, dict):
                        plan_text = tool_input.get("plan", "")
                    elif isinstance(tool_input, str):
                        plan_text = tool_input

                    future = asyncio.run_coroutine_threadsafe(
                        server_ref._request_plan_review(
                            tool_use_id, plan_text, tool_input,
                        ),
                        server_ref._loop,
                    )
                    try:
                        approved = future.result(timeout=600)
                    except Exception:
                        log.exception("Plan review future failed")
                        approved = True

                    decision = "allow" if approved else "deny"
                    log.info("ExitPlanMode — user decision: %s", decision)
                    resp_body = json.dumps({
                        "hookSpecificOutput": {"permissionDecision": decision}
                    })

                elif tool_name == "AskUserQuestion":
                    # Deny so Claude falls back to text questions.
                    # Surface the structured questions to the TUI.
                    questions = []
                    if isinstance(tool_input, dict):
                        questions = tool_input.get("questions", [])
                    log.info(
                        "AskUserQuestion hook — denying (%d questions)",
                        len(questions),
                    )
                    if server_ref._on_question and questions:
                        asyncio.run_coroutine_threadsafe(
                            server_ref._notify_question(tool_use_id, questions),
                            server_ref._loop,
                        )
                    resp_body = json.dumps({
                        "hookSpecificOutput": {
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                "Ask your questions as regular text messages "
                                "in the group chat instead."
                            ),
                        }
                    })

                else:
                    decision = "allow"
                    log.debug("Auto-approved tool=%s id=%s", tool_name, tool_use_id)
                    resp_body = json.dumps({
                        "hookSpecificOutput": {"permissionDecision": decision}
                    })

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body.encode())

            def log_message(self, format, *args):
                pass  # Suppress default HTTP logging

        try:
            self._server = HTTPServer(("127.0.0.1", 0), Handler)
            self.port = self._server.server_address[1]
        except OSError:
            log.error("Failed to bind permission server")
            return False

        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._thread.start()
        log.info("Permission server started on port %d", self.port)
        return True

    def stop(self) -> None:
        """Shut down the server, denying any pending reviews."""
        for tool_use_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result(True)
        self._pending.clear()

        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=2)
        log.info("Permission server stopped")

    def resolve_plan_review(self, tool_use_id: str, approved: bool) -> None:
        """Called by the TUI when the user approves or rejects a plan."""
        future = self._pending.pop(tool_use_id, None)
        if future and not future.done():
            future.set_result(approved)

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._thread is not None

    @property
    def hook_settings_json(self) -> str:
        """JSON settings for Claude CLI's --settings flag."""
        if not self.is_running or self.port is None:
            return json.dumps({})
        return json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "",
                    "hooks": [{
                        "type": "http",
                        "url": f"http://127.0.0.1:{self.port}/permission",
                    }],
                }],
            },
        })

    async def _request_plan_review(
        self, tool_use_id: str, plan_text: str, full_input: dict,
    ) -> bool:
        """Create a future and notify the TUI of a pending plan review."""
        future = self._loop.create_future()
        self._pending[tool_use_id] = future
        if self._on_plan_review:
            self._on_plan_review(tool_use_id, plan_text, full_input)
        return await future

    async def _notify_question(
        self, tool_use_id: str, questions: list[dict],
    ) -> None:
        """Notify the TUI of questions (fire-and-forget, no blocking)."""
        if self._on_question:
            self._on_question(tool_use_id, questions)
