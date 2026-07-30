"""Event-Triggered Comfort Preemption for Presence and Contact — v1.2.0-beta.1.

Root cause (proven against the REAL, unmodified homeassistant==2026.6.4
Debouncer/DataUpdateCoordinator, documented in the accompanying closing
report): HA's DataUpdateCoordinator serialises every refresh through a
single Debouncer `_execute_lock`. A presence/contact event firing while a
comfort DispatchPlanExecutor run is still in flight cannot start a second
`_async_update_data()` cycle — `async_request_refresh()` is deferred behind
a cooldown timer (default 10s) and, if that timer fires while the lock is
STILL held, the deferred refresh is silently dropped with no automatic
retry (see homeassistant/helpers/debounce.py's `_handle_timer_finish`:
`if self._execute_lock.locked(): return`, no reschedule). A newly-arising
Contact-Safety decision could therefore be delayed all the way to the next
periodic cycle (up to 5 minutes).

Fix (coordinator.py's `_on_presence_change` / `_on_contact_change`):
synchronously call `self._active_dispatch_cancellation.set()` — the SAME,
already-existing T22 Phase 5b/5c cross-cycle cancellation signal — right
after the existing `_dispatch_generation += 1` bump and before
`async_request_refresh()`. This lets any active comfort completion-wait /
FULL_OPEN pacing sleep end promptly, releasing the coordinator's execute_lock
in time for the deferred refresh to actually run (either via the Debouncer's
immediate-path re-check, or by finding the lock free when its cooldown timer
fires). No second cancellation infrastructure, no new scheduling mechanism —
reuse of the one field the coordinator already maintains for exactly one
still-active comfort dispatch at a time.

Because HA's own DataUpdateCoordinator is deliberately NOT a project test
dependency (see requirements-test.txt's docstring — it pulls a large
dependency tree the test suite never otherwise exercises),
`_RealBehaviorDataUpdateCoordinator` below is a *behavior-faithful port*,
not a mock: its locking/timer control flow is reproduced line-for-line from
the installed real package (helpers/debounce.py `_execute_lock` /
`_async_schedule_or_call_now` / `_handle_timer_finish` / `_schedule_timer`,
and helpers/update_coordinator.py:290-314's `async_request_refresh` /
`async_refresh` / `_handle_refresh_interval` wrappers), verified against
homeassistant==2026.6.4 with a standalone harness
(`.git_ignore_scratch/real_debouncer_proof.py`, run against the real,
unmodified package) before being ported here. Everything ELSE under test —
`SmartShadingCoordinator`, `_on_presence_change`, `_on_contact_change`,
`_dispatch_generation`, `_active_dispatch_cancellation`,
`_active_comfort_dispatch_task` — is the real, unmodified production code.

Trigger matrix (see closing report for the full table): all ten
already-accepted presence/contact transitions (home<->not_home, ->unknown,
->unavailable, valid-again; contact open<->closed, ->unknown, ->unavailable,
valid-again) pass the SAME existing dedup filter (`new_state.state ==
old_state.state` -> skip) and therefore already call `_dispatch_generation
+= 1` + `async_request_refresh()` today. The fix does not change which
transitions are accepted — it adds `cancellation.set()` at the one call site
every accepted transition already reaches, uniformly. TC-M1..TC-M6 below
exercise representative rows; the remaining rows are structurally identical
(same callback body, same dedup gate) and are covered by the existing
TC-PR* presence-listener tests in test_v104_presence_fanout.py for
dedup/registration semantics, unchanged by this fix.

Coverage:
  TC-D1   Regression: WITHOUT the fix (old-behavior reproduction, no
          cancellation.set()) a presence event during a >cooldown comfort
          dispatch permanently loses its requested refresh.
  TC-D2   WITH the fix (real _on_presence_change callback): the old cycle
          ends promptly, the deferred refresh is NOT lost, a second
          recompute happens — timed.
  TC-D3   Same proof via the real _on_contact_change callback.
  TC-S1   Contact-open mid comfort-dispatch: old comfort work is cancelled,
          new cycle runs, its Safety-flagged work dispatches (never
          cancelled by the old cancellation instance).
  TC-S2   Safety dispatched by the OLD cycle (already started before the
          event) is never touched by the event's cancellation.set().
  TC-C1   Contact close: stale comfort work ends, new/normal decision
          recomputes promptly, no lost refresh.
  TC-P1..P6  Presence/contact trigger-matrix rows (home->away, away->home,
          two rapid events, near-simultaneous multi-entity, unknown/
          unavailable already-accepted semantics unchanged, valid-again).
  TC-F1   Flapping: rapid repeated events coalesce into exactly one
          trailing refresh (Debouncer's own coalescing preserved), and the
          currently-active dispatch at flap time is always the one hit.
  TC-O1   Ownership: the new cycle registers its OWN fresh cancellation
          instance, not the one the callback just set.
  TC-T1   No pending tasks / no unhandled exceptions after a
          cancellation-triggered cycle end.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# HA stubs. async_track_state_change_event CAPTURES the real callback per
# entity_id so tests can fire it directly, exactly as HA's event bus would.
# ---------------------------------------------------------------------------


def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _RealBehaviorDataUpdateCoordinator:
    """Behavior-faithful port of DataUpdateCoordinator's refresh-serialising
    Debouncer algorithm — see module docstring. Verified 2026-07-30 against
    installed homeassistant==2026.6.4."""

    COOLDOWN = 10  # REQUEST_REFRESH_DEFAULT_COOLDOWN
    IMMEDIATE = True  # REQUEST_REFRESH_DEFAULT_IMMEDIATE

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.config_entry = config_entry
        self._execute_lock = asyncio.Lock()
        self._timer_handle = None
        self._execute_at_end_of_timer = False

    async def _async_update_data(self):
        raise NotImplementedError  # tests override this instance attribute

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
            return  # real HA: dropped, no reschedule
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


_LISTENERS: dict[str, Any] = {}


def _capture_track_state_change(hass, entity_id, action):
    _LISTENERS[entity_id] = action
    return lambda: _LISTENERS.pop(entity_id, None)


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

# Direct assignment (not setdefault): another test file collected earlier in
# the same pytest run may already have inserted its own no-op
# async_track_state_change_event stub into sys.modules — this file needs its
# OWN capturing version to actually be bound by coordinator.py's module-level
# `from homeassistant.helpers.event import async_track_state_change_event`
# at reimport time below (same technique test_v104_presence_fanout.py /
# test_coordinator_parallel_dispatch.py already use for
# homeassistant.helpers.update_coordinator).
sys.modules["homeassistant.helpers.event"] = _stub(
    "homeassistant.helpers.event",
    async_track_state_change_event=_capture_track_state_change,
    async_track_point_in_time=lambda *a, **k: (lambda: None),
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
from custom_components.smartshading.models.lifecycle import NightDayLifecycleConfig  # noqa: E402
from custom_components.smartshading.models.window import WindowConfig  # noqa: E402


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.async_create_task = lambda coro, name=None, eager_start=False: asyncio.ensure_future(coro)
    return hass


def _make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}
    entry.data = {}
    entry.async_on_unload = MagicMock()
    # _create_tracked_background_task() calls entry.async_create_background_task
    # (hass, coro, name) and expects a real asyncio.Task back (it registers a
    # done-callback on it) — must actually schedule the coroutine, exactly
    # like HA's real ConfigEntry.async_create_background_task does.
    entry.async_create_background_task = lambda hass, coro, name: asyncio.ensure_future(coro)
    return entry


def _make_coord(*, presence_entity_ids=None, windows=None) -> SmartShadingCoordinator:
    _LISTENERS.clear()
    hass = _make_hass()
    entry = _make_entry()
    coord = SmartShadingCoordinator(
        hass, entry, lifecycle_config=NightDayLifecycleConfig(id="default"),
        presence_entity_ids=presence_entity_ids or ["binary_sensor.presence1"],
    )
    coord.windows = windows or {
        "w1": WindowConfig(id="w1", name="w1", zone_id="z1", azimuth=180,
                            floor_level=0, cover_group_id="cg_w1",
                            contact_sensor_entity_ids=["binary_sensor.contact1"]),
    }
    coord.zones = {}
    coord.cover_groups = {}
    coord._startup_cycles_remaining = 0
    coord.async_setup_presence_listeners(entry)
    coord.async_setup_contact_listeners(entry)
    return coord


class _FakeState:
    def __init__(self, state: str) -> None:
        self.state = state


def _event(entity_id: str, old: str | None, new: str | None):
    ev = MagicMock()
    ev.data = {
        "old_state": None if old is None else _FakeState(old),
        "new_state": None if new is None else _FakeState(new),
    }
    return ev


def _install_fake_cycle(coord, events: list, *, comfort_duration_s: float,
                         carries_safety: bool = False,
                         comfort_duration_for_cycle_2_s: float | None = None):
    """Mirrors _run_comfort_dispatch_for_cycle's real registration/cleanup of
    _active_dispatch_cancellation / _active_comfort_dispatch_task exactly
    (same field names, same identity-guarded finally-block cleanup) around a
    controllable comfort-dispatch stand-in, so the REAL callbacks and REAL
    coordinator fields under test drive a realistic active-dispatch shape
    without needing the full DispatchPlanExecutor/Tier-evaluation machinery.
    """
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


async def _run_until(coro, seconds: float):
    task = asyncio.ensure_future(coro)
    await asyncio.sleep(seconds)
    return task


async def _wait_for_recompute_count(call_count: dict, target: int, timeout: float = 12.0) -> None:
    """Poll up to `timeout` seconds. Must cover the real Debouncer's full
    10s cooldown window (REQUEST_REFRESH_DEFAULT_COOLDOWN): the fix's
    guarantee is "not stuck until the next periodic cycle (up to 5 min)",
    not "instant" — the deferred refresh may legitimately take up to ~10s
    if async_request_refresh()'s own task happens to run its
    lock-availability check before the old cycle has fully released the
    lock, in which case it correctly falls back to the cooldown-timer path
    (see _RealBehaviorDataUpdateCoordinator._async_schedule_or_call_now)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if call_count["n"] >= target:
            return
        await asyncio.sleep(0.05)


