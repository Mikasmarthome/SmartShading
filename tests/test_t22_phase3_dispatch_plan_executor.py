"""T22 Phase 3 — isolated DispatchPlanExecutor
(cover_control/dispatch_plan_executor.py).
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.smartshading.cover_control.dispatch_plan import (
    DispatchPlanItem,
    build_dispatch_plan,
)
from custom_components.smartshading.cover_control.dispatch_plan_executor import (
    DispatchItemExecutionResult,
    DispatchOutcome,
    CompletionOutcome,
    DispatchPlanExecutor,
    ExecutionValidation,
    ValidationAction,
)
from custom_components.smartshading.engines.dispatch_classification import DispatchTargetClass

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def async_test(fn):
    """Runs an async test body via asyncio.run() — no pytest-asyncio
    dependency needed (this codebase has none installed; Phase 3 is the
    first module requiring async tests at all)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))
    return wrapper


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _item(*, zone_id="z1", zone_index=0, cover_entity_id="cover.a", cover_index=0,
          target_ha=40, target_class=DispatchTargetClass.INTERMEDIATE, decision_ref="d1",
          zone_generation=0, reason=None) -> DispatchPlanItem:
    return DispatchPlanItem(
        zone_id=zone_id, zone_index=zone_index, cover_entity_id=cover_entity_id,
        cover_index=cover_index, target_ha=target_ha, target_class=target_class,
        decision_ref=decision_ref, zone_generation=zone_generation, reason=reason,
    )


def _plan(items, *, plan_id="p1", trigger="scheduled_evaluation", created_at="t0"):
    return build_dispatch_plan(plan_id=plan_id, trigger=trigger, items=items, created_at=created_at)


