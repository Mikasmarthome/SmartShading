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
from dataclasses import dataclass, field
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
from ..models.thermal_response import ThermalResponseModel, ThermalResponseObservation
from ..models.bounded_experiment import BoundedExperiment
from ..models.persistent_adoption import PersistentTargetAdoption
from ..models.strategy_learning import (
    BoundedStrategyExperiment,
    PersistentStrategyAdoption,
)
from ..models.shadow_proposal import ShadowProposal
from ..models.window_contribution import (
    WindowContributionEvidence,
    WindowContributionModel,
)
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


def _prune_support_events(events: list, now: datetime) -> list:
    """Keep support critical events < 48 h old and bounded to 500 records."""
    cutoff = now.timestamp() - 48 * 3600
    kept = []
    for e in events:
        ts = e.get("ts")
        if ts is None:
            continue
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.timestamp() >= cutoff:
                kept.append(e)
        except Exception:
            pass
    return kept[-500:]  # newest 500


def _prune_daily_buckets(buckets: dict, now: datetime) -> dict:
    """Keep daily research buckets within the last 365 days, capped at 365 entries."""
    cutoff = (now - timedelta(days=365)).date().isoformat()
    filtered = {k: v for k, v in buckets.items()
                if isinstance(k, str) and len(k) == 10 and k >= cutoff}
    if len(filtered) > 365:
        sorted_keys = sorted(filtered)
        filtered = {k: filtered[k] for k in sorted_keys[-365:]}
    return filtered