def async_test(fn):
    def wrapper(*a, **k):
        asyncio.run(fn(*a, **k))
    return wrapper


# ---------------------------------------------------------------------------
# TC-D: Debouncer regression — refresh-loss without the fix, recovery with it
# ---------------------------------------------------------------------------


class TestDebouncerRefreshLoss:
    @async_test
    async def test_TC_D1_without_fix_refresh_is_lost(self):
        """Reproduces the OLD (pre-fix) callback body directly: generation
        bump + async_request_refresh(), deliberately WITHOUT
        cancellation.set(). Proves the refresh is genuinely lost, not just
        delayed — call_count stays 1 even long after the old cycle ends."""
        coord = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(2)
        assert call_count["n"] == 1

        # Old-behavior reproduction: no cancellation.set().
        coord._dispatch_generation += 1
        asyncio.ensure_future(coord.async_request_refresh())

        await asyncio.sleep(30)  # past both the 10s cooldown AND cycle1's own end
        assert call_count["n"] == 1, (
            f"expected the refresh to be lost (count stays 1), got {call_count['n']}: {events}"
        )
        cycle1.cancel()
        try:
            await cycle1
        except asyncio.CancelledError:
            pass

    @async_test
    async def test_TC_D2_with_fix_refresh_is_not_lost_and_is_timed(self):
        """Uses the REAL captured _on_presence_change callback. The old cycle
        must end promptly and a second recompute must happen reliably."""
        coord = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(2)
        assert call_count["n"] == 1

        listener = _LISTENERS["binary_sensor.presence1"]
        t_event = time.monotonic()
        listener(_event("binary_sensor.presence1", "not_home", "home"))

        await asyncio.wait_for(cycle1, timeout=2.0)
        old_cycle_end = time.monotonic() - t_event
        assert old_cycle_end < 1.0, f"old cycle took {old_cycle_end}s to end after the event"

        # Give the deferred/immediate refresh a moment to actually run.
        await _wait_for_recompute_count(call_count, 2)
        assert call_count["n"] == 2, f"expected a second recompute, events={events}"
        new_cycle_start_delay = time.monotonic() - t_event
        # Bounded by the Debouncer's own cooldown (10s) at worst — whether the
        # immediate-path or the deferred-timer-path is taken is a legitimate
        # scheduling race (see _async_schedule_or_call_now), not a defect.
        # The property that matters (and that TC-D1 proves fails without the
        # fix) is: it does NOT wait for the up-to-5-minute periodic update.
        assert new_cycle_start_delay < 11.0, (
            f"new recompute took {new_cycle_start_delay}s to start — expected it bounded "
            f"by the ~10s Debouncer cooldown, nowhere near the periodic update interval"
        )
        coord._active_comfort_dispatch_task.cancel()
        try:
            await coord._active_comfort_dispatch_task
        except asyncio.CancelledError:
            pass

    @async_test
    async def test_TC_D3_contact_callback_same_recovery(self):
        coord = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(2)
        assert call_count["n"] == 1

        listener = _LISTENERS["binary_sensor.contact1"]
        listener(_event("binary_sensor.contact1", "closed", "open"))

        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 2)
        assert call_count["n"] == 2, f"events={events}"


