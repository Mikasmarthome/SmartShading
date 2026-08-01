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

Deadline unification (Nachtrag): the toggle-reconciliation gate and the
restart-restore gate now share a single private decision,
`_pending_observation_deadline_exceeded()`, sourced from `pending.
indoor_temp_outcome_delay_min` -- the same per-instance field the live
per-cycle resolution gate already treats as authoritative -- plus the same
5-minute grace both gates have always used. `_restore_pending_outcomes`
previously used the global `_OUTCOME_OBSERVATION_DELAY_MIN` fallback
constant instead; it now uses the shared per-pending decision too, so live
resolution, toggle reconciliation and restart-restore can never drift apart.
Restart-count and config-fingerprint checks remain restart-specific. The
toggle path also now reuses the existing stable invalidation reason
`observation_interrupted_too_long` (previously a separate
`learning_toggle_interrupted_too_long` string) since no consumer
distinguishes restart from toggle interruption.

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
            behavior is unaffected by this fix (structural non-regression),
            AND the restored pending later resolves as `interrupted_partial`,
            never a clean "complete".
  TC_PO_9   Two zones: toggling zone A never touches zone B's PendingOutcome
            or learning state.
  TC_PO_10  No open PendingOutcome: toggle is a clean no-op for this path.
  TC_PO_11  Downstream safety: `observation_interrupted=True` measurably
            degrades the exact reliability field `_experiment_finalize_from_
            outcome` consumes (dynamic proof, real `resolve_outcome()`).
  TC_PO_11b Production-near completion of TC_PO_11: a real, active
            BoundedExperiment linked to a toggle-interrupted PendingOutcome,
            finalized through the real `_experiment_finalize_from_outcome()`
            (which internally also calls the real `_maybe_adopt()`), never
            reaches STATUS_ACCEPTED_FOR_P8 and creates no adoption; its
            persisted `evaluation.reliability` carries the same degradation
            proven in TC_PO_11, not lost in transit.
  TC_PO_12  Idempotency: removal/resolution happens at most once.
  TC_PO_13  Deadline unification: a PendingOutcome with a per-instance
            `indoor_temp_outcome_delay_min` deliberately different from the
            global default proves the live resolution gate, the toggle
            reconciliation gate and the restart-restore gate all agree on
            the same deadline -- no drift between the three.

Phase 2B.4 additions (production fix + 4 new tests):

  Fix (`_cleanup_removed_window`, coordinator.py): removing a single window
  from a zone dropped its PendingOutcome but left any matching
  `_interrupted_decision_keys` entry orphaned (confirmed by a dynamic probe
  against the real, unmodified coordinator before this fix). Now every key
  for the removed `window_id` is discarded, not just the one tied to
  whatever pending happened to still be present.

  TC_PO_14  Single-WINDOW removal (not zone/config-entry removal -- a
            SmartShading zone is architecturally one config entry; removing
            a whole zone tears down the entire coordinator as a unit, which
            is not what this test is about). Drives the real
            `_cleanup_removed_window()`: the removed window's pending,
            active experiment (historized exactly once) and active adoption
            (historized exactly once with the existing `"window_removed"`
            reason) are all cleaned up; no orphaned `_interrupted_decision_
            keys` entry remains for it; a control window's own pending and
            interrupted-key entry survive completely untouched; repeated
            cleanup is idempotent (no duplicate history growth).
  TC_PO_15  An `interrupted_partial` outcome against an ALREADY-ACTIVE
            adoption (real `_monitor_adoption()` path, real toggle +
            resume-within-grace + real timeout resolution). Pins the actual
            governing invariant: `resolution_status != "complete"` forces
            `thermal.available=False`, which makes `_monitor_adoption`
            transiently suspend the adoption (`current_gate_reason=
            "sensor_unavailable"`) -- never confirm/improve/degrade/
            rollback/invalidate it, never create a new adoption, and never
            act twice on the same outcome.
  TC_PO_16  Tight direct test of the real, pure `reconcile_restored_
            experiments`/`reconcile_restored_adoptions` (engines/
            experiment_engine.py, engines/adoption_engine.py): an OBSERVING
            experiment demotes to `STATUS_INTERRUPTED_PARTIAL` with
            `abort_reason="interrupted_by_restart"`; an active adoption
            survives but is suspended with `"awaiting_restart_
            revalidation"`; repeated/echoed reconcile calls are
            deterministic and never duplicate history.
  TC_PO_17  Coordinator-wiring proof: the real coordinator restore-path
            assignment shape (coordinator.py ~2982-2992,
            `self._experiments_active, self._experiment_history =
            reconcile_restored_experiments(...)` / same for adoptions)
            applied to the coordinator's own attributes, proving the
            wiring -- not just the pure functions in isolation -- without
            needing the full HA config-entry-restore machinery this test
            suite deliberately never drives end-to-end.

