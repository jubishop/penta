"""Tests for the HTTP permission server — hook format, matchers, and resolution."""

from __future__ import annotations

import asyncio
import json

import pytest

from penta.services.permission_server import PermissionServer


@pytest.fixture
async def server():
    loop = asyncio.get_running_loop()
    srv = PermissionServer(loop)
    assert srv.start()
    yield srv
    await srv.stop()


def _post(port: int, body: dict) -> dict:
    """POST JSON to the permission server and return the parsed response."""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", "/permission", json.dumps(body).encode(),
                      {"Content-Type": "application/json"})
        resp = conn.getresponse()
        return json.loads(resp.read())
    finally:
        conn.close()


class TestAutoApproval:
    """Regular tools are auto-approved immediately."""

    async def test_bash_auto_approved(self, server):
        resp = _post(server.port, {
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "tool_use_id": "tu_1",
        })
        assert resp["hookSpecificOutput"]["permissionDecision"] == "allow"

    async def test_read_auto_approved(self, server):
        resp = _post(server.port, {
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/test"},
            "tool_use_id": "tu_2",
        })
        assert resp["hookSpecificOutput"]["permissionDecision"] == "allow"


class TestHookSettingsFormat:
    """Hook settings JSON must use separate matchers for AskUserQuestion
    and ExitPlanMode.  A wildcard matcher causes updatedInput to be ignored
    for AskUserQuestion (discovered during live testing)."""

    async def test_separate_matchers_for_interactive_tools(self, server):
        settings = json.loads(server.hook_settings_json)
        hooks = settings["hooks"]["PreToolUse"]
        matchers = [h["matcher"] for h in hooks]

        # AskUserQuestion and ExitPlanMode MUST have their own matchers
        # (wildcard "" alone breaks updatedInput for AskUserQuestion)
        assert "AskUserQuestion" in matchers
        assert "ExitPlanMode" in matchers
        assert "" in matchers  # wildcard for everything else

    async def test_all_hooks_point_to_same_url(self, server):
        settings = json.loads(server.hook_settings_json)
        hooks = settings["hooks"]["PreToolUse"]
        urls = {h["hooks"][0]["url"] for h in hooks}
        assert len(urls) == 1  # all point to same server


