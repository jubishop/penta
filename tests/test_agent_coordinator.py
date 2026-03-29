"""Tests for AgentCoordinator behaviour."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from penta.coordinators.agent_coordinator import AgentCoordinator
from penta.models import AgentConfig, AgentStatus, AgentType, Message, TaggedMessage
from penta.services.agent_service import StreamEvent, StreamEventType
from penta.services.db import PentaDB

from .fakes import FakeAgentService


# -- Helpers ------------------------------------------------------------------


def _make_coordinator(db: PentaDB, fake: FakeAgentService) -> AgentCoordinator:
    config = AgentConfig(
        id=uuid4(),
        name="test-agent",
        type=AgentType.CLAUDE,
        status=AgentStatus.IDLE,
    )
    return AgentCoordinator(
        config=config,
        working_dir=Path("/tmp"),
        db=db,
        other_agent_names=["other"],
        service=fake,
    )


async def _let_task_start() -> None:
    """Yield to the event loop so a just-created task can advance to its
    first await (e.g. the hang point in FakeAgentService)."""
    await asyncio.sleep(0)


# -- Fixtures -----------------------------------------------------------------


@pytest.fixture
def fake() -> FakeAgentService:
    return FakeAgentService()


@pytest.fixture
def coordinator(memory_db: PentaDB, fake: FakeAgentService) -> AgentCoordinator:
    return _make_coordinator(memory_db, fake)


# -- Tests --------------------------------------------------------------------


class TestFirstMessageNoCatchUp:
    """On a fresh chat with no history, the prompt should contain only the
    current message — no '[Messages since your last response:]' header."""

    async def test_fresh_chat_no_catchup_header(self, coordinator: AgentCoordinator):
        """First message ever sent should not include catch-up framing."""
        current = TaggedMessage(sender_label="User", text="hello")
        prompt = coordinator._build_prompt(current)

        assert "[Messages since your last response:]" not in prompt
        assert "[New message:]" not in prompt
        assert "hello" in prompt


class TestCatchUpHistoryAfterRestart:
    """After load_chat_history(), the first prompt should replay all prior
    messages as catch-up context (last_prompted_index=0)."""

    async def test_first_prompt_includes_full_history(self, coordinator: AgentCoordinator):
        coordinator.full_history = [
            TaggedMessage(sender_label="User", text="first message"),
            TaggedMessage(sender_label="other", text="reply from other"),
        ]
        coordinator.last_prompted_index = 0

        current = TaggedMessage(sender_label="User", text="new question")
        prompt = coordinator._build_prompt(current)

        assert "[Messages since your last response:]" in prompt
        assert "first message" in prompt
        assert "reply from other" in prompt
        assert "new question" in prompt

    async def test_second_prompt_does_not_repeat_catchup(self, coordinator: AgentCoordinator):
        coordinator.full_history = [
            TaggedMessage(sender_label="User", text="old message"),
        ]
        coordinator.last_prompted_index = 0

        first = TaggedMessage(sender_label="User", text="first new")
        coordinator._build_prompt(first)
        coordinator.full_history.append(first)
        coordinator.last_prompted_index = len(coordinator.full_history)

        second = TaggedMessage(sender_label="User", text="second new")
        prompt = coordinator._build_prompt(second)

        assert "old message" not in prompt
        assert "first new" not in prompt
        assert "second new" in prompt


class TestCancelledStreamIsNotTreatedAsSuccess:
    """Regression: cancelling a stream must NOT persist, route, or callback."""

    async def test_cancelled_response_is_flagged(
        self, coordinator: AgentCoordinator, fake: FakeAgentService,
    ):
        """A cancelled stream should set is_cancelled on the response."""
        fake.enqueue_hang()
        fake.enqueue_hang()

        conversation: list[Message] = []
        tagged = TaggedMessage(sender_label="User", text="hello")

        response = coordinator.send(tagged, conversation)
        await _let_task_start()
        assert response.text == "partial"

        # Cancel by sending a new message (same as the real flow).
        tagged2 = TaggedMessage(sender_label="User", text="new message")
        coordinator.send(tagged2, conversation)
        await response.wait_for_completion()

        assert response.is_cancelled is True
        assert response.is_streaming is False

        # Clean up second task.
        coordinator._current_task.cancel()
        await asyncio.sleep(0)

    async def test_cancelled_stream_not_added_to_history(
        self, coordinator: AgentCoordinator, fake: FakeAgentService,
    ):
        """Cancelled response must NOT be appended to full_history."""
        fake.enqueue_hang()
        fake.enqueue_hang()

        conversation: list[Message] = []
        tagged = TaggedMessage(sender_label="User", text="hello")

        response = coordinator.send(tagged, conversation)
        await _let_task_start()

        history_before = len(coordinator.full_history)

        # Cancel via a new send.
        tagged2 = TaggedMessage(sender_label="User", text="new")
        coordinator.send(tagged2, conversation)
        await response.wait_for_completion()

        # Only the new user message should have been added, not a cancelled agent reply.
        agent_replies = [
            m
            for m in coordinator.full_history[history_before:]
            if m.sender_label == coordinator.config.name
        ]
        assert agent_replies == []

        coordinator._current_task.cancel()
        await asyncio.sleep(0)

    async def test_cancelled_stream_fires_callback_with_cancelled_flag(
        self, coordinator: AgentCoordinator, fake: FakeAgentService,
    ):
        """on_stream_complete MUST fire for cancelled streams (UI cleanup)
        but the message should carry is_cancelled=True."""
        fake.enqueue_hang()
        fake.enqueue_hang()

        completed: list[Message] = []
        coordinator.on_stream_complete = lambda msg, _aid: completed.append(msg)

        conversation: list[Message] = []
        tagged = TaggedMessage(sender_label="User", text="hello")
        response = coordinator.send(tagged, conversation)
        await _let_task_start()

        # Cancel.
        tagged2 = TaggedMessage(sender_label="User", text="new")
        coordinator.send(tagged2, conversation)
        await response.wait_for_completion()

        assert len(completed) == 1, "on_stream_complete must fire so the UI can clear streaming state"
        assert completed[0].is_cancelled is True

        coordinator._current_task.cancel()
        await asyncio.sleep(0)


class TestCancelledStreamRollsBackPromptIndex:
    """Regression: when two agents finish simultaneously and both mention the
    same target agent, the second send() cancels the first stream.  The
    replacement prompt must include messages from the cancelled stream so the
    agent doesn't miss context."""

    async def test_cancelled_send_does_not_skip_messages(
        self, coordinator: AgentCoordinator, fake: FakeAgentService,
    ):
        fake.enqueue_hang()
        fake.enqueue_hang()
        fake.enqueue_hang()

        conversation: list[Message] = []

        # Seed: a user message was already prompted (hop 0).
        user_msg = TaggedMessage(sender_label="User", text="plan a trip")
        resp0 = coordinator.send(user_msg, conversation)
        await _let_task_start()
        coordinator._current_task.cancel()
        await resp0.wait_for_completion()
        # Manually advance as if the stream finished (normal completion path).
        coordinator.last_prompted_index = len(coordinator.full_history)
        coordinator._pre_prompt_index = coordinator.last_prompted_index

        # Now two agents respond simultaneously and both mention us.
        codex_msg = TaggedMessage(sender_label="codex", text="@test-agent here's my take")
        other_msg = TaggedMessage(sender_label="other", text="@test-agent I agree")

        # First send: Codex's message arrives.
        resp1 = coordinator.send(codex_msg, conversation)
        await _let_task_start()

        # Second send: other agent's message arrives, cancelling Codex's stream.
        coordinator.send(other_msg, conversation)
        await resp1.wait_for_completion()

        assert resp1.is_cancelled is True

        # Verify: build a third prompt and confirm Codex's message
        # is NOT in the catch-up (meaning it WAS included in the second prompt).
        coordinator._current_task.cancel()
        await asyncio.sleep(0)
        coordinator.last_prompted_index = len(coordinator.full_history)

        check_msg = TaggedMessage(sender_label="User", text="check")
        check_prompt = coordinator._build_prompt(check_msg)

        assert "@test-agent here's my take" not in check_prompt
        assert "@test-agent I agree" not in check_prompt
        assert "check" in check_prompt

    async def test_replacement_prompt_contains_cancelled_message(
        self, coordinator: AgentCoordinator, fake: FakeAgentService,
    ):
        """Directly verify the prompt text of the replacement send includes
        the message from the cancelled stream."""
        fake.enqueue_hang()
        fake.enqueue_hang()

        conversation: list[Message] = []

        # Prime: a user message was already prompted.
        user_msg = TaggedMessage(sender_label="User", text="hello")
        coordinator._build_prompt(user_msg)
        coordinator.full_history.append(user_msg)
        coordinator.last_prompted_index = len(coordinator.full_history)

        # Two agents respond simultaneously.
        codex_msg = TaggedMessage(sender_label="codex", text="@test-agent Codex here")
        other_msg = TaggedMessage(sender_label="other", text="@test-agent Other here")

        # First send (Codex) — starts streaming.
        resp1 = coordinator.send(codex_msg, conversation)
        await _let_task_start()

        # Capture prompt for the second send.
        prompts: list[str] = []
        original_build = coordinator._build_prompt

        def capturing_build(tagged):
            result = original_build(tagged)
            prompts.append(result)
            return result

        coordinator._build_prompt = capturing_build  # type: ignore[assignment]
        coordinator.send(other_msg, conversation)
        await resp1.wait_for_completion()

        assert len(prompts) == 1
        replacement_prompt = prompts[0]

        # The replacement prompt MUST contain Codex's message as catch-up.
        assert "Codex here" in replacement_prompt, (
            "Codex's message was lost — the cancelled stream advanced "
            "last_prompted_index past it"
        )
        assert "Other here" in replacement_prompt

        coordinator._current_task.cancel()
        await asyncio.sleep(0)


