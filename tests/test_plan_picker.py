"""Textual pilot tests for PlanPickerScreen."""

from __future__ import annotations

from uuid import uuid4

from textual.app import App, ComposeResult
from textual.widgets import Static

from penta.models.pending_plan import PendingPlan
from penta.widgets.plan_picker import PlanPickerScreen


class _TestApp(App):
    def compose(self) -> ComposeResult:
        yield Static("base")


def _make_plans() -> dict:
    id1 = uuid4()
    id2 = uuid4()
    return {
        id1: PendingPlan(
            agent_id=id1,
            agent_name="Claude",
            control_request_id="cr_1",
            plan_text="Claude's plan",
        ),
        id2: PendingPlan(
            agent_id=id2,
            agent_name="Codex",
            control_request_id="cr_2",
            plan_text="Codex's plan",
        ),
    }


async def test_escape_dismisses_with_none():
    """Pressing escape should dismiss with None."""
    app = _TestApp()
    results = []

    async with app.run_test() as pilot:
        app.push_screen(
            PlanPickerScreen(_make_plans()),
            callback=lambda r: results.append(r),
        )
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert len(results) == 1
    assert results[0] is None


async def test_cancel_dismisses_with_none():
    app = _TestApp()
    results = []

    async with app.run_test() as pilot:
        modal = PlanPickerScreen(_make_plans())
        app.push_screen(modal, callback=lambda r: results.append(r))
        await pilot.pause()

        cancel = modal.query_one("#cancel-btn")
        await pilot.click(cancel)
        await pilot.pause()

    assert len(results) == 1
    assert results[0] is None


async def test_select_returns_agent_id():
    """Selecting a plan and clicking Select should return the agent_id."""
    plans = _make_plans()
    plan_ids = list(plans.keys())
    app = _TestApp()
    results = []

    async with app.run_test() as pilot:
        modal = PlanPickerScreen(plans)
        app.push_screen(modal, callback=lambda r: results.append(r))
        await pilot.pause()

        # Focus the radio set and select first option
        radio_set = modal.query_one("RadioSet")
        radio_set.focus()
        await pilot.press("enter")
        await pilot.pause()

        select_btn = modal.query_one("#select-btn")
        await pilot.click(select_btn)
        await pilot.pause()

    assert len(results) == 1
    assert results[0] == plan_ids[0]


async def test_renders_plan_names():
    """Both plan options should appear."""
    app = _TestApp()
    async with app.run_test() as pilot:
        modal = PlanPickerScreen(_make_plans())
        app.push_screen(modal)
        await pilot.pause()

        from textual.widgets import RadioButton
        buttons = modal.query(RadioButton)
        assert len(buttons) == 2
        labels = {b.label.plain for b in buttons}
        assert any("Claude" in l for l in labels)
        assert any("Codex" in l for l in labels)
