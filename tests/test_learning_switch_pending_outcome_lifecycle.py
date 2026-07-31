"""PendingOutcome lifecycle across Learning-Switch toggles — Phase 2B.3.

Root cause (CONFIRMED, Phase 2B / 2B-Nachtrag dynamic audit): a PendingOutcome
created while Learning was ON, and still open when the zone is toggled OFF
before its observation window elapses, was neither dropped nor actively
resolved by the OFF transition. It stayed queued unchanged while its
`decision_timestamp`-relative elapsed clock kept running in real wall-clock
time. If the zone was switched back ON only after that clock had already
passed `indoor_temp_outcome_delay_min`, the very next per-cycle timeout sweep
(coordinator.py ~4926-4953) resolved it immediately — using whatever indoor
temperature happened to exist at that later moment, not the temperature at
the originally-intended observation deadline — and reported it as a clean
"complete" observation with a full, undiscounted learning score. A dynamic
probe (real `resolve_outcome()` call, same PendingOutcome, two different
resolution-time temperatures) showed a full score swing from +0.663 to
-0.800 purely from *when* the same pending happened to be resolved.

Fix (`async_set_zone_learning_enabled`): reuses the EXACT existing restart-
interruption mechanism `_restore_pending_outcomes` already relies on, applied
additionally at the ON<->OFF transition instead of only at HA restart:

  - ON->OFF (`_interrupt_zone_pending_outcomes`): every open PendingOutcome
    for the zone's windows is marked via `_interrupted_decision_keys` — the
    SAME set every resolution call site (`_store_outcome` and its six
    `resolve_outcome()` callers) already reads. No removal, no invalidation,
    no synthetic outcome, no new PendingOutcome, no other zone touched here.
  - OFF->ON (`_reconcile_zone_pending_outcomes_on_resume`): for every open,
    interrupted PendingOutcome of the zone, the SAME threshold shape
    `_restore_pending_outcomes` uses (observation delay + a 5-minute grace)
    decides whether it can still resolve later (left queued, already marked
    interrupted — the normal per-cycle sweep will discount it correctly) or
    must be invalidated immediately (removed from the queue before the
    normal sweep can resolve it with stale present-moment sensor data; the
    underlying decision record is marked invalidated via the existing
    `mark_decision_invalidated` mechanism — no synthetic outcome).

Both new methods are gated on `_state_actually_changed` (same guard the
Phase 2A dispatch-generation fix already established in this exact function),
so redundant ON->ON / OFF->OFF calls never reprocess anything.

Downstream safety (no new production change needed — proven by test, not
assumed): `observation_interrupted=True` already propagates all the way
through `resolve_outcome()`'s reliability computation
(`engines/outcome_resolution.py:_finalize_reliability`, thermal reliability
drops from 1.0 to 0.15 when interrupted+partial), and
`_experiment_finalize_from_outcome` (coordinator.py) reads exactly
`mo.reliability.thermal` as its causal-evaluation weight — so an interrupted
outcome cannot win an experiment or drive an adoption with full confidence,
without any additional gating needed in this phase.

Coverage:
  TC_PO_1   ON->OFF before due: pending stays queued, gets marked
            interrupted, no immediate (mis-)resolution.
  TC_PO_2   ON->OFF->ON before due: resolves at most once, never "complete"
            with a full/undiscounted score.
  TC_PO_3   Re-enabled after due but within grace: resolves as
            interrupted_partial with degraded reliability -- not a clean
            "complete".
  TC_PO_4   Re-enabled after delay+grace exceeded: pending removed, decision
            invalidated, no outcome produced at all.
  TC_PO_5   Very long OFF period: same as TC_PO_4 -- no late outcome built
            from current-moment sensor data.
  TC_PO_6   Redundant ON->ON / OFF->OFF: no reprocessing.
  TC_PO_7   Rapid ON->OFF->ON->OFF->ON: deterministic end state, no double
            resolution, no double invalidation.
  TC_PO_8   Restart/restore while OFF: existing `_restore_pending_outcomes`
            behavior is unaffected by this fix (structural non-regression).
  TC_PO_9   Two zones: toggling zone A never touches zone B's PendingOutcome
            or learning state.
  TC_PO_10  No open PendingOutcome: toggle is a clean no-op for this path.
  TC_PO_11  Downstream safety: `observation_interrupted=True` measurably
            degrades the exact reliability field `_experiment_finalize_from_
            outcome` consumes (dynamic proof, real `resolve_outcome()`).
  TC_PO_12  Idempotency: removal/resolution happens at most once.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock


def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _RealBehaviorDataUpdateCoordinator:
    """Behavior-faithful port of DataUpdateCoordinator's refresh-serialising
    Debouncer algorithm — see test_event_triggered_comfort_preemption.py for
    the full verification note against installed homeassistant==2026.6.4."""

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
# Import the dotted submodule path FIRST -- this is the one that forces a
# genuinely fresh execution of coordinator.py (since the sys.modules entry
# was just popped) and, as a side effect of a successful module load, resets
# the parent package's stale `.coordinator` attribute. Importing via the
# parent-package attribute form BEFORE this would silently pick up whatever
# module a previously-imported sibling test file left cached there.
from custom_components.smartshading.coordinator import SmartShadingCoordinator  # noqa: E402
from custom_components.smartshading import coordinator as _coordinator_module  # noqa: E402
from custom_components.smartshading.models.lifecycle import NightDayLifecycleConfig  # noqa: E402
from custom_components.smartshading.models.window import WindowConfig  # noqa: E402
from custom_components.smartshading.models.zone import ZoneConfig  # noqa: E402
from custom_components.smartshading.models.zone_execution_config import ZoneExecutionConfig  # noqa: E402
from custom_components.smartshading.models.pending_outcome import PendingOutcome  # noqa: E402
from custom_components.smartshading.models.decision_provenance import LearningDecisionRecord  # noqa: E402
from custom_components.smartshading.state_machine.states import ShadingState  # noqa: E402
from custom_components.smartshading.engines.outcome_resolution import (  # noqa: E402
    resolve_outcome, OutcomeResolutionInput, OutcomeResolutionTrigger,
)

DELAY_MIN = 30  # matches _OUTCOME_OBSERVATION_DELAY_MIN default used throughout the fixture
GRACE = timedelta(minutes=5)  # mirrors the grace window _reconcile_zone_pending_outcomes_on_resume uses


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


def _install_noop_cycle(coord) -> None:
    """No-op fake cycle -- these tests only exercise the synchronous toggle-
    triggered PendingOutcome bookkeeping, not comfort dispatch."""
    async def _cycle():
        return None
    coord._async_update_data = _cycle


def _make_pending(window_id: str, decision_ts: datetime, *, delay_min: int = DELAY_MIN,
                   decision_id: str | None = None) -> PendingOutcome:
    return PendingOutcome(
        window_id=window_id,
        decision_timestamp=decision_ts,
        from_state=ShadingState.NEUTRAL if hasattr(ShadingState, "NEUTRAL") else list(ShadingState)[0],
        to_state=ShadingState.NORMAL_SHADE,
        decided_by="tier2_comfort",
        lifecycle_state="day",
        indoor_temp_outcome_delay_min=delay_min,
        indoor_temp_at_decision=24.0,
        decision_id=decision_id,
    )


def _seed_pending(coord, window_id: str, decision_ts: datetime, *, delay_min: int = DELAY_MIN,
                   with_decision_record: bool = False, decision_id: str | None = None,
                   experiment_decision_id: str | None = None) -> PendingOutcome:
    did = decision_id or experiment_decision_id
    pending = _make_pending(window_id, decision_ts, delay_min=delay_min, decision_id=did)
    coord._pending_outcomes.create(pending)
    if with_decision_record and did is not None:
        coord._learning_store.record_decision(LearningDecisionRecord(
            decision_id=did, decision_timestamp=decision_ts, cycle_id=1, window_id=window_id,
        ))
    return pending


def _simulate_timeout_resolution(coord, window_id: str, now: datetime, *,
                                  indoor_temp: float | None = 22.0):
    """Mirrors coordinator.py's real per-cycle timeout-sweep block (~4926-4953)
    line-for-line in shape: same queue read/remove, same
    _interrupted_decision_keys-derived observation_interrupted flag, same real
    resolve_outcome() call, same real coord._store_outcome() call. Movement/
    thermal-maturity inputs are omitted (both optional, default None) since
    these tests don't exercise those dimensions. Returns the resolved
    DecisionOutcome, or None if nothing was due (mirrors the real sweep's
    silent no-op when not yet due or nothing pending).
    """
    pending = coord._pending_outcomes.get(window_id)
    if pending is None:
        return None
    elapsed_min = (now - pending.decision_timestamp).total_seconds() / 60
    if elapsed_min < pending.indoor_temp_outcome_delay_min:
        return None
    removed = coord._pending_outcomes.remove(window_id)
    if removed is None:
        return None
    outcome = resolve_outcome(
        removed,
        OutcomeResolutionInput(
            trigger=OutcomeResolutionTrigger.TIMEOUT,
            resolution_timestamp=now,
            indoor_temp_outcome_c=indoor_temp,
            solar_exposure_at_decision=removed.solar_exposure_at_decision,
            observation_interrupted=(window_id, removed.decision_timestamp) in coord._interrupted_decision_keys,
        ),
    )
    coord._store_outcome(outcome)
    return outcome


def async_test(fn):
    def wrapper(*a, **k):
        asyncio.run(fn(*a, **k))
    return wrapper


class _frozen_time:
    """Freezes `dt_util.utcnow()` as seen by coordinator.py's
    `async_set_zone_learning_enabled` (which calls it directly, not via any
    parameter) to a fixed, test-controlled instant -- so elapsed-time
    decisions in `_reconcile_zone_pending_outcomes_on_resume` are
    deterministic and independent of the real wall clock."""

    def __init__(self, fixed_dt: datetime):
        self._fixed = fixed_dt
        self._orig = None

    def __enter__(self):
        self._orig = _coordinator_module.dt_util.utcnow
        _coordinator_module.dt_util.utcnow = lambda: self._fixed
        return self

    def __exit__(self, *exc):
        _coordinator_module.dt_util.utcnow = self._orig


# ---------------------------------------------------------------------------
# TC_PO_1 / TC_PO_2: ON -> OFF before due, with/without a resume before due
# ---------------------------------------------------------------------------


class TestInterruptBeforeDue:
    @async_test
    async def test_TC_PO_1_off_before_due_marks_interrupted_does_not_resolve(self):
        coord, entry = _make_coord()
        _install_noop_cycle(coord)
        now = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        pending = _seed_pending(coord, "w_z1", now)

        with _frozen_time(now):
            await coord.async_set_zone_learning_enabled("z1", False)

        assert coord._pending_outcomes.get("w_z1") is not None, (
            "the pending outcome must stay queued -- OFF only marks it interrupted"
        )
        assert ("w_z1", pending.decision_timestamp) in coord._interrupted_decision_keys
        # Not due yet (elapsed 0 min < 30 min delay) -- no resolution should occur.
        outcome = _simulate_timeout_resolution(coord, "w_z1", now)
        assert outcome is None
        assert coord._pending_outcomes.get("w_z1") is not None

    @async_test
    async def test_TC_PO_2_off_then_on_before_due_resolves_at_most_once_never_clean_complete(self):
        coord, entry = _make_coord()
        _install_noop_cycle(coord)
        decision_ts = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        _seed_pending(coord, "w_z1", decision_ts)

        with _frozen_time(decision_ts):
            await coord.async_set_zone_learning_enabled("z1", False)
            await coord.async_set_zone_learning_enabled("z1", True)  # before due (elapsed=0)

        # Still not due -- must not resolve yet.
        assert _simulate_timeout_resolution(coord, "w_z1", decision_ts) is None

        # Now due, resolved exactly once.
        due_now = decision_ts + timedelta(minutes=DELAY_MIN)
        outcome = _simulate_timeout_resolution(coord, "w_z1", due_now)
        assert outcome is not None
        assert outcome.resolution_status == "interrupted_partial", (
            "must never resolve as a clean 'complete' after surviving an OFF interval"
        )
        assert coord._pending_outcomes.get("w_z1") is None, "resolved exactly once, then removed"
        # A second attempt must be a no-op (nothing left to resolve).
        assert _simulate_timeout_resolution(coord, "w_z1", due_now) is None


# ---------------------------------------------------------------------------
# TC_PO_3 / TC_PO_4 / TC_PO_5: resume timing relative to delay + grace
# ---------------------------------------------------------------------------


class TestResumeTiming:
    @async_test
    async def test_TC_PO_3_resume_after_due_but_within_grace_resolves_interrupted_partial(self):
        coord, entry = _make_coord()
        _install_noop_cycle(coord)
        decision_ts = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        _seed_pending(coord, "w_z1", decision_ts)

        with _frozen_time(decision_ts):
            await coord.async_set_zone_learning_enabled("z1", False)
        # Re-enable 32 min later: past the 30-min delay, but well within the 5-min grace.
        resume_at = decision_ts + timedelta(minutes=32)
        with _frozen_time(resume_at):
            await coord.async_set_zone_learning_enabled("z1", True)

        assert coord._pending_outcomes.get("w_z1") is not None, (
            "within delay+grace: must be left queued, not invalidated"
        )
        outcome = _simulate_timeout_resolution(coord, "w_z1", resume_at)
        assert outcome is not None
        assert outcome.resolution_status == "interrupted_partial"
        assert outcome.multi_objective.reliability.thermal < 0.5, (
            "an interrupted resolution must carry materially degraded reliability, "
            "not a normal learning-quality score"
        )

    @async_test
    async def test_TC_PO_4_resume_after_delay_plus_grace_invalidates_no_outcome(self):
        coord, entry = _make_coord()
        _install_noop_cycle(coord)
        decision_ts = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        did = "dec-1"
        pending = _seed_pending(coord, "w_z1", decision_ts, with_decision_record=True, decision_id=did)

        with _frozen_time(decision_ts):
            await coord.async_set_zone_learning_enabled("z1", False)
        # Re-enable 40 min later: past delay(30) + grace(5).
        resume_at = decision_ts + timedelta(minutes=40)
        with _frozen_time(resume_at):
            await coord.async_set_zone_learning_enabled("z1", True)

        assert coord._pending_outcomes.get("w_z1") is None, (
            "delay+grace exceeded: the pending must be removed immediately at OFF->ON, "
            "before the normal sweep could resolve it with stale sensor data"
        )
        assert ("w_z1", pending.decision_timestamp) not in coord._interrupted_decision_keys
        rec = coord._learning_store.get_decision(pending.window_id, did)
        assert rec is not None
        assert rec.invalidation_reason == "learning_toggle_interrupted_too_long"
        # No outcome must ever be produced for this decision.
        assert _simulate_timeout_resolution(coord, "w_z1", resume_at) is None

    @async_test
    async def test_TC_PO_5_very_long_off_period_no_late_outcome_with_current_sensor_data(self):
        coord, entry = _make_coord()
        _install_noop_cycle(coord)
        decision_ts = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        did = "dec-long"
        _seed_pending(coord, "w_z1", decision_ts, with_decision_record=True, decision_id=did)

        with _frozen_time(decision_ts):
            await coord.async_set_zone_learning_enabled("z1", False)
        resume_at = decision_ts + timedelta(days=3)  # far beyond delay+grace
        with _frozen_time(resume_at):
            await coord.async_set_zone_learning_enabled("z1", True)

        assert coord._pending_outcomes.get("w_z1") is None
        rec = coord._learning_store.get_decision("w_z1", did)
        assert rec.invalidation_reason == "learning_toggle_interrupted_too_long"
        assert _simulate_timeout_resolution(coord, "w_z1", resume_at) is None, (
            "a 3-day-late toggle-return must never fabricate an outcome from "
            "whatever sensor reading exists at that arbitrary later moment"
        )


# ---------------------------------------------------------------------------
# TC_PO_6 / TC_PO_7: redundant and rapid toggles
# ---------------------------------------------------------------------------


class TestRedundantAndRapidToggles:
    @async_test
    async def test_TC_PO_6_redundant_off_off_and_on_on_do_not_reprocess(self):
        coord, entry = _make_coord()
        _install_noop_cycle(coord)
        decision_ts = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        pending = _seed_pending(coord, "w_z1", decision_ts)

        with _frozen_time(decision_ts):
            await coord.async_set_zone_learning_enabled("z1", False)
            keys_after_first_off = set(coord._interrupted_decision_keys)
            await coord.async_set_zone_learning_enabled("z1", False)  # redundant OFF->OFF
            assert coord._interrupted_decision_keys == keys_after_first_off, (
                "a redundant OFF->OFF call must not touch pending-outcome bookkeeping again"
            )

            await coord.async_set_zone_learning_enabled("z1", True)
            assert coord._pending_outcomes.get("w_z1") is not None  # still not due, left queued
            await coord.async_set_zone_learning_enabled("z1", True)  # redundant ON->ON
        assert coord._pending_outcomes.get("w_z1") is not None, (
            "a redundant ON->ON call must not re-run reconciliation (e.g. re-invalidate)"
        )

    @async_test
    async def test_TC_PO_7_rapid_flap_deterministic_no_double_resolution_or_invalidation(self):
        coord, entry = _make_coord()
        _install_noop_cycle(coord)
        decision_ts = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        _seed_pending(coord, "w_z1", decision_ts)

        with _frozen_time(decision_ts):
            for enabled in (False, True, False, True):
                await coord.async_set_zone_learning_enabled("z1", enabled)

        assert coord.effective_zone_execution("z1").learning_enabled is True
        # Still not due (elapsed=0) throughout -- must still be queued, exactly once.
        assert coord._pending_outcomes.count() == 1
        due_now = decision_ts + timedelta(minutes=DELAY_MIN)
        outcome = _simulate_timeout_resolution(coord, "w_z1", due_now)
        assert outcome is not None
        assert outcome.resolution_status == "interrupted_partial"
        assert _simulate_timeout_resolution(coord, "w_z1", due_now) is None, "no double resolution"


# ---------------------------------------------------------------------------
# TC_PO_8: restart/restore behavior unaffected
# ---------------------------------------------------------------------------


class TestRestartUnaffected:
    @async_test
    async def test_TC_PO_8_restore_pending_outcomes_still_works_after_toggle_interruption(self):
        coord, entry = _make_coord()
        _install_noop_cycle(coord)
        decision_ts = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        pending = _seed_pending(coord, "w_z1", decision_ts)

        await coord.async_set_zone_learning_enabled("z1", False)
        # Simulate a restart: pop it out of the live queue (as a real restart
        # would start with an empty in-memory queue) and feed it through the
        # REAL, unmodified _restore_pending_outcomes exactly as persisted
        # pendings are restored on HA startup.
        persisted = coord._pending_outcomes.remove("w_z1")
        assert persisted is not None
        restore_now = decision_ts + timedelta(minutes=10)  # well within window+grace
        coord._restore_pending_outcomes([persisted], restore_now)

        restored = coord._pending_outcomes.get("w_z1")
        assert restored is not None, "existing restart-restore behavior must be unaffected"
        assert restored.restart_count == 1
        assert ("w_z1", persisted.decision_timestamp) in coord._interrupted_decision_keys


# ---------------------------------------------------------------------------
# TC_PO_9: multi-zone isolation
# ---------------------------------------------------------------------------


class TestMultiZoneIsolation:
    @async_test
    async def test_TC_PO_9_toggling_zone_a_never_touches_zone_b_pending_outcome(self):
        coord, entry = _make_coord(zone_ids=("z1", "z2"))
        _install_noop_cycle(coord)
        decision_ts = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        _seed_pending(coord, "w_z1", decision_ts)
        pending_b = _seed_pending(coord, "w_z2", decision_ts)

        await coord.async_set_zone_learning_enabled("z1", False)

        assert ("w_z2", pending_b.decision_timestamp) not in coord._interrupted_decision_keys, (
            "zone B's pending outcome must not be marked interrupted by zone A's toggle"
        )
        assert coord._pending_outcomes.get("w_z2") is not None
        assert coord.effective_zone_execution("z2").learning_enabled is True, (
            "zone B's learning state must be completely untouched"
        )

        # Zone B resolves normally (never interrupted), zone A's does not.
        due_now = decision_ts + timedelta(minutes=DELAY_MIN)
        outcome_b = _simulate_timeout_resolution(coord, "w_z2", due_now)
        assert outcome_b is not None
        assert outcome_b.resolution_status == "complete", (
            "zone B's outcome must resolve cleanly -- it was never touched by zone A's toggle"
        )


# ---------------------------------------------------------------------------
# TC_PO_10: clean no-op when nothing is pending
# ---------------------------------------------------------------------------


class TestNoOpWhenNothingPending:
    @async_test
    async def test_TC_PO_10_toggle_with_no_open_pending_is_a_clean_noop(self):
        coord, entry = _make_coord()
        _install_noop_cycle(coord)
        assert coord._pending_outcomes.count() == 0

        await coord.async_set_zone_learning_enabled("z1", False)
        assert coord._interrupted_decision_keys == set()
        await coord.async_set_zone_learning_enabled("z1", True)
        assert coord._interrupted_decision_keys == set()
        assert coord._pending_outcomes.count() == 0


# ---------------------------------------------------------------------------
# TC_PO_11: downstream safety -- reliability degradation reaches experiment
# evaluation's actual input field
# ---------------------------------------------------------------------------


class TestDownstreamSafety:
    @async_test
    async def test_TC_PO_11_interrupted_resolution_degrades_the_exact_reliability_field_experiments_consume(self):
        """coordinator.py's _experiment_finalize_from_outcome reads
        `mo.reliability.thermal` directly (`reliability = (mo.reliability.thermal
        if mo else 0.0)`) and feeds it as the causal-evaluation confidence input.
        This test proves, with the real resolve_outcome() function, that an
        interrupted resolution's thermal reliability is materially lower than
        an uninterrupted one for the identical decision -- so a toggle-
        interrupted outcome structurally cannot carry the same weight toward
        winning an experiment or confirming an adoption as a clean one.
        No production change is made here: the existing mechanism already
        provides this protection, proven rather than assumed.
        """
        decision_ts = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        pending = _make_pending("w_z1", decision_ts, decision_id="exp-dec-1")
        resolution_time = decision_ts + timedelta(minutes=DELAY_MIN)

        clean = resolve_outcome(pending, OutcomeResolutionInput(
            trigger=OutcomeResolutionTrigger.TIMEOUT,
            resolution_timestamp=resolution_time,
            indoor_temp_outcome_c=21.0,
            observation_interrupted=False,
        ))
        interrupted = resolve_outcome(pending, OutcomeResolutionInput(
            trigger=OutcomeResolutionTrigger.TIMEOUT,
            resolution_timestamp=resolution_time,
            indoor_temp_outcome_c=21.0,
            observation_interrupted=True,
        ))

        assert clean.resolution_status == "complete"
        assert interrupted.resolution_status == "interrupted_partial"
        assert clean.multi_objective.reliability.thermal >= 0.9
        assert interrupted.multi_objective.reliability.thermal <= 0.2, (
            "the exact field _experiment_finalize_from_outcome reads as its "
            "'reliability' input must be materially degraded when interrupted"
        )


# ---------------------------------------------------------------------------
# TC_PO_12: idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    @async_test
    async def test_TC_PO_12_removal_and_resolution_happen_at_most_once(self):
        coord, entry = _make_coord()
        _install_noop_cycle(coord)
        decision_ts = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        did = "dec-idem"
        _seed_pending(coord, "w_z1", decision_ts, with_decision_record=True, decision_id=did)

        with _frozen_time(decision_ts):
            await coord.async_set_zone_learning_enabled("z1", False)
        resume_at = decision_ts + timedelta(minutes=40)  # past delay+grace -> invalidate path
        with _frozen_time(resume_at):
            await coord.async_set_zone_learning_enabled("z1", True)

        assert coord._pending_outcomes.get("w_z1") is None
        rec_after_first = coord._learning_store.get_decision("w_z1", did)
        assert rec_after_first.invalidation_reason == "learning_toggle_interrupted_too_long"

        # A second OFF->ON with nothing left pending must be a true no-op --
        # no re-invalidation, no exception, no duplicate processing.
        with _frozen_time(resume_at):
            await coord.async_set_zone_learning_enabled("z1", False)
            await coord.async_set_zone_learning_enabled("z1", True)
        rec_after_second = coord._learning_store.get_decision("w_z1", did)
        assert rec_after_second == rec_after_first, "must not be reprocessed a second time"
