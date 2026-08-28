"""B3-012 — dedicated regression suite for the three dispatch target
classes (FULL_CLOSE/INTERMEDIATE/FULL_OPEN).

This file does not re-derive classification logic -- it proves properties
of the EXISTING, already-tested classify_dispatch_target()
(engines/dispatch_classification.py) and DispatchPlanExecutor
(cover_control/dispatch_plan_executor.py) the more granular T22 test files
don't already assert explicitly: group/zone symmetry, revalidation
re-classifying a changed target, and a safety/contact limitation producing
a different class than an unrestricted sibling cover.

No new classifier, no new executor, no new timing rule -- see
tests/test_t22_phase1_dispatch_classification.py and
tests/test_t22_phase3_dispatch_plan_executor.py for the boundary-value and
executor-timing tests this file assumes and builds on.
"""
from __future__ import annotations

import asyncio

from custom_components.smartshading.cover_control.dispatch_plan import (
    DispatchPlanItem,
    build_dispatch_plan,
)
from custom_components.smartshading.cover_control.dispatch_plan_executor import (
    DispatchOutcome,
    CompletionOutcome,
    DispatchPlanExecutor,
    ExecutionValidation,
    ValidationAction,
)
from custom_components.smartshading.engines.dispatch_classification import (
    DispatchTargetClass,
    classify_dispatch_target,
)

_TOL = 3


def async_test(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))
    return wrapper


def _item(*, zone_id="z1", zone_index=0, cover_entity_id="cover.a", cover_index=0,
          target_ha=40, target_class=DispatchTargetClass.INTERMEDIATE, decision_ref="d1",
          zone_generation=0, reason=None) -> DispatchPlanItem:
    return DispatchPlanItem(
        zone_id=zone_id, zone_index=zone_index, cover_entity_id=cover_entity_id,
        cover_index=cover_index, target_ha=target_ha, target_class=target_class,
        decision_ref=decision_ref, zone_generation=zone_generation, reason=reason,
    )


class _Harness:
    def __init__(self):
        self.dispatch_calls: list[str] = []
        self.completion_calls: list[str] = []
        self.validations: dict[str, ExecutionValidation] = {}
        self.cancellation = asyncio.Event()
        self._t = 0.0

    async def dispatch_item(self, item):
        self.dispatch_calls.append(item.cover_entity_id)
        return DispatchOutcome(success=True)

    async def wait_for_completion(self, item, cancellation):
        self.completion_calls.append(item.cover_entity_id)
        return CompletionOutcome(status="completed", completion_method="position")

    def validate_item(self, item):
        return self.validations.get(item.cover_entity_id, ExecutionValidation(ValidationAction.EXECUTE))

    def is_generation_current(self, item) -> bool:
        return True

    async def sleep(self, seconds: float) -> None:
        self._t += seconds

    def clock(self) -> float:
        return self._t

    def kwargs(self) -> dict:
        return dict(
            dispatch_item=self.dispatch_item,
            wait_for_completion=self.wait_for_completion,
            validate_item=self.validate_item,
            is_generation_current=self.is_generation_current,
            cancellation=self.cancellation,
            sleep=self.sleep,
            clock=self.clock,
        )


# ---------------------------------------------------------------------------
# Target boundaries (explicit table, mirrors this ticket's own requirement)
# ---------------------------------------------------------------------------

class TestTargetBoundaryTable:
    def _classify(self, target_ha, current_ha=50):
        return classify_dispatch_target(
            resolved_target_ha=target_ha, current_position_ha=current_ha,
            dispatch_action="sent", position_tolerance=_TOL,
        ).target_class

    def test_0_is_full_close(self):
        assert self._classify(0) is DispatchTargetClass.FULL_CLOSE

    def test_1_is_intermediate(self):
        assert self._classify(1) is DispatchTargetClass.INTERMEDIATE

    def test_50_is_intermediate(self):
        assert self._classify(50, current_ha=90) is DispatchTargetClass.INTERMEDIATE

    def test_99_is_intermediate(self):
        assert self._classify(99) is DispatchTargetClass.INTERMEDIATE

    def test_100_is_full_open(self):
        assert self._classify(100) is DispatchTargetClass.FULL_OPEN


