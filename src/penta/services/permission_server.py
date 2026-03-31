"""Localhost HTTP server handling Claude CLI PreToolUse hook requests.

Uses separate hook matchers:
- AskUserQuestion: pauses for user answers via asyncio future, returns updatedInput
- ExitPlanMode: pauses for user review via asyncio future, returns allow/deny
- Everything else: auto-approves immediately
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

log = logging.getLogger(__name__)


class PermissionServer:
    """HTTP server for Claude CLI PreToolUse hooks.

    Runs on a daemon thread; bridges blocking HTTP handlers to the
    asyncio event loop for interactive decisions (questions, plan review).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._pending: dict[str, asyncio.Future] = {}
        self._shutting_down = threading.Event()
        self._cancel_pending = False
        self._on_plan_review: Callable[[str, str, dict], None] | None = None
        self._on_question: Callable[[str, list[dict]], None] | None = None
        self._server: ThreadingHTTPServer | None = None
        self.port: int | None = None
        self._thread: threading.Thread | None = None

    def set_plan_review_callback(
        self, callback: Callable[[str, str, dict], None],
    ) -> None:
        """callback(tool_use_id, plan_text, full_input)"""
        self._on_plan_review = callback

    def set_question_callback(
        self, callback: Callable[[str, list[dict]], None],
    ) -> None:
        """callback(tool_use_id, questions)"""
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

                if tool_name == "AskUserQuestion":
                    resp_body = server_ref._handle_question(tool_use_id, tool_input)
                elif tool_name == "ExitPlanMode":
                    resp_body = server_ref._handle_plan_review(tool_use_id, tool_input)
                else:
                    log.debug("Auto-approved tool=%s id=%s", tool_name, tool_use_id)
                    resp_body = json.dumps({
                        "hookSpecificOutput": {"permissionDecision": "allow"}
                    })

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body.encode())

            def log_message(self, format, *args):
                pass

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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

    async def stop(self) -> None:
        self._shutting_down.set()
        for tool_use_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result(True)
        self._pending.clear()
        # Yield so resolved futures' coroutines can complete,
        # unblocking HTTP handlers waiting on concurrent futures.
        await asyncio.sleep(0)
        if self._server:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._server.shutdown)
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None
        log.info("Permission server stopped")

    def resolve_all_pending(self) -> None:
        """Resolve all pending futures so HTTP handlers can unblock.

        Sets _cancel_pending so late-registered futures (from coroutines
        scheduled before the cancel but not yet run) also resolve immediately.
        The flag is cleared on the next event loop tick so it doesn't leak
        into future turns.
        """
        self._cancel_pending = True
        for tool_use_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result(True)
        self._pending.clear()
        # Clear on next tick — late coroutines run before this callback,
        # so the flag won't leak into the next real request.
        self._loop.call_soon(self._clear_cancel_pending)

    def _clear_cancel_pending(self) -> None:
        self._cancel_pending = False

    # -- Resolvers (called by TUI) --

    def resolve_plan_review(self, tool_use_id: str, approved: bool) -> None:
        """Called by the TUI when the user approves or rejects a plan."""
        future = self._pending.pop(tool_use_id, None)
        if future and not future.done():
            future.set_result(approved)

    def resolve_question(
        self, tool_use_id: str, answers: dict[str, str],
    ) -> None:
        """Called by the TUI when the user answers questions."""
        future = self._pending.pop(tool_use_id, None)
        if future and not future.done():
            future.set_result(answers)

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._thread is not None

    @property
    def hook_settings_json(self) -> str:
        """JSON settings for Claude CLI's --settings flag.

        Uses separate matchers so AskUserQuestion and ExitPlanMode
        get their own hooks (updatedInput requires a targeted matcher).
        """
        if not self.is_running or self.port is None:
            return json.dumps({})
        url = f"http://127.0.0.1:{self.port}/permission"
        hook = {"type": "http", "url": url}
        return json.dumps({
            "hooks": {
                "PreToolUse": [
                    {"matcher": "AskUserQuestion", "hooks": [hook]},
                    {"matcher": "ExitPlanMode", "hooks": [hook]},
                    {"matcher": "", "hooks": [hook]},
                ],
            },
        })

    # -- Internal handlers (run on HTTP thread) --

    def _handle_question(self, tool_use_id: str, tool_input: dict) -> str:
        if self._shutting_down.is_set():
            return json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}})
        questions = tool_input.get("questions", []) if isinstance(tool_input, dict) else []
        log.info("AskUserQuestion hook — pausing for user answers (%d questions)", len(questions))

        if not questions:
            return json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}})

        future = asyncio.run_coroutine_threadsafe(
            self._request_answers(tool_use_id, questions),
            self._loop,
        )
        try:
            answers = future.result(timeout=600)
        except Exception:
            log.exception("Question answer future failed")
            return json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}})

        if not isinstance(answers, dict):
            # Resolved by shutdown/cancel — allow without injecting answers
            return json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}})

        # Build updatedInput with the user's answers
        updated = dict(tool_input)
        updated["answers"] = answers

        log.info("AskUserQuestion — answers provided: %s", answers)
        return json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": updated,
            }
        })

    def _handle_plan_review(self, tool_use_id: str, tool_input: dict) -> str:
        if self._shutting_down.is_set():
            return json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}})
        plan_text = ""
        if isinstance(tool_input, dict):
            plan_text = tool_input.get("plan", "")
        elif isinstance(tool_input, str):
            plan_text = tool_input

        log.info("ExitPlanMode hook — pausing for review")

        future = asyncio.run_coroutine_threadsafe(
            self._request_plan_review(tool_use_id, plan_text, tool_input),
            self._loop,
        )
        try:
            approved = future.result(timeout=600)
        except Exception:
            log.exception("Plan review future failed")
            approved = True

        decision = "allow" if approved else "deny"
        log.info("ExitPlanMode — user decision: %s", decision)
        return json.dumps({
            "hookSpecificOutput": {"permissionDecision": decision}
        })

    # -- Async bridge methods (run on event loop) --

    async def _request_answers(
        self, tool_use_id: str, questions: list[dict],
    ) -> dict[str, str]:
        future = self._loop.create_future()
        self._pending[tool_use_id] = future
        # Check after registering: stop() or resolve_all_pending() may have run.
        if self._shutting_down.is_set() or self._cancel_pending:
            self._cancel_pending = False
            if not future.done():
                future.set_result({})
            return await future
        try:
            if self._on_question:
                self._on_question(tool_use_id, questions)
        except Exception:
            log.exception("Question callback failed")
            if not future.done():
                future.set_result({})
        return await future

    async def _request_plan_review(
        self, tool_use_id: str, plan_text: str, full_input: dict,
    ) -> bool:
        future = self._loop.create_future()
        self._pending[tool_use_id] = future
        # Check after registering: stop() or resolve_all_pending() may have run.
        if self._shutting_down.is_set() or self._cancel_pending:
            self._cancel_pending = False
            if not future.done():
                future.set_result(True)
            return await future
        try:
            if self._on_plan_review:
                self._on_plan_review(tool_use_id, plan_text, full_input)
        except Exception:
            log.exception("Plan review callback failed")
            if not future.done():
                future.set_result(True)
        return await future