class TestShutdownAwaitsCancelTask:
    """Regression: shutdown() must wait for the in-flight cancel task to finish
    before calling service.shutdown(), otherwise cancel and shutdown race."""

    async def test_cancel_completes_before_shutdown(
        self, memory_db: PentaDB,
    ):
        fake = FakeAgentService()
        fake._cancel_delay = 0.05
        coord = _make_coordinator(memory_db, fake)

        await coord.shutdown()

        assert fake.order == ["cancel", "shutdown"]


class TestSendWhileStreamingWaitsForCleanup:
    """Regression: when send() cancels an in-flight stream and immediately
    starts a new one, the new stream must wait for the old task's cleanup.

    The FakeAgentService enforces the real CliAgentService single-stream
    invariant: it raises RuntimeError if send() is called while _streaming
    is still True.  If the coordinator's wait_for mechanism were removed,
    this test would fail with RuntimeError."""

    async def test_replacement_send_does_not_raise(
        self, memory_db: PentaDB,
    ):
        fake = FakeAgentService()
        fake.enqueue_hang()
        fake.enqueue_hang()
        coord = _make_coordinator(memory_db, fake)

        completed: list[Message] = []
        coord.on_stream_complete = lambda msg, _aid: completed.append(msg)

        conversation: list[Message] = []

        # First send — starts streaming.
        resp1 = coord.send(
            TaggedMessage(sender_label="User", text="first"), conversation,
        )
        await _let_task_start()
        assert resp1.text == "partial"

        # Second send — cancels first, must NOT raise RuntimeError.
        resp2 = coord.send(
            TaggedMessage(sender_label="User", text="second"), conversation,
        )
        await resp1.wait_for_completion()

        # First stream should be cancelled, second should be streaming.
        assert resp1.is_cancelled is True
        assert resp2.text == "partial"  # second stream started successfully

        # Cleanup.
        coord._current_task.cancel()
        await asyncio.sleep(0)


