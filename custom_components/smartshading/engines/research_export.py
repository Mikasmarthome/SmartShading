"""Anonymized research export for SmartShading learning analysis.

Builds a richer export than the Support Export, intended for development of
Learning Engine 2.0.  The export is NEVER generated automatically, NEVER
transmitted, and NEVER uploaded.  It is only created on explicit user request
with a prior confirmation step.

Privacy rules
-------------
  NEVER include: real zone or window names, entity IDs, config-entry IDs,
  device IDs, addresses, lat/lon, HA instance identifiers, usernames, raw
  file paths, exact cover entity names.
  NEVER include: individual record timestamps — only aggregated statistics.
  NEVER include: raw internal positions (0=open, 100=shaded) — convert to
  HA convention (0=closed, 100=open) where exposed, or use coarse buckets.
  Anonymized refs: zone_1, zone_2, window_1, window_2, ...

LE 2.0 capability flags
-----------------------
  The export documents which analyses are possible with the current data
  and which are not, so offline tooling can adapt without guessing.

  Available for analysis:
    - Evaluator attribution (decided_by distributions)
    - Override patterns (direction, frequency, duration)
    - Outcome scores (-1.0 to +1.0) and override rates
    - Target adaptation profiles (adapted positions per intensity level)
    - Forecast trust quality metrics
    - Environmental context at transition time (aggregated)

  Not available (not stored per-event in current architecture):
    - Per-event baseline_target vs adapted_target comparison
    - Per-event forecast_modifier_applied flag
    - Per-event similarity_match_applied flag
    - Exact per-event timestamps (only aggregate counts)

No HA import — pure Python.  All HA interactions happen in config_flow.py.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

RESEARCH_EXPORT_FORMAT_VERSION: int = 1
RESEARCH_EXPORT_TYPE: str = "smartshading_research_export"
RESEARCH_EXPORT_SCHEMA_VERSION: int = 2
RESEARCH_EXPORT_NOTICE_VERSION: int = 1

_INTENSITY_LEVELS = ("light", "normal", "strong")


def _round5(value: float) -> int:
    """Round a float to the nearest 5 for coarse position buckets."""
    return round(round(value / 5) * 5)


def _to_ha_position(internal: int) -> int:
    """Convert internal position (0=open, 100=shaded) to HA (0=closed, 100=open)."""
    return 100 - internal


def _build_forecast_trust_summary(forecast_store: Any | None) -> dict:
    """Aggregate forecast trust data from a ForecastLearningStore.  Never raises."""
    if forecast_store is None:
        return {"available": False}
    try:
        from .learning_export import build_learning_export
        from datetime import timezone
        dummy_ts = datetime.now(timezone.utc)
        result = build_learning_export(forecast_store=forecast_store, generated_at_utc=dummy_ts)
        return result.get("forecast_learning", {"available": False})
    except Exception:
        _LOGGER.warning("SmartShading: research_export: forecast trust summary failed")
        return {"available": False}


def _build_learned_profiles(target_adapter: Any | None) -> list[dict]:
    """Build learned profile summaries from a TargetPositionAdapter.  Never raises.

    Returns one entry per (window, intensity_level) combination that has data.
    Positions are in HA convention (0=closed, 100=open), rounded to nearest 5
    for coarse representation.  ShadeIntensityAdaptation already stores in HA
    convention, so no conversion is needed.
    """
    if target_adapter is None:
        return []
    profiles: list[dict] = []
    try:
        window_ids = target_adapter.get_window_ids()
        for window_id in sorted(window_ids):
            window_data = getattr(target_adapter, "_windows", {}).get(window_id)
            if window_data is None:
                continue
            for intensity in _INTENSITY_LEVELS:
                adaptation = window_data.get_intensity(intensity) if hasattr(window_data, "get_intensity") else None
                if adaptation is None or adaptation.sample_count == 0:
                    continue
                tw = adaptation.total_weight
                if tw >= 25:
                    confidence = "high"
                elif tw >= 15:
                    confidence = "medium"
                elif tw >= 8:
                    confidence = "low"
                else:
                    confidence = "insufficient"
                adaptation_active = getattr(adaptation, "has_enough_data", False)
                learned_avg = adaptation.learned_avg_ha
                profiles.append({
                    "intensity_level": intensity,
                    "sample_count": adaptation.sample_count,
                    "confidence_level": confidence,
                    "adaptation_active": adaptation_active,
                    # ShadeIntensityAdaptation stores positions in HA convention already.
                    # Round to nearest 5 for coarse export.
                    "adapted_target_ha": _round5(learned_avg) if learned_avg is not None else None,
                    # base_target_ha is zone config — not available in this context.
                    "base_target_ha": None,
                })
    except Exception:
        _LOGGER.warning("SmartShading: research_export: learned profiles section failed")
    return profiles


def _build_evaluator_distribution(transitions: list[Any]) -> dict[str, int]:
    """Count how many transitions each evaluator (decided_by) produced."""
    dist: dict[str, int] = {}
    for rec in transitions:
        name = getattr(rec, "decided_by", None)
        if name:
            dist[name] = dist.get(name, 0) + 1
    return dist


def _build_override_analysis(overrides: list[Any]) -> dict:
    """Aggregate override event statistics.  Never raises.

    override_position and overridden_position are internal convention in
    OverrideRecord; they are converted to HA convention before export.
    """
    if not overrides:
        return {
            "override_count": 0,
            "started_count": 0,
            "expired_count": 0,
            "renewed_count": 0,
            "cleared_by_safety_count": 0,
            "avg_duration_min": None,
        }
    try:
        started = sum(1 for r in overrides if getattr(r, "event_type", None) == "started")
        expired = sum(1 for r in overrides if getattr(r, "event_type", None) == "expired")
        renewed = sum(1 for r in overrides if getattr(r, "event_type", None) == "renewed")
        cleared_safety = sum(1 for r in overrides if getattr(r, "event_type", None) == "cleared_by_safety")
        durations = [
            r.override_duration_min for r in overrides
            if getattr(r, "override_duration_min", None) is not None
        ]
        avg_dur = round(sum(durations) / len(durations), 1) if durations else None
        return {
            "override_count": len(overrides),
            "started_count": started,
            "expired_count": expired,
            "renewed_count": renewed,
            "cleared_by_safety_count": cleared_safety,
            "avg_duration_min": avg_dur,
        }
    except Exception:
        _LOGGER.warning("SmartShading: research_export: override analysis failed")
        return {"override_count": len(overrides)}


def _build_outcome_summary(outcomes: list[Any]) -> dict:
    """Aggregate DecisionOutcome statistics.  Never raises.

    outcome_score is -1.0 … +1.0 (set on resolution).
    """
    if not outcomes:
        return {
            "total_count": 0,
            "resolved_count": 0,
            "pending_count": 0,
            "mean_outcome_score": None,
            "override_rate": None,
        }
    try:
        resolved = [
            r for r in outcomes
            if getattr(r, "resolution_status", "pending") != "pending"
        ]
        pending_count = len(outcomes) - len(resolved)
        override_count = sum(1 for r in resolved if getattr(r, "override_occurred", False))
        scores = [
            r.outcome_score for r in resolved
            if getattr(r, "outcome_score", None) is not None
        ]
        mean_score = round(sum(scores) / len(scores), 3) if scores else None
        override_rate = round(override_count / len(resolved), 3) if resolved else None
        return {
            "total_count": len(outcomes),
            "resolved_count": len(resolved),
            "pending_count": pending_count,
            "mean_outcome_score": mean_score,
            "override_rate": override_rate,
            # P3: multi-objective dimension aggregates (privacy-safe counts/means).
            **_build_multi_objective_summary(outcomes),
        }
    except Exception:
        _LOGGER.warning("SmartShading: research_export: outcome summary failed")
        return {"total_count": len(outcomes)}


def _build_multi_objective_summary(outcomes: list[Any]) -> dict:
    """Aggregate P3 multi-objective dimension stats.  Privacy-safe (counts/means
    only).  Never raises."""
    legacy = 0
    multi = 0
    thermal_avail = 0
    movement_avail = 0
    preference_avail = 0
    thermal_scores: list[float] = []
    preference_scores: list[float] = []
    confounder_counts: dict[str, int] = {}
    try:
        for o in outcomes:
            mo = getattr(o, "multi_objective", None)
            if mo is None:
                if getattr(o, "outcome_score", None) is not None:
                    legacy += 1
                continue
            multi += 1
            if mo.thermal.available:
                thermal_avail += 1
                if mo.thermal.score is not None:
                    thermal_scores.append(mo.thermal.score)
            if mo.movement.available:
                movement_avail += 1
            if mo.preference.available:
                preference_avail += 1
                if mo.preference.score is not None:
                    preference_scores.append(mo.preference.score)
            for name in mo.confounders.detected:
                confounder_counts[name] = confounder_counts.get(name, 0) + 1
    except Exception:
        _LOGGER.warning("SmartShading: research_export: multi-objective summary failed")
    return {
        "multi_objective_count": multi,
        "legacy_only_count": legacy,
        "dimension_availability": {
            "thermal": thermal_avail,
            "movement": movement_avail,
            "preference": preference_avail,
        },
        "thermal_score_mean": (
            round(sum(thermal_scores) / len(thermal_scores), 3) if thermal_scores else None
        ),
        "preference_score_mean": (
            round(sum(preference_scores) / len(preference_scores), 3) if preference_scores else None
        ),
        "confounder_distribution": confounder_counts,
    }


def build_thermal_research_summary(
    models: dict | None, observations: dict | None
) -> dict:
    """Aggregate per-zone thermal-response stats for the Research Export.

    Privacy-safe: zone keys are replaced by anonymized indices; no entity IDs,
    no raw timestamps, no raw temperature series.  Never raises.
    """
    models = models or {}
    observations = observations or {}
    try:
        onsets: list[float] = []
        windows: list[float] = []
        magnitudes: list[float] = []
        confidences: list[float] = []
        source_counts: dict[str, int] = {}
        total_obs = 0
        confounded_obs = 0
        zone_shared_obs = 0
        for zid, model in models.items():
            if model.response_onset_minutes is not None:
                onsets.append(model.response_onset_minutes)
            if model.effective_observation_minutes is not None:
                windows.append(model.effective_observation_minutes)
            if model.typical_temperature_response_c is not None:
                magnitudes.append(model.typical_temperature_response_c)
            confidences.append(model.confidence)
            source_counts[model.source_kind] = source_counts.get(model.source_kind, 0) + 1
        for zid, lst in observations.items():
            for o in lst:
                total_obs += 1
                if o.confounded:
                    confounded_obs += 1
                if len(o.decision_ids) > 1:
                    zone_shared_obs += 1

        def _mean(v: list[float]) -> float | None:
            return round(sum(v) / len(v), 2) if v else None

        return {
            "zone_model_count": len(models),
            "response_onset_mean_min": _mean(onsets),
            "observation_window_mean_min": _mean(windows),
            "response_magnitude_mean_c": _mean(magnitudes),
            "model_confidence_mean": _mean(confidences),
            "source_kind_distribution": source_counts,
            "observation_count": total_obs,
            "confounded_observation_count": confounded_obs,
            "zone_shared_observation_count": zone_shared_obs,
        }
    except Exception:
        _LOGGER.warning("SmartShading: research_export: thermal summary failed")
        return {"zone_model_count": 0}


def build_window_contribution_research_summary(
    models: dict | None, evidence: dict | None
) -> dict:
    """Aggregate per-window contribution stats for the Research Export.
    Privacy-safe: no entity IDs, no raw zone/window keys, no timestamps."""
    models = models or {}
    evidence = evidence or {}
    try:
        iso = cand = shared = unknown = 0
        indices: list[float] = []
        confidences: list[float] = []
        windows_without_isolated = 0
        for wid, lst in evidence.items():
            for e in lst:
                q = e.attribution_quality
                if q == "window_isolated":
                    iso += 1
                elif q == "window_candidate":
                    cand += 1
                elif q == "zone_shared":
                    shared += 1
                else:
                    unknown += 1
        for wid, m in models.items():
            if m.normalized_relative_contribution_index is not None:
                indices.append(m.normalized_relative_contribution_index)
            confidences.append(m.confidence)
            if m.isolated_sample_count == 0:
                windows_without_isolated += 1
        total = iso + cand + shared + unknown

        def _rate(x: int) -> float | None:
            return round(x / total, 3) if total else None

        def _mean(v: list[float]) -> float | None:
            return round(sum(v) / len(v), 3) if v else None

        return {
            "window_model_count": len(models),
            "isolated_event_rate": _rate(iso),
            "candidate_event_rate": _rate(cand),
            "shared_event_rate": _rate(shared),
            "unknown_event_rate": _rate(unknown),
            "contribution_index_mean": _mean(indices),
            "confidence_mean": _mean(confidences),
            "windows_without_isolated_evidence": windows_without_isolated,
        }
    except Exception:
        _LOGGER.warning("SmartShading: research_export: contribution summary failed")
        return {"window_model_count": 0}


def build_strategy_research_summary(
    experiments: list | None, adoptions: list | None
) -> dict:
    """Aggregate P9B strategy experiment + adoption stats.  Privacy-safe: no
    entity IDs, no raw keys, no timestamps.  Never raises."""
    experiments = experiments or []
    adoptions = adoptions or []
    try:
        exp_by_family: dict[str, int] = {}
        eval_classes: dict[str, int] = {}
        for e in experiments:
            exp_by_family[e.parameter_family] = exp_by_family.get(e.parameter_family, 0) + 1
            eval_classes[e.evaluation_class] = eval_classes.get(e.evaluation_class, 0) + 1
        ad_by_family: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        rollbacks = 0
        for a in adoptions:
            ad_by_family[a.parameter_family] = ad_by_family.get(a.parameter_family, 0) + 1
            status_counts[a.status] = status_counts.get(a.status, 0) + 1
            if a.rollback_reason:
                rollbacks += 1
        n = len(adoptions)
        return {
            "strategy_experiment_count": len(experiments),
            "experiment_count_by_family": exp_by_family,
            "experiment_evaluation_classes": eval_classes,
            "strategy_adoption_count": n,
            "adoption_count_by_family": ad_by_family,
            "adoption_status_counts": status_counts,
            "rollback_rate": round(rollbacks / n, 3) if n else None,
        }
    except Exception:
        _LOGGER.warning("SmartShading: research_export: strategy summary failed")
        return {"strategy_experiment_count": 0, "strategy_adoption_count": 0}


def build_adoption_research_summary(adoptions: list | None) -> dict:
    """Aggregate persistent-adoption stats for the Research Export.  Privacy-safe:
    no entity IDs, no raw keys, no timestamps.  Never raises."""
    adoptions = adoptions or []
    try:
        status_counts: dict[str, int] = {}
        deltas: list[int] = []
        suspended = 0
        pref_rollbacks = 0
        thermal_rollbacks = 0
        second_stage = 0
        active = 0
        for a in adoptions:
            status_counts[a.status] = status_counts.get(a.status, 0) + 1
            if a.adopted_delta_ha:
                deltas.append(a.adopted_delta_ha)
            if a.suspended:
                suspended += 1
            if a.stage == 2:
                second_stage += 1
            if a.status in ("adopted", "monitoring", "confirmed", "reduced", "eligible"):
                active += 1
            if a.rollback_reason == "preference_open_more_rejection":
                pref_rollbacks += 1
            elif a.rollback_reason == "repeated_thermal_degradation":
                thermal_rollbacks += 1
        n = len(adoptions)

        def _rate(s: str) -> float | None:
            return round(status_counts.get(s, 0) / n, 3) if n else None

        return {
            "adoption_total": n, "active_adoption_count": active,
            "adoption_delta_distribution": {str(d): deltas.count(d) for d in set(deltas)},
            "confirmed_rate": _rate("confirmed"), "reduced_rate": _rate("reduced"),
            "rolled_back_rate": _rate("rolled_back"),
            "preference_rollback_count": pref_rollbacks,
            "thermal_rollback_count": thermal_rollbacks,
            "suspended_count": suspended, "second_stage_count": second_stage,
        }
    except Exception:
        _LOGGER.warning("SmartShading: research_export: adoption summary failed")
        return {"adoption_total": 0}


def build_experiment_research_summary(experiments: list | None) -> dict:
    """Aggregate bounded-experiment stats for the Research Export.  Privacy-safe:
    no entity IDs, no raw window/zone keys, no timestamps.  Never raises."""
    experiments = experiments or []
    try:
        status_counts: dict[str, int] = {}
        eval_counts: dict[str, int] = {}
        gate_failures: dict[str, int] = {}
        deltas: list[int] = []
        rollbacks = 0
        pref_rejections = 0
        no_feedback = 0
        # 3H — bounded staged + causal aggregates (privacy-safe; no IDs/timestamps).
        stage_counts: dict[str, int] = {}
        step_counts: dict[str, int] = {}
        scope_counts: dict[str, int] = {}
        staged_escalations = 0
        score_gaps: list[float] = []
        for e in experiments:
            status_counts[e.status] = status_counts.get(e.status, 0) + 1
            ev = e.evaluation.decision
            eval_counts[ev] = eval_counts.get(ev, 0) + 1
            if e.evaluation.decision == "preference_rejected":
                pref_rejections += 1
            if e.rollback_state and e.rollback_state != "none":
                rollbacks += 1
            if e.delta_ha is not None:
                deltas.append(e.delta_ha)
            snap = e.eligibility_snapshot or {}
            for code in (snap.get("blocked_by") or []):
                gate_failures[code] = gate_failures.get(code, 0) + 1
            if e.confirmation == "command_sent":  # sent but not confirmed by feedback
                no_feedback += 1
            _st = str(getattr(e, "stage", 1))
            stage_counts[_st] = stage_counts.get(_st, 0) + 1
            _sp = str(getattr(e, "target_step_ha", 5))
            step_counts[_sp] = step_counts.get(_sp, 0) + 1
            if getattr(e, "previous_experiment_id", None) is not None:
                staged_escalations += 1
            _dist = (e.evaluation.baseline_thermal_distribution or {}) if e.evaluation else {}
            _scope = _dist.get("scope") or "none"
            scope_counts[_scope] = scope_counts.get(_scope, 0) + 1
            _obs = e.evaluation.experiment_thermal_score if e.evaluation else None
            _cf = _dist.get("counterfactual_baseline_score")
            if _obs is not None and _cf is not None:
                score_gaps.append(round(_obs - _cf, 4))
        n = len(experiments)

        def _rate(s: str) -> float | None:
            return round(status_counts.get(s, 0) / n, 3) if n else None

        return {
            "experiments_total": n,
            "experiments_planned": status_counts.get("planned", 0) + status_counts.get("armed", 0),
            "experiments_activated": status_counts.get("activated", 0) + status_counts.get("observing", 0),
            "experiments_completed": (
                status_counts.get("completed", 0) + status_counts.get("accepted_for_p8", 0)
                + status_counts.get("rejected", 0)
            ),
            "aborted_rate": _rate("aborted"),
            "invalidated_rate": _rate("invalidated"),
            "interrupted_partial_rate": _rate("interrupted_partial"),
            "outcome_class_distribution": eval_counts,
            "activation_gate_failure_distribution": gate_failures,
            "candidate_delta_distribution": {str(d): deltas.count(d) for d in set(deltas)},
            "preference_rejection_rate": round(pref_rejections / n, 3) if n else None,
            "rollback_rate": round(rollbacks / n, 3) if n else None,
            "experiments_without_reliable_feedback": no_feedback,
            # 3H bounded staged + causal scoring (aggregates only).
            "experiment_stage_distribution": stage_counts,
            "tested_step_distribution": step_counts,
            "staged_escalation_count": staged_escalations,
            "baseline_scope_distribution": scope_counts,
            "causal_score_gap_count": len(score_gaps),
            "causal_score_gap_mean": (
                round(sum(score_gaps) / len(score_gaps), 4) if score_gaps else None),
        }
    except Exception:
        _LOGGER.warning("SmartShading: research_export: experiment summary failed")
        return {"experiments_total": 0}


def build_shadow_research_summary(proposals: list | None) -> dict:
    """Aggregate shadow-proposal stats for the Research Export.  Privacy-safe:
    no entity IDs, no raw window/zone keys, no timestamps.  Never raises."""
    proposals = proposals or []
    try:
        status_counts: dict[str, int] = {}
        reason_counts: dict[str, int] = {}
        deltas: list[int] = []
        veto = 0
        for p in proposals:
            status_counts[p.status] = status_counts.get(p.status, 0) + 1
            if p.proposal_reason:
                reason_counts[p.proposal_reason] = reason_counts.get(p.proposal_reason, 0) + 1
            if p.net_shadow_delta_vs_real_ha is not None:
                deltas.append(p.net_shadow_delta_vs_real_ha)
            if p.evaluation.preference_veto:
                veto += 1
        n = len(proposals)

        def _rate(s: str) -> float | None:
            return round(status_counts.get(s, 0) / n, 3) if n else None

        return {
            "proposal_count": n,
            "supported_rate": _rate("supported"),
            "observing_rate": _rate("observing"),
            "inconclusive_rate": _rate("inconclusive"),
            "rejected_rate": _rate("rejected"),
            "expired_rate": _rate("expired"),
            "invalidated_rate": _rate("invalidated"),
            "candidate_delta_distribution": {str(d): deltas.count(d) for d in set(deltas)},
            "reason_distribution": reason_counts,
            "preference_veto_rate": round(veto / n, 3) if n else None,
        }
    except Exception:
        _LOGGER.warning("SmartShading: research_export: shadow summary failed")
        return {"proposal_count": 0}


def _build_window_section(
    window_ref: str,
    window_id: str,
    learning_store: Any | None,
) -> dict:
    """Build per-window summary for research export.  Never raises.

    Includes evaluator distributions, override analysis, and outcome summaries
    derived from the LearningStore ring buffers.
    """
    transitions: list[Any] = []
    overrides: list[Any] = []
    snapshots: list[Any] = []
    outcomes: list[Any] = []

    if learning_store is not None:
        try:
            transitions = learning_store.get_transitions(window_id)
            overrides = learning_store.get_overrides(window_id)
            snapshots = learning_store.get_snapshots(window_id)
            outcomes = learning_store.get_outcomes(window_id)
        except Exception:
            _LOGGER.warning(
                "SmartShading: research_export: error reading LearningStore for window"
            )

    has_data = (len(transitions) + len(overrides) + len(snapshots) + len(outcomes)) > 0

    return {
        "window_ref": window_ref,
        "has_learning_data": has_data,
        "transitions_count": len(transitions),
        "overrides_count": len(overrides),
        "snapshots_count": len(snapshots),
        "outcomes_count": len(outcomes),
        "evaluator_distribution": _build_evaluator_distribution(transitions),
        "override_analysis": _build_override_analysis(overrides),
        "outcome_summary": _build_outcome_summary(outcomes),
        "capabilities": {
            "per_event_baseline_target": False,
            "per_event_adapted_target": False,
            "per_event_forecast_modifier": False,
            "per_event_similarity_match": False,
            "outcome_scores_available": any(
                getattr(r, "outcome_score", None) is not None for r in outcomes
            ),
            "evaluator_attribution_available": len(transitions) > 0,
        },
    }


def _build_zone_section(
    zone_ref: str,
    zone_entry: dict,
    generated_at_utc: datetime,
) -> dict:
    """Build per-zone section for research export.  Never raises."""
    window_ids: list[str] = zone_entry.get("window_ids", [])
    learning_store = zone_entry.get("learning_store")
    forecast_store = zone_entry.get("forecast_store")
    target_adapter = zone_entry.get("target_position_adapter")

    windows_out: list[dict] = []
    for w_idx, window_id in enumerate(sorted(window_ids), start=1):
        window_ref = f"window_{w_idx}"
        windows_out.append(_build_window_section(window_ref, window_id, learning_store))

    forecast_trust = _build_forecast_trust_summary(forecast_store)
    learned_profiles = _build_learned_profiles(target_adapter)

    return {
        "zone_ref": zone_ref,
        "windows_count": len(windows_out),
        "forecast_trust_summary": forecast_trust,
        "learned_profiles": learned_profiles,
        "windows": windows_out,
    }


def _build_application_summary(zone_entries: list[dict]) -> dict:
    """Aggregate application counters across all zones.  Never raises."""
    manual_override_count = 0
    outcome_count = 0
    resolved_with_score = 0

    for zone_entry in zone_entries:
        learning_store = zone_entry.get("learning_store")
        window_ids: list[str] = zone_entry.get("window_ids", [])
        if learning_store is None:
            continue
        try:
            for window_id in window_ids:
                override_records = learning_store.get_overrides(window_id)
                outcome_records = learning_store.get_outcomes(window_id)
                manual_override_count += sum(
                    1 for r in override_records
                    if getattr(r, "event_type", None) == "started"
                )
                outcome_count += len(outcome_records)
                resolved_with_score += sum(
                    1 for r in outcome_records
                    if getattr(r, "outcome_score", None) is not None
                )
        except Exception:
            _LOGGER.warning(
                "SmartShading: research_export: error building application summary"
            )

    return {
        "manual_override_count": manual_override_count,
        "outcome_count": outcome_count,
        "resolved_outcome_count": resolved_with_score,
        # These require per-event flags not yet stored — marked unavailable.
        "adaptive_target_applied_count": None,
        "forecast_modifier_applied_count": None,
        "similarity_match_applied_count": None,
        "confidence_blocked_count": None,
        "insufficient_data_count": None,
    }


def build_research_export(
    *,
    zone_entries: list[dict],
    generated_at_utc: datetime,
    startup_grace_cycles: int = 1,
    override_warmup_cycles: int = 1,
) -> dict:
    """Build an anonymized research export covering all zones.

    Parameters
    ----------
    zone_entries:
        List of per-zone dicts, same format as build_global_learning_export():
          "entry_id"            str               — raw config entry ID (not included in output)
          "window_ids"          list[str]
          "learning_store"      LearningStore | None
          "forecast_store"      ForecastLearningStore | None
          "target_position_adapter" TargetPositionAdapter | None

    generated_at_utc:
        UTC-aware datetime.  Raises ValueError for naive datetimes.

    startup_grace_cycles:
        Value of STARTUP_GRACE_CYCLES at export time (default: 1 since v1.0.6).
        Allows offline tooling to reconstruct the dispatch-suppression semantics
        active when the learning data in this export was collected.

    override_warmup_cycles:
        Value of _WARMUP_CYCLES_REQUIRED at export time (default: 1 since v1.0.6).
        Allows offline tooling to know how many cycles of override-detection
        warmup were in effect for the data in this export.

    Returns
    -------
    dict
        JSON-serializable research export.

    Raises
    ------
    ValueError
        If generated_at_utc has no timezone info.
    """
    if generated_at_utc.tzinfo is None:
        raise ValueError(
            "build_research_export: generated_at_utc must be timezone-aware (UTC policy)"
        )

    sorted_entries = sorted(zone_entries, key=lambda e: e.get("entry_id", ""))
    zones_out: list[dict] = []

    for z_idx, zone_entry in enumerate(sorted_entries, start=1):
        zone_ref = f"zone_{z_idx}"
        zone_section = _build_zone_section(zone_ref, zone_entry, generated_at_utc)
        zones_out.append(zone_section)

    application_summary = _build_application_summary(sorted_entries)

    return {
        "format_version": RESEARCH_EXPORT_FORMAT_VERSION,
        "export_type": RESEARCH_EXPORT_TYPE,
        "research_export_schema_version": RESEARCH_EXPORT_SCHEMA_VERSION,
        "generated_at_utc": generated_at_utc.isoformat(),
        "research_export_notice_version": RESEARCH_EXPORT_NOTICE_VERSION,
        "zones_count": len(zones_out),
        "zones": zones_out,
        "learning_application_summary": application_summary,
        "runtime_semantics": {
            "startup_grace_cycles": startup_grace_cycles,
            "override_warmup_cycles": override_warmup_cycles,
            "override_reference_strategy": (
                "last_commanded > previous_observation > observed_internal"
            ),
            "support_export_schema_version": 2,
            "research_export_schema_version": RESEARCH_EXPORT_SCHEMA_VERSION,
            "per_event_override_reference_source": "not_stored",
            "per_event_startup_phase": "not_stored",
            "per_event_dispatch_status": "not_stored",
        },
        "eligibility_semantics": {
            "eligible_behavior_modes": ["fully_automatic"],
            "excluded_behavior_modes": [
                "absence_and_schedule",
                "absence_only",
                "disabled_automatic",
            ],
            "current_zone_metrics_use_current_behavior_mode": True,
            "behavior_mode_at_decision": "not_stored",
            "learning_eligible_at_decision": "not_stored",
        },
        "le2_capability_notes": {
            "evaluator_attribution": "available — decided_by field on all transition records",
            "override_patterns": "available — OverrideRecord with event_type, duration",
            "outcome_scores": "available when resolved — DecisionOutcome.outcome_score",
            "adapted_targets": "available — TargetPositionAdapter per intensity level",
            "forecast_trust": "available — ForecastLearningStore trust scores",
            "per_event_baseline_vs_adapted": "not_available — not stored per transition",
            "per_event_forecast_modifier": "not_available — not stored per transition",
            "per_event_similarity_match": "not_available — not yet implemented",
            "per_event_override_reference_source": "not_available — follow-up LE2.0 Phase 2",
            "per_event_startup_phase": "not_available — follow-up LE2.0 Phase 2",
            "per_event_dispatch_status": "not_available — follow-up LE2.0 Phase 2",
            "behavior_mode_at_decision": "not_available — follow-up LE2.0 Phase 2",
            "learning_eligible_at_decision": "not_available — follow-up LE2.0 Phase 2",
        },
    }
