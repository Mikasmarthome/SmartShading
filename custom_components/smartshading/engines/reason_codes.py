"""Reason-code registry — LE 2.0 / Phase P11 (pure).

Inventories the stable machine-readable reason codes already emitted across the
P2–P10 control/learning/storage paths and adds P11-specific diagnostics codes.
This is an EXPORT-side registry only: it does NOT rename or migrate existing
production reason strings (no control-path change for formal centralisation).
Localized user texts live in translations/ — codes here stay machine-primary.

No Home Assistant import.
"""
from __future__ import annotations

from dataclasses import dataclass

# categories
CAT_DECISION = "decision"
CAT_AUTHORITY = "authority_gate"
CAT_EXPERIMENT = "experiment_eligibility"
CAT_ADOPTION = "adoption_gate"
CAT_ROLLBACK = "rollback_reduction"
CAT_STORAGE = "storage_validation"
CAT_MIGRATION = "restore_migration"
CAT_DISPATCH = "dispatch_filter"
CAT_NO_DISPATCH = "no_dispatch"
CAT_OVERRIDE = "manual_override"
CAT_FORECAST = "forecast_trust"
CAT_HEALTH = "health"

# severities
SEV_INFO = "info"
SEV_OPERATIONAL = "operational"
SEV_DEGRADED = "degraded"
SEV_ERROR = "error"

# product visibility flags
VIS_DIAGNOSTICS = "diagnostics"
VIS_SUPPORT = "support"
VIS_RESEARCH = "research"


@dataclass(frozen=True)
class ReasonCode:
    code: str
    category: str
    machine_semantics: str
    description: str
    severity: str
    product_visibility: tuple[str, ...]


def _r(code, cat, sem, desc, sev=SEV_INFO,
       vis=(VIS_DIAGNOSTICS, VIS_SUPPORT)) -> ReasonCode:
    return ReasonCode(code, cat, sem, desc, sev, vis)


