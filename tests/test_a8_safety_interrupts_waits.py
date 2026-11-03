"""A8 final gate -- Safety really interrupts waits, it does not merely outlast them.

The executor receives a preemption signal.  These tests give it waits that would
last practically forever (a completion wait and a settle pause of an hour) and
raise the signal from outside while the wait is running.  A correct executor
returns at once with status "preempted" and never starts the next cover; one
that only checks the signal *after* the wait would hang, which the guard
``asyncio.wait_for`` turns into a clean failure.

The guard timeout is a hang detector only; no assertion depends on elapsed time.

Self-contained: imports only production modules.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.smartshading.cover_control import dispatch_plan_executor as dpe
from custom_components.smartshading.cover_control.dispatch_plan import (
    DispatchPlanItem,
    build_dispatch_plan,
)
from custom_components.smartshading.cover_control.dispatch_plan_executor import (
    CompletionOutcome,
    DispatchOutcome,
    DispatchPlanExecutor,
    ExecutionValidation,
    ValidationAction,
)
from custom_components.smartshading.engines.dispatch_classification import DispatchTargetClass

_HOUR_S = 3600.0
_HANG_GUARD_S = 5.0


def _plan(count: int):
    items = [
        DispatchPlanItem(
            zone_id="z1", zone_index=0, cover_entity_id=f"cover.c{i}", cover_index=i,
            target_ha=40, target_class=DispatchTargetClass.INTERMEDIATE, decision_ref=f"d{i}",
            zone_generation=0, reason=None,
        )
        for i in range(count)
    ]
    return build_dispatch_plan(plan_id="p", trigger="scheduled_evaluation", items=items, created_at="t0")


class _Harness:
    def __init__(self, *, long_completion_wait: bool) -> None:
        self.cancellation = asyncio.Event()
        self.dispatched: list[str] = []
        self.completion_wait_started = asyncio.Event()
        self.settle_started = asyncio.Event()
        self.long_completion_wait = long_completion_wait

    async def dispatch_item(self, item):
        self.dispatched.append(item.cover_entity_id)
        return DispatchOutcome(success=True)

    async def wait_for_completion(self, item, cancellation):
        if self.long_completion_wait:
            self.completion_wait_started.set()
            await asyncio.sleep(_HOUR_S)
        return CompletionOutcome(status="completed", completion_method="position")

    async def sleep(self, seconds):
        self.settle_started.set()
        await asyncio.sleep(seconds)

    async def run(self, plan):
        return await DispatchPlanExecutor().execute_plan(
            plan=plan,
            dispatch_item=self.dispatch_item,
            wait_for_completion=self.wait_for_completion,
            validate_item=lambda item: ExecutionValidation(ValidationAction.EXECUTE),
            is_generation_current=lambda item: True,
            cancellation=self.cancellation,
            sleep=self.sleep,
            clock=lambda: 0.0,
        )


async def _safety_arrives_after(event: asyncio.Event, harness: _Harness) -> None:
    await event.wait()
    for _ in range(3):                  # let the executor settle into the wait
        await asyncio.sleep(0)
    harness.cancellation.set()


@pytest.mark.parametrize("count", [2, 3, 4])
def test_safety_ends_a_running_completion_wait(count: int) -> None:
    async def scenario():
        harness = _Harness(long_completion_wait=True)
        trigger = asyncio.ensure_future(_safety_arrives_after(harness.completion_wait_started, harness))
        try:
            result = await asyncio.wait_for(harness.run(_plan(count)), timeout=_HANG_GUARD_S)
        finally:
            trigger.cancel()
        return harness, result

    harness, result = asyncio.run(scenario())
    assert result.status == "preempted"
    assert harness.dispatched == ["cover.c0"], "no further cover may start after Safety"


@pytest.mark.parametrize("count", [2, 3, 4])
def test_safety_ends_a_running_settle_pause(count: int, monkeypatch) -> None:
    monkeypatch.setattr(dpe, "INTERMEDIATE_POST_COMPLETION_PAUSE_S", _HOUR_S)

    async def scenario():
        harness = _Harness(long_completion_wait=False)
        trigger = asyncio.ensure_future(_safety_arrives_after(harness.settle_started, harness))
        try:
            result = await asyncio.wait_for(harness.run(_plan(count)), timeout=_HANG_GUARD_S)
        finally:
            trigger.cancel()
        return harness, result

    harness, result = asyncio.run(scenario())
    assert result.status == "preempted"
    assert harness.dispatched == ["cover.c0"], "no further cover may start after Safety"


def test_no_task_is_left_behind_after_a_preempted_wait(monkeypatch) -> None:
    monkeypatch.setattr(dpe, "INTERMEDIATE_POST_COMPLETION_PAUSE_S", _HOUR_S)

    async def scenario():
        harness = _Harness(long_completion_wait=False)
        trigger = asyncio.ensure_future(_safety_arrives_after(harness.settle_started, harness))
        await asyncio.wait_for(harness.run(_plan(3)), timeout=_HANG_GUARD_S)
        await trigger
        await asyncio.sleep(0)
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    assert asyncio.run(scenario()) == []


def test_without_safety_the_same_plan_runs_to_the_end(monkeypatch) -> None:
    """Control: the waits above are interruptible, not simply skipped."""
    monkeypatch.setattr(dpe, "INTERMEDIATE_POST_COMPLETION_PAUSE_S", 0.0)

    async def scenario():
        harness = _Harness(long_completion_wait=False)
        result = await asyncio.wait_for(harness.run(_plan(3)), timeout=_HANG_GUARD_S)
        return harness, result

    harness, result = asyncio.run(scenario())
    assert result.status == "completed"
    assert harness.dispatched == ["cover.c0", "cover.c1", "cover.c2"]