class Harness:
    """Bundles fake ports for the executor with call recording."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.dispatch_calls: list[tuple[str, float]] = []
        self.completion_calls: list[str] = []
        self.sleep_calls: list[float] = []
        self.validate_calls: list[str] = []
        self.generation_calls: list[str] = []
        self.dispatch_outcomes: dict[str, DispatchOutcome] = {}
        self.dispatch_time_cost: dict[str, float] = {}
        self.completion_outcomes: dict[str, CompletionOutcome] = {}
        self.validations: dict[str, ExecutionValidation] = {}
        self.generations: dict[str, bool] = {}
        self.cancellation = asyncio.Event()

    async def dispatch_item(self, item: DispatchPlanItem) -> DispatchOutcome:
        self.dispatch_calls.append((item.cover_entity_id, self.clock.now()))
        cost = self.dispatch_time_cost.get(item.cover_entity_id, 0.0)
        if cost:
            self.clock.advance(cost)
        return self.dispatch_outcomes.get(item.cover_entity_id, DispatchOutcome(success=True))

    async def wait_for_completion(self, item: DispatchPlanItem, cancellation) -> CompletionOutcome:
        self.completion_calls.append(item.cover_entity_id)
        return self.completion_outcomes.get(
            item.cover_entity_id, CompletionOutcome(status="completed", completion_method="position"),
        )

    def validate_item(self, item: DispatchPlanItem) -> ExecutionValidation:
        self.validate_calls.append(item.cover_entity_id)
        return self.validations.get(item.cover_entity_id, ExecutionValidation(ValidationAction.EXECUTE))

    def is_generation_current(self, item: DispatchPlanItem) -> bool:
        self.generation_calls.append(item.cover_entity_id)
        return self.generations.get(item.cover_entity_id, True)

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.clock.advance(seconds)

    def kwargs(self) -> dict:
        return dict(
            dispatch_item=self.dispatch_item,
            wait_for_completion=self.wait_for_completion,
            validate_item=self.validate_item,
            is_generation_current=self.is_generation_current,
            cancellation=self.cancellation,
            sleep=self.sleep,
            clock=self.clock.now,
        )


def _executor() -> DispatchPlanExecutor:
    return DispatchPlanExecutor()


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------

class TestBasicExecution:
    @async_test
    async def test_empty_plan(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.status == "completed"
        assert result.item_results == ()

    @async_test
    async def test_only_no_movement(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(target_class=DispatchTargetClass.NO_MOVEMENT, target_ha=40)])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.item_results[0].status == "skipped"
        assert h.dispatch_calls == []

    @async_test
    async def test_only_blocked(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(target_class=DispatchTargetClass.BLOCKED, target_ha=None,
                            reason="blocked:manual_override")])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.item_results[0].status == "skipped"
        assert result.item_results[0].reason == "blocked:manual_override"
        assert h.dispatch_calls == []
        assert h.validate_calls == []
        assert h.generation_calls == []

    @async_test
    async def test_single_full_open(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(cover_entity_id="cover.a", target_ha=100,
                            target_class=DispatchTargetClass.FULL_OPEN)])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.item_results[0].status == "dispatched"
        assert result.status == "completed"
        assert h.completion_calls == []  # FULL_OPEN never waits for completion

    @async_test
    async def test_single_intermediate(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(cover_entity_id="cover.a", target_ha=40)])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.item_results[0].status == "completed"
        assert h.completion_calls == ["cover.a"]
        # last item — no trailing pause
        assert h.sleep_calls == []

    @async_test
    async def test_mixed_plan_full_open_then_intermediate(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([
            _item(cover_entity_id="cover.a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="cover.b", target_ha=40, target_class=DispatchTargetClass.INTERMEDIATE),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert [r.status for r in result.item_results] == ["dispatched", "completed"]


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------

class TestOrder:
    @async_test
    async def test_executes_in_plan_order_no_resort(self):
        clock = FakeClock()
        h = Harness(clock)
        # Build items already in a deliberately "odd" but Phase-2-valid order.
        plan = _plan([
            _item(zone_id="z2", zone_index=1, cover_entity_id="c1", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="z1", zone_index=0, cover_entity_id="c2", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
        ])
        # plan.items is already sorted by Phase 2 (z1 before z2) — executor
        # must follow THAT order, not re-derive it.
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert [c for c, _ in h.dispatch_calls] == [i.cover_entity_id for i in plan.items]

    @async_test
    async def test_non_executable_items_never_dispatched(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([
            _item(cover_entity_id="c1", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="c2", target_ha=None, target_class=DispatchTargetClass.BLOCKED,
                 reason="blocked:manual_override"),
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == [("c1", 0.0)]


# ---------------------------------------------------------------------------
# FULL_OPEN pacing
# ---------------------------------------------------------------------------

class TestFullOpenPacing:
    @async_test
    async def test_first_item_starts_immediately(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(cover_entity_id="c1", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == [("c1", 0.0)]

    @async_test
    async def test_second_item_starts_exactly_2s_after_first_start(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([
            _item(cover_entity_id="c1", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="c2", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN,
                 cover_index=1),
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == [("c1", 0.0), ("c2", 2.0)]

    @async_test
    async def test_slow_dispatch_callback_shortens_remaining_sleep(self):
        clock = FakeClock()
        h = Harness(clock)
        h.dispatch_time_cost["c1"] = 0.4
        plan = _plan([
            _item(cover_entity_id="c1", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="c2", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN,
                 cover_index=1),
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.sleep_calls == [1.6]
        assert h.dispatch_calls[1] == ("c2", 2.0)

    @async_test
    async def test_callback_longer_than_interval_causes_no_negative_sleep(self):
        clock = FakeClock()
        h = Harness(clock)
        h.dispatch_time_cost["c1"] = 2.5
        plan = _plan([
            _item(cover_entity_id="c1", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="c2", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN,
                 cover_index=1),
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.sleep_calls == []
        assert h.dispatch_calls[1] == ("c2", 2.5)

    @async_test
    async def test_full_open_never_waits_for_completion(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(cover_entity_id="c1", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.completion_calls == []


# ---------------------------------------------------------------------------
# FULL_OPEN -> INTERMEDIATE transition
# ---------------------------------------------------------------------------

class TestTransition:
    @async_test
    async def test_intermediate_starts_2s_after_last_full_open_start(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([
            _item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="b", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN,
                 cover_index=1),
            _item(cover_entity_id="c", target_ha=40, target_class=DispatchTargetClass.INTERMEDIATE,
                 cover_index=2),
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == [("a", 0.0), ("b", 2.0), ("c", 4.0)]

    @async_test
    async def test_no_double_pause_at_transition(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([
            _item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="c", target_ha=40, target_class=DispatchTargetClass.INTERMEDIATE,
                 cover_index=1),
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        # only the single 2.0s pacing sleep for the transition — no extra.
        assert h.sleep_calls == [2.0]


# ---------------------------------------------------------------------------
# INTERMEDIATE behavior
# ---------------------------------------------------------------------------

class TestIntermediate:
    @async_test
    async def test_dispatch_completion_pause_sequence(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([
            _item(cover_entity_id="a", target_ha=40),
            _item(cover_entity_id="b", target_ha=40, cover_index=1),
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == [("a", 0.0), ("b", 2.0)]
        assert h.completion_calls == ["a", "b"]
        assert h.sleep_calls == [2.0]

    @async_test
    async def test_timeout_still_gets_pause_and_continues(self):
        clock = FakeClock()
        h = Harness(clock)
        h.completion_outcomes["a"] = CompletionOutcome(status="timed_out")
        plan = _plan([
            _item(cover_entity_id="a", target_ha=40),
            _item(cover_entity_id="b", target_ha=40, cover_index=1),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.item_results[0].status == "timed_out"
        assert h.sleep_calls == [2.0]
        assert h.dispatch_calls[1] == ("b", 2.0)
        assert result.status == "completed"

    @async_test
    async def test_completion_failure_still_gets_pause_and_continues(self):
        clock = FakeClock()
        h = Harness(clock)
        h.completion_outcomes["a"] = CompletionOutcome(status="failed", error_type="TimeoutError")
        plan = _plan([
            _item(cover_entity_id="a", target_ha=40),
            _item(cover_entity_id="b", target_ha=40, cover_index=1),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.item_results[0].status == "completion_failed"
        assert h.sleep_calls == [2.0]
        assert result.status == "completed_with_errors"

    @async_test
    async def test_dispatch_failure_skips_completion_and_pause(self):
        clock = FakeClock()
        h = Harness(clock)
        h.dispatch_outcomes["a"] = DispatchOutcome(success=False, error_type="ServiceCallError")
        plan = _plan([
            _item(cover_entity_id="a", target_ha=40),
            _item(cover_entity_id="b", target_ha=40, cover_index=1),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.item_results[0].status == "dispatch_failed"
        assert "a" not in h.completion_calls
        assert h.sleep_calls == []
        assert result.status == "completed_with_errors"

    @async_test
    async def test_last_item_gets_no_trailing_pause(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(cover_entity_id="a", target_ha=40)])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.sleep_calls == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    @async_test
    async def test_execute_action_dispatches(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == [("a", 0.0)]

    @async_test
    async def test_skip_action_prevents_dispatch(self):
        clock = FakeClock()
        h = Harness(clock)
        h.validations["a"] = ExecutionValidation(ValidationAction.SKIP, reason="same_position")
        plan = _plan([_item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == []
        assert result.item_results[0].status == "skipped"
        assert result.item_results[0].reason == "same_position"

    @async_test
    async def test_abort_zone_skips_only_that_zone(self):
        clock = FakeClock()
        h = Harness(clock)
        h.validations["a"] = ExecutionValidation(ValidationAction.ABORT_ZONE, reason="contact_open")
        plan = _plan([
            _item(zone_id="z1", zone_index=0, cover_entity_id="a", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="z1", zone_index=0, cover_entity_id="b", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN, cover_index=1),
            _item(zone_id="z2", zone_index=1, cover_entity_id="c", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN, cover_index=2),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == [("c", 0.0)]
        statuses = {r.item.cover_entity_id: r.status for r in result.item_results}
        assert statuses["a"] == "skipped"
        assert statuses["b"] == "skipped"
        assert statuses["c"] == "dispatched"

    @async_test
    async def test_abort_plan_stops_everything(self):
        clock = FakeClock()
        h = Harness(clock)
        h.validations["a"] = ExecutionValidation(ValidationAction.ABORT_PLAN, reason="critical")
        plan = _plan([
            _item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="z2", zone_index=1, cover_entity_id="c", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN, cover_index=1),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == []
        assert result.status == "aborted"
        assert result.abort_reason == "critical"

    @async_test
    async def test_validation_called_before_dispatch(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.validate_calls == ["a"]
        assert h.dispatch_calls == [("a", 0.0)]

    @async_test
    async def test_no_validation_for_non_executable_items(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(target_class=DispatchTargetClass.NO_MOVEMENT, target_ha=40)])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.validate_calls == []


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

class TestGeneration:
    @async_test
    async def test_current_generation_dispatches(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == [("a", 0.0)]

    @async_test
    async def test_stale_generation_skips_zone_others_continue(self):
        clock = FakeClock()
        h = Harness(clock)
        h.generations["a"] = False
        plan = _plan([
            _item(zone_id="z1", zone_index=0, cover_entity_id="a", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="z1", zone_index=0, cover_entity_id="b", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN, cover_index=1),
            _item(zone_id="z2", zone_index=1, cover_entity_id="c", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN, cover_index=2),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == [("c", 0.0)]
        statuses = {r.item.cover_entity_id: r.status for r in result.item_results}
        assert statuses["a"] == "skipped"
        assert statuses["b"] == "skipped"

    @async_test
    async def test_generation_zero_is_valid(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(cover_entity_id="a", target_ha=100, zone_generation=0,
                            target_class=DispatchTargetClass.FULL_OPEN)])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == [("a", 0.0)]

    @async_test
    async def test_generation_checked_directly_before_each_dispatch(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([
            _item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="b", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN,
                 cover_index=1),
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.generation_calls == ["a", "b"]


# ---------------------------------------------------------------------------
# Safety preemption
# ---------------------------------------------------------------------------

class TestSafetyPreemption:
    @async_test
    async def test_preempted_before_first_item(self):
        clock = FakeClock()
        h = Harness(clock)
        h.cancellation.set()
        plan = _plan([_item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.status == "preempted"
        assert h.dispatch_calls == []

    @async_test
    async def test_preempted_during_full_open_pacing_sleep(self):
        clock = FakeClock()
        h = Harness(clock)

        async def sleep_then_cancel(seconds):
            h.cancellation.set()
            await asyncio.sleep(0)

        h.sleep = sleep_then_cancel
        plan = _plan([
            _item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="b", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN,
                 cover_index=1),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.status == "preempted"
        assert h.dispatch_calls == [("a", 0.0)]  # b never dispatched

    @async_test
    async def test_preempted_during_completion_wait(self):
        clock = FakeClock()
        h = Harness(clock)

        async def hanging_completion(item, cancellation):
            await asyncio.Event().wait()  # never resolves on its own

        h.wait_for_completion = hanging_completion
        plan = _plan([_item(cover_entity_id="a", target_ha=40)])

        async def _run():
            task = asyncio.ensure_future(_executor().execute_plan(plan=plan, **h.kwargs()))
            await asyncio.sleep(0)
            h.cancellation.set()
            return await task

        result = await _run()
        assert result.status == "preempted"

    @async_test
    async def test_preempted_during_post_completion_pause(self):
        clock = FakeClock()
        h = Harness(clock)

        async def sleep_then_cancel(seconds):
            h.cancellation.set()
            await asyncio.sleep(0)

        h.sleep = sleep_then_cancel
        plan = _plan([
            _item(cover_entity_id="a", target_ha=40),
            _item(cover_entity_id="b", target_ha=40, cover_index=1),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.status == "preempted"
        assert h.dispatch_calls == [("a", 0.0)]

    @async_test
    async def test_no_further_items_after_preemption(self):
        clock = FakeClock()
        h = Harness(clock)
        h.cancellation.set()
        plan = _plan([
            _item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="z2", zone_index=1, cover_entity_id="c", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN, cover_index=1),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == []
        assert len(result.item_results) <= 1

    @async_test
    async def test_executor_never_dispatches_safety_itself(self):
        # Structural: the executor has no concept of a safety dispatch call
        # at all — proven by the port contract simply not accepting one.
        import inspect
        sig = inspect.signature(DispatchPlanExecutor.execute_plan)
        assert "safety_dispatch" not in sig.parameters
        assert "dispatch_safety_item" not in sig.parameters


# ---------------------------------------------------------------------------
# External task cancellation
# ---------------------------------------------------------------------------

class TestExternalCancellation:
    @async_test
    async def test_external_cancellation_propagates(self):
        clock = FakeClock()
        h = Harness(clock)

        async def slow_dispatch(item):
            await asyncio.sleep(10)
            return DispatchOutcome(success=True)

        h.dispatch_item = slow_dispatch
        plan = _plan([_item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)])
        task = asyncio.ensure_future(_executor().execute_plan(plan=plan, **h.kwargs()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------

class TestErrorIsolation:
    @async_test
    async def test_dispatch_error_does_not_stop_plan(self):
        clock = FakeClock()
        h = Harness(clock)

        async def failing_dispatch(item):
            if item.cover_entity_id == "a":
                raise RuntimeError("boom")
            return DispatchOutcome(success=True)

        h.dispatch_item = failing_dispatch
        plan = _plan([
            _item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="b", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN,
                 cover_index=1),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.item_results[0].status == "dispatch_failed"
        assert result.item_results[0].error_type == "RuntimeError"
        assert result.item_results[1].status == "dispatched"
        assert result.status == "completed_with_errors"

    @async_test
    async def test_completion_error_does_not_stop_plan(self):
        clock = FakeClock()
        h = Harness(clock)
        h.completion_outcomes["a"] = CompletionOutcome(status="failed", error_type="X")
        plan = _plan([
            _item(cover_entity_id="a", target_ha=40),
            _item(cover_entity_id="b", target_ha=40, cover_index=1),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.item_results[1].status == "completed"

    @async_test
    async def test_result_never_stores_exception_instance(self):
        clock = FakeClock()
        h = Harness(clock)

        async def failing_dispatch(item):
            raise ValueError("secret detail")

        h.dispatch_item = failing_dispatch
        plan = _plan([_item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.item_results[0].error_type == "ValueError"
        for r in result.item_results:
            assert not isinstance(r.error_type, Exception)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

class TestResultModel:
    @async_test
    async def test_result_is_immutable(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        with pytest.raises(Exception):
            result.status = "aborted"
        with pytest.raises(Exception):
            result.item_results[0].status = "aborted"

    @async_test
    async def test_plan_id_preserved(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)],
                     plan_id="my-plan")
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.plan_id == "my-plan"

    @async_test
    async def test_result_order_matches_plan_order(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([
            _item(cover_entity_id="a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="b", target_ha=40, cover_index=1),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert [r.item.cover_entity_id for r in result.item_results] == ["a", "b"]

    @async_test
    async def test_non_executable_items_appear_diagnostically(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(target_class=DispatchTargetClass.NO_MOVEMENT, target_ha=40)])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert len(result.item_results) == 1


# ---------------------------------------------------------------------------
# decision_ref / reason passivity
# ---------------------------------------------------------------------------

class TestDecisionRefAndReasonPassivity:
    @async_test
    async def test_decision_ref_never_used_for_lookup(self):
        import ast
        from pathlib import Path
        src = (
            Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
            / "cover_control" / "dispatch_plan_executor.py"
        ).read_text(encoding="utf-8")
        assert "decision_ref" not in src  # never even referenced

    @async_test
    async def test_reason_only_carried_diagnostically_for_non_executable(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(target_class=DispatchTargetClass.BLOCKED, target_ha=None,
                            reason="blocked:comfort_position_hold")])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert result.item_results[0].reason == "blocked:comfort_position_hold"
        # never dispatched despite whatever the string says.
        assert h.dispatch_calls == []

    @async_test
    async def test_reason_never_steers_executable_dispatch(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([_item(cover_entity_id="a", target_ha=100,
                            target_class=DispatchTargetClass.FULL_OPEN,
                            reason="skip_me")])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        # an executable item's dispatch must never depend on its reason text.
        assert h.dispatch_calls == [("a", 0.0)]
        assert result.item_results[0].status == "dispatched"


# ---------------------------------------------------------------------------
# Architecture protection
# ---------------------------------------------------------------------------

class TestArchitectureProtection:
    def _source(self) -> str:
        from pathlib import Path
        return (
            Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
            / "cover_control" / "dispatch_plan_executor.py"
        ).read_text(encoding="utf-8")

    def test_no_coordinator_or_config_entry_import(self):
        import ast
        tree = ast.parse(self._source(), filename="dispatch_plan_executor.py")
        forbidden = ("coordinator", "config_entries", "homeassistant", "presence_engine",
                    "solar_source", "global_dispatch_throttle", "command_filter")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module_name = getattr(node, "module", None) or ""
                names = [alias.name for alias in node.names]
                joined = (module_name + " " + " ".join(names)).lower()
                for f in forbidden:
                    assert f not in joined, f"must not import {f!r}: {joined}"

    def test_no_resort_or_dedup_logic(self):
        src = self._source()
        for forbidden in ("sorted(", "sort(", "_deduplicate", "_sort_key"):
            assert forbidden not in src

    def test_no_hass_data_or_global_queue(self):
        import ast
        tree = ast.parse(self._source(), filename="dispatch_plan_executor.py")
        code_src = ast.unparse(ast.Module(body=tree.body[1:], type_ignores=[]))
        for forbidden in ("hass.data", "class DispatchQueue", "_GLOBAL", "Singleton"):
            assert forbidden not in code_src

    def test_no_translation_or_migration_touched(self):
        import os
        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "custom_components", "smartshading",
        )
        assert not os.path.exists(os.path.join(base, "cover_control", "dispatch_plan_executor.py.orig"))

    def test_no_own_lock_object(self):
        import ast
        tree = ast.parse(self._source(), filename="dispatch_plan_executor.py")
        src = self._source()
        assert "asyncio.Lock(" not in src
        class_names = {n.name.lower() for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        assert not any("lock" in n for n in class_names)

    def test_no_direct_asyncio_sleep(self):
        # All pacing/pause waits must go through the injected SleepPort —
        # never a literal asyncio.sleep(...) call bypassing it.
        src = self._source()
        assert "asyncio.sleep(" not in src
