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
from ..models.runtime_mode import derive_authority
from ..models.decision_provenance import ProvenanceSummary

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


def _zone_runtime_mode(c) -> str:
    """Derived runtime mode of the (first) zone — PUBLIC_SAFE label only."""
    try:
        zid = next(iter(getattr(c, "zones", {}) or {}), None)
        eff = getattr(c, "effective_zone_execution", None)
        cfg = eff(zid) if (eff and zid is not None) else None
        return derive_authority(
            bool(getattr(cfg, "learning_enabled", False)),
            bool(getattr(cfg, "active_control_enabled", False)),
        ).mode.value
    except Exception:
        return "unknown"


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
    # Exclusion accounting: excluded records must never silently vanish (survivorship
    # bias guard) — every examined decision is counted as eligible or excluded-by-reason.
    accounting = {"examined": 0, "excluded": {}}

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
                accounting["examined"] += 1
                # A record is eligible if it carries EITHER a compact summary
                # (demoted records) OR full provenance (recent records).  Reading
                # only `summary` would silently drop every recent full-provenance
                # decision — i.e. the entire active window of interest.
                if (getattr(d, "summary", None) is None
                        and getattr(d, "provenance", None) is None):
                    accounting["excluded"]["no_provenance"] = (
                        accounting["excluded"].get("no_provenance", 0) + 1)
                    continue
                r = _project_decision(d, wid, pz)
                if r is None:
                    accounting["excluded"]["not_projectable"] = (
                        accounting["excluded"].get("not_projectable", 0) + 1)
                    continue
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
            "window_count": len(getattr(c, "windows", {}) or {}),
            "runtime_mode": _zone_runtime_mode(c),
            "rain_sensor_configured": getattr(c, "_rain_sensor_id", None) is not None,
            "contact_sensor_any_configured": any(
                getattr(w, "contact_sensor_entity_id", None) is not None
                for w in (getattr(c, "windows", {}) or {}).values()
            ),
        }, errors, "system"),
        "research_records": capped,
        "aggregations": _safe(lambda: _aggregations(research_records, c), errors, "aggregations"),
        "survivorship": _safe(lambda: _survivorship(c), errors, "survivorship"),
        "per_window_summaries": _safe(lambda: _per_window(capped, pz), errors,
                                      "per_window_summaries"),
        "record_accounting": {
            "total_decisions_examined": accounting["examined"],
            "eligible_record_count": (len(research_records)
                                      if isinstance(research_records, list) else 0),
            "excluded_record_count": sum(accounting["excluded"].values()),
            "excluded_by_reason": dict(sorted(accounting["excluded"].items())),
            "capped_record_count": len(capped),
            "serialized_record_count": None,  # filled after byte truncation below
            "record_cap": MAX_RESEARCH_RECORDS_PER_ZONE,
        },
        "data_availability": {
            "research_records": ("available" if research_records else "not_available"),
            "counterfactual": "not_available",
            "solar_buckets": "not_available",  # exposure not in the persisted summary
            "confidence_buckets": "available_from_adoption_history",
            "reliability_buckets": "not_available",   # not in the persisted summary
            "behavior_mode_buckets": "not_available",  # not in the persisted summary
            "per_decision_confidence": "not_available",  # only adoption snapshots exist
            "per_decision_reliability": "not_available",
        },
        "section_errors": errors,
    }
    contract["reason_codes"] = {}
    contract = enforce_depth(truncate_strings(contract, max_len=MAX_STRING_LENGTH),
                             max_depth=MAX_NESTED_DEPTH)
    contract = _byte_cap(contract, rec_trunc)
    # serialized_record_count is only known AFTER byte truncation removed records.
    if (isinstance(contract.get("record_accounting"), dict)
            and isinstance(contract.get("research_records"), list)):
        contract["record_accounting"]["serialized_record_count"] = len(
            contract["research_records"])
    if not is_json_safe(contract):
        return {"research_export_schema_version": RESEARCH_EXPORT_SCHEMA_VERSION,
                "generated_at_utc": _iso_s(now),
                "section_errors": {"json_safety": {"count": 1, "reason_codes": ["json_unsafe"]}}}
    return contract


def _first_attr(obj, *names):
    """Return the first present, non-None attribute among ``names``."""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return None