# ---------------------------------------------------------------------------
# Live revalidation: the classifier is a pure function of the CURRENT
# target -- re-running it on a changed value reclassifies correctly. The
# executor itself never mutates a plan item's class mid-run (a plan is
# fixed once built); a genuinely changed target is the COORDINATOR's next
# cycle's responsibility to reclassify via a fresh call, and THIS cycle's
# now-stale item is the validate_item port's job to SKIP, never execute
# under its original (now wrong) class -- both properties are asserted here.
# ---------------------------------------------------------------------------

class TestRevalidationReclassifies:
    def test_intermediate_target_revalidated_to_full_close_reclassifies(self):
        before = classify_dispatch_target(
            resolved_target_ha=40, current_position_ha=90,
            dispatch_action="sent", position_tolerance=_TOL,
        )
        assert before.target_class is DispatchTargetClass.INTERMEDIATE
        after = classify_dispatch_target(
            resolved_target_ha=0, current_position_ha=90,
            dispatch_action="sent", position_tolerance=_TOL,
        )
        assert after.target_class is DispatchTargetClass.FULL_CLOSE

    def test_intermediate_target_revalidated_to_full_open_reclassifies(self):
        before = classify_dispatch_target(
            resolved_target_ha=60, current_position_ha=10,
            dispatch_action="sent", position_tolerance=_TOL,
        )
        assert before.target_class is DispatchTargetClass.INTERMEDIATE
        after = classify_dispatch_target(
            resolved_target_ha=100, current_position_ha=10,
            dispatch_action="sent", position_tolerance=_TOL,
        )
        assert after.target_class is DispatchTargetClass.FULL_OPEN

    def test_full_open_target_limited_by_safety_to_intermediate_reclassifies(self):
        # A prior cycle's FULL_OPEN candidate, revalidated down to a
        # protective INTERMEDIATE target (e.g. heat/glare/contact/safety
        # override) -- the classifier reflects the NEW target honestly.
        before = classify_dispatch_target(
            resolved_target_ha=100, current_position_ha=20,
            dispatch_action="sent", position_tolerance=_TOL,
        )
        assert before.target_class is DispatchTargetClass.FULL_OPEN
        after = classify_dispatch_target(
            resolved_target_ha=35, current_position_ha=20,
            dispatch_action="sent", position_tolerance=_TOL,
        )
        assert after.target_class is DispatchTargetClass.INTERMEDIATE

    @async_test
    async def test_stale_item_is_skipped_never_dispatched_under_its_old_class(self):
        # The executor's own contract: validate_item() returning SKIP means
        # dispatch_item is never called at all -- a plan item whose target
        # became stale (revalidation would classify it differently now)
        # never executes under the class it was built with.
        h = _Harness()
        h.validations["c1"] = ExecutionValidation(ValidationAction.SKIP, reason="target_no_longer_needed")
        plan = build_dispatch_plan(
            plan_id="p1", trigger="scheduled_evaluation", created_at="t0",
            items=[_item(cover_entity_id="c1", target_ha=40,
                          target_class=DispatchTargetClass.INTERMEDIATE)],
        )
        result = await DispatchPlanExecutor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == []
        assert result.item_results[0].status == "skipped"
        assert result.item_results[0].reason == "target_no_longer_needed"


# ---------------------------------------------------------------------------
# Generation: a newer generation invalidates an old classified item.
# ---------------------------------------------------------------------------

class TestStaleGenerationNeverExecutesOldClass:
    @async_test
    async def test_stale_generation_item_never_dispatched(self):
        class _StaleGenHarness(_Harness):
            def is_generation_current(self, item) -> bool:
                return False

        h = _StaleGenHarness()
        plan = build_dispatch_plan(
            plan_id="p1", trigger="scheduled_evaluation", created_at="t0",
            items=[_item(cover_entity_id="c1", target_ha=0,
                          target_class=DispatchTargetClass.FULL_CLOSE)],
        )
        result = await DispatchPlanExecutor().execute_plan(plan=plan, **h.kwargs())
        assert h.dispatch_calls == []
        assert result.item_results[0].status == "skipped"
        assert result.item_results[0].reason == "stale_generation"


# ---------------------------------------------------------------------------
# Group/zone boundaries -- classification only, no B3-013 ordering.
# ---------------------------------------------------------------------------