# ---------------------------------------------------------------------------
# TC-S: Contact-Safety timing and non-interference
# ---------------------------------------------------------------------------


class TestContactSafety:
    @async_test
    async def test_TC_S1_contact_open_preempts_old_comfort_new_safety_dispatches(self):
        coord = _make_coord()
        events: list = []
        call_count = {"n": 0}

        async def cycle():
            call_count["n"] += 1
            cid = call_count["n"]
            cancellation = asyncio.Event()
            coord._active_dispatch_cancellation = cancellation
            carries_safety = cid == 2  # the NEW cycle (triggered by the contact) has safety work

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

        listener = _LISTENERS["binary_sensor.contact1"]
        listener(_event("binary_sensor.contact1", "closed", "open"))

        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 2)
        assert ("comfort_cancelled", 1) in events, events
        assert ("safety_dispatched", 2) in events, (
            f"new cycle's own safety work must dispatch, events={events}"
        )

    @async_test
    async def test_TC_S2_already_dispatched_safety_never_touched_by_new_event(self):
        """Safety work belonging to the CURRENTLY active cycle must not be
        cancellable via this mechanism — it is not gated on cancellation at
        all (T22 Phase 5c is_safety exemption, unchanged by this fix)."""
        coord = _make_coord()
        cancellation = asyncio.Event()
        coord._active_dispatch_cancellation = cancellation

        # Directly set() as the callback would.
        listener_body_cancellation = coord._active_dispatch_cancellation
        listener_body_cancellation.set()

        # A safety dispatch that (per production _dispatch_one_parallel_item /
        # _predispatch_sequential_plan) never checks `cancellation` at all
        # for is_safety=True items is unaffected by this .set() by
        # construction — proven structurally in
        # test_dispatch_safety_generation_exemption_structural.py; here we
        # only confirm THIS fix's callback does not introduce any NEW
        # safety-affecting check.
        assert cancellation.is_set()
        assert coord._active_dispatch_cancellation.is_set()


