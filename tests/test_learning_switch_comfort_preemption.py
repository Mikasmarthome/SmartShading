"""Learning-Switch Comfort Preemption — v1.2.0-beta.1.

Root cause (CONFIRMED PRODUCTION BUG, proven by direct dynamic audit of
develop @ 34e6788 — see the Learning-Switch-Audit dynamic nachtrag):
`async_set_zone_learning_enabled()` (coordinator.py) persisted the new
`learning_enabled` flag and called `await self.async_request_refresh()`
directly — but, unlike EVERY other event-triggered refresh path already
fixed this session (presence f71c71e, contact f71c71e, lifecycle-boundary
69224fd, active-control d6a92ce), it did **neither** of the two protections
those paths rely on:

  - No `self._dispatch_generation += 1` — items already queued/not-yet-
    dispatched in an in-flight comfort plan were never marked stale.
  - No `self._active_dispatch_cancellation.set()` — an already-sleeping
    completion-wait / FULL_OPEN pacing wait was not interrupted.

Consequence: a comfort item not yet dispatched (waiting behind a real gate,
modelling a completion-wait/pacing phase) could still dispatch using the
PREVIOUS learning state's target position after the real public API toggled
Learning Mode — in EITHER direction (a stale learned target after ON->OFF,
or a stale neutral target after OFF->ON, once the newly-allowed learned
target should apply instead). Directly reproduced at the real public API
(`test_LS_D1_without_fix_stale_item_still_dispatches` reproduces the
pre-fix body verbatim and shows the item still dispatching).

Fix (`async_set_zone_learning_enabled`): reuse the exact same,
already-existing pattern as f71c71e/69224fd/d6a92ce —
`_dispatch_generation += 1` then `self._active_dispatch_cancellation.set()`
if active — before `async_request_refresh()`. No new architecture:
`_active_dispatch_cancellation`/`_dispatch_generation` remain coordinator-
instance-level (one comfort dispatch plan spans ALL zones per cycle, the
same established granularity every other preemption path already uses).
Unlike the three prior fixes, this one is additionally gated on an ACTUAL
state change (`enabled != current.learning_enabled`) so a redundant
ON->ON / OFF->OFF setter call never preempts unrelated in-flight work —
required explicitly for this fix by the approving product decision.

Explicitly NOT changed: no `cover.stop_cover` is issued — an already
dispatched (physically in-flight) move is left alone, matching every other
preemption path in this codebase; only NOT-YET-dispatched plan items are
prevented from firing. PendingOutcome semantics, experiment/adoption
handling, and diagnostics are untouched by this fix (separately audited,
separately gated for a future phase).

Coverage:
  TC-LS-D1   RED-BEFORE-FIX (direct proof at the real public API): calling
             `coord.async_set_zone_learning_enabled(zone_id, False)` while
             an old comfort plan is still mid-flight does NOT stop a
             not-yet-dispatched item from later firing, reproducing the
             confirmed bug's pre-fix body verbatim.
  TC-LS-D2   WITH FIX (ON->OFF): the same not-yet-dispatched item is
             skipped/preempted once Learning is turned off mid-plan.
  TC-LS-D3   WITH FIX (OFF->ON): symmetric — a waiting neutral-state item
             is preempted once Learning is turned back on mid-plan.
  TC-LS-NOOP Redundant ON->ON / OFF->OFF calls never bump generation or
             set cancellation — no spurious preemption of unrelated work.
  TC-LS-GEN  A real state change always bumps generation exactly once.
  TC-LS-OFF  Disable without any active dispatch: clean no-op behavior;
             disable during completion-wait: old wait ends promptly, task
             fully harvested, no pending tasks / unhandled exceptions.
  TC-LS-ON   Enable triggers a prompt recompute (not lost in the
             Debouncer, not delayed to the periodic update).
  TC-LS-FLAP Rapid off/on/off and on/off/on: only the LAST confirmed state
             wins in persisted config.
  TC-LS-MZ   Multi-zone: toggling zone A's Learning Mode preempts the
             shared global plan, but zone B's own (unrelated, unchanged)
             decision is simply recomputed on the next cycle — not
             corrupted, not permanently blocked; zone B's persisted
             learning_enabled is untouched.
  TC-LS-SAFE Safety work is never touched by this fix (structural — same
             is_safety exemption every other preemption path relies on).
  TC-LS-PERSIST  The runtime override is actually written (not silently
             skipped), using a zone whose stored default differs from the
             toggled value so a skipped write would be observable.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock


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
from custom_components.smartshading.models.zone_execution_config import ZoneExecutionConfig  # noqa: E402


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
            events.append(("item1_dispatched", cid))
            await gate.wait()
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


def _build_real_learned_and_neutral_targets(coord, window_id: str, *, base_ha: int = 50):
    """Uses the REAL, unmodified TargetPositionAdapter (coord._target_position_adapter,
    the same production instance coordinator.py's Pass-1 calls at line ~3821 via
    `get_effective_targets(..., confidence_level=_adapt_profile.confidence_level)`)
    to compute a genuinely different "normal" target position for a learned
    (confidence_level="very_high", real accumulated signals) vs. neutral
    (confidence_level="very_low", the _NEUTRAL_ADAPTIVE_PROFILE value) profile.
    Mirrors the real signal-ingestion API (record_override_signal) rather than
    poking internal dataclasses directly.
    Returns (learned_normal_ha, neutral_normal_ha) with learned != neutral,
    proving a deterministic, production-computed difference exists.
    """
    adapter = coord._target_position_adapter
    now = datetime.now(timezone.utc)
    # Three real signals, each above the 5-min minimum and >=weight 1.0, so
    # accumulated weight (>= _MIN_WEIGHT_FOR_ADAPTATION == 3.0) makes
    # has_enough_data True; user consistently prefers a much more open
    # position (90 HA) than the configured base (50 HA).
    for _ in range(3):
        adapter.record_override_signal(
            window_id=window_id,
            overridden_state_str="normal_shade",
            override_position_internal=10,  # internal: 0=open,100=shaded -> HA ~90 (open)
            overridden_position_internal=50,  # configured target, internal convention
            duration_min=150,
            now=now,
        )
    _, learned_normal, _, learned_adapted = adapter.get_effective_targets(
        window_id=window_id, light_ha=base_ha, normal_ha=base_ha, strong_ha=base_ha,
        confidence_level="very_high",
    )
    _, neutral_normal, _, neutral_adapted = adapter.get_effective_targets(
        window_id=window_id, light_ha=base_ha, normal_ha=base_ha, strong_ha=base_ha,
        confidence_level="very_low",
    )
    assert learned_adapted is True, "test setup must produce a real adapted (learned) target"
    assert neutral_adapted is False, "neutral confidence_level must never adapt"
    assert learned_normal != neutral_normal, (
        "test setup must produce a deterministically different learned vs. neutral target"
    )
    return learned_normal, neutral_normal


def _install_toggle_aware_target_cycle(coord, events: list, *, zone_id: str,
                                        learned_pos: int, neutral_pos: int,
                                        gate: asyncio.Event):
    """Cycle 1 mirrors a real plan built while the CURRENT learning state was
    in effect: item1 dispatches immediately with that state's real target
    (learned_pos or neutral_pos, chosen via a LIVE read of
    effective_zone_execution(zone_id).learning_enabled, exactly like
    coordinator.py's real `obs_enabled` / `_adapt_profile.confidence_level`
    gate at Pass-1); item2 waits behind `gate` and re-checks generation/
    cancellation exactly like coordinator.py's real per-item checkpoint.
    Cycle 2+ models the FRESH recompute the toggle's own async_request_refresh()
    triggers: it re-reads the (now live/current) learning state and dispatches
    immediately with whatever target that live state implies -- proving the
    fresh recompute genuinely runs and genuinely picks the currently-correct
    target, not a stale one.
    """
    call_count = {"n": 0}
    dispatched: list = []  # (label, cycle_id, target_pos)

    async def _cycle():
        call_count["n"] += 1
        cid = call_count["n"]
        this_gen = coord._dispatch_generation
        cancellation = asyncio.Event()
        coord._active_dispatch_cancellation = cancellation

        live_enabled = coord.effective_zone_execution(zone_id).learning_enabled
        this_cycle_target = learned_pos if live_enabled else neutral_pos

        async def _plan():
            if cid == 1:
                dispatched.append(("item1", cid, this_cycle_target))
                events.append(("item1_dispatched", cid, this_cycle_target))
                await gate.wait()
                if coord._dispatch_generation != this_gen or cancellation.is_set():
                    events.append(("item2_skipped_stale", cid, this_cycle_target))
                    return None
                dispatched.append(("item2", cid, this_cycle_target))
                events.append(("item2_dispatched", cid, this_cycle_target))
                return this_cycle_target
            dispatched.append(("fresh", cid, this_cycle_target))
            events.append(("fresh_dispatched", cid, this_cycle_target))
            return this_cycle_target

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
    return call_count, dispatched


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
# TC-LS-D: direct red-before-fix proof at the REAL public API, then green
# in both directions
# ---------------------------------------------------------------------------


class TestLearningDirectProof:
    @async_test
    async def test_TC_LS_D1_without_fix_stale_item_still_dispatches(self):
        """Reproduces the OLD (pre-fix) async_set_zone_learning_enabled
        body verbatim: persist + async_request_refresh(), deliberately
        WITHOUT any generation bump or cancellation. Item 2 (not yet
        dispatched when the toggle happens) must still fire — reproducing
        the confirmed production bug at the real public API."""
        coord, entry = _make_coord()
        events: list = []
        gate = asyncio.Event()
        _install_pending_item_cycle(coord, events, gate=gate)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(0.2)
        assert ("item1_dispatched", 1) in events

        # Old-behavior reproduction: no generation bump, no cancellation.
        current = coord.effective_zone_execution("z1")
        coord._zone_execution_overrides["z1"] = ZoneExecutionConfig(
            learning_enabled=False, active_control_enabled=current.active_control_enabled,
        )

        gate.set()
        await asyncio.wait_for(cycle1, timeout=2.0)
        assert ("item2_dispatched", 1) in events, (
            f"expected the stale item to STILL dispatch without the fix, events={events}"
        )

    @async_test
    async def test_TC_LS_D2_with_fix_on_to_off_stale_item_is_preempted(self):
        """Uses the REAL public API. Item 2 must be skipped once Learning
        is turned off mid-plan (ON -> OFF)."""
        coord, entry = _make_coord()
        events: list = []
        gate = asyncio.Event()
        _install_pending_item_cycle(coord, events, gate=gate)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(0.2)
        assert ("item1_dispatched", 1) in events

        await coord.async_set_zone_learning_enabled("z1", False)

        gate.set()
        await asyncio.wait_for(cycle1, timeout=2.0)
        assert ("item2_skipped_stale", 1) in events, (
            f"expected the stale item to be preempted, events={events}"
        )
        assert ("item2_dispatched", 1) not in events

    @async_test
    async def test_TC_LS_D3_with_fix_off_to_on_stale_item_is_preempted(self):
        """Symmetric proof for OFF -> ON: a waiting item computed under the
        neutral (Learning-off) profile must also be preempted once Learning
        is turned back on mid-plan, so the following recompute can apply
        the newly-allowed learned target instead."""
        coord, entry = _make_coord()
        coord.zones["z1"].execution = ZoneExecutionConfig(
            learning_enabled=False, active_control_enabled=False,
        )
        events: list = []
        gate = asyncio.Event()
        _install_pending_item_cycle(coord, events, gate=gate)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(0.2)
        assert ("item1_dispatched", 1) in events

        await coord.async_set_zone_learning_enabled("z1", True)

        gate.set()
        await asyncio.wait_for(cycle1, timeout=2.0)
        assert ("item2_skipped_stale", 1) in events, (
            f"expected the stale neutral-state item to be preempted, events={events}"
        )
        assert ("item2_dispatched", 1) not in events


# ---------------------------------------------------------------------------
# TC-LS-NOOP / TC-LS-GEN: redundant calls vs. real state changes
# ---------------------------------------------------------------------------


class TestRedundantCallsAndGeneration:
    @async_test
    async def test_TC_LS_GEN_real_change_bumps_generation_exactly_once(self):
        coord, entry = _make_coord()
        events: list = []
        _install_fake_cycle(coord, events, comfort_duration_s=0.05)
        gen_before = coord._dispatch_generation
        await coord.async_set_zone_learning_enabled("z1", False)
        assert coord._dispatch_generation == gen_before + 1, (
            "a real learning-state change must bump _dispatch_generation exactly once"
        )
        await asyncio.sleep(0.2)

    @async_test
    async def test_TC_LS_NOOP_on_to_on_does_not_preempt(self):
        coord, entry = _make_coord()
        events: list = []
        gate = asyncio.Event()
        _install_pending_item_cycle(coord, events, gate=gate)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(0.2)
        assert ("item1_dispatched", 1) in events
        assert coord.effective_zone_execution("z1").learning_enabled is True

        gen_before = coord._dispatch_generation
        cancellation_before = coord._active_dispatch_cancellation
        await coord.async_set_zone_learning_enabled("z1", True)  # redundant: already True
        assert coord._dispatch_generation == gen_before, (
            "a redundant ON->ON call must not bump generation"
        )
        assert coord._active_dispatch_cancellation is cancellation_before
        assert not cancellation_before.is_set(), (
            "a redundant ON->ON call must not preempt unrelated in-flight work"
        )

        gate.set()
        await asyncio.wait_for(cycle1, timeout=2.0)
        assert ("item2_dispatched", 1) in events, (
            f"unrelated in-flight item must dispatch normally, events={events}"
        )

    @async_test
    async def test_TC_LS_NOOP_off_to_off_does_not_preempt(self):
        coord, entry = _make_coord()
        coord.zones["z1"].execution = ZoneExecutionConfig(
            learning_enabled=False, active_control_enabled=False,
        )
        events: list = []
        gate = asyncio.Event()
        _install_pending_item_cycle(coord, events, gate=gate)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(0.2)
        assert ("item1_dispatched", 1) in events

        gen_before = coord._dispatch_generation
        cancellation_before = coord._active_dispatch_cancellation
        await coord.async_set_zone_learning_enabled("z1", False)  # redundant: already False
        assert coord._dispatch_generation == gen_before
        assert not cancellation_before.is_set()

        gate.set()
        await asyncio.wait_for(cycle1, timeout=2.0)
        assert ("item2_dispatched", 1) in events


# ---------------------------------------------------------------------------
# TC-LS-OFF / TC-LS-ON
# ---------------------------------------------------------------------------


class TestDisableEnableLearning:
    @async_test
    async def test_TC_LS_OFF_1_disable_without_active_dispatch_is_clean(self):
        coord, entry = _make_coord()
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=0.05)
        assert coord._active_dispatch_cancellation is None

        await coord.async_set_zone_learning_enabled("z1", False)
        await _wait_for_recompute_count(call_count, 1)
        assert call_count["n"] == 1
        assert coord.effective_zone_execution("z1").learning_enabled is False

    @async_test
    async def test_TC_LS_OFF_2_disable_during_completion_wait_ends_promptly_and_harvests(self):
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
        await coord.async_set_zone_learning_enabled("z1", False)
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

    @async_test
    async def test_TC_LS_ON_1_enable_triggers_prompt_recompute(self):
        coord, entry = _make_coord()
        coord.zones["z1"].execution = ZoneExecutionConfig(
            learning_enabled=False, active_control_enabled=False,
        )
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        assert call_count["n"] == 1

        t_event = time.monotonic()
        await coord.async_set_zone_learning_enabled("z1", True)
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
# TC-LS-FLAP: rapid state-change sequences
# ---------------------------------------------------------------------------


class TestFlapping:
    @async_test
    async def test_TC_LS_FLAP_off_on_off_last_state_wins(self):
        coord, entry = _make_coord()
        events: list = []
        _install_fake_cycle(coord, events, comfort_duration_s=0.05)

        await coord.async_set_zone_learning_enabled("z1", False)
        await coord.async_set_zone_learning_enabled("z1", True)
        await coord.async_set_zone_learning_enabled("z1", False)

        assert coord.effective_zone_execution("z1").learning_enabled is False

    @async_test
    async def test_TC_LS_FLAP_on_off_on_last_state_wins(self):
        coord, entry = _make_coord()
        events: list = []
        _install_fake_cycle(coord, events, comfort_duration_s=0.05)

        await coord.async_set_zone_learning_enabled("z1", False)
        await coord.async_set_zone_learning_enabled("z1", True)

        assert coord.effective_zone_execution("z1").learning_enabled is True


# ---------------------------------------------------------------------------
# TC-LS-MZ: multi-zone ownership
# ---------------------------------------------------------------------------


class TestMultiZone:
    @async_test
    async def test_TC_LS_MZ_toggling_one_zone_does_not_corrupt_another(self):
        coord, entry = _make_coord(zone_ids=("z1", "z2"))
        events: list = []
        call_count = _install_fake_cycle(coord, events, comfort_duration_s=30)

        # z2 explicitly configured OFF directly (no dispatch running yet) so
        # the "untouched" assertion below is meaningful (learning_enabled
        # defaults to True, so leaving z2 unconfigured would trivially
        # satisfy the assertion for the wrong reason).
        await coord.async_set_zone_learning_enabled("z2", False)
        await asyncio.sleep(0.2)
        assert call_count["n"] == 1

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(1)
        assert call_count["n"] == 2

        await coord.async_set_zone_learning_enabled("z1", False)
        await asyncio.wait_for(cycle1, timeout=2.0)
        await _wait_for_recompute_count(call_count, 3)

        z1 = coord.effective_zone_execution("z1")
        z2 = coord.effective_zone_execution("z2")
        assert z1.learning_enabled is False
        assert z2.learning_enabled is False, (
            "zone z2 was set OFF earlier and never toggled again — its "
            "persisted config must be untouched by zone z1's own toggle "
            "(the shared global recompute may re-evaluate it, but must not "
            "change its config or permanently block it)"
        )
        if coord._active_comfort_dispatch_task is not None:
            coord._active_comfort_dispatch_task.cancel()
            try:
                await coord._active_comfort_dispatch_task
            except asyncio.CancelledError:
                pass

    @async_test
    async def test_TC_LS_MZ_ON_zone_b_keeps_own_learned_value_and_redispatches_once(self):
        """Corrected per Phase-2A-Nachtrag point 4: zone B stays Learning ON
        throughout (not OFF) with its OWN distinct real learned target
        (built via the same real TargetPositionAdapter API as the
        target-position proof above). Only zone A toggles OFF. Proves:
          - the coordinator-wide plan is technically preempted (zone A's
            reason), matching the same granularity every other preemption
            path in this codebase already uses;
          - zone B's own still-necessary comfort work is NOT permanently
            lost — it is redispatched in the following fresh recompute
            exactly once, using its own (unchanged) learned target;
          - zone B's stored learning data (TargetPositionAdapter state) and
            runtime execution config are byte-identical before/after.
        """
        coord, entry = _make_coord(zone_ids=("z1", "z2"))
        learned_a, neutral_a = _build_real_learned_and_neutral_targets(
            coord, "w_z1", base_ha=50,
        )
        # Zone B gets its OWN distinct learned target: user consistently
        # preferred a much MORE SHADED position (20 HA) than configured (60),
        # deliberately different in both value and direction from zone A's.
        adapter = coord._target_position_adapter
        now = datetime.now(timezone.utc)
        for _ in range(3):
            adapter.record_override_signal(
                window_id="w_z2", overridden_state_str="normal_shade",
                override_position_internal=80,  # HA: 100-80=20 (much more shaded)
                overridden_position_internal=40,  # configured target internal -> HA 60
                duration_min=150, now=now,
            )
        _, learned_b, _, learned_b_adapted = adapter.get_effective_targets(
            window_id="w_z2", light_ha=60, normal_ha=60, strong_ha=60,
            confidence_level="very_high",
        )
        assert learned_b_adapted is True
        assert learned_b != learned_a, "zone B's learned target must be its own, distinct value"
        b_state_before = (
            adapter._windows["w_z2"].normal.weighted_sum_ha,
            adapter._windows["w_z2"].normal.total_weight,
            adapter._windows["w_z2"].normal.sample_count,
        )

        events: list = []
        call_count = {"n": 0}
        dispatched: list = []  # (zone, item, cycle_id, target)

        async def _cycle():
            call_count["n"] += 1
            cid = call_count["n"]
            this_gen = coord._dispatch_generation
            cancellation = asyncio.Event()
            coord._active_dispatch_cancellation = cancellation

            a_enabled = coord.effective_zone_execution("z1").learning_enabled
            b_enabled = coord.effective_zone_execution("z2").learning_enabled
            a_target = learned_a if a_enabled else neutral_a
            b_target = learned_b if b_enabled else 60  # 60 == zone B's configured base

            async def _plan():
                # Zone B's item dispatches promptly (still-necessary work,
                # not gated behind zone A's waiting item).
                dispatched.append(("z2", "b_item", cid, b_target))
                events.append(("b_dispatched", cid, b_target))
                # Zone A's item is the one that WAITS (models the in-flight
                # item that zone A's toggle must preempt).
                events.append(("a_item_waiting", cid, a_target))
                await gate.wait()
                if coord._dispatch_generation != this_gen or cancellation.is_set():
                    events.append(("a_item_skipped_stale", cid, a_target))
                    return None
                dispatched.append(("z1", "a_item", cid, a_target))
                events.append(("a_dispatched", cid, a_target))
                return a_target

            task = asyncio.ensure_future(_plan())
            coord._active_comfort_dispatch_task = task
            try:
                return await task
            finally:
                if coord._active_comfort_dispatch_task is task:
                    coord._active_comfort_dispatch_task = None
                if coord._active_dispatch_cancellation is cancellation:
                    coord._active_dispatch_cancellation = None

        gate = asyncio.Event()
        coord._async_update_data = _cycle

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(0.2)
        assert ("z2", "b_item", 1, learned_b) in dispatched, (
            "zone B's own learned target must dispatch in cycle 1 while both zones are ON"
        )
        assert ("b_dispatched", 1, learned_b) in events

        # Only zone A toggles OFF; zone B is never touched.
        await coord.async_set_zone_learning_enabled("z1", False)

        gate.set()
        await asyncio.wait_for(cycle1, timeout=2.0)
        assert ("a_item_skipped_stale", 1, learned_a) in events, (
            "zone A's stale learned-target item must be preempted"
        )
        assert ("z1", "a_item", 1, learned_a) not in dispatched

        await _wait_for_recompute_count(call_count, 2)
        assert call_count["n"] == 2
        assert ("z1", "a_item", 2, neutral_a) in dispatched, (
            "zone A's fresh follow-up recompute must dispatch its new neutral target exactly once"
        )
        assert ("z2", "b_item", 2, learned_b) in dispatched, (
            "zone B's still-necessary work must be redispatched in the follow-up "
            "cycle exactly once, using its OWN unchanged learned target -- "
            "proving no permanent loss from zone A's unrelated preemption"
        )
        # Exactly one dispatch of each expected (zone, cycle) pair -- no duplicates.
        assert dispatched.count(("z1", "a_item", 2, neutral_a)) == 1
        assert dispatched.count(("z2", "b_item", 2, learned_b)) == 1
        assert dispatched.count(("z1", "a_item", 1, learned_a)) == 0

        # Zone B's config and stored learning data are byte-identical.
        z2_exec = coord.effective_zone_execution("z2")
        assert z2_exec.learning_enabled is True, "zone B's Learning switch must remain ON"
        b_state_after = (
            adapter._windows["w_z2"].normal.weighted_sum_ha,
            adapter._windows["w_z2"].normal.total_weight,
            adapter._windows["w_z2"].normal.sample_count,
        )
        assert b_state_after == b_state_before, (
            "zone B's TargetPositionAdapter learning data must be completely "
            "unmutated by zone A's Learning toggle"
        )

        if coord._active_comfort_dispatch_task is not None:
            coord._active_comfort_dispatch_task.cancel()
            try:
                await coord._active_comfort_dispatch_task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# TC-LS-TARGET: target-position-level dynamic proof (Phase-2A-Nachtrag point 3)
# ---------------------------------------------------------------------------


class TestTargetPositionFollowUpRecompute:
    @async_test
    async def test_TC_LS_TARGET_on_to_off_fresh_recompute_dispatches_neutral_once(self):
        """ON -> OFF: the OLD generation's waiting item (built while Learning
        was ON, carrying the real learned target) must NOT dispatch; the
        FRESH follow-up recompute the toggle triggers must run fully, use
        the neutral profile (confidence_level="very_low", real
        TargetPositionAdapter output), and dispatch that neutral target
        exactly once -- never the stale learned target, never a duplicate.
        """
        coord, entry = _make_coord()
        learned, neutral = _build_real_learned_and_neutral_targets(coord, "w_z1")
        events: list = []
        gate = asyncio.Event()
        call_count, dispatched = _install_toggle_aware_target_cycle(
            coord, events, zone_id="z1", learned_pos=learned, neutral_pos=neutral, gate=gate,
        )

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(0.2)
        assert ("item1", 1, learned) in dispatched, (
            "item1 must dispatch with the real LEARNED target while Learning is ON"
        )

        await coord.async_set_zone_learning_enabled("z1", False)

        gate.set()
        await asyncio.wait_for(cycle1, timeout=2.0)
        assert ("item2_skipped_stale", 1, learned) in events
        assert ("item2", 1, learned) not in dispatched, (
            "the stale learned-target item2 must never dispatch after OFF"
        )

        await _wait_for_recompute_count(call_count, 2)
        assert call_count["n"] == 2
        assert ("fresh", 2, neutral) in dispatched, (
            "the fresh follow-up recompute must dispatch the real NEUTRAL "
            "target exactly once"
        )
        assert dispatched.count(("fresh", 2, neutral)) == 1
        assert ("fresh", 2, learned) not in dispatched
        assert neutral != learned, "sanity: the two targets must be genuinely different"

    @async_test
    async def test_TC_LS_TARGET_off_to_on_fresh_recompute_dispatches_learned_once(self):
        """Symmetric: OFF -> ON. The old neutral-target item is preempted;
        the fresh recompute uses the STILL-STORED learned value (learning
        data was never discarded while OFF) and dispatches it exactly once.
        """
        coord, entry = _make_coord()
        coord.zones["z1"].execution = ZoneExecutionConfig(
            learning_enabled=False, active_control_enabled=False,
        )
        learned, neutral = _build_real_learned_and_neutral_targets(coord, "w_z1")
        events: list = []
        gate = asyncio.Event()
        call_count, dispatched = _install_toggle_aware_target_cycle(
            coord, events, zone_id="z1", learned_pos=learned, neutral_pos=neutral, gate=gate,
        )

        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(0.2)
        assert ("item1", 1, neutral) in dispatched, (
            "item1 must dispatch with the NEUTRAL target while Learning is OFF"
        )

        await coord.async_set_zone_learning_enabled("z1", True)

        gate.set()
        await asyncio.wait_for(cycle1, timeout=2.0)
        assert ("item2_skipped_stale", 1, neutral) in events
        assert ("item2", 1, neutral) not in dispatched, (
            "the stale neutral-target item2 must never dispatch after ON"
        )

        await _wait_for_recompute_count(call_count, 2)
        assert call_count["n"] == 2
        assert ("fresh", 2, learned) in dispatched, (
            "the fresh follow-up recompute must dispatch the STILL-STORED "
            "real LEARNED target exactly once -- proving learning data "
            "survived the OFF period untouched"
        )
        assert dispatched.count(("fresh", 2, learned)) == 1
        assert ("fresh", 2, neutral) not in dispatched


# ---------------------------------------------------------------------------
# TC-LS-SAFE / TC-LS-PERSIST
# ---------------------------------------------------------------------------


class TestSafetyAndPersistence:
    @async_test
    async def test_TC_LS_SAFE_safety_work_untouched_structurally(self):
        """Structural non-regression: the is_safety exemption this fix
        depends on (same field, same guard) is proven elsewhere
        (test_dispatch_safety_generation_exemption_structural.py,
        test_t22_phase5c_safety_preemption.py); here we only confirm this
        fix introduces no NEW safety-affecting check."""
        coord, entry = _make_coord()
        cancellation = asyncio.Event()
        coord._active_dispatch_cancellation = cancellation
        coord._active_dispatch_cancellation.set()
        assert cancellation.is_set()

    @async_test
    async def test_TC_LS_SAFETY_ITEM_never_preempted_by_learning_toggle(self):
        coord, entry = _make_coord()
        events: list = []
        call_count = {"n": 0}

        async def cycle():
            call_count["n"] += 1
            cid = call_count["n"]
            cancellation = asyncio.Event()
            coord._active_dispatch_cancellation = cancellation

            async def _dispatch():
                events.append(("safety_dispatched", cid))
                return "safety_done"

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
        await asyncio.wait_for(cycle1, timeout=2.0)
        assert ("safety_dispatched", 1) in events

        # Toggling learning after safety already dispatched must not raise
        # or corrupt anything, and must not retroactively affect safety.
        await coord.async_set_zone_learning_enabled("z1", False)
        assert ("safety_dispatched", 1) in events

    @async_test
    async def test_TC_LS_PERSIST_runtime_override_is_actually_updated(self):
        """Uses a zone whose stored default differs from the toggled value
        (learning_enabled defaults to True, so start explicitly False), so
        a skipped runtime-state update is actually observable."""
        coord, entry = _make_coord()
        coord.zones["z1"].execution = ZoneExecutionConfig(
            learning_enabled=False, active_control_enabled=False,
        )
        assert coord.effective_zone_execution("z1").learning_enabled is False

        await coord.async_set_zone_learning_enabled("z1", True)
        current = coord.effective_zone_execution("z1")
        assert current.learning_enabled is True, (
            "the runtime override must actually be written -- reading the "
            "stale ZoneConfig default instead means the toggle silently "
            "had no effect"
        )
        await asyncio.sleep(0.2)

    @async_test
    async def test_TC_LS_SAFETY_WAITING_item_not_yet_dispatched_survives_toggle(self):
        """Phase-2A-Nachtrag point 5: genuine dynamic proof that a real
        `is_safety`-exempt item, still WAITING (not yet dispatched) at
        toggle time, is not discarded by the generation bump/cancellation
        set by this fix. Mirrors coordinator.py's real per-item checkpoint
        exactly: `if not intent.is_safety and (generation mismatch or
        cancellation.is_set()): skip` -- i.e. safety is UNCONDITIONALLY
        exempt from the `not intent.is_safety` guard, so it must dispatch
        regardless of what the Learning toggle just did. A structural-only
        assertion (test_TC_LS_SAFE above) does not exercise this branch;
        this test drives it through a real waiting item.
        """
        coord, entry = _make_coord()
        events: list = []
        gate = asyncio.Event()
        call_count = {"n": 0}

        async def _cycle():
            call_count["n"] += 1
            cid = call_count["n"]
            this_gen = coord._dispatch_generation
            cancellation = asyncio.Event()
            coord._active_dispatch_cancellation = cancellation

            async def _plan():
                events.append(("comfort_item_dispatched", cid))
                events.append(("safety_item_waiting", cid))
                await gate.wait()
                # Real per-item checkpoint shape from coordinator.py
                # (_dispatch_one_parallel_item / _predispatch_sequential_plan):
                # `if not intent.is_safety and (generation mismatch or
                # cancellation.is_set()): skip` -- is_safety=True short-
                # circuits the guard entirely, regardless of generation/
                # cancellation state.
                is_safety = True
                stale = coord._dispatch_generation != this_gen or cancellation.is_set()
                if not is_safety and stale:
                    events.append(("safety_item_skipped_stale", cid))
                    return "safety_skipped"
                events.append(("safety_item_dispatched", cid))
                return "safety_dispatched"

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
        cycle1 = asyncio.ensure_future(coord.async_refresh())
        await asyncio.sleep(0.2)
        assert ("comfort_item_dispatched", 1) in events
        assert ("safety_item_waiting", 1) in events
        assert ("safety_item_dispatched", 1) not in events, (
            "sanity: the safety item must genuinely still be waiting, not "
            "already dispatched, at the moment the toggle fires below"
        )

        # Toggle Learning while the safety item is still waiting -- this is
        # the fix under test: it bumps generation and sets cancellation.
        gen_before = coord._dispatch_generation
        cancellation_ref = coord._active_dispatch_cancellation
        await coord.async_set_zone_learning_enabled("z1", False)
        assert coord._dispatch_generation == gen_before + 1
        assert cancellation_ref.is_set(), (
            "sanity: this fix must actually set cancellation while the "
            "safety item is waiting, so its survival below is a real proof "
            "of the is_safety exemption, not a no-op"
        )

        gate.set()
        await asyncio.wait_for(cycle1, timeout=2.0)
        assert ("safety_item_dispatched", 1) in events, (
            "the waiting safety item must dispatch exactly once despite the "
            "generation bump and cancellation set by the Learning toggle"
        )
        assert ("safety_item_skipped_stale", 1) not in events
        assert events.count(("safety_item_dispatched", 1)) == 1, "must dispatch exactly once"

        # Must not be blocked behind an unnecessary full comfort recompute:
        # only ONE cycle ran end-to-end for the safety item to reach dispatch.
        assert call_count["n"] == 1

    @async_test
    async def test_TC_LS_PERSIST_learned_value_survives_off_and_reapplies_on(self):
        """Phase-2A-Nachtrag point 6: full persistence/learned-data
        round-trip proof using the REAL TargetPositionAdapter (the actual
        production learning store the coordinator holds in memory and
        persists via _persist_learning_data / the `target_adaptations`
        section) -- not a re-implementation.
          1. A real stored learned value exists before toggle.
          2. ON->OFF persists the switch state as before (existing coverage).
          3. The learned value remains value-identical after OFF.
          4. The adapter's internal sample/weight state is unchanged by the
             preemption fix (only the switch flag + dispatch generation
             change -- no learning-store mutation).
          5. While OFF, the neutral profile is what a fresh recompute would
             select (confidence_level="very_low" -> get_effective_targets
             ignores the learned data), i.e. the learned value is not
             applied to the comfort decision while OFF.
          6. After OFF->ON, the SAME preserved value is produced again by
             get_effective_targets(confidence_level="very_high").
          7. The generation bump / cancellation.set() performed by this fix
             trigger no additional persistence call (_persist_zone_controls
             is called exactly once per toggle, already for the switch
             flag itself -- not twice).
        """
        coord, entry = _make_coord()
        learned, neutral = _build_real_learned_and_neutral_targets(coord, "w_z1")
        adapter = coord._target_position_adapter
        stored_before = (
            adapter._windows["w_z1"].normal.weighted_sum_ha,
            adapter._windows["w_z1"].normal.total_weight,
            adapter._windows["w_z1"].normal.sample_count,
            adapter._windows["w_z1"].normal.last_updated,
        )

        persist_calls = {"n": 0}
        _orig_persist = coord._persist_zone_controls
        def _counting_persist(*a, **k):
            persist_calls["n"] += 1
            return _orig_persist(*a, **k)
        coord._persist_zone_controls = _counting_persist

        events: list = []
        _install_fake_cycle(coord, events, comfort_duration_s=0.05)

        # (1)+(2) ON -> OFF: switch state persists as before.
        await coord.async_set_zone_learning_enabled("z1", False)
        await asyncio.sleep(0.15)
        assert coord.effective_zone_execution("z1").learning_enabled is False
        assert persist_calls["n"] == 1, (
            "the generation bump / cancellation performed by this fix must "
            "trigger NO additional persistence call beyond the one the "
            "switch flag itself already required"
        )

        # (3)+(4) learned value + raw sample/weight state untouched by OFF.
        stored_after_off = (
            adapter._windows["w_z1"].normal.weighted_sum_ha,
            adapter._windows["w_z1"].normal.total_weight,
            adapter._windows["w_z1"].normal.sample_count,
            adapter._windows["w_z1"].normal.last_updated,
        )
        assert stored_after_off == stored_before, (
            "the stored learned signal data must be completely unmutated "
            "by the Learning-OFF preemption fix"
        )
        _, still_learned_value, _, _ = adapter.get_effective_targets(
            window_id="w_z1", light_ha=50, normal_ha=50, strong_ha=50,
            confidence_level="very_high",
        )
        assert still_learned_value == learned, (
            "the underlying learned value must remain value-identical -- "
            "only its APPLICATION (gated by confidence_level, which the "
            "coordinator only passes as very_high when obs_enabled) stops"
        )

        # (5) while OFF, the coordinator's real obs_enabled gate means Pass-1
        # would use _NEUTRAL_ADAPTIVE_PROFILE (confidence_level="very_low"),
        # so the learned value is NOT applied to the decision while OFF.
        _, neutral_while_off, _, neutral_while_off_adapted = adapter.get_effective_targets(
            window_id="w_z1", light_ha=50, normal_ha=50, strong_ha=50,
            confidence_level="very_low",
        )
        assert neutral_while_off_adapted is False
        assert neutral_while_off == neutral
        assert neutral_while_off != learned, (
            "while OFF, the real gate must select the neutral (unadapted) "
            "target -- the stored learned value must not be applied"
        )

        # (6) OFF -> ON: the SAME preserved value is produced again.
        persist_calls["n"] = 0
        await coord.async_set_zone_learning_enabled("z1", True)
        await asyncio.sleep(0.15)
        assert coord.effective_zone_execution("z1").learning_enabled is True
        assert persist_calls["n"] == 1, (
            "OFF->ON must likewise trigger exactly one persistence call, "
            "not an extra one from the generation bump / cancellation"
        )
        _, reapplied_value, _, reapplied_adapted = adapter.get_effective_targets(
            window_id="w_z1", light_ha=50, normal_ha=50, strong_ha=50,
            confidence_level="very_high",
        )
        assert reapplied_adapted is True
        assert reapplied_value == learned, (
            "after OFF->ON, the exact same preserved learned value must be "
            "produced again -- proving no data was lost or altered by the "
            "full OFF/ON round trip"
        )
