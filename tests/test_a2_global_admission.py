"""A2 -- global Safety priority and abortable completion waits (unit level).

Self-contained: only the pure admission / completion modules are imported (the
coordinator-level scenarios live next to the shared coordinator harness in
tests/test_rain_safe_position_semantics.py).  Everything is driven with
controlled events and a fake clock -- no test depends on real elapsed time for
its verdict.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading" / "cover_control"


def _load(name: str):
    """Load a pure module straight from its file (no Home Assistant import)."""
    spec = importlib.util.spec_from_file_location(f"_a2_{name}", _PKG / f"{name}.py",
                                                  submodule_search_locations=[str(_PKG)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


try:  # normal case: the package imports fine under the stubbed test environment
    from custom_components.smartshading.cover_control import dispatch_completion as dc
    from custom_components.smartshading.cover_control import global_dispatch_throttle as gdt
except Exception:  # pragma: no cover - defensive
    dc = _load("dispatch_completion")
    gdt = _load("global_dispatch_throttle")

GlobalSerialDispatch = gdt.GlobalSerialDispatch
PriorityAdmission = gdt.PriorityAdmission
CompletionStatus = dc.CompletionStatus


def run(coro):
    return asyncio.run(coro)


async def _settle(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _request(admission, order, name, *, safety, hold=None):
    async with admission.admit(safety=safety):
        order.append(name)
        if hold is not None:
            await hold.wait()


# ---------------------------------------------------------------------------
# Priority selection
# ---------------------------------------------------------------------------


class TestPrioritySelection:
    def test_safety_is_admitted_before_all_earlier_queued_normals(self):
        """The reproduced pre-fix failure: with FIFO, a late Safety was 7th."""
        order: list[str] = []

        async def main():
            adm = GlobalSerialDispatch().admission
            await adm.acquire()                               # zone A holds the admission
            tasks = [asyncio.ensure_future(_request(adm, order, f"n{i}", safety=False))
                     for i in range(6)]
            await _settle()
            tasks.append(asyncio.ensure_future(_request(adm, order, "S", safety=True)))
            await _settle()
            assert adm.queued_count == 7 and adm.safety_waiting
            adm.release()
            await asyncio.gather(*tasks)

        run(main())
        assert order == ["S", "n0", "n1", "n2", "n3", "n4", "n5"]

    def test_normals_are_fifo_among_themselves(self):
        order: list[str] = []

        async def main():
            adm = PriorityAdmission()
            await adm.acquire()
            tasks = [asyncio.ensure_future(_request(adm, order, f"n{i}", safety=False))
                     for i in range(5)]
            await _settle()
            adm.release()
            await asyncio.gather(*tasks)

        run(main())
        assert order == [f"n{i}" for i in range(5)]

    def test_safety_is_fifo_among_itself_without_a_kind_ranking(self):
        order: list[str] = []

        async def main():
            adm = PriorityAdmission()
            await adm.acquire()
            names = ["storm", "rain", "wind", "rain2"]
            tasks = []
            for n in names:
                tasks.append(asyncio.ensure_future(_request(adm, order, n, safety=True)))
                await _settle()
            adm.release()
            await asyncio.gather(*tasks)

        run(main())
        assert order == ["storm", "rain", "wind", "rain2"]

    def test_mixed_arrival_is_safety_first_then_arrival_order(self):
        order: list[str] = []

        async def main():
            adm = PriorityAdmission()
            await adm.acquire()
            tasks = []
            for name, safety in [("n0", False), ("s0", True), ("n1", False),
                                 ("s1", True), ("n2", False)]:
                tasks.append(asyncio.ensure_future(_request(adm, order, name, safety=safety)))
                await _settle()
            adm.release()
            await asyncio.gather(*tasks)

        run(main())
        assert order == ["s0", "s1", "n0", "n1", "n2"]

    def test_no_barging_a_newcomer_cannot_pass_a_queued_waiter_at_release(self):
        order: list[str] = []

        async def main():
            adm = PriorityAdmission()
            await adm.acquire()
            first = asyncio.ensure_future(_request(adm, order, "queued", safety=False))
            await _settle()
            adm.release()                                     # hands off to "queued"
            assert adm.locked() is True                       # never free in between
            late = asyncio.ensure_future(_request(adm, order, "late", safety=False))
            await asyncio.gather(first, late)

        run(main())
        assert order == ["queued", "late"]

    def test_admitted_holder_is_never_revoked_by_a_later_safety(self):
        order: list[str] = []

        async def main():
            adm = PriorityAdmission()
            gate = asyncio.Event()
            holder = asyncio.ensure_future(_request(adm, order, "holder", safety=False, hold=gate))
            await _settle()
            safety = asyncio.ensure_future(_request(adm, order, "S", safety=True))
            await _settle()
            assert order == ["holder"] and adm.safety_waiting
            gate.set()
            await asyncio.gather(holder, safety)

        run(main())
        assert order == ["holder", "S"]

    def test_normals_are_not_starved_once_safety_is_done(self):
        order: list[str] = []

        async def main():
            adm = PriorityAdmission()
            await adm.acquire()
            tasks = [asyncio.ensure_future(_request(adm, order, f"n{i}", safety=False))
                     for i in range(3)]
            await _settle()
            for i in range(3):
                tasks.append(asyncio.ensure_future(_request(adm, order, f"s{i}", safety=True)))
                await _settle()
            adm.release()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

        run(main())
        assert order == ["s0", "s1", "s2", "n0", "n1", "n2"]


# ---------------------------------------------------------------------------
# Lock compatibility, info, stats
# ---------------------------------------------------------------------------


class TestLockCompatibilityAndInfo:
    def test_lock_surface(self):
        async def main():
            serial = GlobalSerialDispatch()
            lock = serial.lock
            assert lock.locked() is False
            async with lock:
                assert lock.locked() is True
            await lock.acquire()
            assert lock.locked() is True
            lock.release()
            assert lock.locked() is False
            with pytest.raises(RuntimeError):
                lock.release()

        run(main())

    def test_info_reports_class_wait_and_overtaken_count(self):
        infos = {}

        async def one(adm, name, safety):
            async with adm.admit(safety=safety) as a:
                infos[name] = a.info

        async def main():
            clock = {"t": 0.0}
            adm = PriorityAdmission(mono_clock=lambda: clock["t"])
            await adm.acquire()
            tasks = [asyncio.ensure_future(one(adm, f"n{i}", False)) for i in range(4)]
            await _settle()
            clock["t"] = 0.5
            tasks.append(asyncio.ensure_future(one(adm, "S", True)))
            await _settle()
            clock["t"] = 1.5
            adm.release()
            await asyncio.gather(*tasks)

        run(main())
        assert infos["S"].priority == gdt.ADMISSION_SAFETY
        assert infos["S"].overtaken == 4
        assert infos["S"].wait_ms == pytest.approx(1000.0)
        assert infos["n0"].overtaken == 0 and infos["n0"].priority == gdt.ADMISSION_NORMAL

    def test_stats(self):
        async def main():
            adm = PriorityAdmission()
            await adm.acquire()
            t = asyncio.ensure_future(_request(adm, [], "S", safety=True))
            await _settle()
            st = adm.stats()
            assert st["held"] and st["queued"] == 1 and st["queued_safety"] == 1
            adm.release()
            await t
            st = adm.stats()
            assert st["admitted_safety"] == 1 and not st["held"] and st["queued"] == 0

        run(main())

    def test_safety_waiting_event_follows_the_queue(self):
        async def main():
            adm = PriorityAdmission()
            ev = adm.safety_waiting_event
            assert not ev.is_set()
            await adm.acquire()
            t = asyncio.ensure_future(_request(adm, [], "S", safety=True))
            await _settle()
            assert ev.is_set()
            adm.release()
            await t
            assert not ev.is_set()

        run(main())


# ---------------------------------------------------------------------------
# Cancellation / lifecycle: nothing is left behind
# ---------------------------------------------------------------------------


class TestCancellationAndCleanup:
    def test_cancelled_queued_waiter_is_removed_immediately(self):
        async def main():
            adm = PriorityAdmission()
            await adm.acquire()
            t = asyncio.ensure_future(_request(adm, [], "n", safety=False))
            s = asyncio.ensure_future(_request(adm, [], "S", safety=True))
            await _settle()
            assert adm.queued_count == 2
            s.cancel()
            await asyncio.gather(s, return_exceptions=True)
            assert adm.queued_count == 1 and not adm.safety_waiting
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
            assert adm.queued_count == 0
            adm.release()
            assert not adm.locked()
            assert adm.stats()["cancelled_while_waiting_total"] == 2

        run(main())

    def test_waiter_cancelled_after_being_granted_passes_the_admission_on(self):
        order: list[str] = []

        async def main():
            adm = PriorityAdmission()
            await adm.acquire()
            a = asyncio.ensure_future(_request(adm, order, "a", safety=False))
            b = asyncio.ensure_future(_request(adm, order, "b", safety=False))
            await _settle()
            adm.release()                       # granted to a (future resolved) ...
            a.cancel()                          # ... cancelled before it resumed
            await asyncio.gather(a, return_exceptions=True)
            await asyncio.wait_for(b, timeout=2)
            assert not adm.locked()

        run(main())
        assert order == ["b"]

    def test_no_pending_tasks_or_epoch_waiters_after_a_cancelled_epoch_wait(self):
        async def main():
            serial = GlobalSerialDispatch()
            sig = serial.safety_epoch_signal()
            w = asyncio.ensure_future(sig.wait())
            await _settle()
            w.cancel()
            await asyncio.gather(w, return_exceptions=True)
            assert serial.admission._epoch_waiters == []
            others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            assert others == []

        run(main())

    def test_exception_in_holder_still_releases(self):
        async def main():
            adm = PriorityAdmission()
            with pytest.raises(ValueError):
                async with adm.admit(safety=False):
                    raise ValueError("boom")
            assert not adm.locked()

        run(main())


# ---------------------------------------------------------------------------
# Integration-wide safety epoch signal
# ---------------------------------------------------------------------------


class TestSafetyEpochSignal:
    def test_fires_when_any_safety_enters_admission_afterwards(self):
        async def main():
            serial = GlobalSerialDispatch()
            sig = serial.safety_epoch_signal()
            assert not sig.is_set()
            waiter = asyncio.ensure_future(sig.wait())
            await _settle()
            assert not waiter.done()
            async with serial.admit(safety=True):       # admitted immediately (free)
                pass
            await asyncio.wait_for(waiter, timeout=1)
            assert sig.is_set()

        run(main())

    def test_normal_admissions_do_not_fire_it(self):
        async def main():
            serial = GlobalSerialDispatch()
            sig = serial.safety_epoch_signal()
            async with serial.admit(safety=False):
                pass
            assert not sig.is_set()

        run(main())

    def test_is_edge_triggered_a_later_plan_is_not_aborted_by_an_older_safety(self):
        async def main():
            serial = GlobalSerialDispatch()
            async with serial.admit(safety=True):
                pass
            later = serial.safety_epoch_signal()
            assert not later.is_set()

        run(main())

    def test_any_of_signals_reports_which_one_fired_and_leaves_no_task(self):
        async def main():
            serial = GlobalSerialDispatch()
            local = asyncio.Event()
            any_sig = dc.AnyOfSignals(local=local, safety=serial.safety_epoch_signal())
            w = asyncio.ensure_future(any_sig.wait())
            await _settle()
            async with serial.admit(safety=True):
                pass
            await asyncio.wait_for(w, timeout=1)
            assert any_sig.fired == "safety"
            others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            assert others == []

        run(main())


# ---------------------------------------------------------------------------
# wait_for_travel_completion: cancellation / generation / unload awareness
# ---------------------------------------------------------------------------


def _hass_with(state: str, position: int | None):
    hass = MagicMock()
    attrs = {} if position is None else {"current_position": position}
    hass.states.get.return_value = MagicMock(state=state, attributes=attrs)
    return hass


def _kwargs(**over):
    base = dict(
        entity_id="cover.a", target_position_ha=50, invert_position=False,
        has_reliable_position_feedback=True, start_position_ha=0,
        travel_time_open_s=30.0, travel_time_close_s=30.0, max_wait_s=40.0,
    )
    base.update(over)
    return base


class TestAbortableCompletionWait:
    def test_cancellation_ends_a_feedback_poll_wait_immediately(self):
        async def main():
            hass = _hass_with("open", 0)               # never reaches 50
            cancel = asyncio.Event()
            t = asyncio.ensure_future(dc.wait_for_travel_completion(
                hass, **_kwargs(), cancellation=cancel))      # real sleep: 1 s polls
            await _settle()
            assert not t.done()
            cancel.set()
            res = await asyncio.wait_for(t, timeout=0.5)     # far below one poll interval
            return res

        res = run(main())
        assert res.status is CompletionStatus.CANCELLED and res.timed_out is False
        assert res.completed is False and res.cancel_reason == "cancellation_signal"

    def test_cancellation_ends_a_feedbackless_single_sleep_immediately(self):
        async def main():
            hass = MagicMock()
            cancel = asyncio.Event()
            t = asyncio.ensure_future(dc.wait_for_travel_completion(
                hass, **_kwargs(has_reliable_position_feedback=False), cancellation=cancel))
            await _settle()
            cancel.set()
            return await asyncio.wait_for(t, timeout=0.5)     # would sleep 15 s

        res = run(main())
        assert res.status is CompletionStatus.CANCELLED and not res.timed_out

    def test_already_cancelled_before_start_returns_without_waiting(self):
        async def main():
            cancel = asyncio.Event()
            cancel.set()
            return await asyncio.wait_for(dc.wait_for_travel_completion(
                _hass_with("open", 0), **_kwargs(), cancellation=cancel), timeout=0.5)

        assert run(main()).status is CompletionStatus.CANCELLED

    def test_stale_and_unloaded_are_distinct_statuses(self):
        async def main(verdict):
            return await asyncio.wait_for(dc.wait_for_travel_completion(
                _hass_with("open", 0), **_kwargs(), abort_check=lambda: verdict), timeout=0.5)

        stale = run(main((CompletionStatus.STALE, "generation_superseded")))
        unloaded = run(main((CompletionStatus.UNLOADED, "unloading")))
        assert stale.status is CompletionStatus.STALE and stale.cancel_reason == "generation_superseded"
        assert unloaded.status is CompletionStatus.UNLOADED
        assert not stale.timed_out and not unloaded.timed_out
        assert not stale.completed and not unloaded.completed

    def test_generation_change_during_the_wait_is_seen_after_the_next_wake_up(self):
        async def main():
            gen = {"v": 1}
            sleeps = []

            async def fake_sleep(s):
                sleeps.append(s)
                gen["v"] = 2                              # superseded while sleeping

            res = await dc.wait_for_travel_completion(
                _hass_with("open", 0), **_kwargs(), sleep=fake_sleep,
                abort_check=lambda: ((CompletionStatus.STALE, "generation_superseded")
                                     if gen["v"] != 1 else None))
            return res, sleeps

        res, sleeps = run(main())
        assert res.status is CompletionStatus.STALE and len(sleeps) == 1

    def test_timeout_is_a_timeout_and_never_counted_as_completed(self):
        async def main():
            now = {"t": 0.0}

            async def fake_sleep(s):
                now["t"] += s

            return await dc.wait_for_travel_completion(
                _hass_with("open", 0), **_kwargs(max_wait_s=5.0),
                mono_clock=lambda: now["t"], sleep=fake_sleep)

        res = run(main())
        assert res.status is CompletionStatus.TIMED_OUT and res.timed_out is True
        assert res.completed is False

    def test_completion_is_still_reported_as_completed(self):
        res = run(dc.wait_for_travel_completion(
            _hass_with("open", 50), **_kwargs(),
            cancellation=asyncio.Event()))
        assert res.status is CompletionStatus.COMPLETED and res.completed and not res.timed_out

    def test_without_cancel_inputs_the_feedbackless_wait_is_one_plain_sleep(self):
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)

        res = run(dc.wait_for_travel_completion(
            MagicMock(), **_kwargs(has_reliable_position_feedback=False), sleep=fake_sleep))
        assert sleeps == [15.0] and res.status is CompletionStatus.COMPLETED

    def test_external_task_cancellation_propagates_and_leaves_no_task(self):
        async def main():
            t = asyncio.ensure_future(dc.wait_for_travel_completion(
                _hass_with("open", 0), **_kwargs(), cancellation=asyncio.Event()))
            await _settle()
            t.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t
            others = [x for x in asyncio.all_tasks() if x is not asyncio.current_task()]
            assert others == []

        run(main())

    def test_default_status_is_derived_from_timed_out(self):
        assert dc.CompletionResult(dc.CompletionMethod.POSITION, 1.0, False).status is CompletionStatus.COMPLETED
        assert dc.CompletionResult(dc.CompletionMethod.TIMEOUT, 1.0, True).status is CompletionStatus.TIMED_OUT


# ---------------------------------------------------------------------------
# Production-shaped cross-zone preemption with the real executor
# ---------------------------------------------------------------------------


class TestCrossZonePlanPreemption:
    """A normal plan of zone A sits in a completion wait OUTSIDE the admission;
    zone B's Safety enters the shared admission.  The plan must end at once,
    never dispatch its remaining item, and must not be replayed."""

    def _plan(self):
        from custom_components.smartshading.cover_control import dispatch_plan_executor as ex
        return ex

    def test_foreign_safety_ends_the_running_completion_wait(self):
        ex = self._plan()
        sent: list[str] = []

        async def main():
            serial = GlobalSerialDispatch()
            local = asyncio.Event()
            signal = dc.AnyOfSignals(local=local, safety=serial.safety_epoch_signal())

            async def dispatch_item(item):
                async with serial.admit(safety=False):
                    sent.append(item.cover_entity_id)
                return ex.DispatchOutcome(success=True)

            async def wait_for_completion(item, cancellation):
                res = await dc.wait_for_travel_completion(
                    _hass_with("open", 0), **_kwargs(entity_id=item.cover_entity_id),
                    cancellation=cancellation)
                return ex.CompletionOutcome(
                    status="completed" if res.completed else
                    ("timed_out" if res.timed_out else "cancelled"))

            plan = _build_plan(["cover.a1", "cover.a2"])
            task = asyncio.ensure_future(ex.DispatchPlanExecutor().execute_plan(
                plan=plan, dispatch_item=dispatch_item,
                wait_for_completion=wait_for_completion,
                validate_item=lambda item: ex.ExecutionValidation(ex.ValidationAction.EXECUTE),
                is_generation_current=lambda item: True,
                cancellation=signal, sleep=_no_sleep, clock=_clock))
            await _settle(20)
            assert sent == ["cover.a1"] and not task.done()      # parked in the completion wait
            assert not serial.admission.locked()                  # admission is NOT held
            async with serial.admit(safety=True):                 # zone B: Safety, admitted at once
                pass
            result = await asyncio.wait_for(task, timeout=1)
            return result, signal

        result, signal = run(main())
        assert signal.fired == "safety" and result.preempted
        assert sent == ["cover.a1"]                               # a2 never dispatched, never replayed

    def test_without_the_cross_zone_signal_the_wait_keeps_running(self):
        """Counter-proof: with only the coordinator-local event the foreign
        Safety cannot end the wait."""
        ex = self._plan()

        async def main():
            serial = GlobalSerialDispatch()
            local = asyncio.Event()

            async def dispatch_item(item):
                return ex.DispatchOutcome(success=True)

            async def wait_for_completion(item, cancellation):
                res = await dc.wait_for_travel_completion(
                    _hass_with("open", 0), **_kwargs(entity_id=item.cover_entity_id),
                    cancellation=cancellation)
                return ex.CompletionOutcome(status="cancelled" if not res.completed else "completed")

            task = asyncio.ensure_future(ex.DispatchPlanExecutor().execute_plan(
                plan=_build_plan(["cover.a1", "cover.a2"]), dispatch_item=dispatch_item,
                wait_for_completion=wait_for_completion,
                validate_item=lambda item: ex.ExecutionValidation(ex.ValidationAction.EXECUTE),
                is_generation_current=lambda item: True, cancellation=local,
                sleep=_no_sleep, clock=_clock))
            await _settle(20)
            async with serial.admit(safety=True):
                pass
            await asyncio.sleep(0.05)
            still_running = not task.done()
            local.set()
            await asyncio.wait_for(task, timeout=1)
            return still_running

        assert run(main()) is True


async def _no_sleep(_seconds):
    return None


def _clock():
    return 0.0


def _build_plan(entities):
    """A cycle plan with one INTERMEDIATE item per cover (completion wait applies)."""
    from custom_components.smartshading.cover_control.dispatch_plan import (
        DispatchPlanItem,
        build_dispatch_plan,
    )
    from custom_components.smartshading.engines.dispatch_classification import DispatchTargetClass

    items = [
        DispatchPlanItem(
            zone_id="za", zone_index=0, cover_entity_id=ent, cover_index=i, target_ha=50,
            target_class=DispatchTargetClass.INTERMEDIATE, decision_ref=f"d{i}",
            zone_generation=0,
        )
        for i, ent in enumerate(entities)
    ]
    return build_dispatch_plan(plan_id="p1", trigger="t", items=items, created_at="t0")
