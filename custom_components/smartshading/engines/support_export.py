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
    truncate_strings,
)
from . import learning_trace_builder as ltb
from . import reason_codes as rc
from ..models.runtime_mode import derive_authority

SUPPORT_EXPORT_SCHEMA_VERSION: int = 3

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

# Event types that constitute "critical" support events (non-noise).
_CRITICAL_EVENT_TYPES = frozenset({
    "dispatch_sent", "dispatch_failed", "command_blocked", "recommendation_only",
    "safety", "manual_override", "absence", "night_transition", "presence_hold",
    "behavior_hold", "contact_event", "min_interval_bypass",
})

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


def build_support_export_v3(coordinator, *, now=None, integration_version="unknown") -> dict:
    """Build the v3 support export for one config entry / its zone."""
    now = now or datetime.now(timezone.utc)
    c = coordinator
    entry_id = getattr(getattr(c, "config_entry", None), "entry_id", None)
    pz = Pseudonymizer(entry_id)
    errors: dict = {}
    home_tz, home_tz_name, tz_source = _resolve_home_tz(c)

    def _wref(wid):
        return pz.ref(NS_WINDOW, wid)

    def _meta():
        return {
            "algorithm": "hmac_sha256", "output_bits": 64,
            "namespace_separated": True, "stability_scope": "config_entry",
        }

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
                prov["manual_override"] = {
                    "active": bool(getattr(diag, "manual_override_active", False)),
                    "scope": getattr(diag, "manual_override_scope", None),
                    "expires_at": _iso_s(getattr(diag, "manual_override_expires_at", None)),
                    "remaining_min": getattr(diag, "manual_override_remaining_min", None),
                    "release_reason": getattr(diag, "manual_override_release_reason", None),
                    # F31a: the HA position the override is holding.
                    "override_position_ha": getattr(diag, "manual_override_position", None),
                } if diag is not None else {
                    "active": False, "scope": None, "expires_at": None,
                    "remaining_min": None, "release_reason": None,
                    "override_position_ha": None}
                # Position-based self-healing recovery open (v1.1.5): true when a
                # stuck-down ABSENCE_ONLY / A&S window was released this cycle.
                prov["recovery_open_active"] = bool(
                    getattr(diag, "behavior_mode_recovery_open", False)
                ) if diag is not None else False
            return prov
        return _per_window(_b)

    def _position_learning():
        return _per_window(_pseudo_position)

    def _strategy_learning():
        return _per_window(ltb.build_strategy_learning_trace)

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
            tc = r.get("target_chain") or {}
            # F31a: prefer the real resolved/held target over the static
            # config-baseline recommendation — the baseline is only used as
            # a last-resort fallback for older/malformed records that
            # predate this field.
            target_ha = (tc.get("final_dispatched_target_ha")
                         or tc.get("resolved_target_position_ha")
                         or tc.get("recommendation_position_ha"))

            # Suppress same-position noise before classifying anything else.
            if primary in _SAME_POS_REASONS:
                noise_same_pos += 1
                continue

            if command_sent is True:
                evt_type = "dispatch_sent"
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
                "is_critical": evt_type in _CRITICAL_EVENT_TYPES,
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

        # Merge: all critical events + newest non-critical up to cap.
        # non_critical_evts is already newest-first (ring was sorted that way).
        remaining_slots = max(0, MAX_SUPPORT_TIMELINE_EVENTS - len(critical_evts))
        events = critical_evts + non_critical_evts[:remaining_slots]
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
            "critical_event_count": critical_count,
            "non_critical_event_count": non_critical_count,
            "critical_events_guaranteed": True,
            "truncated_at_cap": len(non_critical_evts) > remaining_slots,
        }

    def _decision_records(ring_snapshot, cap):
        # ring_snapshot: {zone: {"records": [...]}}; flatten + pseudonymize + cap.
        recs = []
        for _zid, z in (ring_snapshot or {}).items():
            recs.extend(z.get("records", []))
        kept, meta = cap_records(recs, cap)
        return [_pseudo_decision(r) for r in kept], meta

    def _pseudo_decision(r):
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
            "target_chain": r.get("target_chain"),
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
        "strategy_learning": _safe(_strategy_learning, errors, "strategy_learning"),
        "current_decisions": {},  # filled below (latest record per zone)
        "recent_decisions": recent_dec,
        "recent_dispatches": _safe(_recent_dispatches, errors, "recent_dispatches"),
        "recent_no_dispatches": _safe(lambda: _no_dispatches(recent_dec), errors,
                                      "recent_no_dispatches"),
        # No dedicated outcome/transition rings exist → honest not_recorded.
        "recent_outcomes": {"section_status": "not_recorded",
                            "reason": "no_dedicated_outcome_history_ring"},
        "recent_learning_transitions": {"section_status": "not_recorded",
                                        "reason": "no_dedicated_transition_history_ring"},
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

    # reason-code registry for codes actually present.
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
                                   integration_version="unknown") -> dict:
    """Aggregate Support Export across ALL active zone coordinators.

    Builds the per-zone v3 support export for every active zone and nests them
    under ``zones``, with a top-level system summary (zone/window/cover totals,
    runtime-mode + configured-sensor summary).  A zone that fails to build is
    captured as a per-zone section_error and degrades ``overall_status`` without
    aborting the whole export.  No active zone → an honest no-zone status, never
    a misleading healthy empty export.
    """
    now = now or datetime.now(timezone.utc)
    coords = [c for c in (coordinators or []) if c is not None]
    if not coords:
        return {
            "support_export_schema_version": SUPPORT_EXPORT_SCHEMA_VERSION,
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
            z = build_support_export_v3(c, now=now, integration_version=integration_version)
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
        "generated_at_utc": _iso_s(now),
        "generated_at_local": _first_zone.get("generated_at_local"),
        "home_timezone": _first_zone.get("home_timezone"),
        "timezone_source": _first_zone.get("timezone_source"),
        "integration_version": integration_version,
        "export_scope": "system_all_zones",
        "overall_status": ("degraded" if degraded else "ok"),
        "pseudonymization": {"algorithm": "hmac_sha256", "output_bits": 64,
                             "namespace_separated": True,
                             "stability_scope": "per_zone_config_entry"},
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
    codes: set = set()

    def _scan(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and (k.endswith("reason_code") or k == "primary_reason"
                                           or k == "blocked_reason" or k == "gate_reason"):
                    codes.add(v)
                else:
                    _scan(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                _scan(v)
    _scan(contract)
    return rc.registry_for_codes(c for c in codes if c)


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