def _project_decision(d, wid, pz) -> dict | None:
    summary = getattr(d, "summary", None)
    if summary is None:
        # Recent records keep full provenance (summary is only populated when a
        # record is demoted beyond the full-retention window).  Derive the same
        # compact view from provenance so the active window is not excluded.
        prov = getattr(d, "provenance", None)
        if prov is not None:
            try:
                summary = ProvenanceSummary.from_provenance(prov)
            except Exception:
                summary = None
    if summary is None:
        return None
    # The real ProvenanceSummary uses *_requested_target_ha; tolerate the short
    # field names as a fallback so both real and synthetic records are read.
    base = _num(_first_attr(summary, "baseline_requested_target_ha", "baseline_target_ha"))
    adapted = _num(_first_attr(summary, "final_requested_target_ha", "final_target_ha"))
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
        if mo is not None:
            # The real model exposes confounded on multi_objective.reliability; keep
            # the flat mo.confounded read for backward-compatible callers/tests.
            _rel = getattr(mo, "reliability", None)
            if getattr(_rel, "confounded", False) or getattr(mo, "confounded", False):
                confounders.append("thermal_confounded")
            # Solar source was a fallback/low quality at decision time → make the
            # measured-vs-fallback difference visible and non-attributable.
            _conf = getattr(mo, "confounders", None)
            if getattr(_conf, "solar_fallback_confounded", False):
                confounders.append("solar_fallback")
        score = _num(getattr(outcome, "outcome_score", None))
        resolution = getattr(outcome, "resolution_status", None)
    src_type = _learning_source_type(sources)
    # Attribution requires exactly ONE primitive learning source.  "position_and_strategy"
    # is multi-source and must never receive single-source credit (P11.9 contract).
    single_source = src_type in ("position_learning", "strategy_learning", "forecast")
    # baseline comparison kept honest: missing base/adapted is "unknown", never
    # silently folded into "baseline unchanged".
    if base is None or adapted is None:
        baseline_comparison = "unknown"
    elif is_adapted:
        baseline_comparison = "adapted"
    else:
        baseline_comparison = "unchanged"
    attributable = bool(
        is_adapted and single_source and not confounders and resolution == "complete")
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
    if confounders:
        attribution_status = "confounded"
    elif attributable:
        attribution_status = "attributable"
    elif is_adapted and src_type == "position_and_strategy":
        # multiple learning sources contributed — kept in total/adapted counts but
        # never credited to a single source.
        attribution_status = "multi_source"
    else:
        attribution_status = "inconclusive"
    return {
        "decision_ref": pz.ref(NS_DECISION, getattr(d, "decision_id", None)),
        "window_ref": pz.ref(NS_WINDOW, wid),
        "decision_timestamp_utc": _iso_s(getattr(d, "decision_timestamp", None)),
        "shading_state": getattr(summary, "shading_state", None),
        "baseline_target_ha": base,
        "adapted_target_ha": adapted,
        "is_adapted": is_adapted,
        "baseline_comparison": baseline_comparison,
        "delta_baseline_adapted_ha": (adapted - base) if (is_adapted) else 0,
        "executed_dispatch_status": getattr(summary, "dispatch_status", None),
        "learning_source_type": src_type,
        "outcome_status": getattr(d, "outcome_status", None),
        "outcome_resolution_status": resolution,
        "outcome_score": score,
        "confounder_codes": confounders,
        "attribution_status": attribution_status,
        "thermal_objective_classification": objective,
        "season_bucket": _SEASON_BY_MONTH.get(
            getattr(getattr(d, "decision_timestamp", None), "month", 0), "unknown"),
        "lifecycle_bucket": getattr(outcome, "lifecycle_state", None) if outcome else None,
    }


def _aggregations(records, coord) -> dict:
    # records == ALL eligible projected records (computed BEFORE the per-record count
    # cap and byte truncation), so aggregations use the full eligible dataset.  The
    # serialized record list is a bounded sample — see record_accounting for the
    # eligible/capped/serialized counts.
    total = len(records)
    adapted = [r for r in records if r.get("is_adapted")]
    unchanged = [r for r in records if r.get("baseline_comparison") == "unchanged"]
    unknown = [r for r in records if r.get("baseline_comparison") == "unknown"]
    multi_source = [r for r in records if r.get("attribution_status") == "multi_source"]
    dispatched_adapted = [r for r in adapted
                          if r.get("executed_dispatch_status") in ("sent", "SENT")]
    with_outcome = [r for r in records if r.get("outcome_status") not in (None, "none", "pending")]
    attributable = [r for r in records if r.get("attribution_status") == "attributable"]
    improved = [r for r in attributable if r.get("thermal_objective_classification") == "improved"]
    neutral = [r for r in attributable if r.get("thermal_objective_classification") == "neutral"]
    degraded = [r for r in attributable if r.get("thermal_objective_classification") == "degraded"]
    inconclusive = [r for r in records if r.get("attribution_status") != "attributable"]
    cb = _confidence_buckets(coord)
    cb["scope"] = "adoption_history_snapshots"  # NOT per-decision confidence
    return {
        "total_eligible_decisions": total,
        "eligible_record_count": total,
        "aggregation_record_count": total,
        "aggregation_scope": "all_eligible_records",
        "baseline_unchanged_count": len(unchanged),
        "baseline_unknown_count": len(unknown),
        "adapted_count": len(adapted),
        "multi_source_count": len(multi_source),
        "adaptation_rate": round(len(adapted) / total, 4) if total else 0.0,
        "dispatched_adapted_count": len(dispatched_adapted),
        "outcomes_observed": len(with_outcome),
        "outcomes_attributable": len(attributable),
        "outcomes_inconclusive": len(inconclusive),
        # better/worse stated ONLY for attributable single-source thermal objective;
        # no global score, multi-source never credited here.
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
        "season_basis": "northern_hemisphere_calendar",
        "by_lifecycle": _bucket_counts(records, "lifecycle_bucket"),
        "confidence_buckets": cb,
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
