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

from datetime import datetime, timezone

from .diagnostics_privacy import enforce_depth, is_json_safe, truncate_strings

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
            except Exception:
                cfg = None
            learning = bool(getattr(cfg, "learning_enabled", False))
            active = bool(getattr(cfg, "active_control_enabled", False))
            out[_zone_hash(c, zid)] = {
                "learning_enabled": learning,
                "active_control_enabled": active,
                "observation_active": learning,
                "shadow_evaluation_active": learning,
                "experiments_allowed": learning and active,
                "real_experiments_allowed": learning and active,
                "adoptions_allowed": learning,
                "cover_commands_allowed": active,
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
        ledger = _ledger_integrity(c)
        reasons: list[str] = []
        if save_fail:
            reasons.append("storage_save_failure")
        if restore_fail:
            reasons.append("restore_validation_rejects")
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
    except Exception:
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
