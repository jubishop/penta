"""Tests for the HTTP permission server — hook format, matchers, and resolution."""

from __future__ import annotations

import asyncio
import json
import urllib.request

import pytest

from penta.services.permission_server import PermissionServer


@pytest.fixture
async def server():
    loop = asyncio.get_running_loop()
    srv = PermissionServer(loop)
    assert srv.start()
    yield srv
    srv.stop()


def _post(port: int, body: dict) -> dict:
    """POST JSON to the permission server and return the parsed response."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/permission",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


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
        server.set_plan_review_callback(
            lambda tid, pt, fi: reviews.append((tid, pt))
        )

        async def approve_after_delay():
            await asyncio.sleep(0.1)
            server.resolve_plan_review("tu_plan", True)

        asyncio.create_task(approve_after_delay())

        # This blocks until resolved
        resp = await asyncio.get_running_loop().run_in_executor(
            None, _post, server.port,
            {"tool_name": "ExitPlanMode", "tool_input": {"plan": "step 1"}, "tool_use_id": "tu_plan"},
        )

        assert resp["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert len(reviews) == 1
        assert reviews[0] == ("tu_plan", "step 1")

    async def test_reject_plan(self, server):
        server.set_plan_review_callback(lambda tid, pt, fi: None)

        async def reject_after_delay():
            await asyncio.sleep(0.1)
            server.resolve_plan_review("tu_plan2", False)

        asyncio.create_task(reject_after_delay())

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
        server.set_question_callback(lambda tid, qs: None)

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

        async def answer_after_delay():
            await asyncio.sleep(0.1)
            server.resolve_question("tu_q1", {"What color?": "Red"})

        asyncio.create_task(answer_after_delay())

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
        server.set_question_callback(
            lambda tid, qs: received.append((tid, qs))
        )

        questions = [{"question": "Pick one", "options": [{"label": "A"}]}]

        async def answer_after_delay():
            await asyncio.sleep(0.1)
            server.resolve_question("tu_q2", {"Pick one": "A"})

        asyncio.create_task(answer_after_delay())

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
        server.set_question_callback(lambda tid, qs: None)

        questions = [{"question": "Q?", "options": [{"label": "Y"}]}]

        async def answer():
            await asyncio.sleep(0.1)
            server.resolve_question("tu_q3", {"Q?": "Y"})

        asyncio.create_task(answer())

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

    async def test_pending_plan_resolved_on_stop(self, server):
        server.set_plan_review_callback(lambda tid, pt, fi: None)

        # Start a request that will block
        task = asyncio.get_running_loop().run_in_executor(
            None, _post, server.port,
            {"tool_name": "ExitPlanMode", "tool_input": {"plan": "x"}, "tool_use_id": "tu_shutdown"},
        )

        await asyncio.sleep(0.1)
        server.stop()

        # Should complete without hanging
        resp = await asyncio.wait_for(task, timeout=5)
        assert resp["hookSpecificOutput"]["permissionDecision"] == "allow"