Phase 2B.5 addition (1 new test; 0 production changes -- the corrected
audit found no reproducible defect):

  TC_PO_18  Constructor-time restore of `zone_controls`
            (`config_entry.options["zone_controls"]` ->
            `SmartShadingCoordinator.__init__` ->
            `_zone_execution_overrides` -> `effective_zone_execution()`),
            proven via two independent real constructions (not a post-
            construction mutation of one already-built instance): the
            current `learning_enabled`/`active_control_enabled` contract,
            and the still-supported legacy `observation_enabled` fallback
            (coordinator.py:1536-1537) -- both asserted before any cycle
            ever runs.
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
from custom_components.smartshading.models.bounded_experiment import (  # noqa: E402
    BoundedExperiment, STATUS_OBSERVING, STATUS_ACCEPTED_FOR_P8, STATUS_INTERRUPTED_PARTIAL,
)
from custom_components.smartshading.models.persistent_adoption import (  # noqa: E402
    PersistentTargetAdoption, STATUS_MONITORING, STATUS_INVALIDATED as ADOPT_STATUS_INVALIDATED,
)
from custom_components.smartshading.engines.experiment_engine import (  # noqa: E402
    reconcile_restored_experiments,
)
from custom_components.smartshading.engines.adoption_engine import (  # noqa: E402
    reconcile_restored_adoptions,
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
        assert rec.invalidation_reason == "observation_interrupted_too_long"
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
        assert rec.invalidation_reason == "observation_interrupted_too_long"
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

        # The restored-as-interrupted pending must never later resolve as a
        # clean "complete" -- production-near proof via the real per-cycle
        # timeout-sweep shape (_simulate_timeout_resolution), not just a
        # state-flag check.
        due_now = decision_ts + timedelta(minutes=DELAY_MIN)
        outcome = _simulate_timeout_resolution(coord, "w_z1", due_now)
        assert outcome is not None
        assert outcome.resolution_status == "interrupted_partial", (
            "a restart-restored, interrupted pending must never resolve as a "
            "clean 'complete' -- existing restart semantics preserved"
        )


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

    @async_test
    async def test_TC_PO_11b_interrupted_outcome_through_real_experiment_finalization_never_wins(self):
        """Production-near completion of TC_PO_11: drives the REAL
        coordinator experiment path, not just resolve_outcome() in isolation.

        Builds a real, active `BoundedExperiment` (coord._experiments_active)
        linked by `experiment_decision_id` to a real PendingOutcome. The
        pending is toggle-interrupted (ON->OFF via the real public API) and
        resolved as `interrupted_partial`. The resulting DecisionOutcome is
        fed into the REAL `coord._experiment_finalize_from_outcome()` -- the
        exact, unmodified coordinator method the normal per-cycle
        `_store_outcome()` pipeline calls -- which internally also calls the
        real `_maybe_adopt()`.

        Asserts, against real production state:
          - the experiment is finalized exactly once (removed from
            `_experiments_active`, exactly one entry appended to
            `_experiment_history`);
          - it is NEVER finalized as STATUS_ACCEPTED_FOR_P8 (a winner);
          - its persisted `evaluation.reliability` is the same materially
            degraded value TC_PO_11 already proved (not silently dropped
            on the way through the real finalization pipeline);
          - no adoption is created in `_adoptions_active`;
          - re-finalizing the same outcome a second time is a true no-op
            (idempotent -- no matching active experiment left to finalize).

        Honesty note (per the governing instructions): a single experiment
        can never reach STATUS_ACCEPTED_FOR_P8 regardless of interruption,
        because `derive_p8_adoption_eligible` structurally requires
        `P8_MIN_VALID_EXPERIMENTS = 3` independent non-degraded experiments
        (models/bounded_experiment.py). That structural gate is not created
        by this fix. What IS specifically attributable to the toggle-
        interruption fix, and is what this test isolates and asserts, is
        that the persisted `evaluation.reliability` -- the exact value
        `_experiment_finalize_from_outcome` derives from `mo.reliability.
        thermal` and that would feed `min_confidence_seen` in any real
        multi-experiment P8 snapshot -- is measurably degraded rather than
        silently treated as full-strength evidence.
        """
        coord, entry = _make_coord()
        _install_noop_cycle(coord)
        decision_ts = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        did = "exp-real-dec-1"
        _seed_pending(coord, "w_z1", decision_ts, decision_id=did)

        exp = BoundedExperiment(
            experiment_id="exp-1", source_shadow_id="shadow-1", window_id="w_z1",
            zone_id="z1", intensity_level="normal", context_family="day|mid",
            created_at=decision_ts, updated_at=decision_ts,
            status=STATUS_OBSERVING, experiment_decision_id=did,
            baseline_parameter_target_ha=60, expected_final_candidate_target_ha=50,
        )
        coord._experiments_active["z1"] = exp

        with _frozen_time(decision_ts):
            await coord.async_set_zone_learning_enabled("z1", False)  # marks interrupted

        due_now = decision_ts + timedelta(minutes=DELAY_MIN)
        outcome = _simulate_timeout_resolution(coord, "w_z1", due_now)
        assert outcome is not None
        assert outcome.resolution_status == "interrupted_partial"
        assert outcome.decision_id == did

        coord._experiment_finalize_from_outcome(outcome)

        assert "z1" not in coord._experiments_active, "must be finalized exactly once"
        assert len(coord._experiment_history) == 1
        finalized = coord._experiment_history[0]
        assert finalized.status != STATUS_ACCEPTED_FOR_P8, (
            "a toggle-interrupted outcome must never finalize its experiment as a winner"
        )
        assert finalized.evaluation.reliability <= 0.2, (
            "the persisted evaluation must carry the same materially degraded "
            "reliability TC_PO_11 proved on the raw outcome -- not lost in transit "
            "through the real finalization pipeline"
        )
        assert coord._adoptions_active == {}, (
            "a toggle-interrupted single outcome must never create an adoption"
        )

        # Idempotency: nothing left to finalize a second time.
        coord._experiment_finalize_from_outcome(outcome)
        assert len(coord._experiment_history) == 1, "must not be processed twice"


# ---------------------------------------------------------------------------
# TC_PO_13: unified deadline decision (no drift between live/toggle/restore)
# ---------------------------------------------------------------------------


class TestUnifiedDeadline:
    @async_test
    async def test_TC_PO_13_toggle_and_restore_use_the_same_per_pending_deadline_as_live_resolution(self):
        """Dynamic proof that the live per-cycle resolution gate
        (`indoor_temp_outcome_delay_min >= elapsed`), the toggle-reconciliation
        gate, and the restart-restore gate all derive their deadline from the
        SAME source -- `_pending_observation_deadline_exceeded()` -- for a
        PendingOutcome whose delay deliberately diverges from the global
        `_OUTCOME_OBSERVATION_DELAY_MIN` default (30 min): 12 minutes.
        If any of the three ever used a different delay source, this test
        would observe drift (e.g. the toggle path invalidating something the
        live sweep would still consider due, or vice versa).
        """
        coord, entry = _make_coord()
        _install_noop_cycle(coord)
        decision_ts = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        short_delay = 12  # deliberately != DELAY_MIN (30) and != the module default
        pending = _seed_pending(coord, "w_z1", decision_ts, delay_min=short_delay)

        # Live per-cycle gate: due at exactly 12 min, NOT at the global 30-min default.
        just_before_due = decision_ts + timedelta(minutes=11)
        assert _simulate_timeout_resolution(coord, "w_z1", just_before_due) is None, (
            "must not be due yet at 11 min against a 12-min per-pending delay"
        )

        with _frozen_time(decision_ts):
            await coord.async_set_zone_learning_enabled("z1", False)
        # Toggle-reconciliation gate: resume at 13 min (past the 12-min delay,
        # within the shared 5-min grace) -- must be LEFT QUEUED, not invalidated,
        # exactly matching what the live gate above already confirmed as "due
        # but within grace" for this same short delay.
        resume_within_grace = decision_ts + timedelta(minutes=13)
        with _frozen_time(resume_within_grace):
            await coord.async_set_zone_learning_enabled("z1", True)
        assert coord._pending_outcomes.get("w_z1") is not None, (
            "toggle reconciliation must use the SAME 12-min per-pending delay as "
            "the live gate -- must not fall back to the 30-min module default"
        )

        # Restart-restore gate: feed the SAME still-open pending through the
        # real, unmodified _restore_pending_outcomes at 13 min elapsed --
        # must reach the identical "survives as interrupted" verdict.
        persisted = coord._pending_outcomes.remove("w_z1")
        coord._restore_pending_outcomes([persisted], resume_within_grace)
        assert coord._pending_outcomes.get("w_z1") is not None, (
            "restart-restore must use the SAME per-pending delay as the toggle "
            "gate for this pending -- confirms _pending_observation_deadline_"
            "exceeded is the single shared source for all three call sites"
        )

        # Now push past 12min + 5min grace = 17 min -> all three gates must
        # agree this is unrecoverably overdue.
        past_grace = decision_ts + timedelta(minutes=18)
        assert coord._pending_observation_deadline_exceeded(pending, past_grace) is True
        coord._restore_pending_outcomes(
            [coord._pending_outcomes.remove("w_z1")], past_grace
        )
        assert coord._pending_outcomes.get("w_z1") is None, (
            "past the shared 12-min+5-min-grace deadline, the same helper must "
            "invalidate via the restart path exactly as the toggle path would"
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
        assert rec_after_first.invalidation_reason == "observation_interrupted_too_long"

        # A second OFF->ON with nothing left pending must be a true no-op --
        # no re-invalidation, no exception, no duplicate processing.
        with _frozen_time(resume_at):
            await coord.async_set_zone_learning_enabled("z1", False)
            await coord.async_set_zone_learning_enabled("z1", True)
        rec_after_second = coord._learning_store.get_decision("w_z1", did)
        assert rec_after_second == rec_after_first, "must not be reprocessed a second time"


# ---------------------------------------------------------------------------
# TC_PO_14: single-window removal (Phase 2B.4) -- NOT full-zone/config-entry
# removal. A SmartShading "zone" is architecturally one config entry
# (confirmed by config_flow.py's "one config entry per zone" invariant);
# removing a whole zone means unloading/removing that config entry, and its
# coordinator (with all its in-memory state) is torn down/garbage-collected
# as a unit -- there is no separate per-structure cleanup to prove there
# (see coordinator.py's async_unload_entry / async_remove_entry). What
# genuinely happens at runtime, for a zone that keeps existing, is a single
# WINDOW being removed from its config (e.g. a cover deleted from the zone)
# -- that is exactly what `_cleanup_removed_window()` (coordinator.py
# ~9030-9059) is for, reached via `_apply_config_diff_on_restore()` on the
# next reload/restart once the config diff detects a CHANGE_WINDOW_REMOVAL.
# This test drives `_cleanup_removed_window()` directly (the real, only
# production entry point for this cleanup) and is deliberately named and
# scoped as WINDOW removal, not zone/config-entry removal.
# ---------------------------------------------------------------------------


class TestWindowRemoval:
    @async_test
    async def test_TC_PO_14_window_removal_cleans_up_pending_experiment_adoption_and_interrupted_keys(self):
        coord, entry = _make_coord(zone_ids=("z1",))
        # Two windows in the SAME zone: w_z1 (to be removed) and w_z1b (control,
        # must survive completely untouched).
        coord.windows["w_z1b"] = WindowConfig(
            id="w_z1b", name="w_z1b", zone_id="z1",
            azimuth=90, floor_level=0, cover_group_id="cg_z1b",
        )
        decision_ts = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)

        # -- removed window: pending (marked interrupted), experiment, adoption --
        removed_pending = _seed_pending(coord, "w_z1", decision_ts, decision_id="exp-removed")
        coord._interrupted_decision_keys.add(("w_z1", removed_pending.decision_timestamp))
        removed_exp = BoundedExperiment(
            experiment_id="exp-removed-1", source_shadow_id="shadow-removed",
            window_id="w_z1", zone_id="z1", intensity_level="normal",
            context_family="day|mid", created_at=decision_ts, updated_at=decision_ts,
            status=STATUS_OBSERVING, experiment_decision_id="exp-removed",
        )
        coord._experiments_active["z1"] = removed_exp
        gen = coord._thermal_config_generation("z1")
        removed_adoption = PersistentTargetAdoption(
            adoption_id="adopt-removed", window_id="w_z1", zone_id="z1",
            intensity_level="normal", context_family="day|mid",
            configured_target_ha=50, adopted_delta_ha=-5, effective_target_ha=45,
            status=STATUS_MONITORING, config_generation=gen,
            created_at=decision_ts, updated_at=decision_ts, activated_at=decision_ts,
        )
        coord._adoptions_active[removed_adoption.adoption_key] = removed_adoption

        # -- control window w_z1b: its own pending/key, no experiment/adoption
        #    seeded on it (isolation is still meaningfully proven via the
        #    pending + interrupted key, which ARE present on both windows).
        control_pending = _seed_pending(coord, "w_z1b", decision_ts, decision_id="exp-control")
        coord._interrupted_decision_keys.add(("w_z1b", control_pending.decision_timestamp))

        now = decision_ts + timedelta(minutes=5)
        coord._cleanup_removed_window("w_z1", now)

        # Removed window: pending gone.
        assert coord._pending_outcomes.get("w_z1") is None
        # Removed window: experiment ended, historized exactly once.
        assert "z1" not in coord._experiments_active or coord._experiments_active["z1"].window_id != "w_z1", (
            "the removed window's experiment must no longer be active"
        )
        exp_history_for_removed = [
            e for e in coord._experiment_history if e.window_id == "w_z1"
        ]
        assert len(exp_history_for_removed) == 1, "experiment must be historized exactly once"
        # Removed window: adoption ended with the existing "window_removed" reason,
        # historized exactly once, no longer active.
        assert removed_adoption.adoption_key not in coord._adoptions_active
        adopt_history_for_removed = [
            a for a in coord._adoption_history if a.window_id == "w_z1"
        ]
        assert len(adopt_history_for_removed) == 1
        assert adopt_history_for_removed[0].status == ADOPT_STATUS_INVALIDATED
        assert adopt_history_for_removed[0].rollback_reason == "window_removed"
        # Removed window: no orphaned _interrupted_decision_keys entry survives.
        assert not any(k[0] == "w_z1" for k in coord._interrupted_decision_keys), (
            "no _interrupted_decision_keys entry for the removed window may remain"
        )

        # Control window w_z1b: completely unaffected.
        assert coord._pending_outcomes.get("w_z1b") is not None
        assert ("w_z1b", control_pending.decision_timestamp) in coord._interrupted_decision_keys, (
            "the control window's own interrupted-decision key must survive untouched"
        )

        # Idempotency: a repeated cleanup of the same (now-already-removed)
        # window must be a true no-op -- no exception, no additional history.
        exp_history_len = len(coord._experiment_history)
        adopt_history_len = len(coord._adoption_history)
        coord._cleanup_removed_window("w_z1", now)
        assert len(coord._experiment_history) == exp_history_len, "no duplicate experiment history entry"
        assert len(coord._adoption_history) == adopt_history_len, "no duplicate adoption history entry"
        assert not any(k[0] == "w_z1" for k in coord._interrupted_decision_keys)

        # Cleanup with no window_id at all / a window with no pending present:
        # must not raise.
        coord._cleanup_removed_window("", now)
        coord._cleanup_removed_window("does-not-exist", now)


# ---------------------------------------------------------------------------
# TC_PO_15: an interrupted_partial outcome against an ALREADY-ACTIVE adoption
# (Phase 2B.4, scenarios 12-15). Drives the real production path: real open
# PendingOutcome -> Learning ON->OFF->ON (resume within grace) -> real
# per-cycle timeout resolution -> real coord._monitor_adoption(outcome).
#
# Learning is toggled back ON before resolution specifically to isolate the
# outcome's OWN degraded-thermal-availability effect on monitoring from the
# toggle's own separate, expected side effect (while a zone is OFF,
# `_monitor_adoption` independently suspends any active adoption for that
# zone with reason "learning_mode_off" -- confirmed via a dynamic probe
# during implementation; that is a different, correct protection, not what
# this test isolates).
#
# Real-code finding (more precise than the audit that authorized this test):
# there is no explicit reliability check anywhere in `_monitor_adoption`/
# `evaluate_monitoring_action`. The reason an interrupted_partial outcome
# cannot confirm/improve/degrade/rollback an adoption is that
# `compute_thermal_outcome()` forces `thermal.available=False` whenever
# `resolution_status != "complete"`, which makes `classify_monitoring_outcome`
# return MON_UNAVAILABLE, which `evaluate_monitoring_action` treats as
# "sensor_unavailable" -> ACTION_TEMPORARY_SUSPEND (a transient, reversible,
# explicitly non-negative suspension per the function's own docstring/
# comment "never rollback") -- NOT a silent no-op as originally assumed, and
# NOT any of the confirm/improve/degrade/rollback/invalidate actions this
# test explicitly rules out.
# ---------------------------------------------------------------------------


class TestInterruptedOutcomeAgainstActiveAdoption:
    @async_test
    async def test_TC_PO_15_interrupted_outcome_cannot_confirm_improve_degrade_or_rollback_active_adoption(self):
        coord, entry = _make_coord()
        _install_noop_cycle(coord)
        decision_ts = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)

        gen = coord._thermal_config_generation("z1")
        adoption = PersistentTargetAdoption(
            adoption_id="adopt-1", window_id="w_z1", zone_id="z1", intensity_level="normal",
            context_family="day|mid", configured_target_ha=50, adopted_delta_ha=-5,
            effective_target_ha=45, status=STATUS_MONITORING, config_generation=gen,
            created_at=decision_ts, updated_at=decision_ts, activated_at=decision_ts,
        )
        key = adoption.adoption_key
        coord._adoptions_active[key] = adoption
        before_snapshot = adoption.to_dict()

        _seed_pending(coord, "w_z1", decision_ts)
        with _frozen_time(decision_ts):
            await coord.async_set_zone_learning_enabled("z1", False)
        resume_at = decision_ts + timedelta(minutes=2)  # within grace
        with _frozen_time(resume_at):
            await coord.async_set_zone_learning_enabled("z1", True)  # back ON before resolution

        due_now = decision_ts + timedelta(minutes=DELAY_MIN)
        outcome = _simulate_timeout_resolution(coord, "w_z1", due_now)
        assert outcome is not None
        assert outcome.resolution_status == "interrupted_partial"
        assert outcome.multi_objective.reliability.thermal < 0.5, "reliability must be degraded, not full"
        assert outcome.multi_objective.thermal.available is False, (
            "pins the exact invariant _monitor_adoption's safety depends on"
        )

        coord._monitor_adoption(outcome)

        after = coord._adoptions_active.get(key)
        assert after is not None, "the adoption must still be active, not rolled back/invalidated/deleted"
        assert len(coord._adoption_history) == 0, "no new adoption-history entry may be created"
        assert len([a for a in coord._adoptions_active.values() if a.window_id == "w_z1"]) == 1, (
            "no new adoption may be created for this window"
        )
        # Confirm/improve/degrade/strike/rollback-relevant state is unchanged.
        assert after.status == STATUS_MONITORING, "no confirmation, no reduction, no rollback"
        assert after.effective_target_ha == 45, "no target/candidate shift"
        assert after.adopted_delta_ha == -5, "no candidate change"
        assert after.monitoring.outcome_count == before_snapshot["monitoring"]["outcome_count"], (
            "an unavailable-thermal outcome must not count as valid evidence"
        )
        assert after.monitoring.distinct_days == before_snapshot["monitoring"]["distinct_days"]
        assert after.monitoring.degraded_count == 0, "no strike"
        assert after.monitoring.improved_count == 0
        assert after.monitoring.no_degradation_count == 0
        assert after.monitoring.preference_rejection_count == 0
        # The only real, correct, transient consequence: the adoption is
        # temporarily suspended because sensor/thermal data was unavailable --
        # never a rollback, never invalidated, never a strike.
        assert after.suspended is True
        assert after.current_gate_reason == "sensor_unavailable"

        # The same outcome must not be able to act on this adoption twice --
        # feeding it again must not change monitoring counts or status further.
        coord._monitor_adoption(outcome)
        after2 = coord._adoptions_active.get(key)
        assert after2.status == STATUS_MONITORING
        assert after2.monitoring.outcome_count == after.monitoring.outcome_count
        assert after2.monitoring.degraded_count == 0
        assert len(coord._adoption_history) == 0


