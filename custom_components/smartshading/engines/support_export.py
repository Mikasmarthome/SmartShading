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

MAX_SUPPORT_DECISIONS_PER_ZONE = 100
MAX_SUPPORT_DISPATCHES_PER_ZONE = 100
MAX_SUPPORT_NO_DISPATCHES_PER_ZONE = 100
MAX_SUPPORT_OUTCOMES_PER_ZONE = 50
MAX_SUPPORT_LEARNING_TRANSITIONS_PER_ZONE = 50
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


def _safe(fn, errors, name, default=None):
    try:
        return fn()
    except Exception:
        e = errors.setdefault(name, {"count": 0, "reason_codes": ["section_builder_failed"]})
        e["count"] += 1
        return default if default is not None else {"section_error": True}


def build_support_export_v3(coordinator, *, now=None, integration_version="unknown") -> dict:
    """Build the v3 support export for one config entry / its zone."""
    now = now or datetime.now(timezone.utc)
    c = coordinator
    entry_id = getattr(getattr(c, "config_entry", None), "entry_id", None)
    pz = Pseudonymizer(entry_id)
    errors: dict = {}

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
        "integration_version": integration_version,
        "export_scope": "entry_zone",
        "pseudonymization": _safe(_meta, errors, "pseudonymization"),
        "system": _safe(_system, errors, "system"),
        "configuration": _safe(_configuration, errors, "configuration"),
        "health": _safe(_health, errors, "health"),
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
