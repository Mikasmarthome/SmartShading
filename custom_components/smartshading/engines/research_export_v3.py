"""Research Export v3 — LE 2.0 / Phase P11.9 (read-only, HA-free, duck-typed).

Privacy-safe, bounded, deterministic research export that answers baseline-vs-
adapted HONESTLY from data that is ALREADY persisted (no new ring, no fabricated
counterfactual, no current snapshot used as history):

  source per material decision = LearningStore decision records, whose
  ProvenanceSummary carries baseline_target_ha vs final_target_ha (adapted) +
  adaptation_sources + dispatch_status (executed), with the linked DecisionOutcome
  (observed) and the exact decision_id (attribution).  Survivorship from the
  persisted adoption terminal history.

Never mutates runtime, never dispatches, never saves, never resolves an outcome.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .diagnostics_privacy import (
    DEFAULT_FORBIDDEN_MARKERS,
    MAX_NESTED_DEPTH,
    MAX_STRING_LENGTH,
    NS_ADOPTION,
    NS_DECISION,
    NS_ENTRY,
    NS_WINDOW,
    NS_ZONE,
    Pseudonymizer,
    cap_records,
    contains_forbidden_substring,
    enforce_depth,
    is_json_safe,
    truncate_strings,
)

RESEARCH_EXPORT_SCHEMA_VERSION: int = 3

MAX_RESEARCH_RECORDS_PER_ZONE = 500
MAX_RESEARCH_WINDOW_SUMMARIES = 60
MAX_RESEARCH_EXPORT_BYTES = 2_000_000
MIN_BUCKET_SAMPLE = 5  # smaller groups → insufficient_sample (no misleading stats)

# Improvement classification (single attributable thermal objective): neutral band.
_IMPROVEMENT_NEUTRAL_TOLERANCE = 0.10  # outcome_score is -1.0..+1.0

# Explicit, documented, deterministic bucket boundaries.
_CONFIDENCE_BUCKETS = ((0.0, 0.5, "low"), (0.5, 0.7, "medium"),
                       (0.7, 0.85, "high"), (0.85, 1.0001, "very_high"))
_SEASON_BY_MONTH = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring",
                    5: "spring", 6: "summer", 7: "summer", 8: "summer",
                    9: "autumn", 10: "autumn", 11: "autumn"}


def _iso_s(dt) -> str | None:
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


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _safe(fn, errors, name, default=None):
    try:
        return fn()
    except Exception:
        e = errors.setdefault(name, {"count": 0, "reason_codes": ["section_builder_failed"]})
        e["count"] += 1
        return default if default is not None else {"section_error": True}


def _learning_source_type(sources) -> str:
    s = {str(x) for x in (sources or ())}
    pos = any("position" in x or "target" in x for x in s)
    strat = any("strategy" in x or "threshold" in x or "timing" in x or "tier" in x for x in s)
    if pos and strat:
        return "position_and_strategy"
    if pos:
        return "position_learning"
    if strat:
        return "strategy_learning"
    if any("forecast" in x for x in s):
        return "forecast"
    return "none"


def build_research_export_v3(coordinator, *, now=None, integration_version="unknown") -> dict:
    now = now or datetime.now(timezone.utc)
    c = coordinator
    entry_id = getattr(getattr(c, "config_entry", None), "entry_id", None)
    pz = Pseudonymizer(entry_id)
    errors: dict = {}
    try:
        store = getattr(c, "_learning_store", None)
    except Exception:
        store = None
        errors["learning_store"] = {"count": 1, "reason_codes": ["store_access_failed"]}

    # ---- project persisted decisions into research records (honest) ----
    def _records():
        recs: list = []
        if store is None:
            return recs
        try:
            window_ids = list(store.window_ids())
        except Exception:
            return recs
        for wid in window_ids:
            try:
                decs = store.get_decisions(wid)
            except Exception:
                continue
            for d in decs or []:
                r = _project_decision(d, wid, pz)
                if r is not None:
                    recs.append(r)
        # deterministic order: by timestamp then decision_ref.
        recs.sort(key=lambda r: (r.get("decision_timestamp_utc") or "", r.get("decision_ref") or ""))
        return recs

    research_records = _safe(_records, errors, "research_records", default=[])
    capped, rec_trunc = cap_records(research_records if isinstance(research_records, list) else [],
                                    MAX_RESEARCH_RECORDS_PER_ZONE)

    contract: dict = {
        "research_export_schema_version": RESEARCH_EXPORT_SCHEMA_VERSION,
        "generated_at_utc": _iso_s(now),
        "integration_version": integration_version,
        "export_scope": "entry_zone",
        "research_contract": {
            "baseline": "deterministic ProvenanceSummary.baseline_target_ha (no learned delta)",
            "adapted": "ProvenanceSummary.final_target_ha after learned adaptation",
            "executed": "ProvenanceSummary.dispatch_status (actual dispatch outcome)",
            "observed": "linked DecisionOutcome (resolution_status/outcome_score/objectives)",
            "counterfactual": "not_available — no stored counterfactual simulation",
            "attribution": "attributable only when adapted, single learning source, "
                           "no confounder, outcome resolved complete",
        },
        "pseudonymization": _safe(lambda: {
            "algorithm": "hmac_sha256", "output_bits": 64, "namespace_separated": True,
            "stability_scope": "config_entry",
            "security_note": "deterministic pseudonymization, NOT strong anonymization "
                             "against an actor who knows the config entry id"},
            errors, "pseudonymization"),
        "system": _safe(lambda: {
            "entry_ref": pz.ref(NS_ENTRY, entry_id),
            "zone_ref": pz.ref(NS_ZONE, next(iter(getattr(c, "zones", {}) or {}), None)),
            "window_count": len(getattr(c, "windows", {}) or {})}, errors, "system"),
        "research_records": capped,
        "aggregations": _safe(lambda: _aggregations(research_records, c), errors, "aggregations"),
        "survivorship": _safe(lambda: _survivorship(c), errors, "survivorship"),
        "per_window_summaries": _safe(lambda: _per_window(capped, pz), errors,
                                      "per_window_summaries"),
        "data_availability": {
            "research_records": ("available" if research_records else "not_available"),
            "counterfactual": "not_available",
            "solar_buckets": "not_available",  # exposure not in the persisted summary
            "confidence_buckets": "available_from_adoption_history",
        },
        "section_errors": errors,
    }
    contract["reason_codes"] = {}
    contract = enforce_depth(truncate_strings(contract, max_len=MAX_STRING_LENGTH),
                             max_depth=MAX_NESTED_DEPTH)
    contract = _byte_cap(contract, rec_trunc)
    if not is_json_safe(contract):
        return {"research_export_schema_version": RESEARCH_EXPORT_SCHEMA_VERSION,
                "generated_at_utc": _iso_s(now),
                "section_errors": {"json_safety": {"count": 1, "reason_codes": ["json_unsafe"]}}}
    return contract


def _project_decision(d, wid, pz) -> dict | None:
    summary = getattr(d, "summary", None)
    if summary is None:
        return None  # only decisions with a recorded provenance summary are eligible
    base = _num(getattr(summary, "baseline_target_ha", None))
    adapted = _num(getattr(summary, "final_target_ha", None))
    sources = getattr(summary, "adaptation_sources", frozenset()) or frozenset()
    is_adapted = bool(sources) and base is not None and adapted is not None and base != adapted
    outcome = getattr(d, "outcome", None)
    confounders: list = []
    score = None
    resolution = None
    objective = None
    if outcome is not None:
        if getattr(outcome, "override_occurred", False):
            confounders.append("manual_override")
        mo = getattr(outcome, "multi_objective", None)
        if mo is not None and getattr(mo, "confounded", False):
            confounders.append("thermal_confounded")
        score = _num(getattr(outcome, "outcome_score", None))
        resolution = getattr(outcome, "resolution_status", None)
    attributable = bool(
        is_adapted and len([s for s in (_learning_source_type(sources),) if s != "none"]) == 1
        and not confounders and resolution == "complete")
    # improvement classification — ONLY for attributable, resolved, scored records.
    if attributable and score is not None:
        if score > _IMPROVEMENT_NEUTRAL_TOLERANCE:
            objective = "improved"
        elif score < -_IMPROVEMENT_NEUTRAL_TOLERANCE:
            objective = "degraded"
        else:
            objective = "neutral"
    else:
        objective = "inconclusive"
    return {
        "decision_ref": pz.ref(NS_DECISION, getattr(d, "decision_id", None)),
        "window_ref": pz.ref(NS_WINDOW, wid),
        "decision_timestamp_utc": _iso_s(getattr(d, "decision_timestamp", None)),
        "shading_state": getattr(summary, "shading_state", None),
        "baseline_target_ha": base,
        "adapted_target_ha": adapted,
        "is_adapted": is_adapted,
        "delta_baseline_adapted_ha": (adapted - base) if (is_adapted) else 0,
        "executed_dispatch_status": getattr(summary, "dispatch_status", None),
        "learning_source_type": _learning_source_type(sources),
        "outcome_status": getattr(d, "outcome_status", None),
        "outcome_resolution_status": resolution,
        "outcome_score": score,
        "confounder_codes": confounders,
        "attribution_status": ("attributable" if attributable
                               else ("confounded" if confounders else "inconclusive")),
        "thermal_objective_classification": objective,
        "season_bucket": _SEASON_BY_MONTH.get(
            getattr(getattr(d, "decision_timestamp", None), "month", 0), "unknown"),
        "lifecycle_bucket": getattr(outcome, "lifecycle_state", None) if outcome else None,
    }


def _aggregations(records, coord) -> dict:
    total = len(records)
    adapted = [r for r in records if r.get("is_adapted")]
    dispatched_adapted = [r for r in adapted
                          if r.get("executed_dispatch_status") in ("sent", "SENT")]
    with_outcome = [r for r in records if r.get("outcome_status") not in (None, "none", "pending")]
    attributable = [r for r in records if r.get("attribution_status") == "attributable"]
    improved = [r for r in attributable if r.get("thermal_objective_classification") == "improved"]
    neutral = [r for r in attributable if r.get("thermal_objective_classification") == "neutral"]
    degraded = [r for r in attributable if r.get("thermal_objective_classification") == "degraded"]
    inconclusive = [r for r in records if r.get("attribution_status") != "attributable"]
    return {
        "total_eligible_decisions": total,
        "baseline_unchanged_count": total - len(adapted),
        "adapted_count": len(adapted),
        "adaptation_rate": round(len(adapted) / total, 4) if total else 0.0,
        "dispatched_adapted_count": len(dispatched_adapted),
        "outcomes_observed": len(with_outcome),
        "outcomes_attributable": len(attributable),
        "outcomes_inconclusive": len(inconclusive),
        # better/worse stated ONLY for attributable thermal objective; no global score.
        "thermal_objective": {
            "improved_count": len(improved),
            "neutral_count": len(neutral),
            "degraded_count": len(degraded),
            "sample": len(attributable),
            "sample_status": ("available" if len(attributable) >= MIN_BUCKET_SAMPLE
                              else "insufficient_sample"),
        },
        "by_learning_source": _bucket_counts(records, "learning_source_type"),
        "by_season": _bucket_counts(records, "season_bucket"),
        "by_lifecycle": _bucket_counts(records, "lifecycle_bucket"),
        "confidence_buckets": _confidence_buckets(coord),
        "aggregation_scope": ("exported_records" if len(records) <= MAX_RESEARCH_RECORDS_PER_ZONE
                              else "exported_records_truncated"),
    }


def _bucket_counts(records, key) -> dict:
    out: dict = {}
    for r in records:
        b = r.get(key)
        if b is None:
            b = "unknown"
        out[str(b)] = out.get(str(b), 0) + 1
    return dict(sorted(out.items()))


def _confidence_buckets(coord) -> dict:
    out = {name: 0 for _lo, _hi, name in _CONFIDENCE_BUCKETS}
    sample = 0
    for hist_attr in ("_adoption_history", "_strategy_adoption_history"):
        for a in getattr(coord, hist_attr, []) or []:
            cv = _num(getattr(a, "confidence", None))
            if cv is None:
                continue
            sample += 1
            for lo, hi, name in _CONFIDENCE_BUCKETS:
                if lo <= cv < hi:
                    out[name] += 1
                    break
    out["sample"] = sample
    out["sample_status"] = "available" if sample >= MIN_BUCKET_SAMPLE else "insufficient_sample"
    return out


def _survivorship(coord) -> dict:
    """Rejected / rolled-back / invalidated / expired adoptions are NOT dropped —
    survivorship-bias guard."""
    terminal = {"rolled_back", "rejected", "invalidated", "expired", "reduced"}
    counts: dict = {}
    for hist_attr in ("_adoption_history", "_strategy_adoption_history"):
        for a in getattr(coord, hist_attr, []) or []:
            st = getattr(a, "status", None)
            if st in terminal:
                counts[st] = counts.get(st, 0) + 1
    return {"terminal_adoption_counts": dict(sorted(counts.items())),
            "note": "rejected/rolled_back/expired adoptions retained (no survivorship bias)"}


def _per_window(records, pz) -> list:
    by_window: dict = {}
    for r in records:
        wref = r.get("window_ref")
        s = by_window.setdefault(wref, {"window_ref": wref, "decisions": 0, "adapted": 0,
                                        "attributable": 0})
        s["decisions"] += 1
        if r.get("is_adapted"):
            s["adapted"] += 1
        if r.get("attribution_status") == "attributable":
            s["attributable"] += 1
    out = sorted(by_window.values(), key=lambda x: x["window_ref"] or "")
    return out[:MAX_RESEARCH_WINDOW_SUMMARIES]


def _byte_cap(contract: dict, rec_trunc: dict) -> dict:
    original = _bytes(contract)
    sections: dict = {}
    if original <= MAX_RESEARCH_EXPORT_BYTES:
        contract["truncation"] = {"applied": False, "original_bytes": original,
                                  "final_bytes": original, "byte_cap": MAX_RESEARCH_EXPORT_BYTES,
                                  "sections": {"research_records": rec_trunc}}
        return contract
    recs = contract.get("research_records")
    if isinstance(recs, list):
        orig = len(recs)
        while recs and _bytes(contract) > MAX_RESEARCH_EXPORT_BYTES:
            recs.pop(0)  # oldest-first (records are time-sorted ascending)
        sections["research_records"] = {"original_count": orig, "final_count": len(recs),
                                        "removed_count": orig - len(recs)}
    contract["truncation"] = {"applied": True, "original_bytes": original,
                              "final_bytes": _bytes(contract),
                              "byte_cap": MAX_RESEARCH_EXPORT_BYTES, "sections": sections}
    return contract


def _bytes(obj) -> int:
    try:
        return len(json.dumps(obj, default=str).encode("utf-8"))
    except Exception:
        return 0


def privacy_scan(export: dict) -> list:
    blob = json.dumps(export, default=str)
    found = [m for m in DEFAULT_FORBIDDEN_MARKERS if m in blob.lower()]
    if contains_forbidden_substring(export, DEFAULT_FORBIDDEN_MARKERS):
        found.append("nested_forbidden")
    return found
