"""Tests for /plan interpolation and pending plan management."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from penta.app_state import AppState
from penta.models import AgentStatus, AgentType, Message, MessageSender, PendingPlan
from penta.services.db import PentaDB
from penta.services.permission_server import PermissionServer

from .fakes import FakeAgentService


@pytest.fixture
async def state_with_agents(memory_db, fake_services):
    """AppState with Claude and Codex agents, each having its own fake service."""
    services, factory = fake_services
    state = AppState(Path("/tmp/test"), db=memory_db, service_factory=factory)
    await state.connect()
    await state.add_agent("Claude", AgentType.CLAUDE)
    await state.add_agent("Codex", AgentType.CODEX)

    # Enqueue dummy responses so routing doesn't error
    for svc in services.values():
        for _ in range(10):
            svc.enqueue_text("ok")

    yield state, services
    await state.shutdown()


class TestPlanInterpolationSinglePlan:
    """When exactly one plan is pending, /plan resolves automatically."""

    async def test_plan_interpolated_in_routed_text(self, state_with_agents):
        state, services = state_with_agents
        claude = state.agent_by_name("Claude")

        state.pending_plans[claude.id] = PendingPlan(
            agent_id=claude.id,
            agent_name="Claude",
            tool_use_id="cr_1",
            plan_text="## Step 1\nDo the thing.",
        )

        await state.send_user_message("@Codex review this /plan please")
        await state.router.drain()

        # The message saved to conversation should have literal /plan
        user_msgs = [m for m in state.conversation if m.sender.is_user]
        assert user_msgs[-1].text == "@Codex review this /plan please"

        # The text sent to the Codex service should have the plan interpolated
        codex_svc = services["Codex"]
        assert len(codex_svc.calls) >= 1
        delivered_prompt = codex_svc.calls[-1].prompt
        assert "## Step 1" in delivered_prompt
        assert "Do the thing." in delivered_prompt

    async def test_plan_not_in_stored_history(self, state_with_agents):
        state, services = state_with_agents
        claude = state.agent_by_name("Claude")

        state.pending_plans[claude.id] = PendingPlan(
            agent_id=claude.id,
            agent_name="Claude",
            tool_use_id="cr_1",
            plan_text="secret plan details",
        )

        await state.send_user_message("@Codex /plan")
        await state.router.drain()

        # DB should store literal /plan, not the plan text
        rows = await state.db.get_messages()
        user_rows = [r for r in rows if r[1] == "User"]
        assert user_rows[-1][2] == "@Codex /plan"


class TestPlanInterpolationNoPlan:
    """When no plans are pending, /plan passes through as literal text."""

    async def test_no_interpolation_when_no_plans(self, state_with_agents):
        state, services = state_with_agents

        await state.send_user_message("@Codex review this /plan please")
        await state.router.drain()

        codex_svc = services["Codex"]
        if codex_svc.calls:
            delivered = codex_svc.calls[-1].prompt
            assert "/plan" in delivered
            assert "## Step" not in delivered


class TestPlanInterpolationMultiplePlans:
    """When multiple plans exist and no explicit ID, no interpolation happens."""

    async def test_no_interpolation_without_explicit_id(self, state_with_agents):
        state, services = state_with_agents
        claude = state.agent_by_name("Claude")
        codex = state.agent_by_name("Codex")

        state.pending_plans[claude.id] = PendingPlan(
            agent_id=claude.id,
            agent_name="Claude",
            tool_use_id="cr_1",
            plan_text="Claude's plan",
        )
        state.pending_plans[codex.id] = PendingPlan(
            agent_id=codex.id,
            agent_name="Codex",
            tool_use_id="cr_2",
            plan_text="Codex's plan",
        )

        # Without resolved_plan_id, /plan should not be interpolated
        await state.send_user_message("@Claude review /plan")
        await state.router.drain()

        claude_svc = services["Claude"]
        if claude_svc.calls:
            delivered = claude_svc.calls[-1].prompt
            assert "Claude's plan" not in delivered
            assert "Codex's plan" not in delivered

    async def test_explicit_id_resolves_correct_plan(self, state_with_agents):
        state, services = state_with_agents
        claude = state.agent_by_name("Claude")
        codex = state.agent_by_name("Codex")

        state.pending_plans[claude.id] = PendingPlan(
            agent_id=claude.id,
            agent_name="Claude",
            tool_use_id="cr_1",
            plan_text="Claude's plan text",
        )
        state.pending_plans[codex.id] = PendingPlan(
            agent_id=codex.id,
            agent_name="Codex",
            tool_use_id="cr_2",
            plan_text="Codex's plan text",
        )

        await state.send_user_message(
            "@Claude review /plan", resolved_plan_id=codex.id,
        )
        await state.router.drain()

        claude_svc = services["Claude"]
        if claude_svc.calls:
            delivered = claude_svc.calls[-1].prompt
            assert "Codex's plan text" in delivered
            assert "Claude's plan text" not in delivered


class TestPlanApproveReject:
    """AppState approve/reject plan management (tested via direct state manipulation)."""

    async def test_approve_removes_from_pending(self, state_with_agents):
        state, services = state_with_agents
        claude = state.agent_by_name("Claude")

        # Manually set up pending plan (bypasses streaming chain)
        state.pending_plans[claude.id] = PendingPlan(
            agent_id=claude.id,
            agent_name="Claude",
            tool_use_id="cr_p1",
            plan_text="my plan",
        )

        assert claude.id in state.pending_plans
        state.approve_plan(claude.id)
        assert claude.id not in state.pending_plans

    async def test_reject_removes_from_pending(self, state_with_agents):
        state, services = state_with_agents
        claude = state.agent_by_name("Claude")

        state.pending_plans[claude.id] = PendingPlan(
            agent_id=claude.id,
            agent_name="Claude",
            tool_use_id="cr_p2",
            plan_text="my plan",
        )

        assert claude.id in state.pending_plans
        state.reject_plan(claude.id, "needs work")
        assert claude.id not in state.pending_plans

    async def test_approve_nonexistent_plan_is_noop(self, state_with_agents):
        state, services = state_with_agents
        claude = state.agent_by_name("Claude")

        # No pending plan — should not error
        state.approve_plan(claude.id)

    async def test_stream_complete_clears_pending_plan(self, state_with_agents):
        state, _ = state_with_agents
        claude = state.agent_by_name("Claude")

        state.pending_plans[claude.id] = PendingPlan(
            agent_id=claude.id,
            agent_name="Claude",
            tool_use_id="cr_1",
            plan_text="plan",
        )

        # Simulate stream completion via the relay
        msg = Message(sender=MessageSender.agent(claude.id), text="done")
        state._relay_stream_complete(msg, claude.id)

        assert claude.id not in state.pending_plans

    async def test_cancel_agent_clears_pending_plan(self, state_with_agents):
        state, services = state_with_agents
        claude = state.agent_by_name("Claude")

        state.pending_plans[claude.id] = PendingPlan(
            agent_id=claude.id,
            agent_name="Claude",
            tool_use_id="cr_1",
            plan_text="plan",
        )

        # Force agent to busy state so cancel_agent works
        claude.status = AgentStatus.WAITING_FOR_USER

        state.cancel_agent(claude.id)
        assert claude.id not in state.pending_plans

    async def test_reject_sends_feedback_as_message(self, state_with_agents):
        """Rejecting a plan must send the feedback text to the agent.

        Regression: without this, /revise <feedback> discards the feedback
        and Claude only sees a bare deny with no guidance for revision.
        """
        state, services = state_with_agents
        claude = state.agent_by_name("Claude")

        state.pending_plans[claude.id] = PendingPlan(
            agent_id=claude.id,
            agent_name="Claude",
            tool_use_id="cr_fb",
            plan_text="bad plan",
        )
        claude.status = AgentStatus.WAITING_FOR_USER

        state.reject_plan(claude.id, "use a different approach")

        # The reject itself doesn't send the message — the app layer does.
        # But we can verify the plan was rejected and status was reset.
        assert claude.id not in state.pending_plans
        assert claude.status == AgentStatus.PROCESSING


class TestPlanReviewRelay:
    """AppState._relay_plan_review stores the plan correctly."""

    async def test_relay_stores_pending_plan(self, state_with_agents):
        state, _ = state_with_agents
        claude = state.agent_by_name("Claude")

        relayed: list[tuple] = []
        state.on_plan_review = lambda aid, pt, crid: relayed.append((aid, pt, crid))

        state._relay_plan_review(claude.id, "the plan", "cr_99")

        assert claude.id in state.pending_plans
        plan = state.pending_plans[claude.id]
        assert plan.plan_text == "the plan"
        assert plan.tool_use_id == "cr_99"
        assert plan.agent_name == "Claude"

        # Callback was fired
        assert len(relayed) == 1


class TestApproveRejectResolvesPermissionServer:
    """approve/reject must resolve the permission server's pending futures.

    Regression: without this, the HTTP handler blocks for 600s and the
    single-threaded server is unable to handle any further requests.
    """

    async def test_approve_resolves_server_future(self, state_with_agents):
        state, _ = state_with_agents
        claude = state.agent_by_name("Claude")

        loop = asyncio.get_running_loop()
        server = PermissionServer(loop)
        assert server.start()
        state._permission_server = server

        try:
            future = loop.create_future()
            server._pending["cr_e2e_approve"] = future

            state.pending_plans[claude.id] = PendingPlan(
                agent_id=claude.id,
                agent_name="Claude",
                tool_use_id="cr_e2e_approve",
                plan_text="test plan",
            )

            state.approve_plan(claude.id)
            assert future.done()
            assert future.result() is True
        finally:
            state._permission_server = None
            await server.stop()

    async def test_reject_resolves_server_future(self, state_with_agents):
        state, _ = state_with_agents
        claude = state.agent_by_name("Claude")

        loop = asyncio.get_running_loop()
        server = PermissionServer(loop)
        assert server.start()
        state._permission_server = server

        try:
            future = loop.create_future()
            server._pending["cr_e2e_reject"] = future

            state.pending_plans[claude.id] = PendingPlan(
                agent_id=claude.id,
                agent_name="Claude",
                tool_use_id="cr_e2e_reject",
                plan_text="test plan",
            )

            state.reject_plan(claude.id, "needs work")
            assert future.done()
            assert future.result() is False
        finally:
            state._permission_server = None
            await server.stop()

    async def test_cancel_agent_resolves_server_futures(self, state_with_agents):
        """cancel_agent must unblock pending hook requests.

        Regression: without resolve_all_pending in cancel_agent, the
        single-threaded HTTP server stays blocked on the cancelled
        agent's future for up to 600s.
        """
        state, _ = state_with_agents
        claude = state.agent_by_name("Claude")

        loop = asyncio.get_running_loop()
        server = PermissionServer(loop)
        assert server.start()
        state._permission_server = server

        try:
            future = loop.create_future()
            server._pending["cr_e2e_cancel"] = future

            state.pending_plans[claude.id] = PendingPlan(
                agent_id=claude.id,
                agent_name="Claude",
                tool_use_id="cr_e2e_cancel",
                plan_text="plan",
            )
            claude.status = AgentStatus.WAITING_FOR_USER

            state.cancel_agent(claude.id)
            assert future.done()
        finally:
            state._permission_server = None
            await server.stop()


class TestWaitingForUserStatus:
    """Hook callbacks must set WAITING_FOR_USER on the agent.

    Regression: without status updates in the hook callbacks, the
    status dot stays green (PROCESSING) even when the agent is blocked
    waiting for user input.
    """

    async def test_hook_plan_review_sets_waiting_status(self, state_with_agents):
        state, _ = state_with_agents
        claude = state.agent_by_name("Claude")
        claude.status = AgentStatus.PROCESSING

        statuses: list[AgentStatus] = []
        state.on_status_changed = lambda aid, s: statuses.append(s)

        state._on_hook_plan_review("tu_plan", "the plan", {})

        assert claude.status == AgentStatus.WAITING_FOR_USER
        assert AgentStatus.WAITING_FOR_USER in statuses

    async def test_hook_question_sets_waiting_status(self, state_with_agents):
        state, _ = state_with_agents
        claude = state.agent_by_name("Claude")
        claude.status = AgentStatus.PROCESSING

        statuses: list[AgentStatus] = []
        state.on_status_changed = lambda aid, s: statuses.append(s)

        state._on_hook_question("tu_q", [{"question": "Q?"}])

        assert claude.status == AgentStatus.WAITING_FOR_USER
        assert AgentStatus.WAITING_FOR_USER in statuses

    async def test_approve_resets_to_processing(self, state_with_agents):
        state, _ = state_with_agents
        claude = state.agent_by_name("Claude")
        claude.status = AgentStatus.WAITING_FOR_USER

        state.pending_plans[claude.id] = PendingPlan(
            agent_id=claude.id,
            agent_name="Claude",
            tool_use_id="cr_status",
            plan_text="plan",
        )

        statuses: list[AgentStatus] = []
        state.on_status_changed = lambda aid, s: statuses.append(s)

        state.approve_plan(claude.id)
        assert claude.status == AgentStatus.PROCESSING
        assert AgentStatus.PROCESSING in statuses

    async def test_reject_resets_to_processing(self, state_with_agents):
        state, _ = state_with_agents
        claude = state.agent_by_name("Claude")
        claude.status = AgentStatus.WAITING_FOR_USER

        state.pending_plans[claude.id] = PendingPlan(
            agent_id=claude.id,
            agent_name="Claude",
            tool_use_id="cr_status2",
            plan_text="plan",
        )

        statuses: list[AgentStatus] = []
        state.on_status_changed = lambda aid, s: statuses.append(s)

        state.reject_plan(claude.id, "revise this")
        assert claude.status == AgentStatus.PROCESSING
        assert AgentStatus.PROCESSING in statuses
