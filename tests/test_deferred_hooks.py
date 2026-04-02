"""Regression tests for deferred hook callbacks in PentaApp.

The _on_question_asked and _on_plan_review callbacks are invoked from
run_coroutine_threadsafe (permission server), so they defer UI work via
call_later.  These tests verify:

  1. The original bug: widgets compose correctly when deferred (NoActiveAppError).
  2. Staleness guards: deferred callbacks are skipped when the pending
     tool_use_id / plan is resolved before call_later fires.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from penta.app import PentaApp
from penta.models import AgentStatus, AgentType, PendingPlan
from penta.widgets.question_picker import QuestionPickerScreen


# -- Stubs ------------------------------------------------------------------


class _StubPermissionServer:
    """Minimal stand-in for PermissionServer with a _pending dict."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future] = {}

    def resolve_question(self, tool_use_id: str, answers: dict) -> None:
        future = self._pending.pop(tool_use_id, None)
        if future and not future.done():
            future.set_result(answers)


@dataclass
class _StubAgent:
    id: UUID
    name: str
    type: AgentType = AgentType.CLAUDE


@dataclass
class _StubCoordinator:
    status: AgentStatus = AgentStatus.PROCESSING

    def set_status(self, status: AgentStatus) -> None:
        self.status = status


class _StubState:
    """Minimal stand-in for AppState — just enough for the hook callbacks."""

    def __init__(self) -> None:
        self._permission_server = _StubPermissionServer()
        self.pending_plans: dict[UUID, PendingPlan] = {}
        self.conversation: list = []
        self.coordinators: dict[UUID, _StubCoordinator] = {}
        self._agents: dict[UUID, _StubAgent] = {}

    def add_agent(self, agent_id: UUID, name: str) -> _StubAgent:
        agent = _StubAgent(id=agent_id, name=name)
        self._agents[agent_id] = agent
        return agent

    def agent_by_id(self, agent_id: UUID) -> _StubAgent | None:
        return self._agents.get(agent_id)

    def cancel_agent(self, agent_id: UUID) -> None:
        pass

    async def shutdown(self) -> None:
        pass


class _SkeletonPentaApp(PentaApp):
    """PentaApp that skips heavy on_mount initialisation."""

    CSS_PATH = None  # type: ignore[assignment]

    async def on_mount(self) -> None:
        # Skip DB, agent creation, polling, etc.
        pass


# -- Helpers -----------------------------------------------------------------


def _questions() -> list[dict]:
    return [
        {
            "question": "Pick one?",
            "options": [
                {"label": "A", "description": "first"},
                {"label": "B", "description": "second"},
            ],
            "multiSelect": False,
        }
    ]


def _has_question_screen(app: PentaApp) -> bool:
    return any(isinstance(s, QuestionPickerScreen) for s in app.screen_stack)


# -- Question guard tests ----------------------------------------------------


async def test_question_screen_shown_when_still_pending():
    """QuestionPickerScreen is pushed when tool_use_id is still in _pending."""
    app = _SkeletonPentaApp(Path("/tmp/test"))

    async with app.run_test() as pilot:
        state = _StubState()
        agent_id = uuid4()
        state.add_agent(agent_id, "Claude")
        tid = "tu_pending"
        state._permission_server._pending[tid] = asyncio.get_running_loop().create_future()
        app._state = state  # type: ignore[assignment]

        app._on_question_asked(agent_id, _questions(), tid)
        await pilot.pause()

        assert _has_question_screen(app)


async def test_question_screen_skipped_when_stale():
    """QuestionPickerScreen is NOT pushed when tool_use_id is already resolved."""
    app = _SkeletonPentaApp(Path("/tmp/test"))

    async with app.run_test() as pilot:
        state = _StubState()
        agent_id = uuid4()
        state.add_agent(agent_id, "Claude")
        # tool_use_id NOT in _pending — simulates cancel/resolve before call_later fires
        app._state = state  # type: ignore[assignment]

        app._on_question_asked(agent_id, _questions(), "stale_id")
        await pilot.pause()

        assert not _has_question_screen(app)


async def test_question_screen_skipped_when_state_cleared():
    """Guard handles _state being None (app shutting down)."""
    app = _SkeletonPentaApp(Path("/tmp/test"))

    async with app.run_test() as pilot:
        # _state is None (default after skeleton on_mount)
        app._on_question_asked(uuid4(), _questions(), "tu_x")
        await pilot.pause()

        assert not _has_question_screen(app)


# -- Plan guard tests --------------------------------------------------------


async def test_plan_notification_shown_when_still_pending():
    """Plan render + notify fires when agent_id is still in pending_plans."""
    app = _SkeletonPentaApp(Path("/tmp/test"))

    async with app.run_test() as pilot:
        state = _StubState()
        agent_id = uuid4()
        state.add_agent(agent_id, "Claude")
        state.pending_plans[agent_id] = PendingPlan(
            agent_id=agent_id, agent_name="Claude",
            tool_use_id="tu_plan", plan_text="do stuff",
        )
        app._state = state  # type: ignore[assignment]

        app._on_plan_review(agent_id, "do stuff", "tu_plan")
        await pilot.pause()

        # The plan message was appended (immediately, outside call_later)
        assert len(state.conversation) == 1
        # Notification was shown (inside call_later — would fail without guard passing)
        assert app._notifications


async def test_plan_notification_skipped_when_stale():
    """Plan notify is suppressed when agent_id no longer in pending_plans."""
    app = _SkeletonPentaApp(Path("/tmp/test"))

    async with app.run_test() as pilot:
        state = _StubState()
        agent_id = uuid4()
        state.add_agent(agent_id, "Claude")
        # agent_id NOT in pending_plans — plan was approved/cancelled
        app._state = state  # type: ignore[assignment]

        app._on_plan_review(agent_id, "old plan", "tu_old")
        await pilot.pause()

        # The message was still appended (data-only, outside guard)
        assert len(state.conversation) == 1
        # But notification should NOT have fired
        assert not app._notifications