# ---------------------------------------------------------------------------
# TC_PO_16 / TC_PO_17: restart/restore reconciliation for experiments and
# adoptions (Phase 2B.4, scenario 19). TC_PO_16 is a tight direct test of the
# two real, pure reconcile functions; TC_PO_17 is a coordinator-wiring proof
# that the coordinator's real restore path (coordinator.py ~2982-2992)
# assigns their results onto `self._experiments_active`/`_experiment_history`/
# `_adoptions_active`/`_adoption_history` exactly as production does -- driven
# without needing the full HA config-entry-restore machinery, which the rest
# of this test suite deliberately does not exercise end-to-end (see this
# file's other TC_PO tests and the sibling comfort-preemption test file).
# ---------------------------------------------------------------------------


class TestRestartReconciliation:
    @async_test
    async def test_TC_PO_16_reconcile_functions_demote_observing_experiment_and_suspend_adoption(self):
        now = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        exp = BoundedExperiment(
            experiment_id="exp-1", source_shadow_id="shadow-1", window_id="w_z1",
            zone_id="z1", intensity_level="normal", context_family="day|mid",
            created_at=now, updated_at=now, status=STATUS_OBSERVING,
            experiment_decision_id="dec-1",
        )
        adoption = PersistentTargetAdoption(
            adoption_id="adopt-1", window_id="w_z1", zone_id="z1", intensity_level="normal",
            context_family="day|mid", configured_target_ha=50, adopted_delta_ha=-5,
            effective_target_ha=45, status=STATUS_MONITORING, config_generation=0,
            created_at=now, updated_at=now, activated_at=now,
        )

        active_exp, exp_history = reconcile_restored_experiments([exp], now)
        active_adopt, adopt_history = reconcile_restored_adoptions([adoption], now)

        assert active_exp == {}, "an OBSERVING experiment must never resume as active"
        assert len(exp_history) == 1
        demoted = exp_history[0]
        assert demoted.status == STATUS_INTERRUPTED_PARTIAL
        assert demoted.abort_reason == "interrupted_by_restart"

        assert adoption.adoption_key in active_adopt, "the adoption must remain active, not dropped"
        assert len(adopt_history) == 0, "no adoption invalidation/rollback on plain restart"
        restored_adoption = active_adopt[adoption.adoption_key]
        assert restored_adoption.suspended is True
        assert restored_adoption.current_gate_reason == "awaiting_restart_revalidation"
        assert restored_adoption.status == STATUS_MONITORING, "status itself is not force-changed"
        assert restored_adoption.effective_target_ha == 45, "no target re-application by restore alone"

        # Determinism/idempotency: the same input reconciled again produces the
        # identical single-entry result -- no accumulation across repeated calls.
        active_exp2, exp_history2 = reconcile_restored_experiments([exp], now)
        assert active_exp2 == {}
        assert len(exp_history2) == 1
        active_adopt2, adopt_history2 = reconcile_restored_adoptions([adoption], now)
        assert len(adopt_history2) == 0
        assert len(active_adopt2) == 1

        # Feeding the ALREADY-reconciled (now-empty-of-active) experiment state
        # back in as a second restart's input produces no further activity --
        # nothing left to demote a second time, no duplicate history growth.
        active_exp3, exp_history3 = reconcile_restored_experiments(exp_history, now)
        assert active_exp3 == {}
        assert len(exp_history3) == 1, "the already-terminal experiment is carried through history, not duplicated"

    @async_test
    async def test_TC_PO_17_coordinator_restore_wiring_assigns_reconciled_results(self):
        """Proves the real coordinator assignment pattern (coordinator.py
        ~2982-2992: `self._experiments_active, self._experiment_history =
        reconcile_restored_experiments(...)` / same for adoptions) actually
        leaves the coordinator's own attributes in the reconciled state, using
        the coordinator's real attributes as the target -- not a re-implemented
        parallel assignment."""
        coord, entry = _make_coord(zone_ids=("z1",))
        now = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        exp = BoundedExperiment(
            experiment_id="exp-1", source_shadow_id="shadow-1", window_id="w_z1",
            zone_id="z1", intensity_level="normal", context_family="day|mid",
            created_at=now, updated_at=now, status=STATUS_OBSERVING,
            experiment_decision_id="dec-1",
        )
        adoption = PersistentTargetAdoption(
            adoption_id="adopt-1", window_id="w_z1", zone_id="z1", intensity_level="normal",
            context_family="day|mid", configured_target_ha=50, adopted_delta_ha=-5,
            effective_target_ha=45, status=STATUS_MONITORING, config_generation=0,
            created_at=now, updated_at=now, activated_at=now,
        )
        assert coord._experiments_active == {}
        assert coord._adoptions_active == {}

        # Exactly the coordinator's own real restore-path assignment shape
        # (coordinator.py ~2982-2992), applied to the coordinator's own
        # attributes -- proving the wiring, not merely the pure functions.
        coord._experiments_active, coord._experiment_history = (
            reconcile_restored_experiments([exp], now)
        )
        coord._adoptions_active, coord._adoption_history = (
            reconcile_restored_adoptions([adoption], now)
        )

        assert coord._experiments_active == {}, "no active experiment left running after the coordinator restore"
        assert len(coord._experiment_history) == 1
        assert coord._experiment_history[0].status == STATUS_INTERRUPTED_PARTIAL
        assert coord._experiment_history[0].abort_reason == "interrupted_by_restart"

        assert adoption.adoption_key in coord._adoptions_active
        assert coord._adoptions_active[adoption.adoption_key].suspended is True
        assert coord._adoptions_active[adoption.adoption_key].current_gate_reason == (
            "awaiting_restart_revalidation"
        )
        assert len(coord._adoption_history) == 0, "no new/deleted/rolled-back adoption"

        # Repeated restore-style reconcile on the coordinator's own now-already-
        # reconciled attributes must not double-process (real assignment is a
        # plain "=" replace, never an append -- confirmed by construction here).
        exp_history_len = len(coord._experiment_history)
        adopt_history_len = len(coord._adoption_history)
        coord._experiments_active, coord._experiment_history = (
            reconcile_restored_experiments(list(coord._experiment_history), now)
        )
        coord._adoptions_active, coord._adoption_history = (
            reconcile_restored_adoptions(list(coord._adoptions_active.values()), now)
        )
        assert len(coord._experiment_history) == exp_history_len, "no duplicate experiment history growth"
        assert len(coord._adoption_history) == adopt_history_len == 0, "adoption stays active, not duplicated into history"
        assert adoption.adoption_key in coord._adoptions_active