class TestServiceFailureDoesNotWedgeUI:
    """If the service raises an unexpected exception, the message must still
    be marked complete and the UI cleaned up."""

    async def test_exception_marks_complete_and_fires_callback(
        self, memory_db: PentaDB,
    ):
        fake = FakeAgentService()
        fake.enqueue_exception(
            ConnectionResetError("boom"),
            prefix_events=[StreamEvent(type=StreamEventType.TEXT_DELTA, text="partial")],
        )
        coord = _make_coordinator(memory_db, fake)

        completed: list[Message] = []
        coord.on_stream_complete = lambda msg, _aid: completed.append(msg)

        conversation: list[Message] = []
        tagged = TaggedMessage(sender_label="User", text="hello")
        response = coord.send(tagged, conversation)
        await response.wait_for_completion()

        # Must have fired the callback
        assert len(completed) == 1
        assert completed[0].is_error is True
        assert not completed[0].is_streaming
        assert "failed" in completed[0].text.lower()
        # Status should be IDLE, not stuck
        assert coord.config.status == AgentStatus.IDLE


class TestTripleCancellation:
    """Rapid-fire A→B→C where each replacement send cancels the previous one.

    Verifies that last_prompted_index rollback stays consistent across
    multiple cancellations and only the final stream's response is kept."""

    async def test_double_cancel_index_stays_consistent(
        self, memory_db: PentaDB,
    ):
        """Three sends in quick succession: first two are cancelled, third
        completes.  The final prompt must contain all three messages."""
        fake = FakeAgentService()
        fake.enqueue_hang()       # first send — will be cancelled
        fake.enqueue_hang()       # second send — will also be cancelled
        fake.enqueue_text("done") # third send — completes
        coord = _make_coordinator(memory_db, fake)

        # Seed: prime the coordinator with a user message so we have a baseline.
        user_msg = TaggedMessage(sender_label="User", text="hello")
        coord._build_prompt(user_msg)
        coord.full_history.append(user_msg)
        coord.last_prompted_index = len(coord.full_history)

        conversation: list[Message] = []

        # Send A — starts streaming, hangs.
        msg_a = TaggedMessage(sender_label="alice", text="@test-agent from alice")
        resp_a = coord.send(msg_a, conversation)
        await _let_task_start()

        # Send B — cancels A, starts streaming, hangs.
        msg_b = TaggedMessage(sender_label="bob", text="@test-agent from bob")
        resp_b = coord.send(msg_b, conversation)
        await resp_a.wait_for_completion()
        assert resp_a.is_cancelled is True

        await _let_task_start()

        # Send C — cancels B, starts streaming, completes.
        msg_c = TaggedMessage(sender_label="carol", text="@test-agent from carol")
        resp_c = coord.send(msg_c, conversation)
        await resp_b.wait_for_completion()
        assert resp_b.is_cancelled is True

        await resp_c.wait_for_completion()
        assert resp_c.is_cancelled is False
        assert resp_c.text == "done"

    async def test_double_cancel_final_prompt_contains_all_messages(
        self, memory_db: PentaDB,
    ):
        """The third (final) prompt must include messages from both
        cancelled streams as catch-up context."""
        fake = FakeAgentService()
        fake.enqueue_hang()
        fake.enqueue_hang()
        fake.enqueue_text("done")
        coord = _make_coordinator(memory_db, fake)

        user_msg = TaggedMessage(sender_label="User", text="hello")
        coord._build_prompt(user_msg)
        coord.full_history.append(user_msg)
        coord.last_prompted_index = len(coord.full_history)

        conversation: list[Message] = []

        # Capture all prompts sent to the service.
        prompts: list[str] = []
        original_build = coord._build_prompt

        def capturing_build(tagged):
            result = original_build(tagged)
            prompts.append(result)
            return result

        coord._build_prompt = capturing_build  # type: ignore[assignment]

        # Send A, B, C in rapid succession.
        msg_a = TaggedMessage(sender_label="alice", text="from alice")
        resp_a = coord.send(msg_a, conversation)
        await _let_task_start()

        msg_b = TaggedMessage(sender_label="bob", text="from bob")
        resp_b = coord.send(msg_b, conversation)
        await resp_a.wait_for_completion()
        await _let_task_start()

        msg_c = TaggedMessage(sender_label="carol", text="from carol")
        coord.send(msg_c, conversation)
        await resp_b.wait_for_completion()

        # The third prompt (index 2) must contain alice, bob, AND carol.
        final_prompt = prompts[2]
        assert "from alice" in final_prompt
        assert "from bob" in final_prompt
        assert "from carol" in final_prompt

    async def test_double_cancel_only_final_response_in_history(
        self, memory_db: PentaDB,
    ):
        """Only the final (non-cancelled) response should appear in
        full_history.  The two cancelled responses must not."""
        fake = FakeAgentService()
        fake.enqueue_hang()
        fake.enqueue_hang()
        fake.enqueue_text("final answer")
        coord = _make_coordinator(memory_db, fake)

        conversation: list[Message] = []
        history_before = len(coord.full_history)

        msg_a = TaggedMessage(sender_label="alice", text="from alice")
        resp_a = coord.send(msg_a, conversation)
        await _let_task_start()

        msg_b = TaggedMessage(sender_label="bob", text="from bob")
        resp_b = coord.send(msg_b, conversation)
        await resp_a.wait_for_completion()
        await _let_task_start()

        msg_c = TaggedMessage(sender_label="carol", text="from carol")
        resp_c = coord.send(msg_c, conversation)
        await resp_b.wait_for_completion()
        await resp_c.wait_for_completion()

        agent_replies = [
            m
            for m in coord.full_history[history_before:]
            if m.sender_label == coord.config.name
        ]
        assert len(agent_replies) == 1
        assert agent_replies[0].text == "final answer"


