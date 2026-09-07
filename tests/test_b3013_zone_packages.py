"""B3-013 — Einheitlicher Dispatch mit Gruppen und Zonenpaketen.

Regression coverage for the one fixed, non-configurable dispatch rule at
the DispatchPlanExecutor level (cover_control/dispatch_plan_executor.py),
building on the isolated fakes/pattern established in
test_t22_phase3_dispatch_plan_executor.py:

  - FULL_OPEN/FULL_CLOSE: 2.0s start-to-start pacing, never a completion
    wait, even across a zone-package boundary.
  - INTERMEDIATE: waits for the previous item's real completion, then a
    fixed 2.0s pause, before the next item starts.
  - Zone packages are dispatched fully, in stable zone_index order, before
    the next zone begins — a slow completion, a blocked/no-op item, or a
    submission-order change in one zone must never let a later zone start
    early or interleave with it.

No real 2-second sleeps anywhere — all timing is driven by the same
FakeClock/injectable-sleep pattern already used across the T22 test suite.
"""
from __future__ import annotations

import asyncio
import functools

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


def async_test(fn):
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
    """Like test_t22_phase3_dispatch_plan_executor.py's Harness, plus a
    per-entity `completion_duration_s` that genuinely advances the fake
    clock while a completion wait is in progress — needed to prove a slow
    completion in one zone never lets a later zone start early."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.dispatch_calls: list[tuple[str, float]] = []
        self.completion_calls: list[tuple[str, float]] = []
        self.sleep_calls: list[float] = []
        self.dispatch_outcomes: dict[str, DispatchOutcome] = {}
        self.completion_outcomes: dict[str, CompletionOutcome] = {}
        self.completion_duration_s: dict[str, float] = {}
        self.validations: dict[str, ExecutionValidation] = {}
        self.generations: dict[str, bool] = {}
        self.cancellation = asyncio.Event()

    async def dispatch_item(self, item: DispatchPlanItem) -> DispatchOutcome:
        self.dispatch_calls.append((item.cover_entity_id, self.clock.now()))
        return self.dispatch_outcomes.get(item.cover_entity_id, DispatchOutcome(success=True))

    async def wait_for_completion(self, item: DispatchPlanItem, cancellation) -> CompletionOutcome:
        duration = self.completion_duration_s.get(item.cover_entity_id, 0.0)
        if duration:
            self.clock.advance(duration)
        self.completion_calls.append((item.cover_entity_id, self.clock.now()))
        return self.completion_outcomes.get(
            item.cover_entity_id, CompletionOutcome(status="completed", completion_method="position"),
        )

    def validate_item(self, item: DispatchPlanItem) -> ExecutionValidation:
        return self.validations.get(item.cover_entity_id, ExecutionValidation(ValidationAction.EXECUTE))

    def is_generation_current(self, item: DispatchPlanItem) -> bool:
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
# Endpoint (FULL_OPEN/FULL_CLOSE) groups
# ---------------------------------------------------------------------------

class TestEndpointGroups:
    @async_test
    async def test_four_covers_full_open_same_zone_spaced_2s_apart(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([
            _item(cover_entity_id=f"c{i}", cover_index=i, target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN)
            for i in range(4)
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == [
            ("c0", 0.0), ("c1", 2.0), ("c2", 4.0), ("c3", 6.0),
        ]
        assert h.completion_calls == []

    @async_test
    async def test_four_covers_full_close_same_zone_spaced_2s_apart(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([
            _item(cover_entity_id=f"c{i}", cover_index=i, target_ha=0,
                 target_class=DispatchTargetClass.FULL_CLOSE)
            for i in range(4)
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == [
            ("c0", 0.0), ("c1", 2.0), ("c2", 4.0), ("c3", 6.0),
        ]
        assert h.completion_calls == []

    @async_test
    async def test_blocked_endpoint_causes_no_unnecessary_wait_for_the_next(self):
        # A BLOCKED item consumes no pacing anchor at all — the next real
        # FULL_OPEN item must not be delayed by it.
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([
            _item(cover_entity_id="c0", cover_index=0, target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="c1", cover_index=1, target_ha=None,
                 target_class=DispatchTargetClass.BLOCKED, reason="blocked:manual_override"),
            _item(cover_entity_id="c2", cover_index=2, target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == [("c0", 0.0), ("c2", 2.0)]


# ---------------------------------------------------------------------------
# INTERMEDIATE groups (shared target)
# ---------------------------------------------------------------------------

class TestIntermediateGroups:
    @async_test
    async def test_three_covers_same_target_wait_completion_then_2s_pause_each(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([
            _item(cover_entity_id=f"c{i}", cover_index=i, target_ha=40,
                 target_class=DispatchTargetClass.INTERMEDIATE)
            for i in range(3)
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        # Each item's own completion is instant (0s) in this harness, so
        # only the fixed 2.0s post-completion pause separates starts.
        assert h.dispatch_calls == [("c0", 0.0), ("c1", 2.0), ("c2", 4.0)]

    @async_test
    async def test_differing_completion_durations_do_not_change_order_or_target(self):
        clock = FakeClock()
        h = Harness(clock)
        h.completion_duration_s["c0"] = 5.0
        h.completion_duration_s["c1"] = 0.5
        plan = _plan([
            _item(cover_entity_id="c0", cover_index=0, target_ha=40,
                 target_class=DispatchTargetClass.INTERMEDIATE),
            _item(cover_entity_id="c1", cover_index=1, target_ha=40,
                 target_class=DispatchTargetClass.INTERMEDIATE),
            _item(cover_entity_id="c2", cover_index=2, target_ha=40,
                 target_class=DispatchTargetClass.INTERMEDIATE),
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        order = [c for c, _ in h.dispatch_calls]
        assert order == ["c0", "c1", "c2"]
        # c1 starts only after c0's real 5.0s completion + 2.0s pause.
        assert h.dispatch_calls[1] == ("c1", 7.0)
        # c2 starts only after c1's real 0.5s completion + 2.0s pause.
        assert h.dispatch_calls[2] == ("c2", 9.5)

    @async_test
    async def test_timeout_on_one_item_continues_the_queue(self):
        clock = FakeClock()
        h = Harness(clock)
        h.completion_outcomes["c0"] = CompletionOutcome(status="timed_out", completion_method="timeout")
        plan = _plan([
            _item(cover_entity_id="c0", cover_index=0, target_ha=40,
                 target_class=DispatchTargetClass.INTERMEDIATE),
            _item(cover_entity_id="c1", cover_index=1, target_ha=40,
                 target_class=DispatchTargetClass.INTERMEDIATE),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert [c for c, _ in h.dispatch_calls] == ["c0", "c1"]
        assert result.item_results[0].status == "timed_out"
        assert result.item_results[1].status == "completed"

    @async_test
    async def test_blocked_item_within_group_does_not_permanently_block_the_rest(self):
        clock = FakeClock()
        h = Harness(clock)
        h.validations["c1"] = ExecutionValidation(ValidationAction.SKIP, reason="same_position")
        plan = _plan([
            _item(cover_entity_id="c0", cover_index=0, target_ha=40,
                 target_class=DispatchTargetClass.INTERMEDIATE),
            _item(cover_entity_id="c1", cover_index=1, target_ha=40,
                 target_class=DispatchTargetClass.INTERMEDIATE),
            _item(cover_entity_id="c2", cover_index=2, target_ha=40,
                 target_class=DispatchTargetClass.INTERMEDIATE),
        ])
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        assert [c for c, _ in h.dispatch_calls] == ["c0", "c2"]
        assert result.item_results[1].status == "skipped"


# ---------------------------------------------------------------------------
# Zone-package isolation (>=3 zones)
# ---------------------------------------------------------------------------

class TestZonePackageIsolation:
    def _three_zone_plan(self):
        return _plan([
            _item(zone_id="z1", zone_index=0, cover_entity_id="z1a", cover_index=0,
                 target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="z1", zone_index=0, cover_entity_id="z1b", cover_index=1,
                 target_ha=40, target_class=DispatchTargetClass.INTERMEDIATE),
            _item(zone_id="z2", zone_index=1, cover_entity_id="z2a", cover_index=0,
                 target_ha=0, target_class=DispatchTargetClass.FULL_CLOSE),
            _item(zone_id="z3", zone_index=2, cover_entity_id="z3a", cover_index=0,
                 target_ha=40, target_class=DispatchTargetClass.INTERMEDIATE),
        ])

    @async_test
    async def test_three_zones_stay_contiguous_regardless_of_submission_order(self):
        clock = FakeClock()
        h = Harness(clock)
        items = list(self._three_zone_plan().items)
        for shuffled in (items, list(reversed(items)), [items[2], items[0], items[3], items[1]]):
            clock.t = 0.0
            h2 = Harness(clock)
            p = _plan(shuffled)
            await _executor().execute_plan(plan=p, **h2.kwargs())
            order = [c for c, _ in h2.dispatch_calls]
            assert order == ["z1a", "z1b", "z2a", "z3a"], (
                "zone order must be stable/repeatable regardless of the "
                "order items were submitted in, since build_dispatch_plan "
                "always re-sorts by (zone_index, class_priority, ...)"
            )

    @async_test
    async def test_slow_completion_in_zone_a_never_lets_zone_b_start_early(self):
        clock = FakeClock()
        h = Harness(clock)
        h.completion_duration_s["z1b"] = 50.0
        plan = self._three_zone_plan()
        await _executor().execute_plan(plan=plan, **h.kwargs())
        by_entity = dict(h.dispatch_calls)
        assert by_entity["z1a"] == 0.0
        # z1b: start-pacing anchor from z1a (2.0s), then 50.0s completion,
        # then the fixed 2.0s post-completion pause.
        assert by_entity["z1b"] == 2.0
        assert by_entity["z2a"] == 2.0 + 50.0 + 2.0
        # z3a strictly after z2's own FULL_CLOSE 2.0s start-pacing anchor.
        assert by_entity["z3a"] == by_entity["z2a"] + 2.0

    @async_test
    async def test_blocked_item_in_zone_a_never_pulls_zone_b_forward(self):
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([
            _item(zone_id="z1", zone_index=0, cover_entity_id="z1a", cover_index=0,
                 target_ha=None, target_class=DispatchTargetClass.BLOCKED,
                 reason="blocked:manual_override"),
            _item(zone_id="z1", zone_index=0, cover_entity_id="z1b", cover_index=1,
                 target_ha=40, target_class=DispatchTargetClass.INTERMEDIATE),
            _item(zone_id="z2", zone_index=1, cover_entity_id="z2a", cover_index=0,
                 target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        by_entity = dict(h.dispatch_calls)
        assert "z1a" not in by_entity
        assert by_entity["z1b"] == 0.0
        # z2a strictly after z1b's own completion + 2.0s pause, never
        # "pulled forward" just because z1a itself never dispatched.
        assert by_entity["z2a"] == 2.0

    @async_test
    async def test_abort_zone_in_zone_a_never_pulls_zone_b_forward(self):
        clock = FakeClock()
        h = Harness(clock)
        h.validations["z1a"] = ExecutionValidation(ValidationAction.ABORT_ZONE, reason="contact_open")
        plan = self._three_zone_plan()
        result = await _executor().execute_plan(plan=plan, **h.kwargs())
        by_entity = dict(h.dispatch_calls)
        assert "z1a" not in by_entity
        assert "z1b" not in by_entity, "ABORT_ZONE must skip the REST of the same zone too"
        assert by_entity["z2a"] == 0.0
        statuses = {r.item.cover_entity_id: r.status for r in result.item_results}
        assert statuses["z1a"] == "skipped"
        assert statuses["z1b"] == "skipped"

    @async_test
    async def test_full_open_close_keep_2s_gap_across_a_zone_boundary(self):
        # Back-to-back FULL_OPEN/FULL_CLOSE items with nothing else between
        # them keep the fixed 2.0s start-to-start gap even though they
        # belong to two different zones.
        clock = FakeClock()
        h = Harness(clock)
        plan = _plan([
            _item(zone_id="z1", zone_index=0, cover_entity_id="z1a", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="z2", zone_index=1, cover_entity_id="z2a", target_ha=0,
                 target_class=DispatchTargetClass.FULL_CLOSE),
        ])
        await _executor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == [("z1a", 0.0), ("z2a", 2.0)]

    @async_test
    async def test_stale_generation_in_zone_a_never_pulls_zone_b_forward(self):
        clock = FakeClock()
        h = Harness(clock)
        h.generations["z1a"] = False
        plan = self._three_zone_plan()
        await _executor().execute_plan(plan=plan, **h.kwargs())
        by_entity = dict(h.dispatch_calls)
        assert "z1a" not in by_entity
        assert "z1b" not in by_entity, (
            "a stale generation aborts the REST of that zone too "
            "(see DispatchPlanExecutor's aborted_zones set)"
        )
        assert by_entity["z2a"] == 0.0
