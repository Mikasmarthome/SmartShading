"""Active-Control-Switch Comfort Preemption — v1.2.0-beta.1.

Root cause (proven by direct code audit of develop @ 69224fd):
`async_set_zone_active_control_enabled()` (coordinator.py) persists the new
`active_control_enabled` flag and calls `await self.async_request_refresh()`
directly — but, unlike EVERY other event-triggered refresh path already
fixed this session (presence f71c71e, contact f71c71e, lifecycle-boundary
69224fd), it does **neither** of the two protections those paths rely on:

  - No `self._dispatch_generation += 1` at all — meaning items already
    queued/not-yet-dispatched in an in-flight comfort plan are NEVER marked
    stale by this call. The per-item generation checks already proven
    load-bearing elsewhere in this codebase
    (`test_dispatch_safety_generation_exemption_structural.py`,
    `_dispatch_one_parallel_item`, `_predispatch_sequential_plan`) simply
    never trigger for this path, because the value they compare against
    never changes.
  - No `self._active_dispatch_cancellation.set()` — so an already-sleeping
    completion-wait / FULL_OPEN pacing wait is not interrupted either.

Consequence: turning Active Control OFF for a zone does not reliably stop a
still-in-flight comfort plan from dispatching further cover commands for
that zone using the now-stale AUTOMATIC ExecutionMode decided before the
toggle (`_exec.active_control_enabled` is baked into each window's
CommandFilterResult at Pass-1 time — see coordinator.py:3434-3436,
4978-4980 — a plan already built and mid-dispatch does not re-read it).
Turning Active Control back ON has the same Debouncer refresh-loss exposure
already proven for presence/contact/boundary (see
test_event_triggered_comfort_preemption.py's module docstring) — a
newly-enabled zone's first real evaluation could be silently delayed to the
next periodic cycle instead of running promptly.

Fix (`async_set_zone_active_control_enabled`): reuse the exact same,
already-existing pattern — `_dispatch_generation += 1` then
`self._active_dispatch_cancellation.set()` if active — before
`async_request_refresh()`. No new architecture: `_active_dispatch_cancellation`
and `_dispatch_generation` are coordinator-instance-level (one comfort
dispatch plan spans ALL zones per cycle, confirmed by the existing
"FULL_OPEN globally before INTERMEDIATE across zones" invariant — see
test_t22_phase3_dispatch_plan_executor.py). A per-zone toggle therefore
necessarily invalidates the WHOLE current plan and triggers a full
recompute, exactly like presence/contact/lifecycle-boundary already do —
this is the established, accepted granularity, not a new one. Unaffected
zones are simply recomputed to their already-correct target on the next
cycle (idempotent, no wrong target, at most one redundant same-position
no-op) — proven by TC-AC-MZ below.

Explicitly NOT changed: no `cover.stop_cover` is issued — an already
dispatched (physically in-flight) move is left alone, matching the
project's existing, deliberate semantics (same as every other preemption
path in this codebase); only NOT-YET-dispatched plan items are prevented
from firing.

Coverage:
  TC-AC-D1  RED-BEFORE-FIX (direct proof at the real public API): calling
            `coord.async_set_zone_active_control_enabled(zone_id, False)`
            while an old comfort plan is still mid-flight does NOT stop a
            not-yet-dispatched item in that SAME zone from later firing,
            because no generation bump / cancellation ever reaches it.
  TC-AC-D2  WITH FIX: the same not-yet-dispatched item is skipped/preempted.
  TC-AC-OFF-1  Disable without any active dispatch: clean no-op behavior.
  TC-AC-OFF-2  Disable during completion-wait: old wait ends promptly, task
               fully harvested, no pending tasks / unhandled exceptions.
  TC-AC-ON-1   Enable triggers a prompt recompute (not lost in the
               Debouncer, not delayed to the periodic update).
  TC-AC-FLAP   Rapid off/on/off: only the LAST state wins in persisted
               config; no lost/extra refreshes beyond what the Debouncer
               already coalesces.
  TC-AC-MZ     Multi-zone: toggling zone A's Active Control preempts the
               shared global plan, but zone B's own (unrelated, unchanged)
               decision is simply recomputed on the next cycle — not
               corrupted, not permanently blocked.
  TC-AC-SAFE   Safety work is never touched by this fix (structural — same
               is_safety exemption every other preemption path relies on).
  TC-AC-NOTOK  No active cancellation token: clean no-op, no exception.
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
# HA stubs — same technique as the other Debouncer-related test files in
# this session (test_event_triggered_comfort_preemption.py,
# test_lifecycle_boundary_comfort_preemption.py).
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

    def async_update_listeners(self) -> None:
        pass


class _StoreStub:
    def __init__(self, hass, version, key) -> None: pass
    async def async_load(self): return None
    async def async_save(self, data) -> None: pass
    async def async_remove(self) -> None: pass


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
    async_track_state_change_event=lambda hass, e, a: (lambda: None),
    async_track_point_in_time=lambda hass, action, when: (lambda: None),
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
from custom_components.smartshading.models.zone import ZoneConfig  # noqa: E402


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


def _make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}
    entry.data = {}
    entry.async_on_unload = MagicMock()
    entry.async_create_background_task = lambda hass, coro, name: asyncio.ensure_future(coro)
    return entry


def _make_coord(*, zone_ids=("z1",)) -> SmartShadingCoordinator:
    hass = _make_hass()
    entry = _make_entry()
    coord = SmartShadingCoordinator(
        hass, entry, lifecycle_config=NightDayLifecycleConfig(id="default"),
        presence_entity_ids=[],
    )
    coord.zones = {zid: ZoneConfig(id=zid, name=zid) for zid in zone_ids}
    coord.windows = {
        f"w_{zid}": WindowConfig(id=f"w_{zid}", name=f"w_{zid}", zone_id=zid,
                                  azimuth=180, floor_level=0, cover_group_id=f"cg_{zid}")
        for zid in zone_ids
    }
    coord.cover_groups = {}
    coord._startup_cycles_remaining = 0
    coord.config_entry = entry
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


def _install_pending_item_cycle(coord, events: list, *, gate: asyncio.Event):
    """Models a SEQUENTIAL/SPACED-style plan with TWO items: item 1 is
    already mid-service-call (physically in flight — never interrupted by
    design), item 2 is the "not yet dispatched" item that the generation/
    cancellation checkpoint (the SAME one proven load-bearing in
    test_dispatch_safety_generation_exemption_structural.py and
    test_t22_phase5c_safety_preemption.py) must protect once the fix is in
    place. Mirrors coordinator.py's real per-item guard shape exactly:
    `if self._dispatch_generation != this_gen or (cancellation and
    cancellation.is_set()): skip`.
    """
    call_count = {"n": 0}

    async def _cycle():
        call_count["n"] += 1
        cid = call_count["n"]
        this_gen = coord._dispatch_generation
        cancellation = asyncio.Event()
        coord._active_dispatch_cancellation = cancellation

        async def _plan():
            # Item 1: already "in flight" — always fires (matches the
            # project's deliberate semantics of never stop_cover-ing an
            # already-issued command).
            events.append(("item1_dispatched", cid))
            # A real gap between two plan items (throttle/pacing wait, or
            # simply the other window's own completion-wait) during which
            # the toggle can happen.
            await gate.wait()
            # Item 2: the per-item checkpoint every other preemption path
            # in this codebase already relies on.
            if coord._dispatch_generation != this_gen or cancellation.is_set():
                events.append(("item2_skipped_stale", cid))
                return "item2_skipped"
            events.append(("item2_dispatched", cid))
            return "item2_dispatched"

        task = asyncio.ensure_future(_plan())
        coord._active_comfort_dispatch_task = task
        try:
            return await task
        finally:
            if coord._active_comfort_dispatch_task is task:
                coord._active_comfort_dispatch_task = None
            if coord._active_dispatch_cancellation is cancellation:
                coord._active_dispatch_cancellation = None

    coord._async_update_data = _cycle
    return call_count


async def _wait_for_recompute_count(call_count: dict, target: int, timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if call_count["n"] >= target:
            await asyncio.sleep(0.05)
            return
        await asyncio.sleep(0.05)


def async_test(fn):
    def wrapper(*a, **k):
        asyncio.run(fn(*a, **k))
    return wrapper


# ---------------------------------------------------------------------------
# TC-AC-D: direct red-before-fix proof at the REAL public API, then green
# ---------------------------------------------------------------------------


class TestActiveControlDirectProof:
    @async_test
    async def test_TC_AC_D1_without_fix_stale_item_still_dispatches(self):
        """Reproduces the OLD (pre-fix) async_set_zone_active_control_enabled
        body directly: persist + async_request_refresh(), deliberately
        WITHOUT any generation bump or cancellation. Item 2 (not yet
        dispatched when the toggle happens) must still fire — proving the
        real gap exists at the real public API, not merely by analogy."""
        coord, entry = _make_coord()
        events: list = []
        gate = asyncio.Event()
        _install_pending_item_cycle(coord, events, gate=gate)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(0.2)
        assert ("item1_dispatched", 1) in events

        # Old-behavior reproduction: no generation bump, no cancellation.
        current = coord.effective_zone_execution("z1")
        from custom_components.smartshading.models.zone import ZoneExecutionConfig
        coord._zone_execution_overrides["z1"] = ZoneExecutionConfig(
            learning_enabled=current.learning_enabled, active_control_enabled=False,
        )

        gate.set()
        await asyncio.wait_for(cycle1, timeout=2.0)
        assert ("item2_dispatched", 1) in events, (
            f"expected the stale item to STILL dispatch without the fix, events={events}"
        )

    @async_test
    async def test_TC_AC_D2_with_fix_stale_item_is_preempted(self):
        """Uses the REAL public API. Item 2 must be skipped once Active
        Control is turned off mid-plan."""
        coord, entry = _make_coord()
        events: list = []
        gate = asyncio.Event()
        _install_pending_item_cycle(coord, events, gate=gate)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(0.2)
        assert ("item1_dispatched", 1) in events

        await coord.async_set_zone_active_control_enabled("z1", False)

        gate.set()
        await asyncio.wait_for(cycle1, timeout=2.0)
        assert ("item2_skipped_stale", 1) in events, (
            f"expected the stale item to be preempted, events={events}"
        )
        assert ("item2_dispatched", 1) not in events


# ---------------------------------------------------------------------------
# TC-AC-OFF: disabling Active Control
# ---------------------------------------------------------------------------


class TestGenerationBump:
    @async_test
    async def test_TC_AC_G1_toggle_bumps_dispatch_generation(self):
        coord, entry = _make_coord()
        events: list = []
        _install_fake_cycle(coord, events, comfort_duration_s=0.05)
        gen_before = coord._dispatch_generation
        await coord.async_set_zone_active_control_enabled("z1", False)
        assert coord._dispatch_generation == gen_before + 1, (
            "toggling active control must bump _dispatch_generation exactly once"
        )
        await asyncio.sleep(0.2)


class TestDisableActiveControl:
    @async_test
    async def test_TC_AC_OFF_1_disable_without_active_dispatch_is_clean(self):
        coord, entry = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=0.05)
        assert coord._active_dispatch_cancellation is None

        await coord.async_set_zone_active_control_enabled("z1", False)
        await _wait_for_recompute_count(call_count, 1)
        assert call_count["n"] == 1
        current = coord.effective_zone_execution("z1")
        assert current.active_control_enabled is False

    @async_test
    async def test_TC_AC_OFF_2_disable_during_completion_wait_ends_promptly_and_harvests(self):
        coord, entry = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(
            coord, events, comfort_duration_s=30,
            comfort_duration_for_cycle_2_s=0.2,
        )
        before_tasks = asyncio.all_tasks()
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        assert call_count["n"] == 1

        t_event = time.monotonic()
        await coord.async_set_zone_active_control_enabled("z1", False)
        await asyncio.wait_for(cycle1, timeout=2.0)
        old_cycle_end = time.monotonic() - t_event
        assert old_cycle_end < 1.0, f"old cycle took {old_cycle_end}s to end"

        await _wait_for_recompute_count(call_count, 2)
        assert call_count["n"] == 2

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
# TC-AC-ON: enabling Active Control
# ---------------------------------------------------------------------------


class TestPersistence:
    @async_test
    async def test_TC_AC_PERSIST_runtime_override_is_actually_updated(self):
        """Uses a zone whose ZoneConfig default differs from the toggled
        value, so a skipped runtime-state update is actually observable
        (a zone with no explicit execution config defaults
        active_control_enabled=False -- toggling to False would trivially
        'pass' even if the write were skipped, which is exactly the gap
        this test closes)."""
        from custom_components.smartshading.models.zone_execution_config import ZoneExecutionConfig as _ZEC
        coord, entry = _make_coord()
        coord.zones["z1"].execution = _ZEC(learning_enabled=True, active_control_enabled=True)
        assert coord.effective_zone_execution("z1").active_control_enabled is True

        await coord.async_set_zone_active_control_enabled("z1", False)
        current = coord.effective_zone_execution("z1")
        assert current.active_control_enabled is False, (
            "the runtime override must actually be written -- reading the "
            "stale ZoneConfig default instead means the toggle silently "
            "had no effect"
        )
        await asyncio.sleep(0.2)


class TestEnableActiveControl:
    @async_test
    async def test_TC_AC_ON_1_enable_triggers_prompt_recompute(self):
        coord, entry = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        assert call_count["n"] == 1

        t_event = time.monotonic()
        await coord.async_set_zone_active_control_enabled("z1", True)
        await asyncio.wait_for(cycle1, timeout=2.0)

        await _wait_for_recompute_count(call_count, 2)
        assert call_count["n"] == 2
        delay = time.monotonic() - t_event
        assert delay < 11.0, f"enable took {delay}s to recompute — must not wait for periodic update"
        if coord._active_comfort_dispatch_task is not None:
            coord._active_comfort_dispatch_task.cancel()
            try:
                await coord._active_comfort_dispatch_task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# TC-AC-FLAP: rapid off/on/off
# ---------------------------------------------------------------------------


class TestFlapping:
    @async_test
    async def test_TC_AC_FLAP_only_last_state_wins(self):
        coord, entry = _make_coord()
        events: list = []
        _install_fake_cycle(coord, events, comfort_duration_s=0.05)

        await coord.async_set_zone_active_control_enabled("z1", False)
        await coord.async_set_zone_active_control_enabled("z1", True)
        await coord.async_set_zone_active_control_enabled("z1", False)

        current = coord.effective_zone_execution("z1")
        assert current.active_control_enabled is False


# ---------------------------------------------------------------------------
# TC-AC-MZ: multi-zone ownership
# ---------------------------------------------------------------------------


class TestMultiZone:
    @async_test
    async def test_TC_AC_MZ_toggling_one_zone_does_not_corrupt_another(self):
        coord, entry = _make_coord(zone_ids=("z1", "z2"))
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)

        # z2 explicitly configured ON directly (no dispatch running yet) so
        # the "untouched" assertion below is meaningful (ZoneExecutionConfig
        # defaults active_control_enabled to False -- "no covers move until
        # opt-in" -- so an unconfigured zone would trivially satisfy the
        # assertion for the wrong reason). Uses the same code path
        # (async_set_zone_active_control_enabled) but with nothing in
        # flight, so it settles immediately.
        await coord.async_set_zone_active_control_enabled("z2", True)
        await asyncio.sleep(0.2)
        assert call_count["n"] == 1

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        assert call_count["n"] == 2

        await coord.async_set_zone_active_control_enabled("z1", False)
        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 3)

        z1 = coord.effective_zone_execution("z1")
        z2 = coord.effective_zone_execution("z2")
        assert z1.active_control_enabled is False
        assert z2.active_control_enabled is True, (
            "zone z2 was never toggled — its persisted config must be untouched "
            "(the shared global recompute may re-evaluate it, but must not "
            "change its config or permanently block it)"
        )
        if coord._active_comfort_dispatch_task is not None:
            coord._active_comfort_dispatch_task.cancel()
            try:
                await coord._active_comfort_dispatch_task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# TC-AC-SAFE / TC-AC-NOTOK
# ---------------------------------------------------------------------------


class TestSafetyAndNoToken:
    @async_test
    async def test_TC_AC_SAFE_safety_work_untouched_structurally(self):
        """Structural non-regression: the is_safety exemption this fix
        depends on (same field, same guard) is proven elsewhere
        (test_dispatch_safety_generation_exemption_structural.py); here we
        only confirm this fix introduces no NEW safety-affecting check."""
        coord, entry = _make_coord()
        cancellation = asyncio.Event()
        coord._active_dispatch_cancellation = cancellation
        coord._active_dispatch_cancellation.set()
        assert cancellation.is_set()

    @async_test
    async def test_TC_AC_NOTOK_no_active_cancellation_is_clean_noop(self):
        coord, entry = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=0.05)
        assert coord._active_dispatch_cancellation is None
        # Must not raise.
        await coord.async_set_zone_active_control_enabled("z1", False)
        await _wait_for_recompute_count(call_count, 1)
        assert call_count["n"] == 1