class TestPartialTextFromCancelledStream:
    """Verify that partial text streamed before cancellation doesn't
    leak into coordinator history or subsequent prompts."""

    async def test_partial_text_not_in_history(
        self, memory_db: PentaDB,
    ):
        """Cancelled stream's partial text must not appear in full_history."""
        fake = FakeAgentService()
        fake.enqueue_hang(prefix_text="leaked partial")
        fake.enqueue_text("clean response")
        coord = _make_coordinator(memory_db, fake)

        conversation: list[Message] = []
        history_before = len(coord.full_history)

        # First send — streams "leaked partial" then hangs.
        resp1 = coord.send(
            TaggedMessage(sender_label="User", text="first"), conversation,
        )
        await _let_task_start()
        assert resp1.text == "leaked partial"

        # Second send — cancels first, completes normally.
        resp2 = coord.send(
            TaggedMessage(sender_label="User", text="second"), conversation,
        )
        await resp1.wait_for_completion()
        await resp2.wait_for_completion()

        all_texts = [m.text for m in coord.full_history[history_before:]]
        assert "leaked partial" not in all_texts

    async def test_partial_text_not_in_subsequent_prompt(
        self, memory_db: PentaDB,
    ):
        """A prompt built after a cancelled stream must not include
        the partial text as catch-up context."""
        fake = FakeAgentService()
        fake.enqueue_hang(prefix_text="leaked partial")
        fake.enqueue_text("clean response")
        fake.enqueue_text("third response")
        coord = _make_coordinator(memory_db, fake)

        conversation: list[Message] = []

        # First send — streams partial then hangs.
        resp1 = coord.send(
            TaggedMessage(sender_label="User", text="first"), conversation,
        )
        await _let_task_start()

        # Second send — cancels first.
        resp2 = coord.send(
            TaggedMessage(sender_label="User", text="second"), conversation,
        )
        await resp1.wait_for_completion()
        await resp2.wait_for_completion()

        # Third send — prompt should not contain "leaked partial".
        prompts: list[str] = []
        original_build = coord._build_prompt

        def capturing_build(tagged):
            result = original_build(tagged)
            prompts.append(result)
            return result

        coord._build_prompt = capturing_build  # type: ignore[assignment]
        resp3 = coord.send(
            TaggedMessage(sender_label="User", text="third"), conversation,
        )
        await resp3.wait_for_completion()

        assert "leaked partial" not in prompts[0]
