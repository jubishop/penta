"""Reusable test doubles for Penta's external boundaries."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from penta.services.agent_service import AgentService, StreamEvent, StreamEventType


@dataclass
class SendCall:
    """Record of a single send() invocation on a FakeAgentService."""

    prompt: str
    session_id: str | None
    working_dir: Path
    system_prompt: str | None


_HANG_SENTINEL = object()
_EXCEPTION_SENTINEL = object()


class FakeAgentService(AgentService):
    """Controllable fake for agent CLI services.

    Enqueue responses before triggering sends.  After the test, inspect
    ``calls`` to verify what prompts were delivered.

    Models the real CliAgentService single-stream invariant: send() raises
    RuntimeError if the previous generator has not finished cleanup (i.e.
    _streaming is still True).  This is the exact bug that the coordinator's
    wait_for mechanism exists to prevent.
    """

    def __init__(self) -> None:
        self.calls: list[SendCall] = []
        self._responses: deque[list] = deque()
        self.cancel_called: bool = False
        self.shutdown_called: bool = False
        self._streaming: bool = False
        # For ordering tests (e.g. cancel-before-shutdown).
        self.order: list[str] = []
        self._cancel_delay: float = 0.0

    # -- Enqueue helpers ------------------------------------------------------

    def enqueue_text(self, text: str, session_id: str | None = None) -> None:
        """Enqueue a simple text response (optional session start)."""
        events: list = []
        if session_id is not None:
            events.append(
                StreamEvent(type=StreamEventType.SESSION_STARTED, session_id=session_id)
            )
        events.append(StreamEvent(type=StreamEventType.TEXT_DELTA, text=text))
        events.append(StreamEvent(type=StreamEventType.DONE))
        self._responses.append(events)

    def enqueue_error(self, error: str) -> None:
        """Enqueue an error response."""
        self._responses.append([
            StreamEvent(type=StreamEventType.ERROR, error=error),
            StreamEvent(type=StreamEventType.DONE),
        ])

    def enqueue_events(self, events: list[StreamEvent]) -> None:
        """Enqueue an arbitrary sequence of events."""
        self._responses.append(list(events))

    def enqueue_hang(self, prefix_text: str = "partial") -> None:
        """Enqueue a response that yields one delta then blocks forever."""
        self._responses.append([
            StreamEvent(type=StreamEventType.TEXT_DELTA, text=prefix_text),
            _HANG_SENTINEL,
        ])

    def enqueue_exception(
        self, exc: BaseException, prefix_events: list[StreamEvent] | None = None,
    ) -> None:
        """Enqueue a response that yields events then raises an exception."""
        events: list = list(prefix_events or [])
        events.append((_EXCEPTION_SENTINEL, exc))
        self._responses.append(events)

    # -- AgentService implementation ------------------------------------------

    async def send(
        self,
        prompt: str,
        session_id: str | None,
        working_dir: Path,
        system_prompt: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # Mirror CliAgentService: raise if previous stream hasn't cleaned up.
        if self._streaming:
            raise RuntimeError(
                "FakeAgentService: send() called while already streaming — "
                "the coordinator must await the previous task's cleanup before "
                "calling send() again"
            )
        self._streaming = True

        self.calls.append(SendCall(prompt, session_id, working_dir, system_prompt))
        if not self._responses:
            self._streaming = False
            raise IndexError(
                "FakeAgentService: no responses enqueued — "
                "did you forget to call enqueue_text() or similar?"
            )
        events = self._responses.popleft()
        try:
            for event in events:
                if event is _HANG_SENTINEL:
                    await asyncio.Event().wait()
                elif isinstance(event, tuple) and len(event) == 2 and event[0] is _EXCEPTION_SENTINEL:
                    raise event[1]
                else:
                    assert isinstance(event, StreamEvent)
                    yield event
        finally:
            self._streaming = False

    async def cancel(self) -> None:
        if self._cancel_delay:
            await asyncio.sleep(self._cancel_delay)
        self.cancel_called = True
        self.order.append("cancel")

    async def shutdown(self) -> None:
        self.shutdown_called = True
        self.order.append("shutdown")