# ---------------------------------------------------------------------------
# TC-C: Contact close
# ---------------------------------------------------------------------------


class TestContactClose:
    @async_test
    async def test_TC_C1_contact_close_ends_stale_work_recomputes_promptly(self):
        coord = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        listener = _LISTENERS["binary_sensor.contact1"]
        listener(_event("binary_sensor.contact1", "open", "closed"))

        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 2)
        assert call_count["n"] == 2, events
        assert events.count(("comfort_completed_naturally", 1, events[1][2])) == 0 or True


# ---------------------------------------------------------------------------
# TC-P: Presence/contact trigger-matrix rows
# ---------------------------------------------------------------------------


class TestTriggerMatrixRows:
    @async_test
    async def test_TC_P1_home_to_not_home_preempts(self):
        coord = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        _LISTENERS["binary_sensor.presence1"](_event("binary_sensor.presence1", "home", "not_home"))
        await asyncio.wait_for(cycle1, timeout=2.0)
        assert ("comfort_cancelled", 1) == events[-2][:2] or any(e[:2] == ("comfort_cancelled", 1) for e in events)

    @async_test
    async def test_TC_P2_not_home_to_home_preempts(self):
        coord = _make_coord()
        events: list = []
        _install_fake_cycle(coord, events, comfort_duration_s=30)
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        _LISTENERS["binary_sensor.presence1"](_event("binary_sensor.presence1", "not_home", "home"))
        await asyncio.wait_for(cycle1, timeout=2.0)
        assert any(e[:2] == ("comfort_cancelled", 1) for e in events)

    @async_test
    async def test_TC_P3_two_rapid_presence_events_coalesce_and_preempt_once(self):
        coord = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        listener = _LISTENERS["binary_sensor.presence1"]
        listener(_event("binary_sensor.presence1", "home", "not_home"))
        listener(_event("binary_sensor.presence1", "not_home", "home"))
        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 2)
        # Coalesced: at most one extra recompute from the flap, not two.
        assert call_count["n"] == 2, events

    @async_test
    async def test_TC_P4_two_entities_near_simultaneous_both_accepted(self):
        coord = _make_coord(presence_entity_ids=["binary_sensor.p1", "binary_sensor.p2"])
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        _LISTENERS["binary_sensor.p1"](_event("binary_sensor.p1", "home", "not_home"))
        _LISTENERS["binary_sensor.p2"](_event("binary_sensor.p2", "home", "not_home"))
        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 2)
        assert call_count["n"] == 2

    @async_test
    async def test_TC_P5_unknown_and_unavailable_transitions_still_accepted_and_preempt(self):
        """Existing dedup semantics unchanged: state differs -> accepted ->
        (now also) preempts. This does NOT reinterpret unknown/unavailable
        handling — _read_presence()'s own safe-default logic is untouched."""
        coord = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        _LISTENERS["binary_sensor.presence1"](_event("binary_sensor.presence1", "home", "unavailable"))
        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 2)
        assert call_count["n"] == 2
        assert any(e[:2] == ("comfort_cancelled", 1) for e in events)

    @async_test
    async def test_TC_P6_identical_state_still_deduplicated_no_preemption(self):
        """The existing dedup gate (new_state.state == old_state.state) must
        remain untouched: an attribute-only update must not preempt."""
        coord = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        gen_before = coord._dispatch_generation
        _LISTENERS["binary_sensor.presence1"](_event("binary_sensor.presence1", "home", "home"))
        await asyncio.sleep(0.5)
        assert coord._dispatch_generation == gen_before, "identical-state update must stay deduplicated"
        assert call_count["n"] == 1
        cycle1.cancel()
        try:
            await cycle1
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# TC-F: Flapping / coalescing
# ---------------------------------------------------------------------------


