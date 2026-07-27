"""Support Export v3 — LE 2.0 / Phase P11.8 (read-only, HA-free, duck-typed).

Assembles a privacy-safe, pseudonymized, bounded support export from the existing
P11 contracts (current input/learning/decision/dispatch traces + storage/health).
Deny-by-default allowlist builders; HMAC pseudonymization of every raw id; record
caps + byte cap with deterministic oldest-first truncation; never-raise per
section.  Never mutates runtime state, never triggers a save, never recomputes a
decision.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .diagnostics_privacy import (
    DEFAULT_FORBIDDEN_MARKERS,
    MAX_NESTED_DEPTH,
    MAX_STRING_LENGTH,
    NS_ADOPTION,
    NS_COVER,
    NS_DECISION,
    NS_ENTRY,
    NS_EXPERIMENT,
    NS_WINDOW,
    NS_ZONE,
    Pseudonymizer,
    cap_records,
    contains_forbidden_substring,
    enforce_depth,
    is_json_safe,
    pseudonymization_metadata,
    truncate_strings,
)
from . import decision_record as dr
from . import learning_trace_builder as ltb
from . import reason_codes as rc
from .explainability import build_decision_explanation
from ..models.runtime_mode import derive_authority

SUPPORT_EXPORT_SCHEMA_VERSION: int = 4

# T21 Phase D3: two supported detail levels for the support export.
# "standard" (default) is a compacted, support-case-oriented view; "extended"
# retains the full internal detail the export has always produced (renamed,
# not shrunk, from what was previously the only shape — see
# _compact_for_standard()). Both project the SAME already-built canonical
# data; Standard is never a second independently-computed truth.
_DETAIL_LEVELS = frozenset({"standard", "extended"})


def _normalize_detail_level(detail_level) -> str:
    """Never raises, never rejects a service call: an unrecognized/absent
    value quietly falls back to "standard" rather than erroring out — a
    service call made without detail_level (pre-D3 behaviour) must keep
    working and yield the Standard export."""
    return detail_level if detail_level in _DETAIL_LEVELS else "standard"

# beta.10: raised so a support export can carry roughly the last 24 h of decisions
# and no-dispatch holds per zone (≈1 decision / 5 min per window), which is what a
# user needs to analyse an evening field test the next morning.  cap_records keeps
# the NEWEST records, so the recent test stays in; the byte cap
# (MAX_SUPPORT_EXPORT_BYTES) and history_metadata (oldest/newest/truncated) still
# bound and describe the actual exported window.
MAX_SUPPORT_DECISIONS_PER_ZONE = 300

# Support timeline: max structured events before noise compression (same_position) and
# before byte-cap truncation.  Only significant events survive same_position compression.
MAX_SUPPORT_TIMELINE_EVENTS = 200

# T10: plain-language "what is this override still waiting for" per release
# strategy, for prov["manual_override"]["waiting_on"] — support-readable
# without needing to know the OverrideReleaseStrategy enum.
_OVERRIDE_WAITING_ON = {
    "duration": "configured duration elapses",
    "fixed_time": "configured clock time reached",
    "lifecycle": "next lifecycle transition (e.g. day/night change)",
    "first_comfort": "next automatic Comfort decision",
    "first_protection": "next automatic Protection decision",
    "first_any_decision": "next automatic decision of any kind",
    "manual": "explicit manual clear",
}

# T21 Phase D: event types GUARANTEED to survive the MAX_SUPPORT_TIMELINE_EVENTS
# cap (filled into the cap first, before any non-guaranteed event) — a genuine
# one-shot occurrence a support reader must never lose to truncation. Narrower
# than the old _CRITICAL_EVENT_TYPES: repeated identical holds (behavior_hold,
# presence_hold) were previously guaranteed too, which is exactly the noise
# T21 Phase D's timeline aggregation exists to fix — an actual hold->something-else
# transition already produces its own event with a different aggregation key
# (see _aggregate_repeated_events()), so it survives without needing a blanket
# guarantee on the whole event_type.
_GUARANTEED_EVENT_TYPES = frozenset({
    "dispatch_sent", "dispatch_failed", "safety", "manual_override",
    "absence", "night_transition",
})

# Event types that must never be collapsed into an occurrence-count aggregate,
# even if two consecutive ones happen to share an identical aggregation key —
# each is a genuine one-shot occurrence, not a repeated "still holding" state.
_NEVER_AGGREGATE_EVENT_TYPES = frozenset({"dispatch_sent", "dispatch_failed"})

# T21 Phase D severity model (see ARCHITECTURE / T21 Phase D report): a small,
# explicit event_type -> severity mapping instead of a single is_critical bool.
# "critical" is reserved for genuine safety-relevant/system-wide failures that
# do not exist among today's decision-trace-derived event types (none of the
# current classifications rise to that bar per the T21 Phase D ownership
# analysis) — never emitted by _classify_severity() today, listed here only so
# the full 5-level model is documented in one place.
_EVENT_SEVERITY = {
    "dispatch_sent": "info",
    "dispatch_failed": "error",
    "safety": "state_change",
    "manual_override": "state_change",
    "absence": "state_change",
    "night_transition": "state_change",
    "command_blocked": "warning",
    "recommendation_only": "info",
    "behavior_hold": "info",
    "presence_hold": "info",
    "min_interval": "info",
    "startup_grace": "info",
    "no_change": "info",
}


def _classify_severity(event_type: str) -> str:
    """Never raises: an unrecognized event_type (e.g. a future addition this
    table hasn't caught up with yet) defaults to "info" rather than silently
    escalating to something alarming."""
    return _EVENT_SEVERITY.get(event_type, "info")


def _aggregate_repeated_events(events_newest_first: list[dict]) -> list[dict]:
    """T21 Phase D: collapse consecutive events sharing an identical
    (window_ref, event_type, decided_by, reason, target_ha, shading_state) key
    into one record carrying first_seen/last_seen/occurrence_count, instead of
    emitting one nearly-identical record per evaluation cycle.

    `events_newest_first` must already be sorted newest-first (the caller's
    existing order). A run breaks — and aggregation starts fresh — the moment
    any key field differs, so an actual state change (different reason/
    decided_by/target/shading_state) is never folded into a prior hold's
    aggregate; it always produces its own separate event. Events whose
    event_type is in _NEVER_AGGREGATE_EVENT_TYPES are never collapsed, even
    if adjacent ones happen to share a key.
    """
    def _key(evt: dict):
        return (
            evt.get("window_ref"), evt.get("event_type"), evt.get("decided_by"),
            evt.get("reason"), evt.get("target_ha"), evt.get("shading_state"),
        )

    out: list[dict] = []
    i = 0
    n = len(events_newest_first)
    while i < n:
        evt = events_newest_first[i]
        if evt.get("event_type") in _NEVER_AGGREGATE_EVENT_TYPES:
            out.append(evt)
            i += 1
            continue
        run_key = _key(evt)
        j = i + 1
        while j < n and _key(events_newest_first[j]) == run_key:
            j += 1
        run = events_newest_first[i:j]
        if len(run) == 1:
            out.append(evt)
        else:
            # run is newest-first: run[0] is last_seen, run[-1] is first_seen.
            aggregated = dict(run[0])
            aggregated["occurrence_count"] = len(run)
            aggregated["first_seen"] = run[-1].get("ts")
            aggregated["first_seen_local"] = run[-1].get("local_ts")
            aggregated["last_seen"] = run[0].get("ts")
            aggregated["last_seen_local"] = run[0].get("local_ts")
            out.append(aggregated)
        i = j
    return out


# Decision no_dispatch.primary_reason values that are same-position noise.
_SAME_POS_REASONS = frozenset({"same_position", "same_position_no_change"})
MAX_SUPPORT_DISPATCHES_PER_ZONE = 200
MAX_SUPPORT_NO_DISPATCHES_PER_ZONE = 300
MAX_SUPPORT_OUTCOMES_PER_ZONE = 100
MAX_SUPPORT_LEARNING_TRANSITIONS_PER_ZONE = 100
MAX_SUPPORT_STORAGE_EVENTS_PER_ZONE = 50
MAX_SUPPORT_EXPORT_BYTES = 2_000_000
MAX_SUPPORT_STRING_LENGTH = MAX_STRING_LENGTH
MAX_SUPPORT_NESTED_DEPTH = MAX_NESTED_DEPTH

# Truncation order: history sections shed oldest-first BEFORE current snapshots.
_HISTORY_SECTIONS = (
    "recent_decisions", "recent_dispatches", "recent_no_dispatches",
    "recent_outcomes", "recent_learning_transitions",
)


def _iso_s(dt) -> str | None:
    """ISO UTC to seconds (no microseconds), else None."""
    try:
        if dt is None:
            return None
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        return None


def _iso_local(dt, tz: ZoneInfo | None) -> str | None:
    """F24: ISO local-time (to seconds), else None.  Additive sibling to
    _iso_s — UTC stays the source of truth everywhere; this is display-only.
    """
    if tz is None:
        return None
    try:
        if dt is None:
            return None
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).replace(microsecond=0).isoformat()
    except Exception:
        return None


def _resolve_home_tz(coordinator) -> tuple[ZoneInfo | None, str | None, str]:
    """F24: best-effort HA-configured timezone, duck-typed off coordinator.hass.

    Never raises.  Returns (tz_or_None, tz_name_or_None, timezone_source) where
    timezone_source documents provenance for the export reader — matches the
    existing guarded getattr(c, "hass", ...) pattern already used elsewhere in
    this module (e.g. the rain-sensor-availability check) rather than importing
    homeassistant.util.dt, keeping this module HA-import-free.
    """
    tz_name = getattr(getattr(getattr(coordinator, "hass", None), "config", None),
                       "time_zone", None)
    if not isinstance(tz_name, str) or not tz_name:
        return None, None, "unavailable"
    try:
        return ZoneInfo(tz_name), tz_name, "hass_config"
    except Exception:
        return None, tz_name, "invalid"


def _safe(fn, errors, name, default=None):
    try:
        return fn()
    except Exception:
        e = errors.setdefault(name, {"count": 0, "reason_codes": ["section_builder_failed"]})
        e["count"] += 1
        return default if default is not None else {"section_error": True}


def _support_history_metadata(recent_dec, dec_trunc) -> dict:
    """Transparent span/coverage for the support export.  The support export
    reads the *runtime recent* decision ring (bounded, reset on restart), NOT the
    persisted learning history — so this is labelled store_scope=runtime_recent
    and its span starts at the last restart/reload, by design."""
    stamps = sorted(r.get("decision_timestamp_utc") for r in recent_dec
                    if r.get("decision_timestamp_utc"))
    truncated = bool((dec_trunc or {}).get("truncated"))
    return {
        "store_scope": "runtime_recent",
        "scope_note": "recent in-memory decision ring; resets on restart/reload",
        "requested_window_h": 24,
        "full_window_covered": False,
        "coverage_scope": "since_restart",
        "since_restart_only": True,
        "history_not_persistent": True,
        "oldest_record_utc": stamps[0] if stamps else None,
        "newest_record_utc": stamps[-1] if stamps else None,
        "records_exported": len(recent_dec),
        "truncated": truncated,
        "cap_reason": "per_zone_recent_cap" if truncated else "within_cap",
    }


# ---------------------------------------------------------------------------
# T21 Phase D3: Standard-detail compaction — pure post-hoc projections of the
# already-built (Extended-shaped) contract sections. These never recompute or
# re-derive anything; they only drop fields the D3 ticket classified as
# redundant/duplicate/deep-debug-only for a support-case reader, so Standard
# and Extended can never disagree about a shared value (both read the exact
# same canonical data the rest of this module already produced).
# ---------------------------------------------------------------------------

def _compact_solar_provenance(solar: dict) -> dict:
    if not isinstance(solar, dict):
        return {}
    out: dict = {}
    for key in (
        "selected_solar_source", "raw_measured_solar_w_m2", "effective_exposure_w_m2",
        "solar_source_quality", "fallback_used", "solar_fallback_reason",
        "cloud_not_applied_reason", "direct_exposure_blocked",
        "geometrically_in_solar_sector", "glare_active", "glare_suppressed_reason",
    ):
        v = solar.get(key)
        if v is not None:
            out[key] = v
    sector = solar.get("manual_sector_result")
    if sector is not None and sector != "not_recorded":
        out["sun_sector_result"] = sector
    seasonal = solar.get("seasonal_factor")
    if seasonal is not None and seasonal != 1.0:
        out["seasonal_factor"] = seasonal
    return out


def _compact_threshold_provenance(threshold: dict) -> dict:
    if not isinstance(threshold, dict):
        return {}
    if threshold.get("recording_status") == "not_recorded":
        return {"recording_status": "not_recorded"}
    entries: dict = {}
    for tier, e in (threshold.get("entry_thresholds") or {}).items():
        if not isinstance(e, dict):
            continue
        compact = {"effective_entry_threshold_w_m2": e.get("effective_entry_threshold_w_m2")}
        learned = e.get("entry_learned_delta_w_m2")
        if learned:
            compact["entry_learned_delta_w_m2"] = learned
        forecast = e.get("entry_forecast_delta_w_m2")
        if forecast:
            compact["entry_forecast_delta_w_m2"] = forecast
        entries[tier] = compact
    out = {
        "exposure_value_compared_w_m2": threshold.get("exposure_value_compared_w_m2"),
        "entry_thresholds": entries,
    }
    if threshold.get("forecast_trust_level") is not None:
        out["forecast_trust_level"] = threshold["forecast_trust_level"]
    return out


def _compact_input_entry(entry: dict) -> dict:
    if not isinstance(entry, dict):
        return entry
    out: dict = {}
    if entry.get("window_ref") is not None:
        out["window_ref"] = entry["window_ref"]
    indoor = entry.get("indoor_temperature") or {}
    if indoor.get("value_c") is not None:
        out["indoor_temperature_c"] = indoor["value_c"]
    outdoor = entry.get("outdoor_temperature") or {}
    if outdoor.get("value_c") is not None:
        out["outdoor_temperature_c"] = outdoor["value_c"]
    out["solar"] = _compact_solar_provenance(entry.get("solar") or {})
    out["threshold"] = _compact_threshold_provenance(entry.get("threshold_provenance") or {})
    # contact/rain/heat/manual_override are already compact and carry
    # meaningful falsy values (active: false etc.) — kept verbatim.
    for key in ("contact", "rain", "heat", "manual_override"):
        if key in entry:
            out[key] = entry[key]
    return out


def _compact_position_learning_entry(entry: dict) -> dict:
    """Standard shape: summary / active_effects / blocked_effects / integrity
    — replaces the always-full-3-intensity present:false / null-field
    structure with only the intensities that actually have something to
    report (T21 Phase D3: 'irrelevante Intensitäten fehlen')."""
    if not isinstance(entry, dict):
        return entry
    intensities = entry.get("intensities") or {}
    active_effects: dict = {}
    blocked_effects: dict = {}
    for name, intensity in intensities.items():
        if not isinstance(intensity, dict):
            continue
        adoption = intensity.get("active_adoption") or {}
        if adoption.get("present"):
            eff = {
                "status": adoption.get("status"),
                "effective_delta_ha": adoption.get("effective_delta_ha"),
                "confidence": adoption.get("confidence"),
                "reliability": adoption.get("reliability"),
            }
            exp_delta = intensity.get("experiment_delta_ha")
            if exp_delta:
                eff["experiment_delta_ha"] = exp_delta
            active_effects[name] = {k: v for k, v in eff.items() if v is not None}
        blocked = intensity.get("blocked_reason")
        if blocked:
            blocked_effects[name] = {
                "gate_reason": blocked,
                "rollback_reason": adoption.get("rollback_reason"),
            }
    return {
        "summary": {
            "intensities_with_active_effect": sorted(active_effects),
            "intensities_with_blocked_effect": sorted(blocked_effects),
        },
        "active_effects": active_effects,
        "blocked_effects": blocked_effects,
        "integrity": entry.get("ledger_integrity_state"),
    }


def _compact_snapshot_entry(entry: dict) -> dict:
    """Runtime Snapshot cleanup (T21 Phase D3): keep only genuine
    current-state fields not already owned by the Decision Record
    (current_decisions/target_chain) — decided_by, target-chain stages,
    dispatch metrics, and solar provenance are dropped here since they are
    already exported (compactly) elsewhere for Standard."""
    if not isinstance(entry, dict):
        return entry
    if not entry.get("data_available"):
        return {
            "window_ref": entry.get("window_ref"),
            "data_available": False,
            "reason": entry.get("reason"),
            "current_state_age_seconds": entry.get("current_state_age_seconds"),
            "last_dispatch_age_seconds": entry.get("last_dispatch_age_seconds"),
        }
    keep = (
        "window_ref", "data_available", "actual_position_ha", "target_position_ha",
        "cover_available", "contact_status", "night_contact_blocked", "lifecycle_state",
        "is_safety", "rain_safe_active", "last_command_status", "is_recommendation_only",
        "current_state_age_seconds", "last_dispatch_age_seconds",
    )
    return {k: entry.get(k) for k in keep}


# Sections that are deep/debug-only detail — present in Extended, absent from
# Standard entirely (their essential facts are already surfaced compactly
# elsewhere: current_decisions/support_timeline cover explainability's role;
# position_learning's compact summary covers adaptation_trace's "what
# changed").
_STANDARD_DROPPED_SECTIONS = (
    "explainability", "recent_decisions", "recent_dispatches", "recent_no_dispatches",
    "recent_outcomes", "recent_learning_transitions", "storage", "adaptation_trace",
)


def _compact_for_standard(contract: dict) -> dict:
    """Project the already-assembled (Extended-shaped) contract down to the
    Standard shape. Pure post-hoc projection of the same canonical data —
    never recomputes anything, so Standard can never disagree with Extended
    about a shared value."""
    for key in _STANDARD_DROPPED_SECTIONS:
        contract.pop(key, None)
    inputs = contract.get("inputs")
    if isinstance(inputs, dict):
        contract["inputs"] = {k: _compact_input_entry(v) for k, v in inputs.items()}
    pl = contract.get("position_learning")
    if isinstance(pl, dict):
        contract["position_learning"] = {
            k: _compact_position_learning_entry(v) for k, v in pl.items()
        }
    snap = contract.get("current_snapshot")
    if isinstance(snap, dict):
        contract["current_snapshot"] = {
            k: _compact_snapshot_entry(v) for k, v in snap.items()
        }
    return contract


def build_support_export_v3(
    coordinator, *, now=None, integration_version="unknown", detail_level="standard",
) -> dict:
    """Build the v3 support export for one config entry / its zone."""
    now = now or datetime.now(timezone.utc)
    detail_level = _normalize_detail_level(detail_level)
    c = coordinator
    entry_id = getattr(getattr(c, "config_entry", None), "entry_id", None)
    pz = Pseudonymizer(entry_id)
    errors: dict = {}
    home_tz, home_tz_name, tz_source = _resolve_home_tz(c)

    def _wref(wid):
        return pz.ref(NS_WINDOW, wid)

    def _meta():
        return pseudonymization_metadata(stability_scope="config_entry")

    def _system():
        return {
            "zone_ref": pz.ref(NS_ZONE, next(iter(getattr(c, "zones", {}) or {}), None)),
            "entry_ref": pz.ref(NS_ENTRY, entry_id),
            "zone_count": len(getattr(c, "zones", {}) or {}),
            "window_count": len(getattr(c, "windows", {}) or {}),
            "cover_count": len(getattr(c, "cover_groups", {}) or {}),
        }

    def _configuration():
        windows = []
        for wid, w in (getattr(c, "windows", {}) or {}).items():
            windows.append({
                "window_ref": _wref(wid),
                "cover_ref": pz.ref(NS_COVER, getattr(w, "cover_group_id", None)),
                "behavior_mode": str(getattr(w, "behavior_mode", None)),
                "orientation_category": _orientation_category(getattr(w, "azimuth", None)),
                "manual_sector_configured": getattr(w, "manual_sun_sector_start_deg", None) is not None,
                "obstruction_configured": bool(getattr(w, "obstruction_zones", None)),
            })
        zone_id = next(iter(getattr(c, "zones", {}) or {}), None)
        la = ltb.build_learning_authority(c, next(iter(getattr(c, "windows", {}) or {}), "")) \
            if getattr(c, "windows", None) else {}
        _auth = derive_authority(
            bool(la.get("learning_enabled")), bool(la.get("active_control_enabled")))
        return {
            "zone_ref": pz.ref(NS_ZONE, zone_id),
            "window_count": len(windows),
            "runtime_mode": _auth.mode.value,
            "learning_enabled": la.get("learning_enabled"),
            "active_control_enabled": la.get("active_control_enabled"),
            "adaptive_reads_allowed": _auth.adaptive_reads_allowed,
            "real_control_allowed": _auth.real_control_allowed,
            "experiments_allowed": _auth.experiments_allowed,
            "indoor_temperature_configured": bool(
                getattr(c, "_indoor_temperature_sensor_ids", None)),
            "solar_sensor_configured": getattr(c, "_solar_radiation_sensor_id", None) is not None,
            "weather_configured": getattr(c, "_weather_entity_id", None) is not None,
            "rain_sensor_configured": getattr(c, "_rain_sensor_id", None) is not None,
            "rain_sensor_available": (
                getattr(c, "hass", None) is not None
                and getattr(c, "_rain_sensor_id", None) is not None
                and c.hass.states.get(c._rain_sensor_id) is not None
                and c.hass.states.get(c._rain_sensor_id).state not in ("unavailable", "unknown")
            ) if getattr(c, "_rain_sensor_id", None) is not None else None,
            "windows": windows,
        }

    def _per_window(builder):
        out = {}
        for wid in (getattr(c, "windows", {}) or {}):
            try:
                out[_wref(wid)] = builder(c, wid)
            except Exception:
                errors.setdefault("per_window", {"count": 0, "reason_codes": ["window_builder_failed"]})
                errors["per_window"]["count"] += 1
        return out

    def _inputs():
        # pseudonymize window refs; strip the raw window_id the builder embeds.
        def _b(coord, wid):
            prov = ltb.build_input_provenance(coord, wid)
            if isinstance(prov, dict) and "window_id" in prov:
                prov.pop("window_id", None)
                prov["window_ref"] = _wref(wid)
            # Append contact sensor state (no entity_id — privacy-safe).
            diag = (getattr(getattr(coord, "data", None), "execution_diagnostics", None) or {}).get(wid)
            if isinstance(prov, dict):
                prov["contact"] = {
                    "sensor_configured": bool(getattr(diag, "contact_sensor_configured", False)),
                    "sensor_count": int(getattr(diag, "contact_sensor_count", 0) or 0),
                    "open_count": int(getattr(diag, "contact_open_count", 0) or 0),
                    "status": getattr(diag, "contact_status", None),
                    "is_stale": bool(getattr(diag, "contact_is_stale", False)),
                    "night_contact_blocked": bool(getattr(diag, "night_contact_blocked", False)),
                    "catch_up_pending": bool(getattr(diag, "catch_up_pending", False)),
                    "catch_up_done": bool(getattr(diag, "catch_up_done", False)),
                    "night_vent_active": bool(getattr(diag, "night_vent_active", False)),
                    "state_label": getattr(diag, "night_contact_state_label", None),
                } if diag is not None else {"sensor_configured": False}
                # Rain safety status (privacy-safe: status + hold, no entity_id).
                prov["rain"] = {
                    "configured": getattr(c, "_rain_sensor_id", None) is not None,
                    "status": getattr(diag, "rain_status", None),
                    "rain_safe_active": getattr(diag, "rain_safe_active", None),
                    "release_remaining_s": getattr(diag, "rain_release_remaining_s", None),
                    "source_quality": (
                        "measured" if getattr(diag, "rain_status", None) in ("raining", "dry")
                        else ("unavailable"
                              if getattr(c, "_rain_sensor_id", None) is None else "unknown")),
                } if diag is not None else {
                    "configured": getattr(c, "_rain_sensor_id", None) is not None,
                    "status": None}
                # Heat protection hysteresis (v1.2.0-beta.1, T9): entry/exit
                # thresholds, current readings, and the resolved hysteresis
                # state/reason — lets a support case reconstruct exactly why
                # heat protection was entered, held, or released this cycle.
                prov["heat"] = {
                    "active": getattr(diag, "heat_hysteresis_active", False),
                    "reason": getattr(diag, "heat_hysteresis_reason", None),
                    "outdoor_entry_c": getattr(diag, "heat_outdoor_entry_c", None),
                    "outdoor_exit_c": getattr(diag, "heat_outdoor_exit_c", None),
                    "indoor_entry_c": getattr(diag, "heat_indoor_entry_c", None),
                    "indoor_exit_c": getattr(diag, "heat_indoor_exit_c", None),
                    "outdoor_temp_c": getattr(diag, "heat_outdoor_temp_c", None),
                    "indoor_temp_c": getattr(diag, "heat_indoor_temp_c", None),
                } if diag is not None else {
                    "active": False, "reason": None,
                    "outdoor_entry_c": None, "outdoor_exit_c": None,
                    "indoor_entry_c": None, "indoor_exit_c": None,
                    "outdoor_temp_c": None, "indoor_temp_c": None}
                # Comfort Movement Stability Hold (v1.1.1/v1.1.2 field-fix
                # follow-up). Timing context for a held non-priority dispatch;
                # active/blocked-reason itself is already visible via the
                # sibling command_blocked_reason field.
                prov["comfort_hold"] = {
                    "last_dispatch_age_min": getattr(diag, "comfort_hold_last_dispatch_age_min", None),
                    "remaining_min": getattr(diag, "comfort_hold_remaining_min", None),
                    # F31a/F29: raw ComfortMovementHold + exit-debounce state,
                    # for explaining a held/blocked cycle without a research
                    # export.
                    "last_decided_by": getattr(diag, "comfort_hold_last_decided_by", None),
                    "last_target_ha": getattr(diag, "comfort_hold_last_target_ha", None),
                    "pending_fallback_open_release_count": getattr(
                        diag, "comfort_hold_pending_fallback_open_release_count", None),
                    "fallback_release_allowed": getattr(
                        diag, "comfort_hold_fallback_release_allowed", None),
                } if diag is not None else {
                    "last_dispatch_age_min": None,
                    "remaining_min": None,
                    "last_decided_by": None,
                    "last_target_ha": None,
                    "pending_fallback_open_release_count": None,
                    "fallback_release_allowed": None}
                # Manual Override daytime/night duration scope (v1.1.3).
                # T10: release_strategy/started_at/waiting_on added so support
                # can answer "which strategy is active, since when, and what
                # is it waiting for" without reading code.
                _mo_strategy = getattr(diag, "manual_override_release_strategy", None)
                prov["manual_override"] = {
                    "active": bool(getattr(diag, "manual_override_active", False)),
                    "scope": getattr(diag, "manual_override_scope", None),
                    "started_at": _iso_s(getattr(diag, "manual_override_started_at", None)),
                    "expires_at": _iso_s(getattr(diag, "manual_override_expires_at", None)),
                    "remaining_min": getattr(diag, "manual_override_remaining_min", None),
                    "release_strategy": _mo_strategy,
                    "waiting_on": _OVERRIDE_WAITING_ON.get(_mo_strategy),
                    "release_reason": getattr(diag, "manual_override_release_reason", None),
                    # F31a: the HA position the override is holding.
                    "override_position_ha": getattr(diag, "manual_override_position", None),
                } if diag is not None else {
                    "active": False, "scope": None, "started_at": None, "expires_at": None,
                    "remaining_min": None, "release_strategy": None, "waiting_on": None,
                    "release_reason": None, "override_position_ha": None}
                # Position-based self-healing recovery open (v1.1.5): true when a
                # stuck-down ABSENCE_ONLY / A&S window was released this cycle.
                prov["recovery_open_active"] = bool(
                    getattr(diag, "behavior_mode_recovery_open", False)
                ) if diag is not None else False
            return prov
        return _per_window(_b)

    def _position_learning():
        return _per_window(_pseudo_position)

    def _adaptation_trace(coord, wid):
        # T12: per-cycle audit trail of what learning actually changed —
        # confidence, old/new value per parameter, strength, reason, when.
        # Produced every cycle by apply_adaptive_profile() regardless of
        # whether adaptation took effect (gate-blocked cycles are visible
        # too, not just successful ones).
        tr = (getattr(coord, "_adaptation_traces", {}) or {}).get(wid)
        if tr is None:
            return {"section_status": "not_recorded", "reason": "no_trace_this_cycle"}
        return {
            "computed_at_utc": _iso_s(getattr(tr, "computed_at_utc", None)),
            "learning_active": tr.learning_active,
            "confidence_level": tr.confidence_level,
            "adaptation_strength": tr.adaptation_strength,
            "reason": tr.reason,
            "parameters": {
                "heat_outdoor_threshold_c": {
                    "old": tr.heat_outdoor_original, "new": tr.heat_outdoor_adapted,
                    "factor": tr.heat_outdoor_factor,
                },
                "heat_indoor_threshold_c": {
                    "old": tr.heat_indoor_original, "new": tr.heat_indoor_adapted,
                    "factor": tr.heat_indoor_factor,
                },
                "normal_shade_position": {
                    "old": tr.shade_position_original, "new": tr.shade_position_adapted,
                    "factor": tr.shade_position_factor,
                },
                "light_shade_threshold_wm2": {
                    "old": tr.light_shade_threshold_original,
                    "new": tr.light_shade_threshold_adapted,
                },
                "normal_shade_threshold_wm2": {
                    "old": tr.normal_shade_threshold_original,
                    "new": tr.normal_shade_threshold_adapted,
                },
                "strong_shade_threshold_wm2": {
                    "old": tr.strong_shade_threshold_original,
                    "new": tr.strong_shade_threshold_adapted,
                    "factor": tr.solar_escalation_factor_applied,
                },
            },
        }

    def _latest_decision_by_window() -> dict:
        dec_snap_raw = getattr(c, "decision_trace_snapshot", lambda: {})() or {}
        latest: dict = {}
        for _zid, z in (dec_snap_raw or {}).items():
            for r in z.get("records", []) or []:
                wid = r.get("window_id")
                if wid:
                    latest[wid] = r  # records are oldest->newest; last wins
        return latest

    def _heat_diag_for(wid: str) -> dict | None:
        diag = (getattr(getattr(c, "data", None), "execution_diagnostics", None) or {}).get(wid)
        if diag is None:
            return None
        return {
            "active": bool(getattr(diag, "heat_hysteresis_active", False)),
            "reason": getattr(diag, "heat_hysteresis_reason", None),
        }

    def _assumed_state_diag_for(wid: str) -> dict | None:
        # T16: surfaces AssumedStateManager confidence/drift/uncertainty for
        # this window's cover — previously computed and used internally
        # (T16 tolerance-widening gate) and shown on the diagnostic sensor,
        # but never exported here for a support case.
        window = (getattr(c, "windows", {}) or {}).get(wid)
        manager = getattr(c, "assumed_state_manager", None)
        if window is None or manager is None:
            return None
        cover_group = (getattr(c, "cover_groups", {}) or {}).get(window.cover_group_id)
        if cover_group is None or not getattr(cover_group, "cover_ids", None):
            return None
        cover_id = cover_group.cover_ids[0]
        state = manager.get_state(cover_id, now)
        if state is None:
            return {"available": False}
        return {
            "available": True,
            "confidence": round(state.confidence, 3),
            "position_uncertainty_pct": state.position_uncertainty_pct,
            "is_drift_suspected": state.is_drift_suspected,
            "interrupted_travel": state.interrupted_travel,
        }

    def _explainability():
        # T13: one structured "why" per window — synthesized from the same
        # already-computed decision-trace/heat/adaptation data the other
        # sections already export, not a new parallel reason system.
        latest = _latest_decision_by_window()
        out: dict = {}
        for wid in (getattr(c, "windows", {}) or {}):
            try:
                rec = latest.get(wid)
                heat_diag = _heat_diag_for(wid)
                adapt = _adaptation_trace(c, wid)
                if not isinstance(adapt, dict) or adapt.get("section_status") == "not_recorded":
                    adapt = None
                explanation = build_decision_explanation(
                    rec, heat_diag=heat_diag, adaptation_trace=adapt,
                )
                d = explanation.to_dict()
                d.pop("window_id", None)
                d["window_ref"] = _wref(wid)
                if d.get("decision_id") is not None:
                    d["decision_ref"] = pz.ref(NS_DECISION, d.pop("decision_id"))
                else:
                    d.pop("decision_id", None)
                out[_wref(wid)] = d
            except Exception:
                errors.setdefault("explainability", {"count": 0, "reason_codes": ["window_builder_failed"]})
                errors["explainability"]["count"] += 1
        return out

    def _pseudo_position(coord, wid):
        tr = ltb.build_position_learning_trace(coord, wid)
        for intensity in tr.get("intensities", {}).values():
            a = intensity.get("active_adoption", {})
            if a.get("adoption_id_internal") is not None:
                a["adoption_ref"] = pz.ref(NS_ADOPTION, a.pop("adoption_id_internal"))
            else:
                a.pop("adoption_id_internal", None)
        return tr

    def _age_seconds(ts_map, wid):
        """F24: seconds between `now` and a StateGuard-tracked timestamp for
        this window, else None.  StateGuard already tracks _entered_at /
        _last_action_at per window for its own throttling — reusing that
        existing, already-populated data rather than adding new upstream
        instrumentation."""
        try:
            ts = (ts_map or {}).get(wid)
            if ts is None:
                return None
            return round((now - ts).total_seconds(), 1)
        except Exception:
            return None

    def _current_snapshot():
        """Per-window current state snapshot from execution diagnostics.

        Answers 'what is SmartShading doing right now?' — lifecycle state,
        shading state, command/dispatch status, safety/contact/cover state.
        All data comes from already-computed execution_diagnostics; nothing is
        re-evaluated.  Fields are None/not_available when data is absent.
        """
        diags = getattr(getattr(c, "data", None), "execution_diagnostics", None) or {}
        _guard = getattr(c, "guard", None)
        _entered_at = getattr(_guard, "_entered_at", None)
        _last_action_at = getattr(_guard, "_last_action_at", None)
        windows_out = {}
        for wid in (getattr(c, "windows", {}) or {}):
            diag = diags.get(wid)
            w = (getattr(c, "windows", {}) or {}).get(wid)
            wref = _wref(wid)
            if diag is None:
                windows_out[wref] = {
                    "window_ref": wref,
                    "data_available": False,
                    "reason": "no_execution_diagnostics",
                    # F24: age is independent of execution_diagnostics — the
                    # StateGuard tracking is not gated on this cycle's diag.
                    "current_state_age_seconds": _age_seconds(_entered_at, wid),
                    "last_dispatch_age_seconds": _age_seconds(_last_action_at, wid),
                }
                continue
            windows_out[wref] = {
                "window_ref": wref,
                "data_available": True,
                # Mode / dispatch authorization
                "execution_mode": getattr(diag, "execution_mode", None),
                "active_control_enabled": getattr(diag, "active_control_enabled", None),
                "learning_enabled": getattr(diag, "learning_enabled", None),
                "is_recommendation_only": (
                    getattr(diag, "execution_mode", None) == "recommendation_only"),
                # Current decision
                "decided_by": getattr(diag, "tier_decided_by", None),
                "is_safety": bool(getattr(diag, "is_safety", False)),
                # Command / filter outcome
                "command_allowed": getattr(diag, "command_allowed", None),
                "command_blocked_reason": getattr(diag, "command_blocked_reason", None),
                "last_command_status": getattr(diag, "last_command_status", None),
                # Dispatch / hold (F4 audit follow-up): recovery-open already
                # existed only under inputs.recovery_open_active — surfaced here
                # too since current_snapshot is the natural first place a user
                # looks for "what is SmartShading doing right now".
                "dispatch_throttled": getattr(diag, "dispatch_throttled", None),
                "throttle_wait_ms": getattr(diag, "throttle_wait_ms", None),
                "night_hard_hold_applied": bool(getattr(diag, "night_hard_hold_applied", False)),
                "behavior_mode_recovery_open": bool(
                    getattr(diag, "behavior_mode_recovery_open", False)),
                # Solar source (F5 audit follow-up): which input was
                # authoritative this cycle and the measured/estimated values,
                # so a support case can see whether a fallback was used and why.
                "solar_source": getattr(diag, "selected_solar_source", None),
                "solar_source_quality": getattr(diag, "solar_source_quality", None),
                "measured_solar_wm2": getattr(diag, "measured_solar_wm2", None),
                "estimated_solar_wm2": getattr(diag, "estimated_solar_wm2", None),
                "solar_cloud_applied": getattr(diag, "solar_cloud_applied", None),
                "solar_fallback_reason": getattr(diag, "solar_fallback_reason", None),
                # Position
                "actual_position_ha": getattr(diag, "actual_position_ha", None),
                "target_position_ha": getattr(diag, "target_position_ha", None),
                "cover_available": getattr(diag, "cover_available", None),
                # Learning trace
                "adaptive_applied": getattr(diag, "adaptive_applied", None),
                "deterministic_baseline_target_ha": getattr(
                    diag, "deterministic_baseline_target_ha", None),
                "baseline_to_final_delta_ha": getattr(
                    diag, "baseline_to_final_delta_ha", None),
                # Safety / rain
                "rain_safe_active": getattr(diag, "rain_safe_active", None),
                # Night contact
                "contact_status": getattr(diag, "contact_status", None),
                "night_contact_blocked": bool(getattr(diag, "night_contact_blocked", False)),
                "catch_up_pending": bool(getattr(diag, "catch_up_pending", False)),
                "night_vent_active": bool(getattr(diag, "night_vent_active", False)),
                # Lifecycle
                "lifecycle_state": getattr(diag, "lifecycle_state_at_cycle", None),
                # Startup
                "startup_grace_active": getattr(diag, "startup_grace_active", None),
                # Hardware type (from window config — privacy-safe category, not entity id)
                "hardware_type": (str(getattr(w, "hardware_type", None))
                                  .replace("CoverHardwareType.", "") if w else None),
                # F24: "how long has this been the case" — answers e.g. "the
                # window has been in night_vent for 3 minutes" without needing
                # to cross-reference support_timeline.
                "current_state_age_seconds": _age_seconds(_entered_at, wid),
                "last_dispatch_age_seconds": _age_seconds(_last_action_at, wid),
            }
        return windows_out

    def _support_timeline(dec_records_raw):
        """Classify decision records into typed support events, newest-first.

        Suppresses same_position/no-change noise; retains and marks all critical
        events (dispatches, blocks, safety, overrides, holds, absence, night
        transitions, recommendation-only in SHADOW_ONLY mode).  Each event is
        tagged with is_recommendation_only so support can distinguish real cover
        moves from SHADOW_ONLY trace records.

        Critical events are guaranteed to appear regardless of cap: the 200-event
        cap is filled first with all critical events from the full ring, then with
        the newest non-critical events up to the remaining slots.  This prevents a
        burst of min_interval / no_change records from displacing an earlier safety
        trigger or manual override.

        Source is the raw decision ring records (pre-pseudonymization), so we
        can classify on primary_reason without any field renames.
        """
        noise_same_pos = 0
        dec_snap_raw = getattr(c, "decision_trace_snapshot", lambda: {})() or {}
        raw_recs: list = []
        for _zid, z in (dec_snap_raw or {}).items():
            raw_recs.extend(z.get("records", []))
        # Merge pre-restart persisted critical events into the ring records.
        # The ring covers since-restart; persisted events fill the gap before that.
        _persisted_evts = list(getattr(c, "_support_critical_events", []) or [])
        if _persisted_evts:
            ring_oldest_ts = (
                min((r.get("decision_timestamp_utc") or "") for r in raw_recs)
                if raw_recs else "")
            for pe in _persisted_evts:
                pe_ts = pe.get("ts") or ""
                if ring_oldest_ts and pe_ts >= ring_oldest_ts:
                    continue  # covered by ring records
                raw_recs.append({
                    "decision_timestamp_utc": pe_ts,
                    "window_id": pe.get("window_id"),
                    "resolved_state": pe.get("resolved_state"),
                    "decided_by": pe.get("decided_by"),
                    "no_dispatch": {
                        "command_sent": pe.get("event_type") == "dispatch_sent",
                        "primary_reason": pe.get("reason"),
                    },
                    "target_chain": {
                        "final_dispatched_target_ha": (
                            pe.get("target_ha")
                            if pe.get("event_type") == "dispatch_sent" else None),
                        # F31a: the real resolved/held target regardless of
                        # command_sent — fixed at the source in
                        # _record_support_event (no longer a static
                        # config-baseline fallback for blocked/
                        # manual_override events).
                        "resolved_target_position_ha": pe.get("target_ha"),
                    },
                    # F31a: real post-throttle dispatch timestamp + throttle
                    # context, carried through from _record_support_event.
                    "dispatch_sent_at_utc": pe.get("dispatch_sent_at_utc"),
                    "dispatch_context": {
                        "global_wait_required": pe.get("global_wait_required"),
                        "planned_global_interval_wait_ms": pe.get(
                            "planned_global_interval_wait_ms"),
                        "actual_global_interval_wait_ms": pe.get(
                            "actual_global_interval_wait_ms"),
                        "global_wait_overrun_ms": pe.get("global_wait_overrun_ms"),
                        "required_global_interval_ms": pe.get(
                            "required_global_interval_ms"),
                        # T11: active dispatch strategy + SEQUENTIAL-mode
                        # travel-completion outcome for this event.
                        "dispatch_mode": pe.get("dispatch_mode"),
                        "zone_batching_enabled": pe.get("zone_batching_enabled"),
                        "completion_method": pe.get("completion_method"),
                        "completion_wait_s": pe.get("completion_wait_s"),
                        "completion_timed_out": pe.get("completion_timed_out"),
                        # T11.1: concurrent-batch identity (PARALLEL mode only).
                        "parallel_batch_id": pe.get("parallel_batch_id"),
                        "parallel_batch_size": pe.get("parallel_batch_size"),
                    },
                })
        # Sort newest-first so non-critical fill-up keeps the most recent records.
        raw_recs.sort(key=lambda r: r.get("decision_timestamp_utc") or "", reverse=True)

        critical_evts: list = []
        non_critical_evts: list = []

        for r in raw_recs:
            ts = r.get("decision_timestamp_utc")
            wid = r.get("window_id")
            wref = _wref(wid)
            state = r.get("resolved_state")
            decided_by = r.get("decided_by")
            no_disp = r.get("no_dispatch") or {}
            command_sent = no_disp.get("command_sent")
            primary = no_disp.get("primary_reason") or ""
            # T21 Phase D2: dr.resolve_target_ha() is the single shared
            # target-resolution helper (also used by _pseudo_decision() for
            # current_decisions/recent_decisions) — prefers the real
            # resolved/held target over the static config-baseline
            # recommendation, the same preference order everywhere a target
            # value is needed, so a timeline event can never disagree with
            # current_decisions/explainability about what the target was.
            target_ha = dr.resolve_target_ha(r)

            # Suppress same-position noise before classifying anything else.
            if primary in _SAME_POS_REASONS:
                noise_same_pos += 1
                continue

            # T13: a dispatch that was attempted (not blocked, not skipped as
            # unnecessary) but did not succeed is a genuine failure — already
            # recorded via dispatch_authority.applied/blocked, just never
            # classified into its own timeline event type before.
            _disp_auth = (r.get("authorities") or {}).get("dispatch_authority") or {}
            _is_dispatch_failed = (
                command_sent is False
                and _disp_auth.get("applied") is False
                and _disp_auth.get("blocked") is False
                and (no_disp.get("recommendation_exists") is True)
                and primary not in (
                    "active_control_off", "dispatch_not_required",
                    "command_filter_suppressed",
                )
            )

            if command_sent is True:
                evt_type = "dispatch_sent"
            elif _is_dispatch_failed:
                evt_type = "dispatch_failed"
            elif primary == "active_control_off":
                evt_type = "recommendation_only"
            elif state in ("storm_safe", "wind_safe", "rain_safe"):
                evt_type = "safety"
            elif state == "manual_override":
                evt_type = "manual_override"
            elif primary in ("behavior_mode_hold",):
                evt_type = "behavior_hold"
            elif primary in ("presence_uncertain_hold",):
                evt_type = "presence_hold"
            elif primary in ("min_interval_not_elapsed",):
                evt_type = "min_interval"
            elif primary in ("startup_grace",):
                evt_type = "startup_grace"
            elif decided_by and "Night" in decided_by:
                evt_type = "night_transition"
            elif decided_by and "Absence" in decided_by:
                evt_type = "absence"
            elif primary:
                evt_type = "command_blocked"
            else:
                evt_type = "no_change"

            _ctx = r.get("dispatch_context") or {}
            evt = {
                "ts": _iso_s(ts),
                "local_ts": _iso_local(ts, home_tz),
                "event_type": evt_type,
                "window_ref": wref,
                "shading_state": state,
                "decided_by": decided_by,
                "reason": primary or None,
                "target_ha": target_ha,
                "is_recommendation_only": (evt_type == "recommendation_only"),
                "is_critical": evt_type in _GUARANTEED_EVENT_TYPES,
                "severity": _classify_severity(evt_type),
                # F31a: real post-throttle dispatch timestamp — distinct from
                # "ts" above, which is the shared per-cycle decision time.
                # None when nothing was actually dispatched this event.
                "dispatch_sent_at_utc": _iso_s(r.get("dispatch_sent_at_utc")),
                # F31a: throttle context already computed by the coordinator
                # (F32 global serial dispatch), surfaced here for the first
                # time instead of only in recent_dispatches.
                "global_wait_required": _ctx.get("global_wait_required"),
                "planned_global_interval_wait_ms": _ctx.get(
                    "planned_global_interval_wait_ms"),
                "actual_global_interval_wait_ms": _ctx.get(
                    "actual_global_interval_wait_ms"),
                "global_wait_overrun_ms": _ctx.get("global_wait_overrun_ms"),
                "required_global_interval_ms": _ctx.get("required_global_interval_ms"),
            }
            if evt["is_critical"]:
                critical_evts.append(evt)
            else:
                non_critical_evts.append(evt)

        # T21 Phase D: collapse repeated identical non-critical events (e.g. a
        # BehaviorMode hold re-evaluated every cycle with nothing changed)
        # into occurrence-count aggregates BEFORE capping — this is what
        # actually removes the noise, rather than just truncating it earlier.
        # non_critical_evts is already newest-first (ring was sorted that
        # way), which _aggregate_repeated_events() requires.
        aggregated_non_critical = _aggregate_repeated_events(non_critical_evts)
        aggregated_away = len(non_critical_evts) - len(aggregated_non_critical)

        # Merge: all guaranteed events + newest aggregated non-critical up to cap.
        remaining_slots = max(0, MAX_SUPPORT_TIMELINE_EVENTS - len(critical_evts))
        events = critical_evts + aggregated_non_critical[:remaining_slots]
        # Re-sort merged list newest-first for output.
        events.sort(key=lambda e: e.get("ts") or "", reverse=True)

        critical_count = sum(1 for e in events if e["is_critical"])
        non_critical_count = len(events) - critical_count
        _has_persisted = bool(_persisted_evts)
        return {
            "requested_window_h": 24,
            "coverage_scope": "24h_window" if _has_persisted else "since_restart",
            "since_restart_only": not _has_persisted,
            "scope_note": (
                "pre-restart events backfilled from persisted store; ring covers since-restart"
                if _has_persisted else "decision ring is runtime-only; resets on HA restart"
            ),
            "events": events,
            "event_count": len(events),
            "same_position_noise_suppressed": noise_same_pos,
            # T21 Phase D: individual repeated-hold records collapsed away by
            # _aggregate_repeated_events() (folded into an occurrence_count
            # aggregate elsewhere in `events`, not lost — see each aggregate's
            # own occurrence_count/first_seen/last_seen for the true tally).
            "repeated_events_aggregated": aggregated_away,
            "critical_event_count": critical_count,
            "non_critical_event_count": non_critical_count,
            "critical_events_guaranteed": True,
            "truncated_at_cap": len(aggregated_non_critical) > remaining_slots,
        }

    def _decision_records(ring_snapshot, cap):
        # ring_snapshot: {zone: {"records": [...]}}; flatten + pseudonymize + cap.
        recs = []
        for _zid, z in (ring_snapshot or {}).items():
            recs.extend(z.get("records", []))
        kept, meta = cap_records(recs, cap)
        return [_pseudo_decision(r) for r in kept], meta

    def _pseudo_decision(r):
        # T21 Phase D2: target_chain is only present when the target
        # actually changed somewhere along the pipeline (resolve_target_chain);
        # a routine decision where every stage agrees on the same position no
        # longer repeats that one value across 5 identical fields. The single
        # resolved value is always available as "resolved_target_ha" instead.
        out = {
            "decision_ref": pz.ref(NS_DECISION, r.get("decision_id")),
            "window_ref": _wref(r.get("window_id")),
            "decision_timestamp_utc": _iso_s(r.get("decision_timestamp_utc")),
            "decision_timestamp_local": _iso_local(r.get("decision_timestamp_utc"), home_tz),
            "baseline_state": r.get("baseline_state"),
            "resolved_state": r.get("resolved_state"),
            "decided_by": r.get("decided_by"),
            "config_generation": r.get("config_generation"),
            "candidates": r.get("candidates"),
            "resolved_target_ha": dr.resolve_target_ha(r),
            "target_chain": dr.resolve_target_chain(r.get("target_chain")),
            "dispatch_action": dr.resolve_dispatch_action(r),
            "no_dispatch": r.get("no_dispatch"),
            "authorities": _pseudo_authorities(r.get("authorities", {})),
        }
        return out

    def _pseudo_authorities(auth):
        out = {}
        for name, a in (auth or {}).items():
            a2 = dict(a)
            if a2.get("experiment_id") is not None:
                a2["experiment_ref"] = pz.ref(NS_EXPERIMENT, a2.pop("experiment_id"))
            if a2.get("adoption_id") is not None:
                a2["adoption_ref"] = pz.ref(NS_ADOPTION, a2.pop("adoption_id"))
            out[name] = a2
        return out

    def _pseudo_dispatch(r):
        out = dict(r)
        out["decision_ref"] = pz.ref(NS_DECISION, out.pop("decision_id", None))
        out["window_ref"] = _wref(out.pop("window_id", None))
        out["cover_ref"] = pz.ref(NS_COVER, out.pop("cover_id", None))
        out["at"] = _iso_s(out.get("at"))
        return out

    def _pseudo_outcome(wid: str, o) -> dict:
        mo = getattr(o, "multi_objective", None)
        return {
            "window_ref": _wref(wid),
            "decision_timestamp_utc": _iso_s(getattr(o, "decision_timestamp", None)),
            "evaluation_timestamp_utc": _iso_s(getattr(o, "evaluation_timestamp", None)),
            "decided_by": getattr(o, "decided_by", None),
            "decided_state": str(getattr(o, "decided_state", None)),
            "resolution_status": getattr(o, "resolution_status", None),
            "override_occurred": bool(getattr(o, "override_occurred", False)),
            "escalation_occurred": bool(getattr(o, "escalation_occurred", False)),
            "outcome_score": getattr(o, "outcome_score", None),
            "indoor_temp_delta_c": getattr(o, "indoor_temp_delta_c", None),
            # T12: MultiObjectiveOutcome is the source of truth outcome_score
            # is derived from — surface its dimension breakdown so a support
            # case can see WHY a score was positive/negative, not just the
            # number.
            "multi_objective": {
                "thermal_available": getattr(mo.thermal, "available", False),
                "thermal_score": getattr(mo.thermal, "score", None),
                "movement_available": getattr(mo.movement, "available", False),
                "movement_score": getattr(mo.movement, "score", None),
                "preference_available": getattr(mo.preference, "available", False),
                "preference_score": getattr(mo.preference, "score", None),
                "reliability_overall": getattr(mo.reliability, "overall", None),
                "confounded": getattr(mo.confounders, "detected", ()),
            } if mo is not None else None,
        }

    def _recent_outcomes():
        store = getattr(c, "learning_store", None)
        if store is None:
            return {"section_status": "not_recorded", "reason": "learning_store_unavailable"}
        recs = []
        for wid in (getattr(c, "windows", {}) or {}):
            try:
                # Per-window read is unbounded here — the combined cap below
                # is what actually bounds the exported total across windows.
                for o in store.get_outcomes(wid, limit=MAX_SUPPORT_OUTCOMES_PER_ZONE * 4):
                    recs.append((getattr(o, "decision_timestamp", None), wid, o))
            except Exception:
                continue
        # cap_records keeps the newest via records[-N:] — feed oldest-first.
        recs.sort(key=lambda t: t[0] or now)
        capped, meta = cap_records(
            [{"decision_timestamp": ts, "wid": wid, "o": o} for ts, wid, o in recs],
            MAX_SUPPORT_OUTCOMES_PER_ZONE,
        )
        capped = list(reversed(capped))  # newest-first for display
        return {
            "records": [_pseudo_outcome(r["wid"], r["o"]) for r in capped],
            "truncation": meta,
        }

    def _pseudo_transition(wid: str, t) -> dict:
        return {
            "window_ref": _wref(wid),
            "timestamp_utc": _iso_s(getattr(t, "timestamp", None)),
            "from_state": str(getattr(t, "from_state", None)),
            "to_state": str(getattr(t, "to_state", None)),
            "decided_by": getattr(t, "decided_by", None),
            "lifecycle_state": getattr(t, "lifecycle_state", None),
        }

    def _recent_learning_transitions():
        store = getattr(c, "learning_store", None)
        if store is None:
            return {"section_status": "not_recorded", "reason": "learning_store_unavailable"}
        recs = []
        for wid in (getattr(c, "windows", {}) or {}):
            try:
                # Per-window read is unbounded here — the combined cap below
                # is what actually bounds the exported total across windows.
                for t in store.get_transitions(wid, limit=MAX_SUPPORT_LEARNING_TRANSITIONS_PER_ZONE * 4):
                    recs.append((getattr(t, "timestamp", None), wid, t))
            except Exception:
                continue
        # cap_records keeps the newest via records[-N:] — feed oldest-first.
        recs.sort(key=lambda tpl: tpl[0] or now)
        capped, meta = cap_records(
            [{"timestamp": ts, "wid": wid, "t": t} for ts, wid, t in recs],
            MAX_SUPPORT_LEARNING_TRANSITIONS_PER_ZONE,
        )
        capped = list(reversed(capped))  # newest-first for display
        return {
            "records": [_pseudo_transition(r["wid"], r["t"]) for r in capped],
            "truncation": meta,
        }

    def _recent_dispatches():
        snap = (getattr(c, "dispatch_trace_snapshot", lambda: {})() or {}).get("zones", {})
        recs = []
        for _zid, z in snap.items():
            recs.extend(z.get("records", []))
        kept, meta = cap_records(recs, MAX_SUPPORT_DISPATCHES_PER_ZONE)
        return {"records": [_pseudo_dispatch(r) for r in kept], "truncation": meta}

    def _decisions_snapshot():
        return getattr(c, "decision_trace_snapshot", lambda: {})() or {}

    def _no_dispatches(dec_records):
        nd = [r for r in dec_records
              if (r.get("no_dispatch") or {}).get("command_sent") is False]
        kept, meta = cap_records(nd, MAX_SUPPORT_NO_DISPATCHES_PER_ZONE)
        return {"records": kept, "truncation": meta}

    def _storage():
        sd = getattr(c, "storage_diagnostics", lambda: {})() or {}
        sd = dict(sd)
        last = sd.pop("learning_store_last_save_at", None)
        sd["learning_store_last_save_at_utc"] = _iso_s(last)
        return sd

    def _health():
        from .diagnostics_builder import build_consolidated_diagnostics
        return (build_consolidated_diagnostics(c) or {}).get("health", {})

    # ---- assemble ----
    dec_snap = _safe(_decisions_snapshot, errors, "decisions_snapshot", default={})
    recent_dec, dec_trunc = ([], {"truncated": False})
    try:
        recent_dec, dec_trunc = _decision_records(dec_snap, MAX_SUPPORT_DECISIONS_PER_ZONE)
    except Exception:
        errors.setdefault("recent_decisions", {"count": 1, "reason_codes": ["builder_failed"]})

    contract: dict = {
        "support_export_schema_version": SUPPORT_EXPORT_SCHEMA_VERSION,
        "detail_level": detail_level,
        "generated_at_utc": _iso_s(now),
        "generated_at_local": _iso_local(now, home_tz),
        "home_timezone": home_tz_name,
        "timezone_source": tz_source,
        "integration_version": integration_version,
        "export_scope": "entry_zone",
        "pseudonymization": _safe(_meta, errors, "pseudonymization"),
        "system": _safe(_system, errors, "system"),
        "configuration": _safe(_configuration, errors, "configuration"),
        "health": _safe(_health, errors, "health"),
        "current_snapshot": _safe(_current_snapshot, errors, "current_snapshot"),
        "support_timeline": _safe(lambda: _support_timeline(recent_dec), errors,
                                  "support_timeline"),
        "inputs": _safe(_inputs, errors, "inputs"),
        "position_learning": _safe(_position_learning, errors, "position_learning"),
        "adaptation_trace": _safe(
            lambda: _per_window(_adaptation_trace), errors, "adaptation_trace"),
        "assumed_state": _safe(
            lambda: _per_window(lambda _coord, wid: _assumed_state_diag_for(wid)),
            errors, "assumed_state"),
        "explainability": _safe(_explainability, errors, "explainability"),
        "current_decisions": {},  # filled below (latest record per zone)
        "recent_decisions": recent_dec,
        "recent_dispatches": _safe(_recent_dispatches, errors, "recent_dispatches"),
        "recent_no_dispatches": _safe(lambda: _no_dispatches(recent_dec), errors,
                                      "recent_no_dispatches"),
        # T12: LearningStore's outcome/transition ring buffers are now
        # surfaced here (bounded, pseudonymized) — see _recent_outcomes /
        # _recent_learning_transitions.
        "recent_outcomes": _safe(_recent_outcomes, errors, "recent_outcomes"),
        "recent_learning_transitions": _safe(
            _recent_learning_transitions, errors, "recent_learning_transitions"),
        "storage": _safe(_storage, errors, "storage"),
        "history_metadata": _safe(
            lambda: _support_history_metadata(recent_dec, dec_trunc), errors,
            "history_metadata"),
        "section_errors": errors,
    }
    # current_decisions: latest record per zone (pseudonymized).
    cur: dict = {}
    for zid, z in (dec_snap or {}).items():
        recs = z.get("records", [])
        if recs:
            cur[pz.ref(NS_ZONE, zid)] = _pseudo_decision(recs[-1])
    contract["current_decisions"] = cur

    # T21 Phase D3: Standard is a pure post-hoc projection of this same
    # already-built contract — computed AFTER current_decisions so Standard
    # and Extended can never disagree about the canonical decision values.
    if detail_level == "standard":
        contract = _compact_for_standard(contract)

    # reason-code registry for codes actually present (computed per detail
    # level, after compaction, so it only lists codes actually visible in
    # this export — never a superset of what the reader can see).
    contract["reason_codes"] = _collect_reason_codes(contract)

    # bounded depth + string caps.
    contract = enforce_depth(truncate_strings(contract, max_len=MAX_SUPPORT_STRING_LENGTH),
                             max_depth=MAX_SUPPORT_NESTED_DEPTH)
    # byte cap with deterministic oldest-first history truncation.
    contract = _enforce_byte_cap(contract)
    if not is_json_safe(contract):
        return {"support_export_schema_version": SUPPORT_EXPORT_SCHEMA_VERSION,
                "generated_at_utc": _iso_s(now),
                "section_errors": {"json_safety": {"count": 1, "reason_codes": ["json_unsafe"]}}}
    return contract


def _aggregate_history_metadata(zones) -> dict:
    """System-level span/coverage across all zones' runtime-recent decision rings,
    plus per-zone exported counts (privacy-safe — counts/timestamps only)."""
    olds, news = [], []
    total_exported = 0
    truncated = False
    per_zone: list = []
    for i, z in enumerate(zones):
        hm = z.get("history_metadata", {}) if isinstance(z, dict) else {}
        if hm.get("oldest_record_utc"):
            olds.append(hm["oldest_record_utc"])
        if hm.get("newest_record_utc"):
            news.append(hm["newest_record_utc"])
        cnt = int(hm.get("records_exported", 0) or 0)
        total_exported += cnt
        truncated = truncated or bool(hm.get("truncated"))
        per_zone.append({"zone_index": i, "records_exported": cnt,
                         "oldest_record_utc": hm.get("oldest_record_utc"),
                         "newest_record_utc": hm.get("newest_record_utc")})
    return {
        "store_scope": "runtime_recent",
        "scope_note": "recent in-memory decision rings; reset on restart/reload",
        "oldest_record_utc": min(olds) if olds else None,
        "newest_record_utc": max(news) if news else None,
        "records_exported": total_exported,
        "truncated": truncated,
        "per_zone": per_zone,
    }


def build_support_export_all_zones(coordinators, *, now=None,
                                   integration_version="unknown", detail_level="standard") -> dict:
    """Aggregate Support Export across ALL active zone coordinators.

    Builds the per-zone v3 support export for every active zone and nests them
    under ``zones``, with a top-level system summary (zone/window/cover totals,
    runtime-mode + configured-sensor summary).  A zone that fails to build is
    captured as a per-zone section_error and degrades ``overall_status`` without
    aborting the whole export.  No active zone → an honest no-zone status, never
    a misleading healthy empty export.

    T21 Phase D3: ``detail_level`` ("standard"/"extended", default "standard")
    is threaded through to every per-zone build — an existing caller that
    never passes it (the export button, any pre-D3 code) keeps getting the
    Standard export, unchanged behaviour.
    """
    now = now or datetime.now(timezone.utc)
    detail_level = _normalize_detail_level(detail_level)
    coords = [c for c in (coordinators or []) if c is not None]
    if not coords:
        return {
            "support_export_schema_version": SUPPORT_EXPORT_SCHEMA_VERSION,
            "detail_level": detail_level,
            "generated_at_utc": _iso_s(now),
            "integration_version": integration_version,
            "export_scope": "system_all_zones",
            "overall_status": "no_active_zone",
            "system": {"zone_count": 0, "total_window_count": 0, "total_cover_count": 0},
            "zones": [],
            "section_errors": {"zones": {"count": 1,
                                         "reason_codes": ["no_active_zone_coordinator"]}},
        }
    zones: list = []
    total_windows = total_covers = 0
    runtime_modes: set = set()
    sensors_any = {"solar": False, "weather": False, "rain": False,
                   "indoor": False, "outdoor": False}
    degraded = False
    for c in coords:
        try:
            z = build_support_export_v3(
                c, now=now, integration_version=integration_version, detail_level=detail_level)
        except Exception:
            z = {"section_errors": {"zone": {"count": 1,
                                             "reason_codes": ["zone_builder_failed"]}}}
        zones.append(z)
        sysd = z.get("system", {}) if isinstance(z, dict) else {}
        cfg = z.get("configuration", {}) if isinstance(z, dict) else {}
        total_windows += int(sysd.get("window_count", 0) or 0)
        total_covers += int(sysd.get("cover_count", 0) or 0)
        runtime_modes.add(cfg.get("runtime_mode", "unknown"))
        for k in sensors_any:
            if cfg.get(f"{k}_sensor_configured") or cfg.get(f"{k}_configured"):
                sensors_any[k] = True
        if z.get("section_errors"):
            degraded = True
    # F24: reuse the already-computed per-zone timezone context (first zone —
    # a single HA instance has one configured timezone) instead of resolving
    # it again at this level.
    _first_zone = zones[0] if zones and isinstance(zones[0], dict) else {}
    contract = {
        "support_export_schema_version": SUPPORT_EXPORT_SCHEMA_VERSION,
        "detail_level": detail_level,
        "generated_at_utc": _iso_s(now),
        "generated_at_local": _first_zone.get("generated_at_local"),
        "home_timezone": _first_zone.get("home_timezone"),
        "timezone_source": _first_zone.get("timezone_source"),
        "integration_version": integration_version,
        "export_scope": "system_all_zones",
        "overall_status": ("degraded" if degraded else "ok"),
        "pseudonymization": pseudonymization_metadata(stability_scope="per_zone_config_entry"),
        "system": {
            "zone_count": len(coords),
            "total_window_count": total_windows,
            "total_cover_count": total_covers,
            "runtime_modes": sorted(m for m in runtime_modes if m),
            "configured_sensors_summary": sensors_any,
        },
        "zones": zones,
        "history_metadata": _aggregate_history_metadata(zones),
        "section_errors": ({} if not degraded
                           else {"zones": {"count": 1, "reason_codes": ["zone_degraded"]}}),
    }
    contract = enforce_depth(truncate_strings(contract, max_len=MAX_SUPPORT_STRING_LENGTH),
                             max_depth=MAX_SUPPORT_NESTED_DEPTH)
    contract = _enforce_byte_cap(contract)
    if not is_json_safe(contract):
        return {"support_export_schema_version": SUPPORT_EXPORT_SCHEMA_VERSION,
                "generated_at_utc": _iso_s(now), "export_scope": "system_all_zones",
                "section_errors": {"json_safety": {"count": 1, "reason_codes": ["json_unsafe"]}}}
    return contract


def _collect_reason_codes(contract) -> dict:
    # T21 Phase D3: delegates to the shared collector in reason_codes.py —
    # research_export_v3.py now uses the exact same function.
    return rc.collect_reason_codes_from_contract(contract)


def _enforce_byte_cap(contract: dict) -> dict:
    original_bytes = _bytes(contract)
    sections_meta: dict = {}
    if original_bytes <= MAX_SUPPORT_EXPORT_BYTES:
        contract["truncation"] = {"applied": False, "original_bytes": original_bytes,
                                  "final_bytes": original_bytes,
                                  "byte_cap": MAX_SUPPORT_EXPORT_BYTES, "sections": {}}
        return contract
    # Shed oldest history records first (history before current snapshots).
    for section in _HISTORY_SECTIONS:
        val = contract.get(section)
        recs = val.get("records") if isinstance(val, dict) else (val if isinstance(val, list) else None)
        if not recs:
            continue
        orig = len(recs)
        while recs and _bytes(contract) > MAX_SUPPORT_EXPORT_BYTES:
            recs.pop(0)  # oldest first
        sections_meta[section] = {"original_count": orig, "final_count": len(recs),
                                  "removed_count": orig - len(recs)}
        if _bytes(contract) <= MAX_SUPPORT_EXPORT_BYTES:
            break
    final_bytes = _bytes(contract)
    contract["truncation"] = {"applied": True, "original_bytes": original_bytes,
                              "final_bytes": final_bytes, "byte_cap": MAX_SUPPORT_EXPORT_BYTES,
                              "sections": sections_meta}
    return contract


def _bytes(obj) -> int:
    try:
        return len(json.dumps(obj, default=str).encode("utf-8"))
    except Exception:
        return 0


def _orientation_category(azimuth) -> str:
    if not isinstance(azimuth, (int, float)):
        return "unknown"
    a = azimuth % 360
    for lo, hi, name in ((315, 360, "north"), (0, 45, "north"), (45, 135, "east"),
                         (135, 225, "south"), (225, 315, "west")):
        if lo <= a < hi:
            return name
    return "unknown"


def privacy_scan(export: dict) -> list:
    """Return a list of forbidden raw markers found in the serialized export
    (empty list = clean).  For tests + a final guard."""
    blob = json.dumps(export, default=str)
    found = [m for m in DEFAULT_FORBIDDEN_MARKERS if m in blob.lower()]
    if contains_forbidden_substring(export, DEFAULT_FORBIDDEN_MARKERS):
        found.append("nested_forbidden")
    return found
