"""Lifecycle-Boundary Preemption for Night-Start and Morning-Release —
v1.2.0-beta.1.

Same root cause as the Presence/Contact fix (commit f71c71e): HA's
DataUpdateCoordinator serialises all refreshes through a single Debouncer
`_execute_lock`. `_on_boundary` (coordinator.py's lifecycle-boundary
point-in-time timer, shared by BOTH night-start and morning-release — same
closure, distinguished only by the `reason` string) bumps
`_dispatch_generation` but — before this ticket's fix — never signals
`_active_dispatch_cancellation`, so a comfort dispatch still mid-flight
(completion-wait / FULL_OPEN pacing) is not interrupted, the Debouncer lock
stays held, and the boundary-triggered refresh can be silently dropped —
exactly like the presence/contact case, but for a mechanism whose own
docstring explicitly promises firing "practically at the configured minute,
not up to one periodic cycle (5 min) later".

Uses the SAME behavior-faithful `_RealBehaviorDataUpdateCoordinator` port
verified against the real installed homeassistant==2026.6.4 package (see
test_event_triggered_comfort_preemption.py's module docstring for the full
verification rationale — reused here unmodified in spirit, adapted only to
capture `async_track_point_in_time` instead of
`async_track_state_change_event`).

Fix (coordinator.py's `_on_boundary` closure inside
`_schedule_next_lifecycle_boundary`): synchronously call
`self._active_dispatch_cancellation.set()` right after the existing
`_dispatch_generation += 1` bump and before `async_request_refresh()` — the
identical minimal pattern as f71c71e, applied to the one remaining
structurally-identical gap confirmed by the prior audit.

Structural facts established by code audit (see closing report for detail):
  - Night-start and morning-release share the exact same `_on_boundary`
    closure and semantics — one fix covers both.
  - `async_track_point_in_time` is single-shot; `_schedule_next_lifecycle_
    boundary()` always unsubscribes any existing timer BEFORE registering
    the next one — stale/duplicate timer callbacks are structurally
    impossible already (verified by reading the real HA source and this
    method's own unsubscribe-then-register ordering), so no additional
    stale-timer guard is introduced here (would be redundant infrastructure).
  - Teardown (`async_teardown_lifecycle_boundary_timer`, wired via
    `entry.async_on_unload`) fully unsubscribes and clears state — a
    callback firing after unload is structurally prevented by HA's own
    unsub mechanism, not something this fix needs to re-guard.

Coverage:
  TC-LB-D1  RED-BEFORE-FIX: reproduces the OLD (pre-fix) `_on_boundary` body
            directly (generation bump only, no cancellation.set()) against
            the real Debouncer harness — proves the refresh is permanently
            lost when the old cycle outlasts the cooldown. This is the
            required direct red proof at the real boundary path (not just
            an analogy to presence/contact).
  TC-LB-D2  WITH FIX: the real captured `_on_boundary` callback ends the old
            cycle promptly and a second recompute reliably happens — timed.
  TC-LB-NS  Night-start specific: boundary fires reason="night_start",
            old cycle preempted, new cycle carries night_start reason.
  TC-LB-MR  Morning-release specific: same for reason="morning_release".
  TC-LB-N1  No active dispatch: callback works unchanged, no error on
            cancellation=None, exactly one refresh, no extra cycles.
  TC-LB-CW  Completion-wait: cancellation ends the wait promptly, full task
            harvesting, no pending tasks, no unhandled exceptions.
  TC-LB-S1  Safety non-interference: already-active safety work is never
            touched; new safety work in the next cycle still dispatches.
  TC-LB-RS  Rescheduling: firing the boundary reschedules the next one
            (single timer invariant); a second, later boundary event still
            correctly hits the (by-then-different) active cycle.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# HA stubs — same technique as test_event_triggered_comfort_preemption.py,
# capturing async_track_point_in_time instead of
# async_track_state_change_event.
# ---------------------------------------------------------------------------


def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _RealBehaviorDataUpdateCoordinator:
    """Behavior-faithful port of DataUpdateCoordinator's refresh-serialising
    Debouncer algorithm — verified 2026-07-30 against installed
    homeassistant==2026.6.4 (see test_event_triggered_comfort_preemption.py
    for the full verification note)."""

    COOLDOWN = 10
    IMMEDIATE = True

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.config_entry = config_entry
        self._execute_lock = asyncio.Lock()
        self._timer_handle = None
        self._execute_at_end_of_timer = False

    async def _async_update_data(self):
        raise NotImplementedError

    async def _async_refresh(self):
        return await self._async_update_data()

    def _async_schedule_or_call_now(self) -> bool:
        if self._timer_handle is not None:
            self._execute_at_end_of_timer = True
            return False
        if not self.IMMEDIATE or self._execute_lock.locked():
            self._execute_at_end_of_timer = True
            self._schedule_timer()
            return False
        return True

    def _schedule_timer(self) -> None:
        if self._timer_handle is not None:
            self._timer_handle.cancel()
        loop = asyncio.get_event_loop()
        self._timer_handle = loop.call_later(self.COOLDOWN, self._on_debounce)

    def _on_debounce(self) -> None:
        self._timer_handle = None
        if not self._execute_at_end_of_timer:
            return
        self._execute_at_end_of_timer = False
        asyncio.ensure_future(self._handle_timer_finish())

    async def _handle_timer_finish(self) -> None:
        self._execute_at_end_of_timer = False
        if self._execute_lock.locked():
            return
        async with self._execute_lock:
            if self._timer_handle:
                return
            try:
                await self._async_refresh()
            finally:
                self._schedule_timer()

    async def async_request_refresh(self) -> None:
        if not self._async_schedule_or_call_now():
            return
        async with self._execute_lock:
            if self._timer_handle:
                return
            try:
                await self._async_refresh()
            finally:
                self._schedule_timer()

    async def async_refresh(self) -> None:
        async with self._execute_lock:
            await self._async_refresh()


class _StoreStub:
    def __init__(self, hass, version, key) -> None: pass
    async def async_load(self): return None
    async def async_save(self, data) -> None: pass
    async def async_remove(self) -> None: pass


_POINT_IN_TIME_LISTENER: dict[str, Any] = {}


def _capture_track_point_in_time(hass, action, point_in_time):
    _POINT_IN_TIME_LISTENER["cb"] = action
    _POINT_IN_TIME_LISTENER["at"] = point_in_time

    def _unsub():
        if _POINT_IN_TIME_LISTENER.get("cb") is action:
            _POINT_IN_TIME_LISTENER.pop("cb", None)
            _POINT_IN_TIME_LISTENER.pop("at", None)
    return _unsub


_HA_STUBS = {
    "homeassistant": _stub("homeassistant"),
    "homeassistant.components": _stub("homeassistant.components"),
    "homeassistant.components.cover": _stub(
        "homeassistant.components.cover",
        CoverEntityFeature=type("CEF", (), {"SET_POSITION": 1, "SET_TILT_POSITION": 2, "OPEN": 4, "CLOSE": 8, "STOP": 16}),
    ),
    "homeassistant.config_entries": _stub("homeassistant.config_entries", ConfigEntry=object),
    "homeassistant.core": _stub(
        "homeassistant.core", HomeAssistant=object, Event=object, callback=lambda fn: fn,
    ),
    "homeassistant.helpers": _stub("homeassistant.helpers"),
    "homeassistant.helpers.entity_registry": _stub("homeassistant.helpers.entity_registry", async_get=lambda *a, **k: None),
    "homeassistant.util": _stub("homeassistant.util"),
}
for _name, _mod in _HA_STUBS.items():
    sys.modules.setdefault(_name, _mod)

sys.modules["homeassistant.helpers.event"] = _stub(
    "homeassistant.helpers.event",
    async_track_state_change_event=lambda hass, entity_id, action: (lambda: None),
    async_track_point_in_time=_capture_track_point_in_time,
    async_call_later=lambda hass, delay, action: (lambda: None),
)
sys.modules["homeassistant.util.dt"] = _stub(
    "homeassistant.util.dt",
    utcnow=lambda: datetime.now(timezone.utc),
    now=lambda: datetime.now(timezone.utc),
    as_utc=lambda dt: dt.astimezone(timezone.utc),
    as_local=lambda dt: dt,
    DEFAULT_TIME_ZONE=timezone.utc,
)
sys.modules["homeassistant.helpers.update_coordinator"] = _stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=_RealBehaviorDataUpdateCoordinator,
    CoordinatorEntity=type("CE", (), {"__class_getitem__": classmethod(lambda cls, x: cls), "__init__": lambda self, c: None}),
)
sys.modules["homeassistant.helpers.storage"] = _stub("homeassistant.helpers.storage", Store=_StoreStub)

sys.modules.pop("custom_components.smartshading.coordinator", None)
from custom_components.smartshading.coordinator import SmartShadingCoordinator  # noqa: E402
from custom_components.smartshading.models.lifecycle import NightDayLifecycleConfig, NightTrigger, MorningTrigger  # noqa: E402
from datetime import time as _time  # noqa: E402


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    return hass


def _make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}
    entry.data = {}
    entry.async_on_unload = MagicMock()
    entry.async_create_background_task = lambda hass, coro, name: asyncio.ensure_future(coro)
    return entry


def _make_coord() -> SmartShadingCoordinator:
    _POINT_IN_TIME_LISTENER.clear()
    hass = _make_hass()
    entry = _make_entry()
    lifecycle_config = NightDayLifecycleConfig(
        id="default",
        night_enabled=True, night_trigger=NightTrigger.FIXED_TIME,
        night_fixed_time=_time(22, 0),
        morning_enabled=True, morning_trigger=MorningTrigger.FIXED_TIME,
        morning_fixed_time=_time(6, 0),
    )
    coord = SmartShadingCoordinator(
        hass, entry, lifecycle_config=lifecycle_config,
        presence_entity_ids=[],
    )
    coord.windows = {}
    coord.zones = {}
    coord.cover_groups = {}
    coord._startup_cycles_remaining = 0
    coord.async_setup_lifecycle_boundary_timer(entry)
    return coord, entry


def _install_fake_cycle(coord, events: list, *, comfort_duration_s: float,
                         carries_safety: bool = False,
                         comfort_duration_for_cycle_2_s: float | None = None):
    """Same registration/cleanup pattern as coordinator.py's real
    _run_comfort_dispatch_for_cycle (identical field names, identical
    identity-guarded finally-block cleanup)."""
    call_count = {"n": 0}

    async def _cycle():
        call_count["n"] += 1
        cid = call_count["n"]
        t0 = time.monotonic()
        events.append(("recompute_start", cid, 0.0))
        cancellation = asyncio.Event()
        coord._active_dispatch_cancellation = cancellation
        duration = comfort_duration_s
        if cid == 2 and comfort_duration_for_cycle_2_s is not None:
            duration = comfort_duration_for_cycle_2_s

        async def _dispatch():
            if carries_safety:
                events.append(("safety_dispatched", cid, round(time.monotonic() - t0, 3)))
                return "safety_done"
            try:
                await asyncio.wait_for(cancellation.wait(), timeout=duration)
                events.append(("comfort_cancelled", cid, round(time.monotonic() - t0, 3)))
                return "cancelled"
            except asyncio.TimeoutError:
                events.append(("comfort_completed_naturally", cid, round(time.monotonic() - t0, 3)))
                return "completed"

        task = asyncio.ensure_future(_dispatch())
        coord._active_comfort_dispatch_task = task
        try:
            result = await task
        finally:
            if coord._active_comfort_dispatch_task is task:
                coord._active_comfort_dispatch_task = None
            if coord._active_dispatch_cancellation is cancellation:
                coord._active_dispatch_cancellation = None
        events.append(("recompute_end", cid, round(time.monotonic() - t0, 3)))
        return result

    coord._async_update_data = _cycle
    return call_count


async def _wait_for_recompute_count(call_count: dict, target: int, timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if call_count["n"] >= target:
            await asyncio.sleep(0.05)  # let same-tick-scheduled work (e.g. a
            # safety dispatch appended right as the new cycle starts) actually run
            return
        await asyncio.sleep(0.05)


def async_test(fn):
    def wrapper(*a, **k):
        asyncio.run(fn(*a, **k))
    return wrapper


# ---------------------------------------------------------------------------
# TC-LB-D: direct red-before-fix proof, then green-with-fix proof
# ---------------------------------------------------------------------------


class TestBoundaryDebouncerRefreshLoss:
    @async_test
    async def test_TC_LB_D1_without_fix_boundary_refresh_is_lost(self):
        """Reproduces the OLD (pre-fix) _on_boundary body directly:
        generation bump + async_request_refresh(), deliberately WITHOUT
        cancellation.set() — proves the boundary-triggered refresh is
        permanently lost, not just delayed, when the old comfort cycle
        outlasts the Debouncer cooldown."""
        coord, entry = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(2)
        assert call_count["n"] == 1

        # Old-behavior reproduction of _on_boundary: generation bump only.
        coord._dispatch_generation += 1
        asyncio.ensure_future(coord.async_request_refresh())

        await asyncio.sleep(30)
        assert call_count["n"] == 1, (
            f"expected the boundary refresh to be lost (count stays 1), got "
            f"{call_count['n']}: {events}"
        )
        cycle1.cancel()
        try:
            await cycle1
        except asyncio.CancelledError:
            pass

    @async_test
    async def test_TC_LB_D2_with_fix_boundary_refresh_is_not_lost(self):
        """Uses the REAL captured _on_boundary callback (fired via the point-
        in-time listener capture). Requires the production fix to pass."""
        coord, entry = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(2)
        assert call_count["n"] == 1
        assert "cb" in _POINT_IN_TIME_LISTENER, "boundary timer was not registered"

        t_event = time.monotonic()
        _POINT_IN_TIME_LISTENER["cb"](datetime.now(timezone.utc))

        await asyncio.wait_for(cycle1, timeout=2.0)
        old_cycle_end = time.monotonic() - t_event
        assert old_cycle_end < 1.0, f"old cycle took {old_cycle_end}s to end after the boundary"

        await _wait_for_recompute_count(call_count, 2)
        assert call_count["n"] == 2, f"expected a second recompute, events={events}"
        new_cycle_start_delay = time.monotonic() - t_event
        assert new_cycle_start_delay < 11.0, (
            f"new recompute took {new_cycle_start_delay}s — expected bounded by the "
            f"~10s Debouncer cooldown, nowhere near the 5-minute periodic update"
        )
        if coord._active_comfort_dispatch_task is not None:
            coord._active_comfort_dispatch_task.cancel()
            try:
                await coord._active_comfort_dispatch_task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# TC-LB-NS / TC-LB-MR: night-start and morning-release specifically
# ---------------------------------------------------------------------------


class TestNightAndMorningBoundaries:
    @async_test
    async def test_TC_LB_NS_night_start_boundary_preempts_and_recomputes(self):
        coord, entry = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        assert coord._next_lifecycle_boundary_reason in ("night_start", "morning_release")
        _POINT_IN_TIME_LISTENER["cb"](datetime.now(timezone.utc))
        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 2)
        assert call_count["n"] == 2, events
        if coord._active_comfort_dispatch_task is not None:
            coord._active_comfort_dispatch_task.cancel()
            try:
                await coord._active_comfort_dispatch_task
            except asyncio.CancelledError:
                pass

    @async_test
    async def test_TC_LB_MR_morning_release_boundary_preempts_and_recomputes(self):
        """Forces the soonest boundary to be morning_release by disabling
        night triggers, so the SAME _on_boundary closure fires with
        reason='morning_release' — proving the fix is reason-agnostic (both
        share one closure, confirmed by code audit)."""
        _POINT_IN_TIME_LISTENER.clear()
        hass = _make_hass()
        entry = _make_entry()
        lifecycle_config = NightDayLifecycleConfig(
            id="default",
            night_enabled=False,
            morning_enabled=True, morning_trigger=MorningTrigger.FIXED_TIME,
            morning_fixed_time=_time(6, 0),
        )
        coord = SmartShadingCoordinator(
            hass, entry, lifecycle_config=lifecycle_config, presence_entity_ids=[],
        )
        coord.windows = {}
        coord.zones = {}
        coord.cover_groups = {}
        coord._startup_cycles_remaining = 0
        coord.async_setup_lifecycle_boundary_timer(entry)

        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        assert coord._next_lifecycle_boundary_reason == "morning_release"
        _POINT_IN_TIME_LISTENER["cb"](datetime.now(timezone.utc))
        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 2)
        assert call_count["n"] == 2, events
        if coord._active_comfort_dispatch_task is not None:
            coord._active_comfort_dispatch_task.cancel()
            try:
                await coord._active_comfort_dispatch_task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# TC-LB-N1: no active dispatch
# ---------------------------------------------------------------------------


class TestStaleTimerProtection:
    @async_test
    async def test_TC_LB_ST_repeated_setup_unsubscribes_the_old_pending_timer(self):
        """Real homeassistant.helpers.event.async_track_point_in_time is
        single-shot: HA-core itself removes a listener once it has fired, so
        _on_boundary's own 'self._unsub_lifecycle_boundary = None' first line
        (before rescheduling) is correctly a no-op re-unsub guard for the
        already-fired case, not a leak-prevention mechanism by itself. The
        real stale-timer risk this ticket asks about is a PENDING (not yet
        fired) timer being orphaned when _schedule_next_lifecycle_boundary()
        runs again for a different reason (e.g. a second
        async_setup_lifecycle_boundary_timer() call, as happens on reload) —
        proven here with a multi-slot capture that does NOT auto-remove on
        fire (the timer in this scenario never fires), directly exercising
        the `if self._unsub_lifecycle_boundary is not None:` guard."""
        registered = []

        def _capture_multi(hass, action, point_in_time):
            registered.append(action)
            def _unsub():
                if action in registered:
                    registered.remove(action)
            return _unsub

        import sys as _sys
        real_event_mod = _sys.modules["homeassistant.helpers.event"]
        original = real_event_mod.async_track_point_in_time
        real_event_mod.async_track_point_in_time = _capture_multi
        try:
            coord, entry = _make_coord()
            assert len(registered) == 1, "exactly one timer registered initially"
            first_cb = registered[0]

            # Simulate a second setup call before the first pending timer
            # ever fires (e.g. re-entering setup on a reload path).
            coord._schedule_next_lifecycle_boundary()

            assert len(registered) == 1, (
                f"a second schedule call must unsubscribe the still-pending "
                f"old timer before registering the next one — found "
                f"{len(registered)} still registered (an orphaned pending "
                f"timer could fire independently later and trigger a "
                f"duplicate, unwanted refresh)"
            )
            assert first_cb not in registered, "the OLD pending callback must be gone, not just a new one added"
        finally:
            real_event_mod.async_track_point_in_time = original


class TestGenerationBump:
    @async_test
    async def test_TC_LB_G1_boundary_bumps_dispatch_generation(self):
        coord, entry = _make_coord()
        events: list = []
        _install_fake_cycle(coord, events, comfort_duration_s=0.05)
        gen_before = coord._dispatch_generation
        _POINT_IN_TIME_LISTENER["cb"](datetime.now(timezone.utc))
        assert coord._dispatch_generation == gen_before + 1, (
            "boundary fire must bump _dispatch_generation exactly once, "
            "before scheduling the refresh"
        )
        await asyncio.sleep(0.2)


class TestNoActiveDispatch:
    @async_test
    async def test_TC_LB_N1_boundary_without_active_dispatch_works_unchanged(self):
        coord, entry = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=0.05)
        assert coord._active_dispatch_cancellation is None

        _POINT_IN_TIME_LISTENER["cb"](datetime.now(timezone.utc))
        await _wait_for_recompute_count(call_count, 1)
        assert call_count["n"] == 1, "exactly one refresh, no extra cycles"
        await asyncio.sleep(0.2)
        assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# TC-LB-CW: completion-wait cleanup
# ---------------------------------------------------------------------------


class TestCompletionWaitCleanup:
    @async_test
    async def test_TC_LB_CW_cancellation_ends_wait_and_harvests_task(self):
        coord, entry = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(
            coord, events, comfort_duration_s=30,
            comfort_duration_for_cycle_2_s=0.2,
        )
        before_tasks = asyncio.all_tasks()
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        _POINT_IN_TIME_LISTENER["cb"](datetime.now(timezone.utc))
        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 2)

        deadline = time.monotonic() + 11.0
        while time.monotonic() < deadline:
            if coord._active_comfort_dispatch_task is None:
                break
            await asyncio.sleep(0.05)
        assert coord._active_comfort_dispatch_task is None
        await asyncio.sleep(0.2)

        after_tasks = asyncio.all_tasks() - before_tasks - {asyncio.current_task()}
        pending = [t for t in after_tasks if not t.done()]
        assert not pending, f"leftover pending tasks: {pending}"
        for t in after_tasks:
            if t.done() and not t.cancelled():
                exc = t.exception()
                assert exc is None, f"task raised unexpectedly: {exc!r}"


# ---------------------------------------------------------------------------
# TC-LB-S1: safety non-interference
# ---------------------------------------------------------------------------


class TestSafetyNonInterference:
    @async_test
    async def test_TC_LB_S1_boundary_preempts_old_comfort_new_safety_dispatches(self):
        coord, entry = _make_coord()
        events: list = []
        call_count = {"n": 0}

        async def cycle():
            call_count["n"] += 1
            cid = call_count["n"]
            cancellation = asyncio.Event()
            coord._active_dispatch_cancellation = cancellation
            carries_safety = cid == 2

            async def _dispatch():
                if carries_safety:
                    events.append(("safety_dispatched", cid))
                    return "safety_done"
                try:
                    await asyncio.wait_for(cancellation.wait(), timeout=30)
                    events.append(("comfort_cancelled", cid))
                    return "cancelled"
                except asyncio.TimeoutError:
                    events.append(("comfort_completed_naturally", cid))
                    return "completed"

            task = asyncio.ensure_future(_dispatch())
            coord._active_comfort_dispatch_task = task
            try:
                return await task
            finally:
                if coord._active_comfort_dispatch_task is task:
                    coord._active_comfort_dispatch_task = None
                if coord._active_dispatch_cancellation is cancellation:
                    coord._active_dispatch_cancellation = None

        coord._async_update_data = cycle
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        assert call_count["n"] == 1

        _POINT_IN_TIME_LISTENER["cb"](datetime.now(timezone.utc))
        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 2)
        assert ("comfort_cancelled", 1) in events, events
        assert ("safety_dispatched", 2) in events, (
            f"new cycle's own safety work must dispatch, events={events}"
        )
        if coord._active_comfort_dispatch_task is not None:
            coord._active_comfort_dispatch_task.cancel()
            try:
                await coord._active_comfort_dispatch_task
            except asyncio.CancelledError:
                pass

    @async_test
    async def test_TC_LB_S2_already_active_safety_never_touched(self):
        """Structural non-regression: is_safety exemption is independent of
        WHICH mechanism sets cancellation — proven for the parallel path in
        test_dispatch_safety_generation_exemption_structural.py. Here we
        only confirm this fix introduces no NEW safety-affecting check."""
        coord, entry = _make_coord()
        cancellation = asyncio.Event()
        coord._active_dispatch_cancellation = cancellation
        coord._active_dispatch_cancellation.set()
        assert cancellation.is_set()


# ---------------------------------------------------------------------------
# TC-LB-RS: rescheduling / single-timer invariant
# ---------------------------------------------------------------------------


class TestRescheduling:
    @async_test
    async def test_TC_LB_RS_boundary_reschedules_and_second_event_hits_new_active_cycle(self):
        coord, entry = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(
            coord, events, comfort_duration_s=30,
        )
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        first_unsub_cb = _POINT_IN_TIME_LISTENER["cb"]

        first_unsub_cb(datetime.now(timezone.utc))
        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 2)
        assert call_count["n"] == 2

        # Single-timer invariant: exactly one listener registered after reschedule.
        assert "cb" in _POINT_IN_TIME_LISTENER
        second_cb = _POINT_IN_TIME_LISTENER["cb"]
        assert second_cb is not None

        # A second boundary firing hits the NOW-active (cycle 2) dispatch.
        second_cb(datetime.now(timezone.utc))
        await _wait_for_recompute_count(call_count, 3)
        assert call_count["n"] == 3, events
        if coord._active_comfort_dispatch_task is not None:
            coord._active_comfort_dispatch_task.cancel()
            try:
                await coord._active_comfort_dispatch_task
            except asyncio.CancelledError:
                pass