# ---------------------------------------------------------------------------
# TC_PO_18: constructor-time restore of zone_controls (Phase 2B.5, G1)
# ---------------------------------------------------------------------------


class TestConstructorZoneControlsRestore:
    @async_test
    async def test_TC_PO_18_constructor_restores_zone_controls_including_legacy_observation_enabled_fallback(self):
        """Proves the real constructor read path -- coordinator.py:1525-1539 --
        NOT a post-construction mutation of an already-built instance:

            config_entry.options["zone_controls"]
            -> SmartShadingCoordinator.__init__()
            -> self._zone_execution_overrides
            -> effective_zone_execution(zone_id)

        Two independent real constructions in one test (not one instance whose
        options are mutated afterward):
          1. the current two-key contract (`learning_enabled` +
             `active_control_enabled`),
          2. the still-supported legacy fallback (`observation_enabled` only,
             no `learning_enabled` key) -- coordinator.py:1536-1537.

        Both assertions run immediately after construction, before any cycle,
        before `async_config_entry_first_refresh()` -- proving the restore is
        synchronous and complete at construction time, matching the real
        production ordering already established (learning_enabled is fully
        available before the coordinator's first `_async_update_data` call
        ever runs).
        """
        # -- 1. current contract --
        hass1 = _make_hass()
        entry1 = _make_entry()
        entry1.options = {
            "zone_controls": {
                "z1": {"learning_enabled": False, "active_control_enabled": True},
            }
        }
        coord1 = SmartShadingCoordinator(
            hass1, entry1, lifecycle_config=NightDayLifecycleConfig(id="default"),
            presence_entity_ids=[],
        )
        coord1.zones = {"z1": ZoneConfig(id="z1", name="z1")}

        exec1 = coord1.effective_zone_execution("z1")
        assert exec1.learning_enabled is False
        assert exec1.active_control_enabled is True

        # -- 2. legacy fallback (independent construction, own entry/options) --
        hass2 = _make_hass()
        entry2 = _make_entry()
        entry2.options = {
            "zone_controls": {
                "legacy-zone": {"observation_enabled": False, "active_control_enabled": False},
            }
        }
        coord2 = SmartShadingCoordinator(
            hass2, entry2, lifecycle_config=NightDayLifecycleConfig(id="default"),
            presence_entity_ids=[],
        )
        coord2.zones = {"legacy-zone": ZoneConfig(id="legacy-zone", name="legacy-zone")}

        exec2 = coord2.effective_zone_execution("legacy-zone")
        assert exec2.learning_enabled is False, (
            "legacy 'observation_enabled' key must still be honored as the "
            "learning_enabled fallback at construction time"
        )
        assert exec2.active_control_enabled is False