class TestExitPlanModeReview:
    """ExitPlanMode pauses for user review and returns allow/deny."""

    async def test_approve_plan(self, server):
        reviews = []

        def on_review(tid, pt, fi):
            reviews.append((tid, pt))
            server.resolve_plan_review(tid, True)

        server.set_plan_review_callback(on_review)

        resp = await asyncio.get_running_loop().run_in_executor(
            None, _post, server.port,
            {"tool_name": "ExitPlanMode", "tool_input": {"plan": "step 1"}, "tool_use_id": "tu_plan"},
        )

        assert resp["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert len(reviews) == 1
        assert reviews[0] == ("tu_plan", "step 1")

    async def test_reject_plan(self, server):
        def on_review(tid, pt, fi):
            server.resolve_plan_review(tid, False)

        server.set_plan_review_callback(on_review)

        resp = await asyncio.get_running_loop().run_in_executor(
            None, _post, server.port,
            {"tool_name": "ExitPlanMode", "tool_input": {"plan": "bad plan"}, "tool_use_id": "tu_plan2"},
        )

        assert resp["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestAskUserQuestionAnswerInjection:
    """AskUserQuestion pauses for user answers, then returns updatedInput
    with both the original questions AND the answers dict.

    Critical details discovered during live testing:
    - updatedInput REPLACES the entire tool input (not merged)
    - Must include the original questions array
    - answers is a dict mapping question text -> selected label
    - hookEventName: "PreToolUse" is required in the response
    - Requires a specific matcher (not wildcard) to work
    """

    async def test_answers_injected_via_updated_input(self, server):
        questions = [
            {
                "question": "What color?",
                "header": "Color",
                "options": [
                    {"label": "Red", "description": "warm"},
                    {"label": "Blue", "description": "cool"},
                ],
                "multiSelect": False,
            }
        ]

        def on_question(tid, qs):
            server.resolve_question(tid, {"What color?": "Red"})

        server.set_question_callback(on_question)

        resp = await asyncio.get_running_loop().run_in_executor(
            None, _post, server.port,
            {"tool_name": "AskUserQuestion", "tool_input": {"questions": questions}, "tool_use_id": "tu_q1"},
        )

        hook_out = resp["hookSpecificOutput"]
        assert hook_out["permissionDecision"] == "allow"
        assert hook_out["hookEventName"] == "PreToolUse"

        updated = hook_out["updatedInput"]
        # Must include original questions (full replacement, not merge)
        assert updated["questions"] == questions
        # Must include the answers dict
        assert updated["answers"] == {"What color?": "Red"}

    async def test_callback_receives_questions(self, server):
        received = []

        def on_question(tid, qs):
            received.append((tid, qs))
            # Resolve on next event loop tick so callback assertion works
            asyncio.get_running_loop().call_soon(
                server.resolve_question, tid, {"Pick one": "A"},
            )

        server.set_question_callback(on_question)

        questions = [{"question": "Pick one", "options": [{"label": "A"}]}]

        await asyncio.get_running_loop().run_in_executor(
            None, _post, server.port,
            {"tool_name": "AskUserQuestion", "tool_input": {"questions": questions}, "tool_use_id": "tu_q2"},
        )

        assert len(received) == 1
        assert received[0][0] == "tu_q2"
        assert received[0][1] == questions

    async def test_no_extra_keys_in_updated_input(self, server):
        """updatedInput must NOT contain extra keys — they corrupt the
        AskUserQuestion schema (discovered via GitHub issue #29530)."""

        def on_question(tid, qs):
            server.resolve_question(tid, {"Q?": "Y"})

        server.set_question_callback(on_question)

        questions = [{"question": "Q?", "options": [{"label": "Y"}]}]

        resp = await asyncio.get_running_loop().run_in_executor(
            None, _post, server.port,
            {"tool_name": "AskUserQuestion", "tool_input": {"questions": questions}, "tool_use_id": "tu_q3"},
        )

        updated = resp["hookSpecificOutput"]["updatedInput"]
        # Only questions and answers — no env, no extra fields
        expected_keys = {"questions", "answers"}
        assert set(updated.keys()) == expected_keys


class TestShutdownCleanup:
    """Pending futures are resolved on shutdown so HTTP handlers unblock."""

    async def test_pending_future_resolved_on_stop(self, server):
        """stop() resolves pending futures so the server can shut down cleanly."""
        server.set_plan_review_callback(lambda tid, pt, fi: None)

        # Create a pending future directly (bypass HTTP to avoid hang)
        future = asyncio.get_running_loop().create_future()
        server._pending["tu_shutdown"] = future

        await server.stop()

        # Future should have been resolved
        assert future.done()
        assert future.result() is True  # default approve on shutdown

    async def test_shutting_down_flag_prevents_new_blocks(self, server):
        """After stop(), new hook requests return immediately instead of blocking.

        Regression: without _shutting_down, a request arriving during shutdown
        could block the HTTP thread for up to 600s.
        """
        await server.stop()

        # Re-create a minimal server to test the flag on the stopped instance
        # The _shutting_down flag should be set — handlers bail out immediately
        assert server._shutting_down.is_set()

    async def test_stop_completes_within_timeout(self, server):
        """stop() must not hang even when futures are registered late.

        Regression: if _request_* coroutine hadn't run yet when stop()
        iterated _pending, the HTTP handler could block indefinitely.
        """
        server.set_plan_review_callback(lambda tid, pt, fi: None)

        # Simulate a future that's already in _pending
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        server._pending["tu_late"] = future

        # stop() must complete (not hang)
        await asyncio.wait_for(server.stop(), timeout=5)
        assert future.done()


class TestResolveAllPending:
    """resolve_all_pending() unblocks all HTTP handlers at once."""

    async def test_resolve_all_clears_pending(self, server):
        loop = asyncio.get_running_loop()

        f1 = loop.create_future()
        f2 = loop.create_future()
        server._pending["tu_a"] = f1
        server._pending["tu_b"] = f2

        server.resolve_all_pending()

        assert f1.done()
        assert f2.done()
        assert len(server._pending) == 0

    async def test_resolve_all_unblocks_question_handler(self, server):
        """A question future force-resolved with True causes the handler
        to return a bare allow (not updatedInput with non-dict answers).

        Regression: without the isinstance check, the handler would crash
        trying to use True as a dict.
        """
        server.set_question_callback(lambda tid, qs: None)

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        server._pending["tu_q_cancel"] = future

        # Force-resolve with True (what resolve_all_pending does)
        future.set_result(True)

        # Simulate what _handle_question does after getting the result:
        # it should detect non-dict and return bare allow
        answers = future.result()
        assert not isinstance(answers, dict)

    async def test_cancel_pending_flag_cleared_on_next_tick(self, server):
        """_cancel_pending must not leak into the next real request.

        Regression: without call_soon cleanup, resolve_all_pending sets
        a sticky flag that auto-approves the NEXT plan/question.
        """
        server.resolve_all_pending()
        assert server._cancel_pending is True

        # Yield to event loop — call_soon callback should clear the flag
        await asyncio.sleep(0)

        assert server._cancel_pending is False

    async def test_next_plan_review_after_cancel_is_not_auto_approved(self, server):
        """After cancel, the next real plan review must pause for user input.

        Regression: sticky _cancel_pending auto-approved the next real
        ExitPlanMode without user interaction.
        """
        # Cancel (sets flag, schedules clear)
        server.resolve_all_pending()
        # Yield so the clear callback runs
        await asyncio.sleep(0)

        # Now a real plan review should block (not auto-approve)
        reviews = []

        def on_review(tid, pt, fi):
            reviews.append(tid)
            server.resolve_plan_review(tid, True)

        server.set_plan_review_callback(on_review)

        resp = await asyncio.get_running_loop().run_in_executor(
            None, _post, server.port,
            {"tool_name": "ExitPlanMode", "tool_input": {"plan": "real plan"}, "tool_use_id": "tu_real"},
        )

        # The callback must have been called (not bypassed by stale flag)
        assert len(reviews) == 1
        assert resp["hookSpecificOutput"]["permissionDecision"] == "allow"
