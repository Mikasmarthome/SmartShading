"""Learning persistence layer — Phase 9D.

Architecture:
    LearningStore (HA-independent, in-memory ring buffers)
    ↓
    Pure serialization / deserialization / pruning functions  ← testable without HA
    ↓
    LearningPersistenceAdapter (thin async shell, wraps hass.storage.Store)

Safe-restore invariant (highest priority):
    Missing file, corrupted JSON, unknown version, partial record failures →
    log WARNING, leave LearningStore empty, never raise or block startup.

Storage format (version 1):
    {
        "version": 1,
        "exported_at": "<ISO-8601 UTC>",
        "windows": {
            "<window_id>": {
                "transitions": [ {...}, ... ],   // oldest-first
                "overrides":   [ {...}, ... ],   // oldest-first
                "snapshots":   [ {...}, ... ]    // oldest-first
            }
        }
    }

The HA import in LearningPersistenceAdapter.__init__ is deferred so that
the module can be imported and all pure functions can be tested without a
running Home Assistant instance.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..engines.learning_store import LearningStore
from ..models.decision_provenance import (
    PROVENANCE_SCHEMA_VERSION,
    LearningDecisionRecord,
    ProvenanceSummary,
    RETENTION_FULL,
    RETENTION_PINNED,
    RETENTION_SUMMARY,
)
from ..models.learning import (
    DecisionOutcome,
    OverrideRecord,
    StateTransitionRecord,
    WindowCycleSnapshot,
)
from ..models.pending_outcome import PendingOutcome
from ..state_machine.states import ShadingState

_LOGGER = logging.getLogger(__name__)

# HA Store object version — INTENTIONALLY stays 1.  Bumping it would hand
# migration control to Home Assistant's Store machinery; LE 2.0 versions the
# payload instead (schema_version) and migrates in pure, testable code.
LEARNING_STORE_VERSION: int = 1
LEARNING_STORAGE_KEY: str = "smartshading_learning"

# Payload schema version (P2).  v1 payloads carry "version": 1 and no
# "schema_version".  v2 payloads carry "version": 1 AND "schema_version": 2.
PAYLOAD_SCHEMA_V1: int = 1
PAYLOAD_SCHEMA_V2: int = PROVENANCE_SCHEMA_VERSION  # == 2

# Decision-record retention (P2.8).
#
# Caps revised DOWN after measuring real serialized sizes: a full record is
# ~3.3 KB compact (None-stripped), a summary ~1.1 KB.  The earlier 2000-full /
# 5000-total caps projected ~12.7 MB/window (~382 MB at 30 windows) — clearly
# disproportionate for a .storage file.  We keep the mandated 400-newest-full
# floor and a summary tail for older records, but lower the upper bounds.
#
# Note: DECISIONS_FULL_AGE_DAYS = 120 days ≈ 4 months (honest figure; NOT
# "three seasons").  Resulting worst case ≈ 3.0 MB/window (≈ 600 full × 3.3 KB
# + 900 summary × 1.1 KB).  See classify_and_prune_decisions and the P2 size
# measurement test for the exact figures.
DECISIONS_FULL_MIN: int = 400      # newest N always kept full (mandated floor)
DECISIONS_FULL_MAX: int = 600      # hard cap on full records per window
DECISIONS_FULL_AGE_DAYS: int = 120  # ≈ 4 months kept full within the count cap
DECISIONS_HARD_MAX: int = 1500     # absolute records per window (full + summary)
DECISIONS_MAX_AGE_DAYS: int = 365  # age cap on any decision record


def _strip_none(value: object) -> object:
    """Recursively drop dict keys whose value is None to shrink stored records.

    Empty dicts/lists and non-None scalars are preserved so that required
    nested containers (context/baseline/adaptation/resolved/dispatch) always
    remain present.  Safe because every from_dict() uses .get() with defaults
    for optional fields.
    """
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value]
    return value

# Elapsed-time threshold for the periodic learning persistence save.
# The coordinator saves pending dirty learning data when at least
# PERSISTENCE_INTERVAL_MINUTES minutes have elapsed since the last save,
# or immediately when _learning_dirty is set by an important learning event
# (override signal, outcome resolution).
# At the 5-minute update interval this is at most 60 minutes between saves.
PERSISTENCE_INTERVAL_MINUTES: int = 60

# Legacy cycle-count constant — kept for backward compatibility with existing
# tests that import it.  No longer used by the coordinator.
PERSISTENCE_SAVE_INTERVAL: int = 30


# ---------------------------------------------------------------------------
# Retention / pruning configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LearningPersistenceConfig:
    """Retention limits for each learning record stream.

    These values are maximums — the effective limit in any given run is the
    lesser of the per-window limit below and the LearningStore ring-buffer
    capacity set in the coordinator.

    EvaluatorConfidenceRecord has no automatic age deletion because it
    represents aggregated trust knowledge, not individual events.
    DecisionOutcome limits are defined here for forward-compatibility; the
    Learning Engine will start writing these records in a future phase.
    """

    # StateTransitionRecord: 1 year / 5 000 per window
    # 365d covers all four seasons exactly once; data older than one year
    # risks representing a changed environment (remodelling, moved sensors).
    transitions_max_age_days: int = 365
    transitions_max_per_window: int = 5000

    # OverrideRecord: 1 year / 1 000 per window
    overrides_max_age_days: int = 365
    overrides_max_per_window: int = 1000

    # WindowCycleSnapshot: 1 year / 2 000 per window
    # Was 180d; Spring snapshots were gone before Winter similarity matching.
    snapshots_max_age_days: int = 365
    snapshots_max_per_window: int = 2000

    # DecisionOutcome: 1 year / 5 000
    outcomes_max_age_days: int = 365
    outcomes_max_per_window: int = 5000


# ---------------------------------------------------------------------------
# Pure utility: pruning
# ---------------------------------------------------------------------------

def prune_by_age_and_count(
    records: list,
    max_age_days: int,
    max_count: int,
    now: datetime,
) -> list:
    """Return the records within age and count limits.

    *records* must be ordered oldest-first and each element must have a
    .timestamp attribute (UTC-aware datetime).

    Age filter runs before count filter so the count limit always retains
    the most recent records within the age window.
    """
    cutoff = now - timedelta(days=max_age_days)
    within_age = [r for r in records if r.timestamp >= cutoff]
    if len(within_age) > max_count:
        # Keep the most-recent max_count entries (tail of oldest-first list).
        return within_age[-max_count:]
    return within_age


# ---------------------------------------------------------------------------
# Payload version detection & migration (P2.3) — pure, no I/O
# ---------------------------------------------------------------------------

def detect_payload_version(data: dict) -> int:
    """Return the payload schema version.

    v2 payloads carry an explicit "schema_version".  Legacy v1 payloads have
    only the HA-store "version" field (== 1) and no "schema_version".
    """
    sv = data.get("schema_version")
    if sv is not None:
        return int(sv)
    return PAYLOAD_SCHEMA_V1


def migrate_v1_to_v2(data: dict) -> dict:
    """Pure, idempotent migration of a v1 payload to v2.

    - Existing streams (transitions/overrides/snapshots/outcomes) and
      target_adaptations are preserved verbatim.
    - Each window gains an empty ``decisions`` list (legacy v1 outcomes carry
      no provenance and are NOT fabricated).
    - Top-level ``pending_outcomes`` and ``config_generations`` scaffolding
      is added.
    - Idempotent: a v2 payload is returned unchanged.
    """
    if detect_payload_version(data) == PAYLOAD_SCHEMA_V2:
        return data

    out: dict = dict(data)
    out["schema_version"] = PAYLOAD_SCHEMA_V2
    out.setdefault("version", LEARNING_STORE_VERSION)

    windows = out.get("windows", {})
    if isinstance(windows, dict):
        migrated_windows: dict = {}
        for wid, streams in windows.items():
            if isinstance(streams, dict):
                new_streams = dict(streams)
                new_streams.setdefault("decisions", [])
                migrated_windows[wid] = new_streams
            else:
                migrated_windows[wid] = streams
        out["windows"] = migrated_windows

    out.setdefault("pending_outcomes", [])
    out.setdefault("config_generations", {"fingerprint_version": 1, "windows": {}})
    out.setdefault("legacy_migrated_from", PAYLOAD_SCHEMA_V1)
    return out


# ---------------------------------------------------------------------------
# Decision-record retention (P2.8) — pure
# ---------------------------------------------------------------------------

def classify_and_prune_decisions(
    records: list[LearningDecisionRecord],
    now: datetime,
    protected_ids: set[str] | None = None,
) -> list[LearningDecisionRecord]:
    """Apply age cap, hard count cap, and full→summary demotion.

    *records* are oldest-first.  Returns the retained records, oldest-first,
    with retention_class set and demoted records carrying a ProvenanceSummary
    instead of full provenance.  Pinned records are never demoted and never
    dropped by the count cap, but the absolute age cap still applies.

    *protected_ids* are decision_ids referenced by an ACTIVE pending outcome.
    They are never dropped (age or count) and never demoted, so an outcome that
    has not yet resolved always finds its full provenance record.
    """
    from dataclasses import replace as _replace

    protected = protected_ids or set()
    age_cutoff = now - timedelta(days=DECISIONS_MAX_AGE_DAYS)
    full_age_cutoff = now - timedelta(days=DECISIONS_FULL_AGE_DAYS)

    def _shielded(r: LearningDecisionRecord) -> bool:
        return r.pinned or r.decision_id in protected

    # 1. Age cap — never drop a shielded (pinned or actively-referenced) record.
    kept = [r for r in records if r.decision_timestamp >= age_cutoff or _shielded(r)]

    # 2. Hard count cap — keep newest DECISIONS_HARD_MAX, but never drop shielded.
    if len(kept) > DECISIONS_HARD_MAX:
        shielded = [r for r in kept if _shielded(r)]
        rest = [r for r in kept if not _shielded(r)]
        slots = max(0, DECISIONS_HARD_MAX - len(shielded))
        rest = rest[-slots:] if slots > 0 else []
        kept = sorted(shielded + rest, key=lambda r: r.decision_timestamp)

    n = len(kept)
    out: list[LearningDecisionRecord] = []
    for i, r in enumerate(kept):
        # rank 0 == newest
        rank_from_newest = n - 1 - i
        keep_full = (
            _shielded(r)
            or rank_from_newest < DECISIONS_FULL_MIN
            or (r.decision_timestamp >= full_age_cutoff and rank_from_newest < DECISIONS_FULL_MAX)
        )
        if keep_full:
            rc = RETENTION_PINNED if r.pinned else RETENTION_FULL
            out.append(r if r.retention_class == rc else _replace(r, retention_class=rc))
        else:
            # Demote: drop heavy provenance, keep a compact summary + outcome.
            summary = r.summary
            if summary is None and r.provenance is not None:
                summary = ProvenanceSummary.from_provenance(r.provenance)
            out.append(_replace(
                r,
                provenance=None,
                summary=summary,
                retention_class=RETENTION_SUMMARY,
            ))
    return out


def _serialize_decision(r: LearningDecisionRecord) -> dict:
    # None-stripped to keep stored records compact (~21% smaller).  from_dict()
    # tolerates missing optional keys, so this is lossless on restore.
    return _strip_none(r.to_dict())  # type: ignore[return-value]


def _deserialize_decision(window_id: str, d: dict) -> LearningDecisionRecord:
    rec = LearningDecisionRecord.from_dict(d)
    # Defensive: ensure the record's window_id matches its container.
    if rec.window_id != window_id:
        from dataclasses import replace as _replace
        rec = _replace(rec, window_id=window_id)
    return rec


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _serialize_transition(r: StateTransitionRecord) -> dict:
    return {
        "timestamp": r.timestamp.isoformat(),
        "from_state": r.from_state.value,
        "to_state": r.to_state.value,
        "decided_by": r.decided_by,
        "lifecycle_state": r.lifecycle_state,
        "absence_active": r.absence_active,
        "is_in_solar_sector": r.is_in_solar_sector,
        "outdoor_temp_c": r.outdoor_temp_c,
        "indoor_temp_c": r.indoor_temp_c,
        "solar_radiation_wm2": r.solar_radiation_wm2,
        "wind_speed_ms": r.wind_speed_ms,
        # Step 9F1
        "sun_azimuth": r.sun_azimuth,
        "sun_elevation": r.sun_elevation,
        "solar_relative_azimuth": r.solar_relative_azimuth,
        # Step 9F2
        "weather_condition": r.weather_condition,
        "cloud_cover_pct": r.cloud_cover_pct,
        "raw_solar_radiation_wm2": r.raw_solar_radiation_wm2,
        "effective_exposure_wm2": r.effective_exposure_wm2,
        "learned_solar_impact_factor": r.learned_solar_impact_factor,
    }


def _serialize_override(r: OverrideRecord) -> dict:
    return {
        "timestamp": r.timestamp.isoformat(),
        "event_type": r.event_type,
        "lifecycle_state": r.lifecycle_state,
        "override_position": r.override_position,
        "overridden_state": (
            r.overridden_state.value if r.overridden_state is not None else None
        ),
        "overridden_position": r.overridden_position,
        "override_duration_min": r.override_duration_min,
        "outdoor_temp_c": r.outdoor_temp_c,
        "solar_radiation_wm2": r.solar_radiation_wm2,
        # Step 9F3
        "decided_by": r.decided_by,
        # Step 9F1
        "sun_azimuth": r.sun_azimuth,
        "sun_elevation": r.sun_elevation,
        "solar_relative_azimuth": r.solar_relative_azimuth,
        # Step 9F2
        "weather_condition": r.weather_condition,
        "cloud_cover_pct": r.cloud_cover_pct,
        "raw_solar_radiation_wm2": r.raw_solar_radiation_wm2,
        "effective_exposure_wm2": r.effective_exposure_wm2,
        "learned_solar_impact_factor": r.learned_solar_impact_factor,
    }


def _serialize_snapshot(r: WindowCycleSnapshot) -> dict:
    return {
        "timestamp": r.timestamp.isoformat(),
        "shading_state": r.shading_state.value,
        "decided_by": r.decided_by,
        "lifecycle_state": r.lifecycle_state,
        "absence_active": r.absence_active,
        "override_active": r.override_active,
        "target_position": r.target_position,
        "outdoor_temp_c": r.outdoor_temp_c,
        "indoor_temp_c": r.indoor_temp_c,
        "solar_radiation_wm2": r.solar_radiation_wm2,
        "effective_exposure_wm2": r.effective_exposure_wm2,
        "wind_speed_ms": r.wind_speed_ms,
        # Step 9F1
        "sun_azimuth": r.sun_azimuth,
        "sun_elevation": r.sun_elevation,
        "solar_relative_azimuth": r.solar_relative_azimuth,
        # Step 9F2
        "weather_condition": r.weather_condition,
        "cloud_cover_pct": r.cloud_cover_pct,
        "raw_solar_radiation_wm2": r.raw_solar_radiation_wm2,
        "learned_solar_impact_factor": r.learned_solar_impact_factor,
    }


def _serialize_outcome(r: DecisionOutcome) -> dict:
    return {
        "decision_timestamp": r.decision_timestamp.isoformat(),
        "decided_state": r.decided_state.value,
        "decided_by": r.decided_by,
        "indoor_temp_outcome_delay_min": r.indoor_temp_outcome_delay_min,
        "lifecycle_state": r.lifecycle_state,
        "from_state": r.from_state.value if r.from_state is not None else None,
        "override_occurred": r.override_occurred,
        "override_delay_min": r.override_delay_min,
        "override_event_type": r.override_event_type,
        "indoor_temp_at_decision": r.indoor_temp_at_decision,
        "indoor_temp_outcome_c": r.indoor_temp_outcome_c,
        "indoor_temp_delta_c": r.indoor_temp_delta_c,
        "state_duration_min": r.state_duration_min,
        "escalation_occurred": r.escalation_occurred,
        "outcome_score": r.outcome_score,
        "resolution_status": r.resolution_status,
        "evaluation_timestamp": (
            r.evaluation_timestamp.isoformat() if r.evaluation_timestamp is not None else None
        ),
    }


def serialize_learning_store(
    store: LearningStore,
    config: LearningPersistenceConfig,
    now: datetime,
    *,
    active_window_ids: set[str] | None = None,
    target_adapter: "object | None" = None,
    pending_outcomes: "list[PendingOutcome] | None" = None,
    config_generations: dict | None = None,
) -> dict:
    """Serialize the LearningStore to a JSON-safe dict.

    Pruning is applied before serialization: records outside the age/count
    limits are dropped before writing to disk.

    If *active_window_ids* is provided, windows whose IDs are not in that
    set are omitted — this is the orphan-cleanup step for windows that
    were removed from the SmartShading configuration.

    If *target_adapter* is a TargetPositionAdapter, its state is serialized
    under the ``target_adaptations`` key.  The key is omitted when None.

    The returned dict is suitable for passing directly to
    ``hass.storage.Store.async_save()``.
    """
    windows: dict[str, dict] = {}

    # decision_ids referenced by an active pending outcome must never be pruned
    # or demoted (their outcome has not resolved yet).
    _protected_ids: set[str] = {
        po.decision_id for po in (pending_outcomes or []) if po.decision_id
    }

    for window_id in store.window_ids():
        if active_window_ids is not None and window_id not in active_window_ids:
            continue  # orphan — window removed from config

        # get_transitions/etc. return newest-first; prune expects oldest-first.
        transitions = list(reversed(store.get_transitions(window_id)))
        transitions = prune_by_age_and_count(
            transitions, config.transitions_max_age_days, config.transitions_max_per_window, now
        )

        overrides = list(reversed(store.get_overrides(window_id)))
        overrides = prune_by_age_and_count(
            overrides, config.overrides_max_age_days, config.overrides_max_per_window, now
        )

        snapshots = list(reversed(store.get_snapshots(window_id)))
        snapshots = prune_by_age_and_count(
            snapshots, config.snapshots_max_age_days, config.snapshots_max_per_window, now
        )

        # Legacy outcomes ring only (v1 / migrated).  v2 outcomes live embedded
        # in decision records; get_outcomes() returns the merged view, so to
        # avoid double-writing we serialize ONLY the legacy ring here.
        legacy_outcomes = list(reversed(_legacy_outcomes(store, window_id)))
        legacy_outcomes = prune_by_age_and_count(
            legacy_outcomes, config.outcomes_max_age_days, config.outcomes_max_per_window, now
        )

        # Decision records (LE 2.0) — apply retention/demotion before writing.
        decisions = list(reversed(store.get_decisions(window_id)))  # oldest-first
        decisions = classify_and_prune_decisions(decisions, now, _protected_ids)

        windows[window_id] = {
            "transitions": [_serialize_transition(r) for r in transitions],
            "overrides": [_serialize_override(r) for r in overrides],
            "snapshots": [_serialize_snapshot(r) for r in snapshots],
            "outcomes": [_serialize_outcome(r) for r in legacy_outcomes],
            "decisions": [_serialize_decision(r) for r in decisions],
        }

    # Pending outcomes (restart-safe; max one per window).  Only for active windows.
    pending_list: list[dict] = []
    if pending_outcomes:
        for po in pending_outcomes:
            if active_window_ids is None or po.window_id in active_window_ids:
                pending_list.append(po.to_dict())

    result: dict = {
        "version": LEARNING_STORE_VERSION,
        "schema_version": PAYLOAD_SCHEMA_V2,
        "exported_at": now.isoformat(),
        "windows": windows,
        "pending_outcomes": pending_list,
        "config_generations": config_generations or {"fingerprint_version": 1, "windows": {}},
    }

    if target_adapter is not None:
        try:
            result["target_adaptations"] = target_adapter.to_storage_dict()
        except Exception:
            _LOGGER.warning("Learning: failed to serialize target adaptations (non-fatal)")

    return result


def _legacy_outcomes(store: LearningStore, window_id: str) -> list[DecisionOutcome]:
    """Return only the legacy outcomes ring (newest-first), excluding outcomes
    embedded in decision records (which are persisted within those records)."""
    buf = store._outcomes.get(window_id)  # noqa: SLF001 — same package, intentional
    if buf is None:
        return []
    record_keys = {
        r.outcome.decision_timestamp
        for r in store.get_decisions(window_id)
        if r.outcome is not None
    }
    return [o for o in buf.get_all() if o.decision_timestamp not in record_keys]


# ---------------------------------------------------------------------------
# Deserialization helpers
# ---------------------------------------------------------------------------

def _parse_utc(ts_str: str) -> datetime:
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        # Naive timestamps must not exist in Phase 9D output, but guard anyway.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _deserialize_transition(window_id: str, d: dict) -> StateTransitionRecord:
    return StateTransitionRecord(
        timestamp=_parse_utc(d["timestamp"]),
        window_id=window_id,
        from_state=ShadingState(d["from_state"]),
        to_state=ShadingState(d["to_state"]),
        decided_by=d["decided_by"],
        lifecycle_state=d["lifecycle_state"],
        absence_active=bool(d["absence_active"]),
        is_in_solar_sector=bool(d["is_in_solar_sector"]),
        outdoor_temp_c=d.get("outdoor_temp_c"),
        indoor_temp_c=d.get("indoor_temp_c"),
        solar_radiation_wm2=d.get("solar_radiation_wm2"),
        wind_speed_ms=d.get("wind_speed_ms"),
        # Step 9F1 — None for records written before 9F1 (backward compatible)
        sun_azimuth=d.get("sun_azimuth"),
        sun_elevation=d.get("sun_elevation"),
        solar_relative_azimuth=d.get("solar_relative_azimuth"),
        # Step 9F2 — None for records written before 9F2 (backward compatible)
        weather_condition=d.get("weather_condition"),
        cloud_cover_pct=d.get("cloud_cover_pct"),
        raw_solar_radiation_wm2=d.get("raw_solar_radiation_wm2"),
        effective_exposure_wm2=d.get("effective_exposure_wm2"),
        learned_solar_impact_factor=d.get("learned_solar_impact_factor"),
    )


def _deserialize_override(window_id: str, d: dict) -> OverrideRecord:
    raw_state = d.get("overridden_state")
    return OverrideRecord(
        timestamp=_parse_utc(d["timestamp"]),
        window_id=window_id,
        event_type=d["event_type"],  # type: ignore[arg-type]
        lifecycle_state=d["lifecycle_state"],
        override_position=d.get("override_position"),
        overridden_state=ShadingState(raw_state) if raw_state is not None else None,
        overridden_position=d.get("overridden_position"),
        override_duration_min=d.get("override_duration_min"),
        outdoor_temp_c=d.get("outdoor_temp_c"),
        solar_radiation_wm2=d.get("solar_radiation_wm2"),
        # Step 9F3 — None for records written before 9F3 (backward compatible)
        decided_by=d.get("decided_by"),
        # Step 9F1 — None for records written before 9F1 (backward compatible)
        sun_azimuth=d.get("sun_azimuth"),
        sun_elevation=d.get("sun_elevation"),
        solar_relative_azimuth=d.get("solar_relative_azimuth"),
        # Step 9F2 — None for records written before 9F2 (backward compatible)
        weather_condition=d.get("weather_condition"),
        cloud_cover_pct=d.get("cloud_cover_pct"),
        raw_solar_radiation_wm2=d.get("raw_solar_radiation_wm2"),
        effective_exposure_wm2=d.get("effective_exposure_wm2"),
        learned_solar_impact_factor=d.get("learned_solar_impact_factor"),
    )


def _deserialize_snapshot(window_id: str, d: dict) -> WindowCycleSnapshot:
    return WindowCycleSnapshot(
        timestamp=_parse_utc(d["timestamp"]),
        window_id=window_id,
        shading_state=ShadingState(d["shading_state"]),
        decided_by=d["decided_by"],
        lifecycle_state=d["lifecycle_state"],
        absence_active=bool(d["absence_active"]),
        override_active=bool(d["override_active"]),
        target_position=d.get("target_position"),
        outdoor_temp_c=d.get("outdoor_temp_c"),
        indoor_temp_c=d.get("indoor_temp_c"),
        solar_radiation_wm2=d.get("solar_radiation_wm2"),
        effective_exposure_wm2=d.get("effective_exposure_wm2"),
        wind_speed_ms=d.get("wind_speed_ms"),
        # Step 9F1 — None for records written before 9F1 (backward compatible)
        sun_azimuth=d.get("sun_azimuth"),
        sun_elevation=d.get("sun_elevation"),
        solar_relative_azimuth=d.get("solar_relative_azimuth"),
        # Step 9F2 — None for records written before 9F2 (backward compatible)
        weather_condition=d.get("weather_condition"),
        cloud_cover_pct=d.get("cloud_cover_pct"),
        raw_solar_radiation_wm2=d.get("raw_solar_radiation_wm2"),
        learned_solar_impact_factor=d.get("learned_solar_impact_factor"),
    )


def _deserialize_outcome(window_id: str, d: dict) -> DecisionOutcome:
    raw_eval_ts = d.get("evaluation_timestamp")
    raw_from_state = d.get("from_state")
    return DecisionOutcome(
        decision_timestamp=_parse_utc(d["decision_timestamp"]),
        window_id=window_id,
        decided_state=ShadingState(d["decided_state"]),
        decided_by=d["decided_by"],
        indoor_temp_outcome_delay_min=int(d.get("indoor_temp_outcome_delay_min", 30)),
        # Step 9F4b-5 — "day" / None for records written before 9F4b-5 (backward compat)
        lifecycle_state=d.get("lifecycle_state", "day"),
        from_state=ShadingState(raw_from_state) if raw_from_state is not None else None,
        override_occurred=bool(d.get("override_occurred", False)),
        override_delay_min=d.get("override_delay_min"),
        override_event_type=d.get("override_event_type"),
        indoor_temp_at_decision=d.get("indoor_temp_at_decision"),
        indoor_temp_outcome_c=d.get("indoor_temp_outcome_c"),
        indoor_temp_delta_c=d.get("indoor_temp_delta_c"),
        state_duration_min=d.get("state_duration_min"),
        escalation_occurred=bool(d.get("escalation_occurred", False)),
        outcome_score=d.get("outcome_score"),
        resolution_status=d.get("resolution_status", "pending"),
        evaluation_timestamp=_parse_utc(raw_eval_ts) if raw_eval_ts is not None else None,
    )


@dataclass(frozen=True)
class RestoreExtras:
    """Non-store data recovered from the payload (P2).

    pending_outcomes are returned RAW — the coordinator applies the restart
    interruption gate (age / fingerprint / sensor revalidation) before any of
    them may resolve, so a long outage never fabricates an outcome.
    """

    pending_outcomes: list[PendingOutcome]
    config_generations: dict


def deserialize_into_learning_store(
    data: dict,
    store: LearningStore,
    config: LearningPersistenceConfig,
    now: datetime,
) -> RestoreExtras:
    """Validate, migrate, prune, and restore persisted data into *store*.

    Raises ValueError if the HA-store version field is missing/unknown — the
    caller must catch this and start with an empty store.

    Individual malformed records are skipped with a WARNING and do not
    prevent the rest of the data from being restored.

    Returns RestoreExtras (pending outcomes + config generations) for the
    coordinator to reconcile.  Pruning is applied after deserialization
    (belt-and-suspenders: disk data may predate a config change).
    """
    version = data.get("version")
    if version != LEARNING_STORE_VERSION:
        raise ValueError(
            f"Unknown learning storage version {version!r} "
            f"(expected {LEARNING_STORE_VERSION})"
        )

    # Route by payload schema version; migrate v1 → v2 in pure code.
    payload_version = detect_payload_version(data)
    if payload_version not in (PAYLOAD_SCHEMA_V1, PAYLOAD_SCHEMA_V2):
        raise ValueError(f"Unknown payload schema_version {payload_version!r}")
    if payload_version == PAYLOAD_SCHEMA_V1:
        data = migrate_v1_to_v2(data)

    # Parse pending outcomes FIRST so their referenced decision_ids are protected
    # from pruning/demotion when the per-window decision lists are restored.
    pending_outcomes: list[PendingOutcome] = []
    for i, d in enumerate(data.get("pending_outcomes", []) or []):
        try:
            pending_outcomes.append(PendingOutcome.from_dict(d))
        except Exception:
            _LOGGER.warning("Learning: skipping malformed pending outcome #%d", i)
    _protected_ids: set[str] = {po.decision_id for po in pending_outcomes if po.decision_id}

    windows: dict = data.get("windows", {})
    for window_id, streams in windows.items():
        if not isinstance(window_id, str) or not isinstance(streams, dict):
            _LOGGER.warning("Learning: skipping malformed window entry %r", window_id)
            continue

        # --- Transitions ---
        raw_transitions = streams.get("transitions", [])
        transitions: list[StateTransitionRecord] = []
        for i, d in enumerate(raw_transitions):
            try:
                transitions.append(_deserialize_transition(window_id, d))
            except Exception:
                _LOGGER.warning(
                    "Learning: skipping malformed transition #%d for %s", i, window_id
                )
        transitions = prune_by_age_and_count(
            transitions, config.transitions_max_age_days, config.transitions_max_per_window, now
        )
        for r in transitions:  # oldest-first → ring buffer fills correctly
            store.record_transition(r)

        # --- Overrides ---
        raw_overrides = streams.get("overrides", [])
        overrides: list[OverrideRecord] = []
        for i, d in enumerate(raw_overrides):
            try:
                overrides.append(_deserialize_override(window_id, d))
            except Exception:
                _LOGGER.warning(
                    "Learning: skipping malformed override #%d for %s", i, window_id
                )
        overrides = prune_by_age_and_count(
            overrides, config.overrides_max_age_days, config.overrides_max_per_window, now
        )
        for r in overrides:
            store.record_override(r)

        # --- Snapshots ---
        raw_snapshots = streams.get("snapshots", [])
        snapshots: list[WindowCycleSnapshot] = []
        for i, d in enumerate(raw_snapshots):
            try:
                snapshots.append(_deserialize_snapshot(window_id, d))
            except Exception:
                _LOGGER.warning(
                    "Learning: skipping malformed snapshot #%d for %s", i, window_id
                )
        snapshots = prune_by_age_and_count(
            snapshots, config.snapshots_max_age_days, config.snapshots_max_per_window, now
        )
        for r in snapshots:
            store.record_snapshot(r)

        # --- Outcomes (Step 9F4a) — absent in pre-9F4a storage files ---
        raw_outcomes = streams.get("outcomes", [])
        outcomes: list[DecisionOutcome] = []
        for i, d in enumerate(raw_outcomes):
            try:
                outcomes.append(_deserialize_outcome(window_id, d))
            except Exception:
                _LOGGER.warning(
                    "Learning: skipping malformed outcome #%d for %s", i, window_id
                )
        outcomes = prune_by_age_and_count(
            outcomes, config.outcomes_max_age_days, config.outcomes_max_per_window, now
        )
        for r in outcomes:
            store.record_outcome(r)

        # --- Decision records (LE 2.0 / P2) ---
        raw_decisions = streams.get("decisions", [])
        decisions: list[LearningDecisionRecord] = []
        for i, d in enumerate(raw_decisions):
            try:
                decisions.append(_deserialize_decision(window_id, d))
            except Exception:
                _LOGGER.warning(
                    "Learning: skipping malformed decision #%d for %s", i, window_id
                )
        # Belt-and-suspenders retention on restore (disk may predate config change).
        decisions = classify_and_prune_decisions(decisions, now, _protected_ids)
        if decisions:
            store.set_decisions(window_id, decisions)

    config_generations = data.get("config_generations") or {"fingerprint_version": 1, "windows": {}}

    return RestoreExtras(
        pending_outcomes=pending_outcomes,
        config_generations=config_generations,
    )


# ---------------------------------------------------------------------------
# HA-dependent adapter
# ---------------------------------------------------------------------------

class LearningPersistenceAdapter:
    """Thin async adapter between LearningStore and hass.storage.Store.

    The ``homeassistant`` import is deferred to __init__ so that the rest of
    this module (all pure functions) can be imported and tested without HA.
    """

    def __init__(
        self,
        hass: object,
        config: LearningPersistenceConfig,
        entry_id: str,
    ) -> None:
        from homeassistant.helpers.storage import Store  # type: ignore[import]

        storage_key = f"{LEARNING_STORAGE_KEY}_{entry_id}"
        self._store = Store(hass, LEARNING_STORE_VERSION, storage_key)
        self._config = config
        self._fresh_start: bool = False
        # P2 — set True when a v1 payload was migrated to v2 during restore.
        # The COORDINATOR owns the controlled one-shot save (never the deserializer).
        self._migration_dirty: bool = False
        # P2 — extras recovered from the last restore (pending outcomes + config).
        self._last_restore_extras: RestoreExtras | None = None

    @property
    def fresh_start(self) -> bool:
        """True when async_restore found no persisted file (first run after setup).

        Used by the coordinator to write an initial schema-valid storage file
        immediately after the first restore so that the file is visible in
        /config/.storage/ from the moment SmartShading is configured.
        """
        return self._fresh_start

    @property
    def migration_dirty(self) -> bool:
        """True when the last restore migrated a v1 payload to v2.

        The coordinator triggers ONE controlled async_save after successful
        setup and then clears the flag via clear_migration_dirty().
        """
        return self._migration_dirty

    def clear_migration_dirty(self) -> None:
        self._migration_dirty = False

    @property
    def last_restore_extras(self) -> "RestoreExtras | None":
        return self._last_restore_extras

    async def async_restore(
        self, store: LearningStore, now: datetime
    ) -> "object":
        """Load persisted data into *store*.

        Safe-restore invariant: any failure → WARNING + empty store/adapter, never raise.

        Returns the restored TargetPositionAdapter.  Pending outcomes and
        config generations are exposed via ``last_restore_extras``; a v1→v2
        migration sets ``migration_dirty`` (the coordinator owns the save).
        """
        from ..engines.target_position_adapter import TargetPositionAdapter

        target_adapter = TargetPositionAdapter()
        self._last_restore_extras = None
        self._migration_dirty = False
        try:
            data = await self._store.async_load()
        except Exception:
            _LOGGER.warning(
                "Learning: failed to read storage — starting with empty store"
            )
            return target_adapter
        if data is None:
            self._fresh_start = True
            _LOGGER.debug("Learning: no persisted data found, starting fresh")
            return target_adapter
        self._fresh_start = False
        try:
            needs_migration = detect_payload_version(data) == PAYLOAD_SCHEMA_V1
            extras = deserialize_into_learning_store(data, store, self._config, now)
            self._last_restore_extras = extras
            if needs_migration:
                # Mark for a single controlled save; never save from here.
                self._migration_dirty = True
            _LOGGER.debug("Learning: restored persisted learning data")
        except ValueError as exc:
            _LOGGER.warning("Learning: %s — starting with empty store", exc)
            return target_adapter
        except Exception:
            _LOGGER.warning(
                "Learning: failed to deserialize storage — starting with empty store"
            )
            return target_adapter

        raw_ta = data.get("target_adaptations")
        if raw_ta is not None:
            try:
                target_adapter = TargetPositionAdapter.from_storage_dict(raw_ta)
                _LOGGER.debug("Learning: restored target adaptation data")
            except Exception:
                _LOGGER.warning("Learning: failed to restore target adaptations (non-fatal)")

        return target_adapter

    async def async_save(
        self,
        store: LearningStore,
        active_window_ids: set[str],
        now: datetime,
        *,
        target_adapter: "object | None" = None,
        pending_outcomes: "list[PendingOutcome] | None" = None,
        config_generations: dict | None = None,
    ) -> None:
        """Prune and persist the current in-memory learning data.

        Orphan windows (not in *active_window_ids*) are excluded from the
        saved data.  Any failure is logged as WARNING and does not propagate.
        """
        try:
            data = serialize_learning_store(
                store, self._config, now,
                active_window_ids=active_window_ids,
                target_adapter=target_adapter,
                pending_outcomes=pending_outcomes,
                config_generations=config_generations,
            )
            await self._store.async_save(data)
        except Exception:
            _LOGGER.warning("Learning: failed to persist learning data (non-fatal)")