def serialize_learning_store(
    store: LearningStore,
    config: LearningPersistenceConfig,
    now: datetime,
    *,
    active_window_ids: set[str] | None = None,
    target_adapter: "object | None" = None,
    pending_outcomes: "list[PendingOutcome] | None" = None,
    config_generations: dict | None = None,
    thermal_models: dict | None = None,
    thermal_observations: dict | None = None,
    window_contribution_models: dict | None = None,
    window_contribution_evidence: dict | None = None,
    shadow_proposals: list | None = None,
    bounded_experiments: list | None = None,
    persistent_adoptions: list | None = None,
    strategy_experiments: list | None = None,
    persistent_strategy_adoptions: list | None = None,
    consumed_experiment_ledger: dict | None = None,
    shadow_tombstones: list | None = None,
    active_overrides: list | None = None,
    current_states: dict | None = None,
    config_snapshot: dict | None = None,
    owner_entry_id: str | None = None,
    owner_zone_id: str | None = None,
    support_critical_events: list | None = None,
    research_daily_buckets: dict | None = None,
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
        # P4 — per-zone thermal response models + bounded observations (additive).
        "thermal_response": thermal_models or {},
        "thermal_observations": thermal_observations or {},
        # P5 — per-window contribution models + bounded evidence (additive).
        "window_contribution_models": window_contribution_models or {},
        "window_contribution_evidence": window_contribution_evidence or {},
        # P6 — shadow proposals (analysis only; additive).
        "shadow_proposals": shadow_proposals or [],
        # P7 — bounded experiments (active + terminal history; additive).
        "bounded_experiments": bounded_experiments or [],
        # P8 — persistent target adoptions (active + terminal history; additive).
        "persistent_adoptions": persistent_adoptions or [],
        # P9B — bounded strategy experiments + persistent strategy adoptions.
        "strategy_experiments": strategy_experiments or [],
        "persistent_strategy_adoptions": persistent_strategy_adoptions or [],
        # P10 — permanent consumed-experiment ledger + ownership (v3 additive).
        "consumed_experiment_ledger": consumed_experiment_ledger or {},
        # P10 — compact shadow provenance tombstones (no full time series).
        "shadow_tombstones": shadow_tombstones or [],
        # Restart-safe active manual overrides (additive; bounded by expiry).
        "active_overrides": active_overrides or [],
        # Restart-safe per-window shading state (additive).  Without this, a
        # window's FSM state resets to the OPEN default on restart, which can
        # permanently strand an ABSENCE_ONLY/ABSENCE_AND_SCHEDULE window: the
        # absence-release gate requires current_state == ABSENCE_CLOSED, and a
        # forgotten ABSENCE_CLOSED can never be recovered without this restore.
        "current_states": (
            {wid: state.value for wid, state in (current_states or {}).items()}
        ),
        # P10 — normalised config snapshot for next-restore typed diff invalidation.
        "config_snapshot": config_snapshot or {},
        "owner_entry_id": owner_entry_id,
        "owner_zone_id": owner_zone_id,
        # P4c — persisted critical support events + daily research accumulation buckets.
        "support_critical_events": _prune_support_events(support_critical_events or [], now),
        "research_daily_buckets": _prune_daily_buckets(research_daily_buckets or {}, now),
        "created_by_domain": "smartshading",
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
    thermal_models: dict  # zone_id → ThermalResponseModel
    thermal_observations: dict  # zone_id → list[ThermalResponseObservation]
    window_contribution_models: dict  # window_id → WindowContributionModel
    window_contribution_evidence: dict  # window_id → list[WindowContributionEvidence]
    shadow_proposals: list  # list[ShadowProposal]
    bounded_experiments: list  # list[BoundedExperiment]
    persistent_adoptions: list  # list[PersistentTargetAdoption]
    strategy_experiments: list  # list[BoundedStrategyExperiment]
    persistent_strategy_adoptions: list  # list[PersistentStrategyAdoption]
    consumed_experiment_ledger: dict  # ConsumedExperimentLedger payload
    shadow_tombstones: list  # list[ShadowTombstone]
    owner_entry_id: str | None
    owner_zone_id: str | None
    restore_diagnostics: dict  # P10 structured per-section reason counts (privacy-safe)
    config_snapshot: dict  # P10 previous normalised config snapshot (for typed diff)
    active_overrides: list = field(default_factory=list)  # raw active-override dicts
    current_states: dict = field(default_factory=dict)  # window_id -> ShadingState.value
    support_critical_events: list = field(default_factory=list)  # P4c compact critical events
    research_daily_buckets: dict = field(default_factory=dict)  # P4c date → counts


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

    # P10 central per-section restore validation — THE single section-validation
    # authority.  Unsafe records are dropped BEFORE from_dict so they never become
    # adaptive authority; reason counters feed structured restore diagnostics.
    from .restore_validation import (
        merge_section_diagnostics, validate_keyed_models, validate_records,
        R_NAN_OR_INF, R_OUT_OF_RANGE, R_NEGATIVE_COUNT, R_INVALID_TIMESTAMP,
    )
    from .storage_validation import is_finite_number, parse_utc
    _sv: dict = {}

    # Parse pending outcomes FIRST so their referenced decision_ids are protected
    # from pruning/demotion when the per-window decision lists are restored.
    # Invariant: at most ONE active pending outcome per window — the uniquely newest
    # fully-valid record wins; ambiguous ties leave NO active pending for that window.
    _pending_res = validate_records(
        data.get("pending_outcomes", []), now=now, id_key=None,
        required_fields=("window_id",),
        required_timestamp_fields=("decision_timestamp",),
        timestamp_fields=("created_at_utc",))
    _by_window: dict = {}
    for _rec in _pending_res.valid_records:
        _by_window.setdefault(_rec.get("window_id"), []).append(_rec)
    _pending_unique: list = []
    for _wid, _recs in _by_window.items():
        if len(_recs) == 1:
            _pending_unique.append(_recs[0])
            continue
        _ordered = sorted(
            _recs, key=lambda r: parse_utc(r.get("decision_timestamp")), reverse=True)
        _t0 = parse_utc(_ordered[0].get("decision_timestamp"))
        _t1 = parse_utc(_ordered[1].get("decision_timestamp"))
        if _t0 == _t1:
            # ambiguous newest → no active pending outcome for this window
            _pending_res.ambiguous_count += len(_recs)
            _pending_res.ambiguous_records.extend(["pending"] * len(_recs))
            continue
        _pending_unique.append(_ordered[0])
    _sv["pending_outcomes"] = _pending_res
    pending_outcomes: list[PendingOutcome] = []
    for i, d in enumerate(_pending_unique):
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

        # --- Resolved outcomes (Step 9F4a) — central validation before parse.
        # window_id is implicit from the stream key (not stored on the record). ---
        _out_res = validate_records(
            streams.get("outcomes", []), now=now,
            required_fields=("decided_state",),
            required_timestamp_fields=("decision_timestamp",))
        _sv.setdefault("resolved_outcomes", validate_records([], now=now)).merge(_out_res)
        outcomes: list[DecisionOutcome] = []
        for i, d in enumerate(_out_res.valid_records):
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

        # --- Decision records (LE 2.0 / P2) — central validation before parse ---
        _dec_res = validate_records(
            streams.get("decisions", []), now=now, id_key="decision_id",
            required_fields=("decision_id",),
            required_timestamp_fields=("decision_timestamp",),
            expected_window_id=window_id, conflict_check=True,
            nonneg_count_fields=("config_generation",))
        _sv.setdefault("decisions", validate_records([], now=now)).merge(_dec_res)
        decisions: list[LearningDecisionRecord] = []
        for i, d in enumerate(_dec_res.valid_records):
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

    # --- P4 thermal response models + observations (central validation) ---
    def _bounds_validator(rec: dict):
        """Pattern-based defensive bounds: reliability/confidence ∈ [0,1];
        count/sample fields non-negative ints.  NaN/Inf already rejected upstream."""
        for k, v in rec.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            if ("reliability" in k or "confidence" in k) and not (0.0 <= float(v) <= 1.0):
                return R_OUT_OF_RANGE
            if ("count" in k or "sample" in k) and v < 0:
                return R_NEGATIVE_COUNT
        return None

    _tm_res = validate_keyed_models(
        data.get("thermal_response"), now=now, value_validator=_bounds_validator)
    _sv["thermal_models"] = _tm_res
    thermal_models: dict = {}
    for zid, raw_model in _tm_res.valid_records:
        try:
            thermal_models[zid] = ThermalResponseModel.from_dict(raw_model)
        except Exception:
            _LOGGER.warning("Learning: skipping malformed thermal model for %s", zid)
    thermal_observations: dict = {}
    _to_agg = validate_records([], now=now)
    for zid, raw_list in (data.get("thermal_observations") or {}).items():
        _to_res = validate_records(
            raw_list, now=now, timestamp_fields=("observed_at", "created_at", "timestamp"))
        _to_agg.merge(_to_res)
        obs_list: list[ThermalResponseObservation] = []
        for i, raw in enumerate(_to_res.valid_records):
            try:
                obs_list.append(ThermalResponseObservation.from_dict(raw))
            except Exception:
                _LOGGER.warning("Learning: skipping malformed thermal obs #%d for %s", i, zid)
        if obs_list:
            thermal_observations[zid] = obs_list
    _sv["thermal_observations"] = _to_agg

    # --- P5 window contribution models + evidence (additive, optional) ---
    _cm_res = validate_keyed_models(
        data.get("window_contribution_models"), now=now, value_validator=_bounds_validator)
    _sv["window_contribution_models"] = _cm_res
    contribution_models: dict = {}
    for wid, raw_model in _cm_res.valid_records:
        try:
            contribution_models[wid] = WindowContributionModel.from_dict(raw_model)
        except Exception:
            _LOGGER.warning("Learning: skipping malformed contribution model for %s", wid)
    contribution_evidence: dict = {}
    _ce_agg = validate_records([], now=now)
    for wid, raw_list in (data.get("window_contribution_evidence") or {}).items():
        _ce_res = validate_records(
            raw_list, now=now, expected_window_id=wid,
            timestamp_fields=("created_at", "updated_at", "timestamp"))
        _ce_agg.merge(_ce_res)
        ev_list: list[WindowContributionEvidence] = []
        for i, raw in enumerate(_ce_res.valid_records):
            try:
                ev_list.append(WindowContributionEvidence.from_dict(raw))
            except Exception:
                _LOGGER.warning("Learning: skipping malformed contribution evidence #%d for %s", i, wid)
        if ev_list:
            contribution_evidence[wid] = ev_list
    _sv["window_contribution_evidence"] = _ce_agg

    # P10: config_generations validation — generation must be a non-negative int
    # (no bool), windows mapping well-formed; a corrupt entry must not make stale
    # authority look current (the conservative config_generation gate still holds).
    _cg_raw = data.get("config_generations") or {"fingerprint_version": 1, "windows": {}}
    _cg_res = validate_records([], now=now)
    _cg_windows = _cg_raw.get("windows") if isinstance(_cg_raw, dict) else None
    if isinstance(_cg_windows, dict):
        for _wid, _entry in _cg_windows.items():
            gen = _entry.get("generation") if isinstance(_entry, dict) else _entry
            if isinstance(gen, bool) or not isinstance(gen, int) or gen < 0:
                _cg_res._bump(R_NEGATIVE_COUNT if isinstance(gen, int) else R_INVALID_TIMESTAMP)
    _sv["config_generations"] = _cg_res

    # --- P6 shadow proposals (additive, optional) ---
    _sv["shadow_proposals"] = validate_records(
        data.get("shadow_proposals", []), now=now, id_key="shadow_id",
        timestamp_fields=("created_at", "updated_at"))
    shadow_proposals: list = []
    for i, raw in enumerate(_sv["shadow_proposals"].valid_records):
        try:
            shadow_proposals.append(ShadowProposal.from_dict(raw))
        except Exception:
            _LOGGER.warning("Learning: skipping malformed shadow proposal #%d", i)

    # --- P7 bounded experiments (additive, optional) ---
    _sv["position_experiments"] = validate_records(
        data.get("bounded_experiments", []), now=now, id_key="experiment_id",
        timestamp_fields=("created_at", "updated_at", "completed_at"))
    bounded_experiments: list = []
    for i, raw in enumerate(_sv["position_experiments"].valid_records):
        try:
            bounded_experiments.append(BoundedExperiment.from_dict(raw))
        except Exception:
            _LOGGER.warning("Learning: skipping malformed bounded experiment #%d", i)

    # --- P8 persistent adoptions (additive, optional) ---
    _sv["position_adoptions"] = validate_records(
        data.get("persistent_adoptions", []), now=now, id_key="adoption_id",
        timestamp_fields=("created_at", "updated_at"), reject_negative_counts=True)
    persistent_adoptions: list = []
    for i, raw in enumerate(_sv["position_adoptions"].valid_records):
        try:
            persistent_adoptions.append(PersistentTargetAdoption.from_dict(raw))
        except Exception:
            _LOGGER.warning("Learning: skipping malformed persistent adoption #%d", i)

    # --- P9B strategy experiments + adoptions (additive, optional) ---
    _sv["strategy_experiments"] = validate_records(
        data.get("strategy_experiments", []), now=now, id_key="experiment_id",
        timestamp_fields=("created_at", "updated_at", "completed_at"))
    strategy_experiments: list = []
    for i, raw in enumerate(_sv["strategy_experiments"].valid_records):
        try:
            strategy_experiments.append(BoundedStrategyExperiment.from_dict(raw))
        except Exception:
            _LOGGER.warning("Learning: skipping malformed strategy experiment #%d", i)
    _sv["strategy_adoptions"] = validate_records(
        data.get("persistent_strategy_adoptions", []), now=now, id_key="adoption_id",
        timestamp_fields=("created_at", "updated_at"), reject_negative_counts=True)
    persistent_strategy_adoptions: list = []
    for i, raw in enumerate(_sv["strategy_adoptions"].valid_records):
        try:
            persistent_strategy_adoptions.append(PersistentStrategyAdoption.from_dict(raw))
        except Exception:
            _LOGGER.warning("Learning: skipping malformed strategy adoption #%d", i)

    # --- P10 shadow provenance tombstones (additive, optional) ---
    from ..models.shadow_tombstone import ShadowTombstone
    _sv["position_tombstones"] = validate_records(
        data.get("shadow_tombstones", []), now=now, id_key="shadow_id",
        timestamp_fields=("created_at", "expires_at"))
    shadow_tombstones: list = []
    for i, raw in enumerate(_sv["position_tombstones"].valid_records):
        try:
            shadow_tombstones.append(ShadowTombstone.from_dict(raw))
        except Exception:
            _LOGGER.warning("Learning: skipping malformed shadow tombstone #%d", i)

    restore_diagnostics = merge_section_diagnostics(_sv)

    # P4c — persisted support critical events (non-authority, additive).
    _raw_se = data.get("support_critical_events") or []
    support_critical_events: list = [
        e for e in _raw_se
        if isinstance(e, dict) and "ts" in e and "event_type" in e
    ]

    # P4c — daily research accumulation buckets (non-authority, additive).
    _raw_db = data.get("research_daily_buckets") or {}
    research_daily_buckets: dict = {
        k: v for k, v in _raw_db.items()
        if isinstance(k, str) and isinstance(v, dict)
    }

    # Restart-safe per-window shading state (additive). Raw {window_id: value}
    # dict; the coordinator restores it into _current_states before the first
    # dispatch decision. An unknown/corrupt value is skipped individually —
    # that window simply falls back to the existing OPEN default, never raises.
    _raw_cs = data.get("current_states") or {}
    _valid_state_values = {s.value for s in ShadingState}
    current_states: dict = {
        k: v for k, v in _raw_cs.items()
        if isinstance(k, str) and isinstance(v, str) and v in _valid_state_values
    }

    return RestoreExtras(
        pending_outcomes=pending_outcomes,
        config_generations=config_generations,
        thermal_models=thermal_models,
        thermal_observations=thermal_observations,
        window_contribution_models=contribution_models,
        window_contribution_evidence=contribution_evidence,
        shadow_proposals=shadow_proposals,
        bounded_experiments=bounded_experiments,
        persistent_adoptions=persistent_adoptions,
        strategy_experiments=strategy_experiments,
        persistent_strategy_adoptions=persistent_strategy_adoptions,
        consumed_experiment_ledger=(data.get("consumed_experiment_ledger") or {}),
        shadow_tombstones=shadow_tombstones,
        owner_entry_id=data.get("owner_entry_id"),
        owner_zone_id=data.get("owner_zone_id"),
        restore_diagnostics=restore_diagnostics,
        config_snapshot=(data.get("config_snapshot") or {}),
        # Raw active-override dicts; the coordinator restores them into the
        # override detector (validating expiry) before the first dispatch.
        active_overrides=(data.get("active_overrides") or []),
        current_states=current_states,
        support_critical_events=support_critical_events,
        research_daily_buckets=research_daily_buckets,
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
        self._entry_id = entry_id          # P10: payload ownership validation
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
        # P10: THE single authoritative migration front-door.  migrate_payload is
        # the one schema gate every restored payload passes through: it rejects an
        # unknown-newer or malformed-root payload (→ baseline only, no adaptive
        # authority) and additively normalises v1/v2 → current with owner.  The
        # record-level legacy reconstruction (v1 outcome→decision rebuild) then
        # runs inside deserialize on the ORIGINAL payload so it is never bypassed —
        # one entry, two complementary layers, no second schema authority.
        from .learning_migration import migrate_payload
        mres = migrate_payload(data, owner_entry_id=getattr(self, "_entry_id", None))
        if not mres.accept_authority:
            _LOGGER.warning(
                "Learning: payload not acceptable (%s) — starting with empty store "
                "(no adaptive authority)", mres.reason)
            return target_adapter
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
        thermal_models: dict | None = None,
        thermal_observations: dict | None = None,
        window_contribution_models: dict | None = None,
        window_contribution_evidence: dict | None = None,
        shadow_proposals: list | None = None,
        bounded_experiments: list | None = None,
        persistent_adoptions: list | None = None,
        strategy_experiments: list | None = None,
        persistent_strategy_adoptions: list | None = None,
        consumed_experiment_ledger: dict | None = None,
        shadow_tombstones: list | None = None,
        active_overrides: list | None = None,
        current_states: dict | None = None,
        config_snapshot: dict | None = None,
        owner_zone_id: str | None = None,
        support_critical_events: list | None = None,
        research_daily_buckets: dict | None = None,
    ) -> bool:
        """Prune and persist the current in-memory learning data.

        Orphan windows (not in *active_window_ids*) are excluded from the saved
        data.  Returns True on success, False on failure (P10: callers use this to
        preserve the dirty generation on a failed save).  Never raises."""
        try:
            data = serialize_learning_store(
                store, self._config, now,
                active_window_ids=active_window_ids,
                target_adapter=target_adapter,
                pending_outcomes=pending_outcomes,
                config_generations=config_generations,
                thermal_models=thermal_models,
                thermal_observations=thermal_observations,
                window_contribution_models=window_contribution_models,
                window_contribution_evidence=window_contribution_evidence,
                shadow_proposals=shadow_proposals,
                bounded_experiments=bounded_experiments,
                persistent_adoptions=persistent_adoptions,
                strategy_experiments=strategy_experiments,
                persistent_strategy_adoptions=persistent_strategy_adoptions,
                consumed_experiment_ledger=consumed_experiment_ledger,
                shadow_tombstones=shadow_tombstones,
                active_overrides=active_overrides,
                current_states=current_states,
                config_snapshot=config_snapshot,
                owner_entry_id=getattr(self, "_entry_id", None),
                owner_zone_id=owner_zone_id,
                support_critical_events=support_critical_events,
                research_daily_buckets=research_daily_buckets,
            )
            await self._store.async_save(data)
        except Exception:
            _LOGGER.warning("Learning: failed to persist learning data (non-fatal)")
            return False
        return True