class TestGroupAndZoneClassificationSymmetry:
    def test_multiple_covers_same_zone_same_target_get_the_same_class(self):
        results = [
            classify_dispatch_target(
                resolved_target_ha=70, current_position_ha=cur,
                dispatch_action="sent", position_tolerance=_TOL,
            ).target_class
            for cur in (10, 20, 30)
        ]
        assert results == [DispatchTargetClass.INTERMEDIATE] * 3

    def test_individually_safety_limited_cover_may_carry_a_different_class(self):
        # Three covers of a synchronized group would all get a FULL_OPEN
        # candidate, but one is individually limited (contact/safety) to a
        # protective intermediate target -- its class legitimately differs.
        group_target = classify_dispatch_target(
            resolved_target_ha=100, current_position_ha=0,
            dispatch_action="sent", position_tolerance=_TOL,
        ).target_class
        limited_target = classify_dispatch_target(
            resolved_target_ha=30, current_position_ha=0,
            dispatch_action="sent", position_tolerance=_TOL,
        ).target_class
        assert group_target is DispatchTargetClass.FULL_OPEN
        assert limited_target is DispatchTargetClass.INTERMEDIATE
        assert group_target != limited_target

    def test_two_zones_with_the_same_target_are_classified_identically(self):
        zone_a = classify_dispatch_target(
            resolved_target_ha=0, current_position_ha=100,
            dispatch_action="sent", position_tolerance=_TOL,
        ).target_class
        zone_b = classify_dispatch_target(
            resolved_target_ha=0, current_position_ha=100,
            dispatch_action="sent", position_tolerance=_TOL,
        ).target_class
        assert zone_a is zone_b is DispatchTargetClass.FULL_CLOSE

    def test_classification_does_not_depend_on_call_order(self):
        # A pure function of its own arguments -- no shared/hidden state
        # that iteration order (dict/set) could perturb.
        first_pass = [
            classify_dispatch_target(
                resolved_target_ha=t, current_position_ha=50,
                dispatch_action="sent", position_tolerance=_TOL,
            ).target_class
            for t in (0, 1, 50, 99, 100)
        ]
        second_pass = [
            classify_dispatch_target(
                resolved_target_ha=t, current_position_ha=50,
                dispatch_action="sent", position_tolerance=_TOL,
            ).target_class
            for t in reversed((0, 1, 50, 99, 100))
        ]
        assert first_pass == list(reversed(second_pass))

    def test_no_state_or_class_leaks_between_covers(self):
        # Classifying cover A as FULL_CLOSE must not affect an independent
        # classify_dispatch_target() call for cover B with a different
        # target -- proves the function carries no module-level/instance
        # state across calls.
        a = classify_dispatch_target(
            resolved_target_ha=0, current_position_ha=100,
            dispatch_action="sent", position_tolerance=_TOL,
        )
        b = classify_dispatch_target(
            resolved_target_ha=100, current_position_ha=0,
            dispatch_action="sent", position_tolerance=_TOL,
        )
        assert a.target_class is DispatchTargetClass.FULL_CLOSE
        assert b.target_class is DispatchTargetClass.FULL_OPEN


# ---------------------------------------------------------------------------
# None / invalid targets never fabricate a FULL_CLOSE/FULL_OPEN/INTERMEDIATE
# class -- they are BLOCKED, exactly like before B3-012.
# ---------------------------------------------------------------------------

class TestInvalidTargetsNeverFabricateAClass:
    def test_none_target_is_blocked(self):
        r = classify_dispatch_target(
            resolved_target_ha=None, current_position_ha=50,
            dispatch_action="sent", position_tolerance=_TOL,
        )
        assert r.target_class is DispatchTargetClass.BLOCKED
        assert r.normalized_target is None

    def test_blocked_reason_wins_even_for_a_would_be_full_close_target(self):
        r = classify_dispatch_target(
            resolved_target_ha=0, current_position_ha=50,
            dispatch_action="sent", blocked_reason="manual_override",
            position_tolerance=_TOL,
        )
        assert r.target_class is DispatchTargetClass.BLOCKED

    def test_blocked_reason_wins_even_for_a_would_be_full_open_target(self):
        r = classify_dispatch_target(
            resolved_target_ha=100, current_position_ha=50,
            dispatch_action="sent", blocked_reason="manual_override",
            position_tolerance=_TOL,
        )
        assert r.target_class is DispatchTargetClass.BLOCKED