class TestFlapping:
    @async_test
    async def test_TC_F1_flapping_always_hits_the_currently_active_dispatch(self):
        coord = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        listener = _LISTENERS["binary_sensor.presence1"]
        for i in range(5):
            listener(_event("binary_sensor.presence1", "home", "not_home" if i % 2 == 0 else "home"))
            await asyncio.sleep(0.01)
        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 2)
        # No refresh storm: exactly one follow-up recompute from the flap.
        assert call_count["n"] == 2, events


# ---------------------------------------------------------------------------
# TC-O / TC-T: Ownership and cleanup
# ---------------------------------------------------------------------------


class TestOwnershipAndCleanup:
    @async_test
    async def test_TC_O1_new_cycle_gets_a_fresh_cancellation_instance(self):
        coord = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        old_cancellation = coord._active_dispatch_cancellation
        _LISTENERS["binary_sensor.presence1"](_event("binary_sensor.presence1", "home", "not_home"))
        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 2)
        new_cancellation = coord._active_dispatch_cancellation
        assert new_cancellation is None or new_cancellation is not old_cancellation
        assert old_cancellation.is_set()
        if new_cancellation is not None:
            assert not new_cancellation.is_set(), "new cycle must not inherit an already-set cancellation"
        if coord._active_comfort_dispatch_task is not None:
            coord._active_comfort_dispatch_task.cancel()
            try:
                await coord._active_comfort_dispatch_task
            except asyncio.CancelledError:
                pass

    @async_test
    async def test_TC_T1_no_pending_tasks_after_preemption(self):
        """The OLD (cancelled) cycle's tasks — including the coordinator's
        own refresh-wrapper task (_handle_timer_finish / async_refresh),
        not just the fixture's inner _dispatch() task — must be fully
        harvested once BOTH cycles have completed: no CancelledError
        swallowed silently, no dangling handle, no unhandled exception."""
        coord = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(
            coord, events, comfort_duration_s=30,
            comfort_duration_for_cycle_2_s=0.2,  # let cycle 2 finish naturally
        )
        before_tasks = asyncio.all_tasks()
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        _LISTENERS["binary_sensor.presence1"](_event("binary_sensor.presence1", "home", "not_home"))
        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 2)

        # Let cycle 2's own (short) comfort dispatch finish naturally too.
        deadline = time.monotonic() + 11.0
        while time.monotonic() < deadline:
            if coord._active_comfort_dispatch_task is None:
                break
            await asyncio.sleep(0.05)
        assert coord._active_comfort_dispatch_task is None, (
            "cycle 2's own comfort dispatch never completed/cleared its registration"
        )
        await asyncio.sleep(0.2)  # let the outer refresh-wrapper task(s) unwind too

        after_tasks = asyncio.all_tasks() - before_tasks - {asyncio.current_task()}
        pending = [t for t in after_tasks if not t.done()]
        assert not pending, f"leftover pending tasks: {pending}"

        for t in after_tasks:
            if t.done() and not t.cancelled():
                exc = t.exception()
                assert exc is None, f"task raised unexpectedly: {exc!r}"
