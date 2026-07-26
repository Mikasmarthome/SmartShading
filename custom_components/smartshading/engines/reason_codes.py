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
CAT_ADOPTION = "adoption_gate"
CAT_STORAGE = "storage_validation"
CAT_MIGRATION = "restore_migration"
CAT_NO_DISPATCH = "no_dispatch"
CAT_FORECAST = "forecast_trust"
CAT_HEALTH = "health"

# severities
SEV_INFO = "info"
SEV_OPERATIONAL = "operational"
SEV_DEGRADED = "degraded"

# product visibility flags
VIS_DIAGNOSTICS = "diagnostics"
VIS_SUPPORT = "support"


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
    _r("cover_unavailable", CAT_NO_DISPATCH, "cover unavailable",
       "No cover command: cover entity unavailable.", SEV_DEGRADED),
    _r("behavior_mode_hold", CAT_NO_DISPATCH, "behavior mode holds",
       "No cover command: behavior mode holds the cover.", SEV_INFO),
    _r("startup_grace", CAT_NO_DISPATCH, "startup grace period",
       "No cover command: startup grace period active.", SEV_INFO),
    _r("dispatch_not_required", CAT_NO_DISPATCH, "no change required",
       "No cover command required this cycle.", SEV_INFO),
    # --- command-filter block reasons (cover_control/command_filter.py) ---
    _r("same_position", CAT_NO_DISPATCH, "target within tolerance of current",
       "No cover command: already at the target position (within tolerance; no command needed).",
       SEV_INFO),
    _r("no_target_position", CAT_NO_DISPATCH, "no actionable target position",
       "No cover command: no actionable target position — the current mode/decision "
       "intentionally holds or suppresses dispatch.", SEV_INFO),
    _r("recommendation_only", CAT_NO_DISPATCH, "recommendation-only execution mode",
       "No cover command: execution is recommendation-only (active control off).",
       SEV_OPERATIONAL),
    _r("guard_action_interval", CAT_NO_DISPATCH, "state-guard minimum action interval",
       "No cover command: the state-guard minimum action interval has not elapsed.",
       SEV_INFO),
    _r("presence_uncertain", CAT_NO_DISPATCH, "presence configured but undeterminable",
       "No cover command: presence is configured but currently undeterminable "
       "(all presence entities unknown/unavailable, e.g. just after a restart), "
       "so the daytime fallback holds instead of opening fully until presence is "
       "known.", SEV_INFO),
    # --- health (P11) ---
    _r("forecast_unavailable", CAT_HEALTH, "forecast unavailable",
       "Forecast planning unavailable; current measured control unaffected.", SEV_OPERATIONAL),
    _r("storage_save_failure", CAT_HEALTH, "learning save failed",
       "A learning store save failed; data preserved dirty for retry.", SEV_DEGRADED),
    _r("restore_validation_rejects", CAT_HEALTH, "restore rejected records",
       "Restore rejected one or more records during validation.", SEV_OPERATIONAL),
)}


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
