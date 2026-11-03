"""Zone packages and the shared normal pacing (stub level, controlled time).

``ZonePackageGate`` (cover_control/global_dispatch_throttle.py) lets one zone's
normal package run at a time; ``NormalPacing`` (cover_control/normal_pacing.py)
holds the earliest permitted start of the next normal command across packages.
Neither sleeps for real: every test drives a fake clock.
"""
from __future__ import annotations

import asyncio
import functools
import json

from custom_components.smartshading.cover_control.dispatch_completion import (
    AnyOfSignals,
)
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
from custom_components.smartshading.cover_control.global_dispatch_throttle import (
    PACKAGE_ABORTED,
    PACKAGE_GRANTED,
    PACKAGE_SUPERSEDED,
    PACKAGE_WITHDRAWN,
    GlobalSerialDispatch,
    ZonePackageGate,
)
from custom_components.smartshading.cover_control.normal_pacing import (
    BASIS_ENDPOINT_START,
    BASIS_ESTIMATED,
    BASIS_OBSERVED,
    BASIS_TIMEOUT,
    BASIS_UNCONFIRMED,
    NormalPacing,
)
from custom_components.smartshading.engines.dispatch_classification import DispatchTargetClass


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))
    return wrapper


async def _turns(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


# ===========================================================================
# NormalPacing
# ===========================================================================

class TestNormalPacing:
    def test_endpoint_start_holds_the_next_start_for_the_interval(self):
        p = NormalPacing()
        assert p.remaining(0.0) == 0.0
        p.note_endpoint_start(10.0, 2.0)
        assert p.remaining(10.0) == 2.0
        assert p.remaining(11.5) == 0.5
        assert p.remaining(12.0) == 0.0
        assert p.basis == BASIS_ENDPOINT_START

    def test_intermediate_travel_holds_for_the_settle_after_its_end_only(self):
        p = NormalPacing()
        p.begin_travel(0.0, 15.0, 2.0)
        assert p.travel_pending and p.remaining(0.0) == 0.0     # nothing is held while travelling
        p.finish_travel(20.0, 2.0, "position")
        assert not p.travel_pending
        assert p.remaining(20.0) == 2.0
        assert p.basis == BASIS_OBSERVED

    def test_completion_basis_distinguishes_observed_estimated_timeout(self):
        for method, basis in (("position", BASIS_OBSERVED), ("movement_state", BASIS_OBSERVED),
                              ("time_estimate", BASIS_ESTIMATED), ("combined", BASIS_ESTIMATED),
                              ("timeout", BASIS_TIMEOUT)):
            p = NormalPacing()
            p.begin_travel(0.0, 10.0, 2.0)
            p.finish_travel(10.0, 2.0, method)
            assert p.basis == basis, method

    def test_a_boundary_is_never_shortened(self):
        p = NormalPacing()
        p.begin_travel(0.0, 10.0, 2.0)
        p.finish_travel(10.0, 2.0, "position")           # until 12
        p.note_endpoint_start(10.5, 2.0)                  # would end at 12.5: longer wins
        assert p.remaining(10.5) == 2.0
        p.note_endpoint_start(10.0, 0.5)                  # shorter: ignored
        assert p.remaining(10.0) == 2.5

    def test_unresolved_travel_keeps_a_bounded_conservative_boundary(self):
        p = NormalPacing()
        p.begin_travel(100.0, 15.0, 2.0)
        assert p.resolve_pending(103.0) is True           # replaced / unloaded 3 s into the travel
        assert p.remaining(103.0) == 14.0                 # estimated end (115) + settle (2) - now
        assert p.basis == BASIS_UNCONFIRMED
        assert p.resolve_pending(103.0) is False          # idempotent
        assert p.remaining(103.0) == 14.0

    def test_resolution_after_the_expected_end_adds_only_the_settle(self):
        p = NormalPacing()
        p.begin_travel(0.0, 10.0, 2.0)
        p.resolve_pending(50.0)                           # long past the expected end
        assert p.remaining(50.0) == 2.0

    def test_stats_are_json_safe_primitives(self):
        p = NormalPacing()
        p.begin_travel(0.0, 10.0, 2.0)
        p.resolve_pending(1.0)
        text = json.dumps(p.stats(1.0))
        assert "unconfirmed_resolutions" in text and p.stats(1.0)["unconfirmed_resolutions"] == 1


# ===========================================================================
# ZonePackageGate
# ===========================================================================

def _gate() -> tuple[ZonePackageGate, NormalPacing]:
    pacing = NormalPacing()
    return ZonePackageGate(pacing), pacing


class _Owner:
    pass


class TestZonePackageGate:
    @async_test
    async def test_fifo_between_zones(self):
        gate, _ = _gate()
        a, b, c = _Owner(), _Owner(), _Owner()
        ta, out, _ = await gate.acquire("a", a)
        assert out == PACKAGE_GRANTED
        order: list[str] = []

        async def take(key, owner):
            ticket, outcome, _ = await gate.acquire(key, owner)
            assert outcome == PACKAGE_GRANTED
            order.append(key)
            gate.release(ticket, reason="done")

        tb = asyncio.ensure_future(take("b", b))
        await _turns()
        tc = asyncio.ensure_future(take("c", c))
        await _turns()
        assert gate.queued_count == 2 and gate.waiting_position("b", b) == 1 and gate.waiting_position("c", c) == 2
        gate.release(ta, reason="done")
        await asyncio.gather(tb, tc)
        assert order == ["b", "c"]
        assert gate.is_free and not gate.active

    @async_test
    async def test_the_grant_is_atomic_there_is_no_free_window_while_somebody_waits(self):
        gate, _ = _gate()
        ta, _, _ = await gate.acquire("a", _Owner())
        waiter = asyncio.ensure_future(gate.acquire("b", _Owner()))
        await _turns()
        assert gate.release(ta) is True
        assert gate.active and not gate.is_free        # handed on within the same synchronous step
        ticket, outcome, _ = await waiter
        assert outcome == PACKAGE_GRANTED
        gate.release(ticket)

    @async_test
    async def test_a_newer_request_of_the_same_zone_replaces_the_waiting_one_and_keeps_its_place(self):
        gate, _ = _gate()
        ta, _, _ = await gate.acquire("a", _Owner())
        b_old, c = _Owner(), _Owner()
        first = asyncio.ensure_future(gate.acquire("b", b_old))
        await _turns()
        other = asyncio.ensure_future(gate.acquire("c", c))
        await _turns()
        second = asyncio.ensure_future(gate.acquire("b", b_old))
        await _turns()
        assert (await first)[1] == PACKAGE_SUPERSEDED           # the old waiter never gets the package
        assert gate.queued_count == 2
        assert gate.waiting_position("b", b_old) == 1           # B was not demoted behind C
        gate.release(ta)
        ticket, outcome, info = await second
        assert outcome == PACKAGE_GRANTED and info.inherited_slot is True
        gate.release(ticket)
        ctk, _, _ = await other
        gate.release(ctk)
        assert gate.stats()["superseded_total"] == 1

    @async_test
    async def test_retained_slot_is_inherited_dropped_and_never_blocks(self):
        gate, _ = _gate()
        ta, _, _ = await gate.acquire("a", _Owner())
        b = _Owner()
        waiter = asyncio.ensure_future(gate.acquire("b", b))
        await _turns()
        assert gate.retain_slot("b", b) is True
        assert (await waiter)[1] == PACKAGE_SUPERSEDED
        assert gate.queued_count == 0 and gate.slot_count == 1
        assert gate.retain_slot("b", b) is False                # no waiting ticket any more
        gate.drop_slot("b", b)
        gate.drop_slot("b", b)                                  # idempotent
        assert gate.slot_count == 0
        gate.release(ta)
        assert gate.is_free

    @async_test
    async def test_a_zone_that_lost_its_active_package_registers_anew_at_the_tail(self):
        gate, _ = _gate()
        a, b = _Owner(), _Owner()
        ta, _, _ = await gate.acquire("a", a)
        wb = asyncio.ensure_future(gate.acquire("b", b))
        await _turns()
        # zone A's running package is replaced: its successor queues behind B
        gate.release(ta, reason="replaced")
        a2 = asyncio.ensure_future(gate.acquire("a", a))
        await _turns()
        assert gate.holds("b", b) and gate.waiting_position("a", a) == 1
        tb, _, _ = await wb
        gate.release(tb)
        ta2, outcome, _ = await a2
        assert outcome == PACKAGE_GRANTED
        gate.release(ta2)

    @async_test
    async def test_cancel_while_waiting_removes_the_ticket(self):
        gate, _ = _gate()
        ta, _, _ = await gate.acquire("a", _Owner())
        waiter = asyncio.ensure_future(gate.acquire("b", _Owner()))
        await _turns()
        waiter.cancel()
        await _turns()
        assert gate.queued_count == 0
        assert gate.stats()["cancelled_while_waiting_total"] == 1
        gate.release(ta)
        assert gate.is_free

    @async_test
    async def test_cancel_right_after_the_grant_hands_the_package_on(self):
        gate, _ = _gate()
        ta, _, _ = await gate.acquire("a", _Owner())
        first = asyncio.ensure_future(gate.acquire("b", _Owner()))
        second = asyncio.ensure_future(gate.acquire("c", _Owner()))
        await _turns()
        gate.release(ta)            # B is granted ...
        first.cancel()              # ... but cancelled before it resumed
        await _turns()
        ticket, outcome, _ = await second
        assert outcome == PACKAGE_GRANTED, "the package must not be lost in the cancelled grant"
        gate.release(ticket)
        assert gate.is_free

    @async_test
    async def test_release_is_identity_guarded_and_idempotent(self):
        gate, _ = _gate()
        ta, _, _ = await gate.acquire("a", _Owner())
        waiter = asyncio.ensure_future(gate.acquire("b", _Owner()))
        await _turns()
        assert gate.release(ta) is True
        assert gate.release(ta) is False                        # stale ticket: no effect on B
        tb, _, _ = await waiter
        assert gate.active and gate.release(ta) is False and gate.active
        assert gate.release(tb) is True and gate.release(tb) is False

    @async_test
    async def test_release_resolves_an_unfinished_travel_in_the_pacing(self):
        gate, pacing = _gate()
        ta, _, _ = await gate.acquire("a", _Owner())
        pacing.begin_travel(0.0, 15.0, 2.0)
        gate.release(ta, now=1.0, reason="replaced")
        assert not pacing.travel_pending and pacing.remaining(1.0) == 16.0

    @async_test
    async def test_withdraw_and_discard_only_touch_the_given_owner(self):
        gate, _ = _gate()
        ta, _, _ = await gate.acquire("a", _Owner())
        old, new = _Owner(), _Owner()
        waiting_old = asyncio.ensure_future(gate.acquire("b", old))
        await _turns()
        assert gate.discard_owner(new) == 0                     # a successor instance owns nothing yet
        assert gate.queued_count == 1
        assert gate.discard_owner(old) == 1
        assert (await waiting_old)[1] == PACKAGE_WITHDRAWN
        assert gate.queued_count == 0
        gate.release(ta)

    @async_test
    async def test_successor_instance_is_not_hit_by_the_old_instances_teardown(self):
        gate, _ = _gate()
        ta, _, _ = await gate.acquire("a", _Owner())
        old, new = _Owner(), _Owner()
        w_old = asyncio.ensure_future(gate.acquire("b", old))
        await _turns()
        w_new = asyncio.ensure_future(gate.acquire("b", new))   # reload: replaces the waiting ticket
        await _turns()
        assert gate.discard_owner(old) == 0 and gate.queued_count == 1
        gate.release(ta)
        ticket, outcome, _ = await w_new
        assert outcome == PACKAGE_GRANTED
        assert (await w_old)[1] == PACKAGE_SUPERSEDED
        gate.release(ticket)

    @async_test
    async def test_package_context_abort_while_waiting_holds_nothing(self):
        serial = GlobalSerialDispatch()
        owner_a, owner_b = _Owner(), _Owner()
        abort = asyncio.Event()
        async with serial.package("a", owner_a) as first:
            assert first.granted

            async def second():
                async with serial.package("b", owner_b, abort=abort) as pkg:
                    return pkg.outcome, pkg.granted

            task = asyncio.ensure_future(second())
            await _turns()
            assert serial.packages.queued_count == 1
            abort.set()
            outcome, granted = await task
            assert outcome == PACKAGE_ABORTED and granted is False
            assert serial.packages.queued_count == 0 and serial.packages.holds("a", owner_a)
        assert serial.packages.is_free

    @async_test
    async def test_package_context_with_an_already_set_abort_never_queues(self):
        serial = GlobalSerialDispatch()
        abort = asyncio.Event()
        abort.set()
        async with serial.package("a", _Owner(), abort=abort) as pkg:
            assert pkg.outcome == PACKAGE_ABORTED
        assert serial.packages.is_free and serial.packages.stats()["granted_total"] == 0

    @async_test
    async def test_package_context_releases_in_finally_on_error(self):
        serial = GlobalSerialDispatch()
        try:
            async with serial.package("a", _Owner()) as pkg:
                assert pkg.granted
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert serial.packages.is_free
        assert serial.packages.stats()["last_release_reason"] == "error"

    @async_test
    async def test_safety_admission_never_touches_the_package_gate(self):
        serial = GlobalSerialDispatch()
        async with serial.package("a", _Owner()):
            async with serial.admit(safety=True):
                pass                                            # a Safety send needs no package
            assert serial.packages.active
        assert serial.packages.is_free

    def test_stats_are_json_safe(self):
        gate, _ = _gate()
        text = json.dumps(gate.stats())
        assert "cover." not in text


# ===========================================================================
# Executor + shared pacing across packages
# ===========================================================================

def _item(cover, target, index=0, zone="z1", klass=None):
    if klass is None:
        klass = (DispatchTargetClass.FULL_CLOSE if target == 0
                 else DispatchTargetClass.FULL_OPEN if target == 100
                 else DispatchTargetClass.INTERMEDIATE)
    return DispatchPlanItem(
        zone_id=zone, zone_index=0, cover_entity_id=cover, cover_index=index, target_ha=target,
        target_class=klass, decision_ref="d", zone_generation=0, reason=None)


class _Run:
    """One package's execution against a shared clock and shared pacing."""

    def __init__(self, clock: Clock, pacing: NormalPacing, travel_s: float = 20.0):
        self.clock, self.pacing, self.travel_s = clock, pacing, travel_s
        self.starts: list[tuple[str, float]] = []
        self.cancellation = asyncio.Event()
        self.validations: dict[str, ExecutionValidation] = {}

    async def _dispatch(self, item):
        self.starts.append((item.cover_entity_id, self.clock.now()))
        return DispatchOutcome(success=True, attempted=True, expected_travel_s=self.travel_s)

    async def _complete(self, item, cancellation):
        self.clock.t += self.travel_s
        return CompletionOutcome(status="completed", completion_method="position")

    def _validate(self, item):
        return self.validations.get(item.cover_entity_id, ExecutionValidation(ValidationAction.EXECUTE))

    async def _sleep(self, seconds):
        self.clock.t += seconds

    async def run(self, items):
        plan = build_dispatch_plan(plan_id="p", trigger="t", items=items, created_at="t0")
        return await DispatchPlanExecutor().execute_plan(
            plan=plan, dispatch_item=self._dispatch, wait_for_completion=self._complete,
            validate_item=self._validate, is_generation_current=lambda i: True,
            cancellation=self.cancellation, sleep=self._sleep, clock=self.clock.now,
            pacing=self.pacing)


class TestExecutorSharedPacing:
    @async_test
    async def test_intermediate_packages_of_two_zones_are_one_settle_apart_no_double_pause(self):
        clock, pacing = Clock(), NormalPacing()
        a, b = _Run(clock, pacing), _Run(clock, pacing)
        await a.run([_item("a1", 50), _item("a2", 50, 1)])
        await b.run([_item("b1", 50), _item("b2", 50, 1)])
        times = [t for _, t in a.starts + b.starts]
        assert times == [0.0, 22.0, 44.0, 66.0]      # travel 20 + exactly one settle of 2 at every step

    @async_test
    async def test_end_positions_keep_the_start_spacing_across_zones(self):
        clock, pacing = Clock(), NormalPacing()
        a, b = _Run(clock, pacing), _Run(clock, pacing)
        await a.run([_item("a1", 0), _item("a2", 0, 1)])
        await b.run([_item("b1", 0), _item("b2", 0, 1)])
        assert [t for _, t in a.starts + b.starts] == [0.0, 2.0, 4.0, 6.0]
        assert clock.t == 6.0                         # no completion was ever awaited

    @async_test
    async def test_mixed_targets_follow_the_command_that_was_actually_sent(self):
        clock, pacing = Clock(), NormalPacing()
        run = _Run(clock, pacing)
        await run.run([_item("c1", 50), _item("c2", 0, 1), _item("c3", 0, 2), _item("c4", 50, 3)])
        # the plan puts the end positions first: end -> end is 2 s after the start,
        # end -> intermediate is 2 s after the last end-position start, and the
        # next intermediate cover waits for completion + one settle
        assert run.starts == [("c2", 0.0), ("c3", 2.0), ("c1", 4.0), ("c4", 26.0)]

    @async_test
    async def test_end_position_zone_after_an_intermediate_zone_waits_for_its_settle(self):
        clock, pacing = Clock(), NormalPacing()
        a, b = _Run(clock, pacing), _Run(clock, pacing)
        await a.run([_item("a1", 50)])
        await b.run([_item("b1", 0)])
        assert b.starts == [("b1", 22.0)]

    @async_test
    async def test_intermediate_zone_after_an_end_position_zone_waits_the_start_spacing(self):
        clock, pacing = Clock(), NormalPacing()
        a, b = _Run(clock, pacing), _Run(clock, pacing)
        await a.run([_item("a1", 0)])
        await b.run([_item("b1", 50)])
        assert b.starts == [("b1", 2.0)]

    @async_test
    async def test_a_skipped_item_creates_no_travel_and_no_pacing(self):
        clock, pacing = Clock(), NormalPacing()
        run = _Run(clock, pacing)
        run.validations["a1"] = ExecutionValidation(ValidationAction.SKIP, reason="already_at_target")
        await run.run([_item("a1", 50), _item("a2", 50, 1)])
        assert run.starts == [("a2", 0.0)]            # nothing waited for the skipped cover
        assert pacing.remaining(clock.now()) == 2.0   # only a2's own settle

    @async_test
    async def test_a_cancelled_package_keeps_a_bounded_boundary_for_the_next(self):
        clock, pacing = Clock(), NormalPacing()
        a = _Run(clock, pacing)

        async def cancelled_wait(item, cancellation):
            raise asyncio.CancelledError()

        a._complete = cancelled_wait
        plan = build_dispatch_plan(plan_id="p", trigger="t", items=[_item("a1", 50)], created_at="t0")
        try:
            await DispatchPlanExecutor().execute_plan(
                plan=plan, dispatch_item=a._dispatch, wait_for_completion=a._complete,
                validate_item=a._validate, is_generation_current=lambda i: True,
                cancellation=a.cancellation, sleep=a._sleep, clock=clock.now, pacing=pacing)
        except asyncio.CancelledError:
            pass
        # the travel (expected 20 s) was left unresolved: the next start is bounded by it
        assert pacing.basis == BASIS_UNCONFIRMED and pacing.remaining(0.0) == 22.0
        b = _Run(clock, pacing)
        await b.run([_item("b1", 50)])
        assert b.starts == [("b1", 22.0)]

    @async_test
    async def test_the_any_of_signal_names_the_safety_source(self):
        local, safety = asyncio.Event(), asyncio.Event()
        signal = AnyOfSignals(local=local, safety=safety)
        assert signal.fired is None
        safety.set()
        assert signal.fired == "safety"
