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


class _StubState:
    """Minimal stand-in for AppState — just enough for the hook callbacks."""

    def __init__(self) -> None:
        self._permission_server = _StubPermissionServer()
        self.pending_plans: dict[UUID, PendingPlan] = {}
        self.conversation: list = []
        self.coordinators: dict = {}
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
    """PentaApp that skips heavy on_mount and avoids widget-mounting in tests.

    _render_new_messages is stubbed out because the real implementation
    mounts ChatMessage widgets that require full app initialisation.
    We track calls to it so plan-guard tests can verify it was/wasn't called.
    """

    CSS_PATH = None  # type: ignore[assignment]

    def __init__(self, directory: Path, **kwargs) -> None:
        super().__init__(directory, **kwargs)
        self.render_calls: int = 0

    async def on_mount(self) -> None:
        pass

    def _render_new_messages(self) -> None:
        self.render_calls += 1


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
        app._state = state  # type: ignore[assignment]

        app._on_question_asked(agent_id, _questions(), "stale_id")
        await pilot.pause()

        assert not _has_question_screen(app)


async def test_question_screen_skipped_when_state_cleared():
    """Guard handles _state being None (app shutting down)."""
    app = _SkeletonPentaApp(Path("/tmp/test"))

    async with app.run_test() as pilot:
        app._on_question_asked(uuid4(), _questions(), "tu_x")
        await pilot.pause()

        assert not _has_question_screen(app)


# -- Plan guard tests --------------------------------------------------------


async def test_plan_render_called_when_still_pending():
    """_render_new_messages + notify fire when agent_id is still pending."""
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

        assert len(state.conversation) == 1
        assert app.render_calls == 1
        assert app._notifications


async def test_plan_render_skipped_when_stale():
    """_render_new_messages + notify are suppressed when plan is no longer pending."""
    app = _SkeletonPentaApp(Path("/tmp/test"))

    async with app.run_test() as pilot:
        state = _StubState()
        agent_id = uuid4()
        state.add_agent(agent_id, "Claude")
        app._state = state  # type: ignore[assignment]

        app._on_plan_review(agent_id, "old plan", "tu_old")
        await pilot.pause()

        assert len(state.conversation) == 1
        assert app.render_calls == 0
        assert not app._notifications
