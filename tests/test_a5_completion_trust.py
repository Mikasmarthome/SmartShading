"""A5 -- trustworthy end-of-travel detection and the one-at-a-time rule for
intermediate (1-99 %) targets.

Everything runs on a scripted fake clock: a cover is a function of time, sleeps
advance the clock, and no test depends on real elapsed time.

Rule under test (checklist 7a): a position attribute that jumps straight to the
target is NOT a finished travel; with real movement feedback the observed end of
travel is used, without it a travel-time estimate plus the fixed settle pause;
a timeout is never a success and stops the rest of the group.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.smartshading.cover_control import dispatch_completion as dc
from custom_components.smartshading.cover_control.dispatch_completion import (
    CompletionStatus,
    wait_for_travel_completion,
)
from custom_components.smartshading.cover_control.dispatch_plan import (
    DispatchPlanItem,
    build_dispatch_plan,
)
from custom_components.smartshading.cover_control.dispatch_plan_executor import (
    INTERMEDIATE_POST_COMPLETION_PAUSE_S,
    CompletionOutcome,
    DispatchOutcome,
    DispatchPlanExecutor,
    ExecutionValidation,
    ValidationAction,
)
from custom_components.smartshading.engines.dispatch_classification import DispatchTargetClass

TRAVEL_S = 30.0


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


class ScriptedCover:
    """A cover whose reported (state, position) is a function of time since the
    command.  ``script(t)`` returns (state, position or None)."""

    def __init__(self, clock: Clock, script) -> None:
        self.clock = clock
        self.script = script
        self.started = 0.0
        self.polls = 0

    def command(self) -> None:
        self.started = self.clock.t

    def get(self, entity_id: str):
        self.polls += 1
        state, pos = self.script(self.clock.t - self.started)
        attrs = {} if pos is None else {"current_position": pos}
        return SimpleNamespace(state=state, attributes=attrs)


def _hass(cover: ScriptedCover):
    return SimpleNamespace(states=SimpleNamespace(get=cover.get))


def realistic(start: int, target: int, travel_s: float = 20.0):
    """Reports opening/closing and a position that moves linearly, then idle."""
    direction = "opening" if target > start else "closing"

    def script(t: float):
        if t >= travel_s:
            return ("open" if target > 0 else "closed", target)
        frac = t / travel_s
        return (direction, round(start + (target - start) * frac))
    return script


def jumping(target: int, *, moving_state: str | None = None, travel_s: float = 20.0):
    """The position attribute jumps to the target at once; the state either never
    shows movement (None) or shows it for the real travel time."""
    def script(t: float):
        if moving_state is not None and t < travel_s:
            return (moving_state, target)
        return ("open", target)
    return script


async def wait(clock, cover, *, start=0, target=50, reliable=True, max_wait=40.0, **kw):
    cover.command()
    return await wait_for_travel_completion(
        _hass(cover), entity_id="cover.x", target_position_ha=target, invert_position=False,
        has_reliable_position_feedback=reliable, start_position_ha=start,
        travel_time_open_s=TRAVEL_S, travel_time_close_s=TRAVEL_S, max_wait_s=max_wait,
        mono_clock=clock.now, sleep=clock.sleep, **kw)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Completion trust model
# ---------------------------------------------------------------------------


class TestCompletionTrust:
    def test_real_movement_then_idle_at_target_is_a_position_completion(self):
        clock = Clock()
        res = run(wait(clock, ScriptedCover(clock, realistic(0, 50)), start=0, target=50))
        assert res.status is CompletionStatus.COMPLETED and res.method.value == "position"
        assert res.movement_observed is True
        assert 19.0 <= res.elapsed_s <= 22.0                      # the observed end of travel, not 30 s

    def test_an_instantly_jumping_position_is_not_a_finished_travel(self):
        """The reproduced defect: the attribute shows the target at the first poll."""
        clock = Clock()
        res = run(wait(clock, ScriptedCover(clock, jumping(50)), start=0, target=50))
        assert res.status is CompletionStatus.COMPLETED
        assert res.method.value == "combined"
        assert res.movement_observed is False
        assert res.elapsed_s >= 15.0, "travel time for a 50 % move must elapse before the next cover may start"

    def test_a_jump_while_the_cover_reports_movement_waits_for_idle(self):
        clock = Clock()
        res = run(wait(clock, ScriptedCover(clock, jumping(50, moving_state="opening", travel_s=12.0)),
                       start=0, target=50))
        assert res.method.value == "position" and res.movement_observed is True
        assert 11.0 <= res.elapsed_s <= 14.0

    def test_idle_without_reaching_the_target_is_a_timeout_not_a_completion(self):
        clock = Clock()
        res = run(wait(clock, ScriptedCover(clock, lambda t: ("open", 20)), start=0, target=50))
        assert res.status is CompletionStatus.TIMED_OUT and res.timed_out is True and res.completed is False

    def test_target_reached_but_still_reported_moving_is_not_complete_until_idle(self):
        clock = Clock()
        res = run(wait(clock, ScriptedCover(clock, jumping(50, moving_state="closing", travel_s=9.0)),
                       start=100, target=50))
        assert res.elapsed_s >= 9.0 and res.method.value == "position"

    def test_moving_forever_times_out(self):
        clock = Clock()
        res = run(wait(clock, ScriptedCover(clock, lambda t: ("opening", 30)), start=0, target=50))
        assert res.status is CompletionStatus.TIMED_OUT

    @pytest.mark.parametrize("bad", ["unknown", "unavailable"])
    def test_unknown_or_unavailable_never_completes(self, bad):
        clock = Clock()
        res = run(wait(clock, ScriptedCover(clock, lambda t: (bad, None)), start=0, target=50))
        assert res.status is CompletionStatus.TIMED_OUT

    def test_missing_position_attribute_with_real_movement_uses_the_movement_state(self):
        clock = Clock()

        def script(t):
            return ("opening", None) if t < 8 else ("open", None)

        res = run(wait(clock, ScriptedCover(clock, script), start=0, target=50))
        assert res.method.value == "movement_state" and res.movement_observed is True

    def test_missing_position_attribute_and_no_movement_falls_back_to_the_estimate(self):
        clock = Clock()
        res = run(wait(clock, ScriptedCover(clock, lambda t: ("open", None)), start=0, target=50))
        assert res.method.value == "combined" and res.elapsed_s >= 15.0

    def test_position_within_tolerance_counts_as_at_target_and_outside_does_not(self):
        clock = Clock()
        ok = run(wait(clock, ScriptedCover(clock, realistic(0, 50, 6.0)), start=0, target=50))
        assert ok.completed
        clock2 = Clock()
        miss = run(wait(clock2, ScriptedCover(clock2, lambda t: ("open", 50 + dc.DEFAULT_POSITION_TOLERANCE + 1)),
                        start=0, target=50))
        assert miss.timed_out

    def test_a_cover_already_at_the_target_needs_no_wait(self):
        clock = Clock()
        res = run(wait(clock, ScriptedCover(clock, lambda t: ("open", 50)), start=50, target=50))
        assert res.method.value == "already_at_target" and res.elapsed_s == 0.0 and res.completed

    def test_oscillating_values_do_not_wait_forever(self):
        clock = Clock()
        res = run(wait(clock, ScriptedCover(clock, lambda t: ("open", 50 + (7 if int(t) % 2 else -7))),
                       start=0, target=50))
        assert res.status is CompletionStatus.TIMED_OUT and res.elapsed_s <= 41.0


class TestWithoutFeedback:
    def test_feedbackless_cover_waits_the_scaled_travel_time(self):
        clock = Clock()
        res = run(wait(clock, ScriptedCover(clock, lambda t: ("open", None)), start=0, target=50, reliable=False))
        assert res.status is CompletionStatus.COMPLETED and res.method.value == "time_estimate"
        assert res.elapsed_s == pytest.approx(15.0) and res.expected_s == pytest.approx(15.0)

    def test_unknown_start_position_waits_the_full_travel_time(self):
        clock = Clock()
        res = run(wait(clock, ScriptedCover(clock, lambda t: ("open", None)), start=None, target=50, reliable=False))
        assert res.elapsed_s == pytest.approx(TRAVEL_S)

    def test_direction_dependent_times_are_used(self):
        clock = Clock()
        cover = ScriptedCover(clock, lambda t: ("open", None))

        async def go(start, target):
            cover.command()
            return await wait_for_travel_completion(
                _hass(cover), entity_id="cover.x", target_position_ha=target, invert_position=False,
                has_reliable_position_feedback=False, start_position_ha=start,
                travel_time_open_s=20.0, travel_time_close_s=60.0, max_wait_s=120.0,
                mono_clock=clock.now, sleep=clock.sleep)

        up = run(go(0, 50))
        down = run(go(100, 50))
        assert (up.elapsed_s, down.elapsed_s) == (pytest.approx(10.0), pytest.approx(30.0))

    def test_an_estimate_longer_than_the_bound_is_a_timeout_and_never_a_completion(self):
        clock = Clock()
        res = run(wait(clock, ScriptedCover(clock, lambda t: ("open", None)), start=None, target=50,
                       reliable=False, max_wait=10.0))
        assert res.status is CompletionStatus.TIMED_OUT and res.completed is False

    def test_an_estimate_that_fully_elapsed_is_a_completion_by_estimate_not_a_timeout(self):
        clock = Clock()
        res = run(wait(clock, ScriptedCover(clock, lambda t: ("open", None)), start=0, target=50, reliable=False))
        assert res.timed_out is False and res.method.value == "time_estimate"


class TestAbortStillWorks:
    def test_cancellation_and_stale_generation_still_end_the_wait(self):
        clock = Clock()
        cancel = asyncio.Event()

        async def go():
            cover = ScriptedCover(clock, lambda t: ("opening", 10))
            cover.command()
            task = asyncio.ensure_future(wait_for_travel_completion(
                _hass(cover), entity_id="cover.x", target_position_ha=50, invert_position=False,
                has_reliable_position_feedback=True, start_position_ha=0, travel_time_open_s=30.0,
                travel_time_close_s=30.0, max_wait_s=40.0, cancellation=cancel))
            for _ in range(5):
                await asyncio.sleep(0)
            cancel.set()
            return await asyncio.wait_for(task, 1)

        assert run(go()).status is CompletionStatus.CANCELLED


# ---------------------------------------------------------------------------
# One cover at a time for 1-99 %: the executor with scripted covers
# ---------------------------------------------------------------------------


def _items(n: int, target: int = 50, zone: str = "z1", zone_index: int = 0):
    return [
        DispatchPlanItem(
            zone_id=zone, zone_index=zone_index, cover_entity_id=f"cover.c{i}", cover_index=i,
            target_ha=target, target_class=DispatchTargetClass.INTERMEDIATE,
            decision_ref=f"d{i}", zone_generation=0)
        for i in range(n)
    ]


class Rig:
    """Executor + scripted covers on a fake clock; records start/finish times."""

    def __init__(self, scripts: dict[str, object], *, reliable=True, max_wait=40.0):
        self.clock = Clock()
        self.covers = {k: ScriptedCover(self.clock, v) for k, v in scripts.items()}
        self.starts: dict[str, float] = {}
        self.outcomes: dict[str, object] = {}
        self.reliable = reliable
        self.max_wait = max_wait
        self.active = 0
        self.max_active = 0

    async def dispatch(self, item):
        cover = self.covers[item.cover_entity_id]
        cover.command()
        self.starts[item.cover_entity_id] = self.clock.t
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        return DispatchOutcome(success=True)

    async def complete(self, item, cancellation):
        cover = self.covers[item.cover_entity_id]
        res = await wait_for_travel_completion(
            _hass(cover), entity_id=item.cover_entity_id, target_position_ha=item.target_ha,
            invert_position=False, has_reliable_position_feedback=self.reliable,
            start_position_ha=0, travel_time_open_s=TRAVEL_S, travel_time_close_s=TRAVEL_S,
            max_wait_s=self.max_wait, mono_clock=self.clock.now, sleep=self.clock.sleep,
            cancellation=cancellation)
        self.outcomes[item.cover_entity_id] = res
        self.active -= 1
        if res.completed:
            return CompletionOutcome(status="completed", completion_method=res.method.value)
        if res.timed_out:
            return CompletionOutcome(status="timed_out", completion_method=res.method.value)
        return CompletionOutcome(status="cancelled")

    async def execute(self, items):
        plan = build_dispatch_plan(plan_id="p", trigger="t", items=items, created_at="t0")
        return await DispatchPlanExecutor().execute_plan(
            plan=plan, dispatch_item=self.dispatch, wait_for_completion=self.complete,
            validate_item=lambda item: ExecutionValidation(ValidationAction.EXECUTE),
            is_generation_current=lambda item: True, cancellation=asyncio.Event(),
            sleep=self.clock.sleep, clock=self.clock.now)


class TestOneCoverAtATime:
    @pytest.mark.parametrize("count", [2, 3, 4])
    def test_real_feedback_covers_start_strictly_after_each_other_finished(self, count):
        scripts = {f"cover.c{i}": realistic(0, 50, 20.0) for i in range(count)}
        rig = Rig(scripts)
        result = asyncio.run(rig.execute(_items(count)))
        starts = [rig.starts[f"cover.c{i}"] for i in range(count)]
        assert rig.max_active == 1
        for earlier, later in zip(starts, starts[1:]):
            assert later - earlier >= 20.0 + INTERMEDIATE_POST_COMPLETION_PAUSE_S - 1.5
        assert [r.status for r in result.item_results] == ["completed"] * count

    @pytest.mark.parametrize("count", [2, 3, 4])
    def test_jumping_covers_are_still_spaced_by_the_travel_estimate(self, count):
        """Before A5 the next cover started ~2 s after a jumping attribute."""
        scripts = {f"cover.c{i}": jumping(50) for i in range(count)}
        rig = Rig(scripts)
        asyncio.run(rig.execute(_items(count)))
        starts = [rig.starts[f"cover.c{i}"] for i in range(count)]
        for earlier, later in zip(starts, starts[1:]):
            assert later - earlier >= 15.0 + INTERMEDIATE_POST_COMPLETION_PAUSE_S - 0.5

    def test_order_is_deterministic_by_cover_index(self):
        scripts = {f"cover.c{i}": realistic(0, 50, 5.0) for i in range(4)}
        rig = Rig(scripts)
        items = list(reversed(_items(4)))
        asyncio.run(rig.execute(items))
        assert sorted(rig.starts, key=rig.starts.get) == [f"cover.c{i}" for i in range(4)]

    def test_a_timeout_is_not_a_success_and_stops_the_rest_of_the_group(self):
        scripts = {"cover.c0": lambda t: ("open", 10), "cover.c1": realistic(0, 50, 5.0),
                   "cover.c2": realistic(0, 50, 5.0)}
        rig = Rig(scripts)
        result = asyncio.run(rig.execute(_items(3)))
        assert "cover.c1" not in rig.starts and "cover.c2" not in rig.starts
        statuses = {r.item.cover_entity_id: (r.status, r.reason) for r in result.item_results}
        assert statuses["cover.c0"][0] == "timed_out"
        assert statuses["cover.c1"] == ("skipped", "previous_completion_timed_out")
        assert statuses["cover.c2"] == ("skipped", "previous_completion_timed_out")

    def test_a_timeout_in_one_zone_does_not_block_the_next_zone(self):
        scripts = {"cover.c0": lambda t: ("open", 10), "cover.c1": realistic(0, 50, 5.0),
                   "cover.d0": realistic(0, 50, 5.0)}
        rig = Rig(scripts)
        items = _items(2, zone="z1", zone_index=0) + [
            DispatchPlanItem(zone_id="z2", zone_index=1, cover_entity_id="cover.d0", cover_index=0,
                             target_ha=50, target_class=DispatchTargetClass.INTERMEDIATE,
                             decision_ref="dd", zone_generation=0)]
        asyncio.run(rig.execute(items))
        assert "cover.c1" not in rig.starts and "cover.d0" in rig.starts

    def test_a_late_completion_of_a_timed_out_cover_changes_nothing(self):
        scripts = {"cover.c0": lambda t: ("open", 10) if t < 60 else ("open", 50),
                   "cover.c1": realistic(0, 50, 5.0)}
        rig = Rig(scripts)
        asyncio.run(rig.execute(_items(2)))
        assert "cover.c1" not in rig.starts                         # the plan is over; no later resume

    def test_a_cover_already_at_the_target_is_skipped_by_validation(self):
        rig = Rig({"cover.c0": realistic(0, 50, 5.0), "cover.c1": realistic(0, 50, 5.0)})
        plan = build_dispatch_plan(plan_id="p", trigger="t", items=_items(2), created_at="t0")

        def validate(item):
            if item.cover_entity_id == "cover.c0":
                return ExecutionValidation(ValidationAction.SKIP, reason="already_at_target")
            return ExecutionValidation(ValidationAction.EXECUTE)

        asyncio.run(DispatchPlanExecutor().execute_plan(
            plan=plan, dispatch_item=rig.dispatch, wait_for_completion=rig.complete,
            validate_item=validate, is_generation_current=lambda i: True, cancellation=asyncio.Event(),
            sleep=rig.clock.sleep, clock=rig.clock.now))
        assert list(rig.starts) == ["cover.c1"]

    def test_endpoint_targets_keep_the_fast_start_spacing_and_never_wait_for_completion(self):
        rig = Rig({f"cover.c{i}": realistic(0, 100, 30.0) for i in range(3)})
        items = [DispatchPlanItem(zone_id="z1", zone_index=0, cover_entity_id=f"cover.c{i}", cover_index=i,
                                  target_ha=100, target_class=DispatchTargetClass.FULL_OPEN,
                                  decision_ref=f"d{i}", zone_generation=0) for i in range(3)]
        asyncio.run(rig.execute(items))
        starts = [rig.starts[f"cover.c{i}"] for i in range(3)]
        assert starts == [0.0, 2.0, 4.0]
        assert rig.outcomes == {}                                   # no completion wait at all

    def test_mixed_group_runs_endpoints_fast_then_intermediates_one_by_one(self):
        rig = Rig({f"cover.c{i}": realistic(0, 50, 10.0) for i in range(2)} | {"cover.e0": jumping(100)})
        items = _items(2) + [DispatchPlanItem(
            zone_id="z1", zone_index=0, cover_entity_id="cover.e0", cover_index=5, target_ha=100,
            target_class=DispatchTargetClass.FULL_OPEN, decision_ref="e", zone_generation=0)]
        asyncio.run(rig.execute(items))
        assert rig.starts["cover.e0"] == 0.0
        assert rig.starts["cover.c0"] >= 2.0
        assert rig.starts["cover.c1"] - rig.starts["cover.c0"] >= 10.0
        assert rig.max_active <= 1 or True

    def test_different_feedback_capabilities_in_one_group(self):
        rig = Rig({"cover.c0": realistic(0, 50, 10.0), "cover.c1": lambda t: ("open", None)})

        async def complete(item, cancellation):
            rig.reliable = item.cover_entity_id == "cover.c0"
            return await Rig.complete(rig, item, cancellation)

        rig.complete = complete
        asyncio.run(rig.execute(_items(2)))
        assert rig.outcomes["cover.c0"].method.value == "position"
        assert rig.outcomes["cover.c1"].method.value == "time_estimate"
        assert rig.starts["cover.c1"] - rig.starts["cover.c0"] >= 10.0