# Inventory of stable codes confirmed in the P2–P10 production paths + P11 codes.
_REGISTRY: dict[str, ReasonCode] = {r.code: r for r in (
    # --- storage / ledger / validation (P10) ---
    _r("ledger_integrity_unsafe", CAT_STORAGE, "ledger namespace corrupt/unsupported/owner-mismatch",
       "Consumed-experiment ledger integrity unsafe; adaptive authority blocked.", SEV_DEGRADED),
    _r("owner_mismatch", CAT_STORAGE, "payload owner != current entry",
       "Stored payload belongs to a different config entry; rejected.", SEV_DEGRADED),
    _r("unsupported_schema", CAT_STORAGE, "payload schema newer than supported",
       "Stored payload schema is newer than this version supports.", SEV_DEGRADED),
    _r("nan_or_infinity", CAT_STORAGE, "non-finite numeric in record",
       "Record contained NaN/Infinity and was skipped.", SEV_OPERATIONAL),
    _r("negative_count", CAT_STORAGE, "negative count field",
       "Record had a negative count and was skipped.", SEV_OPERATIONAL),
    _r("future_timestamp", CAT_STORAGE, "timestamp beyond safe tolerance",
       "Record timestamp was implausibly in the future and was skipped.", SEV_OPERATIONAL),
    _r("invalid_timestamp", CAT_STORAGE, "unparseable/naive timestamp",
       "Record timestamp was invalid and the record was skipped.", SEV_OPERATIONAL),
    _r("duplicate_id", CAT_STORAGE, "duplicate stable id",
       "Duplicate record id encountered; de-duplicated.", SEV_INFO),
    _r("ambiguous_duplicate", CAT_STORAGE, "conflicting duplicate id",
       "Conflicting duplicate id; all conflicting copies rejected.", SEV_OPERATIONAL),
    _r("unsupported_record_version", CAT_MIGRATION, "record schema newer than supported",
       "Individual record version newer than supported; skipped.", SEV_OPERATIONAL),
    # --- adoption / reference (P8/P9B/P10) ---
    _r("missing_source_experiment", CAT_ADOPTION, "hard source experiment unresolved",
       "Adoption references a source experiment that does not resolve; suspended.", SEV_DEGRADED),
    _r("missing_source_experiments", CAT_ADOPTION, "no source experiment evidence",
       "Adoption has no source experiment evidence; invalidated.", SEV_DEGRADED),
    _r("config_generation_changed", CAT_ADOPTION, "config generation mismatch",
       "Adoption suspended because the config generation changed.", SEV_OPERATIONAL),
    _r("context_incompatible", CAT_ADOPTION, "context family mismatch",
       "Adoption suspended because the current context is incompatible.", SEV_OPERATIONAL),
    _r("manual_preference_active", CAT_ADOPTION, "manual preference present",
       "Adoption not applied because a manual preference is active.", SEV_INFO),
    _r("learning_mode_off", CAT_ADOPTION, "learning disabled",
       "Adoption suspended because learning mode is off.", SEV_INFO),
    # --- forecast ---
    _r("forecast_provider_changed", CAT_FORECAST, "provider fingerprint changed",
       "Forecast provider/source changed; old trust authority not restored.", SEV_DEGRADED),
    # --- no-dispatch (P11) ---
    _r("active_control_off", CAT_NO_DISPATCH, "active control disabled",
       "No cover command: active control is off (recommendation-only).", SEV_OPERATIONAL),
    _r("learning_only", CAT_NO_DISPATCH, "learning without active control",
       "No cover command: learning/observation only.", SEV_OPERATIONAL),
    _r("same_target", CAT_NO_DISPATCH, "target equals current",
       "No cover command: target equals current position.", SEV_INFO),
    _r("within_position_tolerance", CAT_NO_DISPATCH, "delta within tolerance",
       "No cover command: target within position tolerance.", SEV_INFO),
    _r("state_guard_locked", CAT_NO_DISPATCH, "minimum-hold lock active",
       "No cover command: state guard minimum-hold lock active.", SEV_INFO),
    _r("minimum_action_interval", CAT_NO_DISPATCH, "per-window min interval",
       "No cover command: minimum action interval not elapsed.", SEV_INFO),
    _r("global_dispatch_wait", CAT_NO_DISPATCH, "global serial dispatch wait",
       "No cover command yet: waiting for the global dispatch interval.", SEV_INFO),
    _r("cover_unavailable", CAT_NO_DISPATCH, "cover unavailable",
       "No cover command: cover entity unavailable.", SEV_DEGRADED),
    _r("missing_position_feedback", CAT_NO_DISPATCH, "no reliable position feedback",
       "No cover command: reliable position feedback missing.", SEV_OPERATIONAL),
    _r("behavior_mode_hold", CAT_NO_DISPATCH, "behavior mode holds",
       "No cover command: behavior mode holds the cover.", SEV_INFO),
    _r("startup_grace", CAT_NO_DISPATCH, "startup grace period",
       "No cover command: startup grace period active.", SEV_INFO),
    _r("manual_override_hold", CAT_NO_DISPATCH, "manual override active",
       "No cover command: manual override active.", SEV_OPERATIONAL),
    _r("safety_hold", CAT_NO_DISPATCH, "safety state holds",
       "No cover command: safety state holds the cover.", SEV_OPERATIONAL),
    _r("dispatch_not_required", CAT_NO_DISPATCH, "no change required",
       "No cover command required this cycle.", SEV_INFO),
    # --- health (P11) ---
    _r("missing_optional_input", CAT_HEALTH, "optional input missing",
       "An optional input is missing; deterministic control unaffected.", SEV_OPERATIONAL,
       (VIS_DIAGNOSTICS, VIS_SUPPORT)),
    _r("forecast_unavailable", CAT_HEALTH, "forecast unavailable",
       "Forecast planning unavailable; current measured control unaffected.", SEV_OPERATIONAL),
    _r("storage_save_failure", CAT_HEALTH, "learning save failed",
       "A learning store save failed; data preserved dirty for retry.", SEV_DEGRADED),
    _r("restore_validation_rejects", CAT_HEALTH, "restore rejected records",
       "Restore rejected one or more records during validation.", SEV_OPERATIONAL),
)}


def get(code: str) -> ReasonCode | None:
    return _REGISTRY.get(code)


def describe(code: str) -> dict:
    """Privacy-safe registry description for a code (export-ready); unknown codes
    get a stable 'unknown' fallback so exports never break on a new runtime code."""
    rc = _REGISTRY.get(code)
    if rc is None:
        return {"code": code, "category": "unknown", "severity": SEV_INFO,
                "description": "Unregistered reason code.", "registered": False}
    return {"code": rc.code, "category": rc.category, "severity": rc.severity,
            "machine_semantics": rc.machine_semantics, "description": rc.description,
            "registered": True}


def registry_for_codes(codes) -> dict:
    """Build the reason-code dictionary section for a set of emitted codes."""
    seen: dict = {}
    for c in codes:
        if c and c not in seen:
            seen[c] = describe(c)
    return seen


def all_codes() -> tuple[str, ...]:
    return tuple(_REGISTRY.keys())
