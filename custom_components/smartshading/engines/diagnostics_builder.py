"""Consolidated diagnostics builder — LE 2.0 / Phase P11 (HA-free, read-only).

Assembles the versioned consolidated diagnostics contract from coordinator
getters.  Deny-by-default: only explicitly listed fields are emitted.  Every
section is built in isolation (never-raise) so a corrupt section yields a partial
but valid contract.  Read-only: never mutates runtime state, never triggers a
save, never changes a decision.

The HA-diagnostics product is PUBLIC_SAFE: counts/status only, NO ids, NO raw
entity ids, NO exact historical timestamps.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .diagnostics_privacy import enforce_depth, is_json_safe, truncate_strings
from ..models.runtime_mode import derive_authority

_LOGGER = logging.getLogger(__name__)

DIAGNOSTICS_SCHEMA_VERSION: int = 1


def _safe(section_fn, errors: dict, name: str):
    try:
        return section_fn()
    except Exception:
        errors[name] = errors.get(name, 0) + 1
        return {"section_error": True}


def _iso(dt) -> str | None:
    try:
        return dt.isoformat() if dt is not None else None
    except Exception:
        return None


def _age_seconds(dt, now: datetime) -> float | None:
    try:
        return round((now - dt).total_seconds(), 1) if dt is not None else None
    except Exception:
        return None


def build_consolidated_diagnostics(coordinator, *, integration_version: str = "unknown") -> dict:
    """Build the PUBLIC_SAFE consolidated diagnostics contract (HA diagnostics).

    *coordinator* is duck-typed; missing getters degrade to safe defaults."""
    now = datetime.now(timezone.utc)
    errors: dict = {}
    c = coordinator

    def _system():
        return {
            "zone_count": len(getattr(c, "zones", {}) or {}),
            "window_count": len(getattr(c, "windows", {}) or {}),
            "cover_count": len(getattr(c, "cover_groups", {}) or {}),
            "last_coordinator_update": None,  # PUBLIC: no exact event timestamps
        }

    def _matrix():
        # Learning / Active Control matrix per zone (PUBLIC: status only).
        out: dict = {}
        zones = getattr(c, "zones", {}) or {}
        eff = getattr(c, "effective_zone_execution", None)
        for zid in zones:
            try:
                cfg = eff(zid) if eff else None
            except Exception as exc:
                # F7: this per-zone failure is invisible to _safe()'s errors dict
                # (it's caught here, inside _matrix, before _safe ever sees an
                # exception) — the zone's row silently degrades to
                # learning/active=False. Logged so it's distinguishable from a
                # genuine all-disabled zone.
                _LOGGER.debug(
                    "diagnostics_builder: effective_zone_execution failed for "
                    "zone (%s: %s)", type(exc).__name__, exc,
                )
                cfg = None
            learning = bool(getattr(cfg, "learning_enabled", False))
            active = bool(getattr(cfg, "active_control_enabled", False))
            # Central authority: single derivation, no ad-hoc recombination.
            auth = derive_authority(learning, active)
            out[_zone_hash(c, zid)] = {
                "runtime_mode": auth.mode.value,
                "learning_enabled": learning,
                "active_control_enabled": active,
                "learning_allowed": auth.learning_allowed,
                "shadow_evaluation_allowed": auth.shadow_evaluation_allowed,
                "adaptive_reads_allowed": auth.adaptive_reads_allowed,
                "adaptive_writes_allowed": auth.adaptive_writes_allowed,
                "real_control_allowed": auth.real_control_allowed,
                "experiments_allowed": auth.experiments_allowed,
                "outcomes_allowed": auth.outcomes_allowed,
                # Legacy aliases (kept for existing diagnostics consumers).
                "observation_active": auth.learning_allowed,
                "shadow_evaluation_active": auth.shadow_evaluation_allowed,
                "real_experiments_allowed": auth.experiments_allowed,
                "adoptions_allowed": auth.learning_allowed,
                "cover_commands_allowed": auth.real_control_allowed,
            }
        return out

    def _learning():
        sd = _call(c, "storage_diagnostics")
        counts = (sd or {}).get("learning_store_record_counts", {})
        return {
            "record_counts": counts,
            "ledger_integrity": _ledger_integrity(c),
        }

    def _storage():
        sd = _call(c, "storage_diagnostics") or {}
        sd = dict(sd)
        # PUBLIC: convert last_save_at to an age, drop raw timestamp.
        last = sd.pop("learning_store_last_save_at", None)
        if last is not None:
            try:
                sd["learning_store_last_save_age_seconds"] = _age_seconds(
                    datetime.fromisoformat(last), now)
            except Exception:
                sd["learning_store_last_save_age_seconds"] = None
        return sd

    def _validation():
        sd = _call(c, "storage_diagnostics") or {}
        return sd.get("learning_restore", {}) or {}

    def _execution():
        snap = _call(c, "dispatch_trace_snapshot") or {"zones": {}, "covers": {}}
        # PUBLIC: counts only — no decision/cover ids, no record details.
        zone_counts = {}
        for i, (_zid, z) in enumerate(snap.get("zones", {}).items()):
            zone_counts[f"zone_{i}"] = z.get("count", 0)
        retargets = sum(
            cov.get("same_cover_retarget_count", 0)
            for cov in snap.get("covers", {}).values())
        return {
            "dispatch_records_by_zone": zone_counts,
            "total_same_cover_retargets": retargets,
            "covers_tracked": len(snap.get("covers", {})),
        }

    def _health():
        sd = _call(c, "storage_diagnostics") or {}
        save_fail = sd.get("learning_store_save_failures", 0)
        restore_fail = sd.get("learning_store_restore_failures", 0)
        thermal_fail = sd.get("learning_thermal_finalize_failures", 0)
        ledger = _ledger_integrity(c)
        reasons: list[str] = []
        if save_fail:
            reasons.append("storage_save_failure")
        if restore_fail:
            reasons.append("restore_validation_rejects")
        if thermal_fail:
            # PUBLIC_SAFE: count-driven signal only; the reason is an exception
            # class name (no message/traceback), surfaced via storage section.
            reasons.append("thermal_finalize_failure")
        if any(v not in ("valid", "missing") for v in ledger.values()):
            reasons.append("learning_ledger_unsafe")
        status = "healthy"
        if "storage_save_failure" in reasons or "learning_ledger_unsafe" in reasons:
            status = "degraded"
        return {
            "overall_status": status,
            "reason_codes": reasons,
            "deterministic_control_available": True,
            "learning_available": not reasons,
            "storage_healthy": save_fail == 0 and restore_fail == 0,
            "dispatch_healthy": True,
        }

    def _inputs_summary():
        # PUBLIC: per-source-status counts across windows (no ids/values).
        from .learning_trace_builder import build_input_provenance
        windows = list((getattr(c, "windows", {}) or {}).keys())
        solar = {"measured": 0, "fallback": 0, "not_configured": 0, "missing": 0}
        indoor_missing = 0
        forecast_configured = 0
        for wid in windows:
            prov = build_input_provenance(c, wid)
            st = prov.get("solar", {}).get("selected_solar_source_status")
            if st in solar:
                solar[st] += 1
            if prov.get("indoor_temperature", {}).get("source_status") == "missing":
                indoor_missing += 1
            if prov.get("forecast", {}).get("forecast_configured"):
                forecast_configured += 1
        return {"windows": len(windows), "solar_source_status": solar,
                "indoor_missing": indoor_missing, "forecast_configured": forecast_configured,
                # EMA (v1.2.0-beta.1, T4): configuration state only, never a raw
                # or smoothed sensor reading — consistent with this section's
                # existing counts/status-only, no-raw-value discipline.
                "ema_enabled": bool(getattr(c, "_ema_enabled", False)),
                "ema_alpha": getattr(c, "_ema_alpha", 0.3)}

    def _presence_summary():
        # PUBLIC: policy/config + aggregate status only — no entity ids, no
        # person names, matching this contract's privacy-first discipline.
        policy = getattr(c, "_presence_policy", None)
        reading = getattr(c, "_cycle_presence_reading", None)
        return {
            "policy": policy.value if policy is not None else None,
            "entity_count": len(getattr(c, "_presence_entity_ids", None) or []),
            "raw_status": reading.value if reading is not None else None,
            "absence_active": getattr(c, "_cycle_absence_active", None),
        }

    def _lifecycle_profile_summary():
        # PUBLIC: profile system state + resolution provenance only — NO
        # profile display names (free text a user could set to anything,
        # e.g. an address), no full profile contents/schedules, and (T6
        # pre-push review correction) no raw profile_id either: even though
        # it is a generated uuid4 hex rather than personally identifying, it
        # is internal technical context with no concrete diagnostic value on
        # its own (a support reader can't act on an opaque id), and for the
        # "fallback" case it would echo back a STALE/unknown id from
        # corrupted storage — actively confusing rather than useful.
        # active_profile_selected (a plain bool) already answers everything
        # a diagnostics reader needs: "is a specific profile in effect right
        # now, or is this the legacy config".
        source = getattr(c, "_lifecycle_profile_source", "legacy")
        count = getattr(c, "_lifecycle_profile_count", 0)
        active_id = getattr(c, "_active_lifecycle_profile_id", None)
        return {
            "enabled": count > 0,
            "profile_count": count,
            "active_profile_selected": active_id is not None,
            "source": source,  # "legacy" | "stored" | "fallback"
        }

    def _heat_protection_summary():
        # PUBLIC: configuration + aggregate count only — no window ids, no
        # temperature readings (those are per-window/support-export detail,
        # see engines/support_export.py's prov["heat"]). Uses the same
        # window_id -> bool latch the Coordinator persists across cycles
        # (self._heat_active, engines/heat_hysteresis.py) — no per-window
        # breakdown, matching this contract's counts/status-only discipline.
        comfort = getattr(c, "_comfort_config", None)
        active_latch = getattr(c, "_heat_active", None) or {}
        return {
            "enabled": bool(getattr(comfort, "heat_protection_enabled", True)),
            "hysteresis_c": getattr(comfort, "heat_protection_hysteresis_c", 1.0),
            "active_window_count": sum(1 for v in active_latch.values() if v),
        }

    def _manual_override_summary():
        # PUBLIC: policy configuration + aggregate counts only. No window/
        # cover ids, no override positions, no per-window override details,
        # no profile/person names. fixed_time_configured is a plain bool
        # (not the configured clock time) — the exact value has no
        # diagnostic use for support and is not needed to confirm the
        # feature is wired correctly. nearest_expiry_remaining_min is a
        # RELATIVE duration in minutes (never an absolute timestamp),
        # rounded to 1 decimal place, clamped to >= 0.0 (never negative —
        # an override whose expiry has technically already passed but has
        # not yet been garbage-collected by the detector's own expiry check
        # reports 0.0, not a negative value), and is entirely
        # un-attributable to any specific window (only the minimum across
        # all currently active overrides is reported, not a
        # window_id -> value mapping). Uses OverrideDetector's own PUBLIC
        # active_overrides_snapshot(now) API — which already excludes
        # anything with expires_at <= now — never reaches into the
        # detector's private per-window dict directly.
        detector = getattr(c, "_override_detector", None)
        active_count = 0
        nearest_remaining_min = None
        if detector is not None:
            try:
                snapshot = detector.active_overrides_snapshot(now)
                active_count = len(snapshot)
                if snapshot:
                    remaining = [
                        (datetime.fromisoformat(ov["expires_at"]) - now).total_seconds() / 60
                        for ov in snapshot
                    ]
                    nearest_remaining_min = round(max(0.0, min(remaining)), 1)
            except Exception:
                active_count = 0
                nearest_remaining_min = None
        return {
            "duration_mode": getattr(c, "_override_duration_mode", "legacy"),
            "fixed_time_configured": getattr(c, "_override_fixed_until", None) is not None,
            "allow_comfort": bool(getattr(c, "_override_allow_comfort_actions", False)),
            "allow_protection": bool(getattr(c, "_override_allow_protection_actions", False)),
            "break_on_lifecycle": bool(getattr(c, "_override_break_on_lifecycle", True)),
            "active_override_count": active_count,
            "nearest_expiry_remaining_min": nearest_remaining_min,
        }

    def _learning_authority_summary():
        # PUBLIC: learning-authority counts + blocking-reason histogram (no ids).
        from .learning_trace_builder import build_learning_authority
        windows = list((getattr(c, "windows", {}) or {}).keys())
        learning_on = active_on = pos_applied = strat_applied = 0
        blocking: dict = {}
        for wid in windows:
            la = build_learning_authority(c, wid)
            learning_on += int(la["learning_enabled"])
            active_on += int(la["active_control_enabled"])
            pos_applied += int(la["position_adoption_applied"])
            strat_applied += int(la["strategy_adoption_applied"])
            for r in la["blocking_reasons"]:
                blocking[r] = blocking.get(r, 0) + 1
        return {"windows": len(windows), "windows_learning_on": learning_on,
                "windows_active_control_on": active_on,
                "position_adoption_applied": pos_applied,
                "strategy_adoption_applied": strat_applied,
                "blocking_reason_counts": blocking}

    def _decisions():
        # PUBLIC: counts only — no ids, states or positions.
        snap = _call(c, "decision_trace_snapshot") or {}
        per_zone = {}
        no_dispatch = 0
        for i, (_zid, z) in enumerate(snap.items()):
            per_zone[f"zone_{i}"] = z.get("count", 0)
            for r in z.get("records", []):
                nd = r.get("no_dispatch", {})
                if nd.get("command_sent") is False:
                    no_dispatch += 1
        return {"material_decisions_by_zone": per_zone,
                "no_dispatch_decisions": no_dispatch}

    contract = {
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        "generated_at_utc": _iso(now),
        "integration_version": integration_version,
        "system": _safe(_system, errors, "system"),
        "inputs_summary": _safe(_inputs_summary, errors, "inputs"),
        "presence_summary": _safe(_presence_summary, errors, "presence"),
        "lifecycle_profile_summary": _safe(_lifecycle_profile_summary, errors, "lifecycle_profile"),
        "heat_protection_summary": _safe(_heat_protection_summary, errors, "heat_protection"),
        "manual_override_summary": _safe(_manual_override_summary, errors, "manual_override"),
        "learning_authority_summary": _safe(
            _learning_authority_summary, errors, "learning_authority"),
        "decisions": _safe(_decisions, errors, "decisions"),
        "learning_active_control_matrix": _safe(_matrix, errors, "matrix"),
        "learning": _safe(_learning, errors, "learning"),
        "execution": _safe(_execution, errors, "execution"),
        "storage": _safe(_storage, errors, "storage"),
        "validation": _safe(_validation, errors, "validation"),
        "health": _safe(_health, errors, "health"),
        "section_errors": errors,
    }
    # Bounded depth + string caps + JSON-safety guard (never emit NaN/Infinity).
    contract = enforce_depth(truncate_strings(contract))
    if not is_json_safe(contract):
        contract = {"schema_version": DIAGNOSTICS_SCHEMA_VERSION,
                    "generated_at_utc": _iso(now), "section_errors": {"json_safety": 1}}
    return contract


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _call(c, name):
    fn = getattr(c, name, None)
    if fn is None:
        return None
    try:
        return fn()
    except Exception as exc:
        # F7: previously indistinguishable from "getter not implemented" (the
        # fn is None branch above) — logged so a real coordinator-getter bug
        # is diagnosable instead of silently looking like a missing feature.
        _LOGGER.debug(
            "diagnostics_builder: %s() failed (%s: %s)", name, type(exc).__name__, exc)
        return None


def _ledger_integrity(c) -> dict:
    integ = getattr(c, "_ledger_integrity", None)
    if integ is None:
        return {}
    return {"position": getattr(integ, "position", "unknown"),
            "strategy": getattr(integ, "strategy", "unknown")}


def _zone_hash(c, zid: str) -> str:
    # PUBLIC contract uses positional zone labels (no ids); keep stable order.
    zones = list((getattr(c, "zones", {}) or {}).keys())
    try:
        return f"zone_{zones.index(zid)}"
    except ValueError:
        return "zone_x"
