"""Window contribution engine — LE 2.0 / Phase P5 (pure).

Builds per-window RELATIVE contribution models within a zone from bounded
evidence + a conservative geometric/solar prior.  Deterministic, bounded, with
explicit confidence caps for shared-only / candidate-only histories.  P5 has no
control authority — these indices only weight evidence and prepare eligibility.
No Home Assistant import.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..engines.thermal_response_engine import _recency_weight, _season_similarity, solar_season_bucket
from ..models.window_contribution import (
    ATTR_WINDOW_CANDIDATE,
    ATTR_WINDOW_ISOLATED,
    ATTR_ZONE_SHARED,
    CONF_CAP_CANDIDATE_ONLY,
    CONF_CAP_SHARED_ONLY,
    EXPERIMENT_MIN_CONFIDENCE,
    EXPERIMENT_MIN_DISTINCT_DAYS,
    EXPERIMENT_MIN_ISOLATED_SAMPLES,
    PRIOR_EXPOSURE_SECTOR,
    PRIOR_EXPOSURE_SECTOR_AREA,
    PRIOR_NEUTRAL,
    SHADOW_MIN_CONFIDENCE,
    WindowContributionEvidence,
    WindowContributionModel,
)

_CONFIDENCE_ISOLATED_RAMP: int = 10      # isolated samples for full data-richness
_MIN_DISTINCT_DAYS: int = 3
_MAX_INDEX_CHANGE_PER_RECOMPUTE: float = 0.15   # drift guard on normalized index


@dataclass(frozen=True)
class WindowPriorFacts:
    """Per-window inputs for the geometric/solar prior."""

    window_id: str
    effective_exposure: float | None
    sector_factor: float           # [0,1]: 1 = fully in solar sector, 0 = outside/blocked
    area_m2: float | None
    blocked: bool = False


def compute_geometric_solar_prior(
    windows: list[WindowPriorFacts],
) -> dict[str, tuple[float, str]]:
    """Return {window_id: (prior_index, prior_source)} normalized within the zone.

    Default prior = effective_exposure × sector_factor.  An area factor is added
    ONLY when area_m2 is present for ALL windows (no invented default area, no
    partial area weighting).  Blocked / no-exposure windows get 0 for the prior.
    Falls back to a neutral equal prior when no usable signal exists.
    """
    if not windows:
        return {}
    all_have_area = all(w.area_m2 is not None and w.area_m2 > 0 for w in windows)
    source = PRIOR_EXPOSURE_SECTOR_AREA if all_have_area else PRIOR_EXPOSURE_SECTOR

    raw: dict[str, float] = {}
    for w in windows:
        if w.blocked or w.effective_exposure is None or w.effective_exposure <= 0:
            raw[w.window_id] = 0.0
            continue
        base = w.effective_exposure * max(0.0, min(1.0, w.sector_factor))
        if all_have_area:
            base *= w.area_m2  # type: ignore[operator]
        raw[w.window_id] = base

    total = sum(raw.values())
    if total <= 0:
        # Neutral equal prior — no usable geometric/solar signal.
        n = len(windows)
        return {w.window_id: (1.0 / n, PRIOR_NEUTRAL) for w in windows}
    return {wid: (val / total, source) for wid, val in raw.items()}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def recompute_contribution_models(
    zone_id: str,
    eligible_window_ids: list[str],
    evidence_by_window: dict[str, list[WindowContributionEvidence]],
    priors: dict[str, tuple[float, str]],
    now: datetime,
    *,
    config_generation: int,
    previous: dict[str, WindowContributionModel] | None = None,
) -> dict[str, WindowContributionModel]:
    """Deterministically rebuild per-window contribution models for the zone.

    Only current-generation evidence contributes.  Weight = event_weight ×
    observation_reliability × recency × season_similarity.  Confidence is capped
    for shared-only / candidate-only histories.  Indices are normalized across
    eligible windows; per-recompute change is bounded (drift guard).
    """
    previous = previous or {}
    now_season = solar_season_bucket(now)

    learned_raw: dict[str, float] = {}
    per_window_meta: dict[str, dict] = {}

    for wid in eligible_window_ids:
        prior_index, prior_source = priors.get(wid, (None, PRIOR_NEUTRAL))
        ev = [
            e for e in evidence_by_window.get(wid, [])
            if e.config_generation == config_generation
        ]
        iso = sum(1 for e in ev if e.attribution_quality == ATTR_WINDOW_ISOLATED)
        cand = sum(1 for e in ev if e.attribution_quality == ATTR_WINDOW_CANDIDATE)
        shared = sum(1 for e in ev if e.attribution_quality == ATTR_ZONE_SHARED)
        days = len({e.timestamp.date() for e in ev})

        num = 0.0
        den = 0.0
        for e in ev:
            if e.observed_contribution_signal is None:
                continue
            w = (
                e.event_weight
                * max(0.0, min(1.0, e.observation_reliability))
                * _recency_weight(e.timestamp, now)
                * _season_similarity(now_season, e.context_key.split("|")[0])
            )
            if w <= 0:
                continue
            num += abs(e.observed_contribution_signal) * w
            den += w
        observed_strength = (num / den) if den > 0 else None

        # Blend prior with observed strength.  Trust in observation grows with
        # isolated evidence; without isolated evidence the prior dominates.
        trust = min(1.0, iso / _CONFIDENCE_ISOLATED_RAMP) if iso > 0 else (0.3 if cand > 0 else 0.0)
        if observed_strength is None:
            learned = prior_index if prior_index is not None else 0.0
        elif prior_index is None:
            learned = observed_strength
        else:
            learned = prior_index * (1.0 - trust) + observed_strength * trust
        learned_raw[wid] = max(0.0, learned)

        per_window_meta[wid] = {
            "prior_index": prior_index, "prior_source": prior_source,
            "observed_strength": observed_strength,
            "iso": iso, "cand": cand, "shared": shared, "days": days,
            "context_coverage": len({e.context_key for e in ev}),
            "reliability": (sum(e.observation_reliability for e in ev) / len(ev)) if ev else 0.0,
        }

    # Normalize across eligible windows (sum = 1, or neutral when all zero).
    total = sum(learned_raw.values())
    n = len(eligible_window_ids)
    models: dict[str, WindowContributionModel] = {}
    for wid in eligible_window_ids:
        meta = per_window_meta[wid]
        if total > 0:
            norm = learned_raw[wid] / total
        else:
            norm = (1.0 / n) if n else None
        # Drift guard vs previous normalized index.
        prev = previous.get(wid)
        if prev is not None and prev.normalized_relative_contribution_index is not None \
                and norm is not None:
            norm = prev.normalized_relative_contribution_index + _clamp(
                norm - prev.normalized_relative_contribution_index,
                -_MAX_INDEX_CHANGE_PER_RECOMPUTE, _MAX_INDEX_CHANGE_PER_RECOMPUTE,
            )

        confidence = _confidence(meta["iso"], meta["cand"], meta["shared"], meta["days"])

        models[wid] = WindowContributionModel(
            window_id=wid, zone_id=zone_id,
            prior_contribution_index=meta["prior_index"], prior_source=meta["prior_source"],
            observed_contribution_signal=meta["observed_strength"],
            learned_relative_contribution_index=learned_raw[wid],
            normalized_relative_contribution_index=norm,
            confidence=confidence, reliability=meta["reliability"],
            isolated_sample_count=meta["iso"], candidate_sample_count=meta["cand"],
            shared_sample_count=meta["shared"], distinct_days=meta["days"],
            context_coverage=meta["context_coverage"],
            evidence_sources=("isolated",) * (1 if meta["iso"] else 0)
            + ("candidate",) * (1 if meta["cand"] else 0)
            + ("shared",) * (1 if meta["shared"] else 0),
            last_updated=now, config_generation=config_generation,
            fallback_reason=None if (meta["iso"] or meta["cand"]) else "no_window_specific_evidence",
        )
    return models


def _confidence(iso: int, cand: int, shared: int, days: int) -> float:
    """Window-specific model confidence with hard caps.

    No isolated evidence ⇒ capped (candidate-only or shared-only).  Many shared
    events alone can never produce high window-specific confidence.
    """
    if iso == 0 and cand == 0 and shared == 0:
        return 0.0
    richness = min(1.0, iso / _CONFIDENCE_ISOLATED_RAMP)
    day_div = min(1.0, days / _MIN_DISTINCT_DAYS)
    base = richness * day_div
    if iso == 0:
        cap = CONF_CAP_CANDIDATE_ONLY if cand > 0 else CONF_CAP_SHARED_ONLY
        # candidate/shared evidence still grants a little confidence, but capped.
        partial = min(cap, (min(1.0, cand / 10.0) if cand > 0 else min(1.0, shared / 20.0)) * cap)
        return partial
    return max(0.0, min(1.0, base))


def derive_eligibility(
    model: WindowContributionModel | None,
    current_config_generation: int,
) -> tuple[bool, bool]:
    """Deterministically derive (shadow_eligible, experiment_eligible) from the
    CURRENT model + config generation.  Stale/incompatible/missing → not eligible.

    Persisted eligibility is only a diagnostic snapshot; callers must use this.
    """
    if model is None or model.config_generation != current_config_generation:
        return False, False
    shadow = (
        model.confidence >= SHADOW_MIN_CONFIDENCE
        and (model.isolated_sample_count > 0 or model.candidate_sample_count > 0)
    )
    experiment = (
        model.confidence >= EXPERIMENT_MIN_CONFIDENCE
        and model.isolated_sample_count >= EXPERIMENT_MIN_ISOLATED_SAMPLES
        and model.distinct_days >= EXPERIMENT_MIN_DISTINCT_DAYS
    )
    return shadow, experiment
