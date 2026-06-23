"""Thermal Response engine — LE 2.0 / Phase P4 (pure).

Learns, per zone (== per config entry), a multi-part thermal-reaction model and
selects the outcome observation window.  Deterministic: the model is recomputed
from the bounded observation list (weighted robust medians), so no unbounded EMA
drift and no single sample can materially shift the model.

P4's only active authority is select_observation_window(); cold-start /
low-confidence / no-temperature always falls back to the fixed 30-minute window.
No Home Assistant import.
"""
from __future__ import annotations

from datetime import datetime
from math import exp

from ..models.thermal_response import (
    INERTIA_FAST,
    INERTIA_MEDIUM,
    INERTIA_SLOW,
    INERTIA_UNKNOWN,
    ContextThermalModel,
    ThermalResponseModel,
    ThermalResponseObservation,
)

# --- Hard caps (minutes; coupled to the 5-minute coordinator cycle) ---
COLD_START_WINDOW_MIN: int = 30
MIN_OBSERVATION_MIN: int = 15
MAX_OBSERVATION_MIN: int = 90
MIN_ONSET_MIN: int = 5
MAX_ONSET_MIN: int = 60
MEDIUM_CONFIDENCE_CAP_MIN: int = 15      # ± around the 30-min default at medium confidence
WINDOW_ROUNDING_MIN: int = 5             # round to the cycle granularity

# --- Confidence tiers ---
CONFIDENCE_DIAGNOSTIC: float = 0.40      # below → diagnostic only (30 min)
CONFIDENCE_FULL: float = 0.80            # at/above → full model authority within caps

# --- Onset detection ---
ONSET_NOISE_DEADBAND_C: float = 0.3      # below = sensor noise
ONSET_MIN_POINTS: int = 3
ONSET_TREND_PERSISTENCE: int = 2         # consecutive points in one direction
ONSET_MIN_SOLAR_WM2: float = 150.0       # reaction credited only under real load

# --- Evidence gates ---
MIN_CONTEXT_SAMPLES: int = 8
MIN_CONTEXT_DISTINCT_DAYS: int = 3
MODEL_CONFIDENCE_SAMPLE_RAMP: int = 25   # samples for full data-richness term

# --- Recency / season ---
RECENCY_HALF_LIFE_DAYS: float = 60.0
SEASON_WEIGHT_SAME: float = 1.0
SEASON_WEIGHT_ADJACENT: float = 0.5
SEASON_WEIGHT_OPPOSITE: float = 0.2

# --- Drift guard ---
MAX_ONSET_CHANGE_MIN: float = 5.0
MAX_MAGNITUDE_CHANGE_C: float = 0.3


# ---------------------------------------------------------------------------
# Context bucketing
# ---------------------------------------------------------------------------

_SEASON_BY_MONTH: dict[int, str] = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}
_SEASON_ORDER = ["winter", "spring", "summer", "autumn"]


def solar_season_bucket(ts: datetime) -> str:
    return _SEASON_BY_MONTH.get(ts.month, "unknown")


def _season_similarity(a: str, b: str) -> float:
    if a == b:
        return SEASON_WEIGHT_SAME
    if a in _SEASON_ORDER and b in _SEASON_ORDER:
        gap = abs(_SEASON_ORDER.index(a) - _SEASON_ORDER.index(b)) % 4
        if gap == 1 or gap == 3:
            return SEASON_WEIGHT_ADJACENT
        return SEASON_WEIGHT_OPPOSITE
    return SEASON_WEIGHT_OPPOSITE


def outdoor_bucket(outdoor_c: float | None) -> str:
    if outdoor_c is None:
        return "out_unknown"
    if outdoor_c < 10:
        return "out_cold"
    if outdoor_c < 22:
        return "out_mild"
    if outdoor_c < 30:
        return "out_warm"
    return "out_hot"


def exposure_bucket(wm2: float | None) -> str:
    if wm2 is None:
        return "sol_unknown"
    if wm2 < 150:
        return "sol_low"
    if wm2 < 400:
        return "sol_mid"
    return "sol_high"


def context_key(ts: datetime, outdoor_c: float | None, exposure_wm2: float | None) -> str:
    return f"{solar_season_bucket(ts)}|{outdoor_bucket(outdoor_c)}|{exposure_bucket(exposure_wm2)}"


# ---------------------------------------------------------------------------
# Onset detection
# ---------------------------------------------------------------------------

def detect_response_onset(
    samples: tuple[tuple[int, float], ...] | list[tuple[int, float]],
    *,
    solar_exposure: float | None,
    noise_deadband: float = ONSET_NOISE_DEADBAND_C,
) -> float | None:
    """Earliest offset (minutes) of a persistent, beyond-noise thermal trend.

    Conservative: requires ≥ ONSET_MIN_POINTS points, sufficient solar load, and
    a trend that persists over ≥ ONSET_TREND_PERSISTENCE consecutive points with
    a cumulative change beyond the noise deadband.  Returns None otherwise.
    Never claims minute precision below the sampling granularity.
    """
    pts = sorted(samples, key=lambda s: s[0])
    if len(pts) < ONSET_MIN_POINTS:
        return None
    if solar_exposure is None or solar_exposure < ONSET_MIN_SOLAR_WM2:
        return None
    base = pts[0][1]
    run_dir = 0
    run_len = 0
    for i in range(1, len(pts)):
        delta = pts[i][1] - pts[i - 1][1]
        direction = 1 if delta > 0 else (-1 if delta < 0 else 0)
        if direction != 0 and direction == run_dir:
            run_len += 1
        else:
            run_dir = direction
            run_len = 1
        cumulative = abs(pts[i][1] - base)
        if run_len >= ONSET_TREND_PERSISTENCE and cumulative > noise_deadband:
            return float(pts[i - run_len + 1][0])  # offset of the run's first point
    return None


# ---------------------------------------------------------------------------
# Model recomputation (deterministic, robust, bounded)
# ---------------------------------------------------------------------------

def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    """Weighted median of (value, weight) pairs."""
    items = [(v, w) for v, w in pairs if w > 0]
    if not items:
        return None
    items.sort(key=lambda x: x[0])
    total = sum(w for _, w in items)
    acc = 0.0
    for v, w in items:
        acc += w
        if acc >= total / 2.0:
            return v
    return items[-1][0]


def _recency_weight(obs_ts: datetime, now: datetime) -> float:
    age_days = max(0.0, (now - obs_ts).total_seconds() / 86400.0)
    return exp(-age_days / RECENCY_HALF_LIFE_DAYS) if RECENCY_HALF_LIFE_DAYS > 0 else 1.0


def _inertia_from_onset(onset: float | None) -> str:
    if onset is None:
        return INERTIA_UNKNOWN
    if onset <= 10:
        return INERTIA_FAST
    if onset <= 25:
        return INERTIA_MEDIUM
    return INERTIA_SLOW


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _bounded(old: float | None, new: float | None, max_step: float) -> float | None:
    """Limit the per-recompute change so no batch can move a value too far."""
    if new is None:
        return old
    if old is None:
        return new
    return old + _clamp(new - old, -max_step, max_step)


def recompute_model(
    zone_id: str,
    observations: list[ThermalResponseObservation],
    now: datetime,
    *,
    config_generation: int,
    previous: ThermalResponseModel | None = None,
) -> ThermalResponseModel:
    """Deterministically rebuild the zone model from its bounded observations.

    Only thermally-usable, unconfounded observations of the CURRENT config
    generation contribute.  Weight = recency × season_similarity × reliability.
    A drift guard limits how far each recompute may move onset/magnitude.
    """
    usable = [
        o for o in observations
        if o.thermal_available and not o.confounded and o.config_generation == config_generation
    ]
    sample_count = len(usable)
    distinct_days = len({o.started_at.date() for o in usable})

    if sample_count == 0:
        return ThermalResponseModel(
            zone_id=zone_id, config_generation=config_generation,
            sample_count=0, distinct_days=0, unconfounded_sample_count=0,
            last_updated=now, fallback_reason="no_usable_observations",
            context_models=(previous.context_models if previous else {}),
        )

    now_season = solar_season_bucket(now)

    def _weight(o: ThermalResponseObservation) -> float:
        season = o.context_key.split("|")[0]
        return (
            _recency_weight(o.started_at, now)
            * _season_similarity(now_season, season)
            * max(0.0, min(1.0, o.reliability))
        )

    def _onset_of(o: ThermalResponseObservation) -> float | None:
        return detect_response_onset(o.indoor_samples, solar_exposure=o.solar_start)

    onset_pairs = [(v, _weight(o)) for o in usable if (v := _onset_of(o)) is not None]
    mag_pairs = [
        (abs(d), _weight(o)) for o in usable
        if (d := o.indoor_delta_c) is not None
    ]

    raw_onset = _weighted_median(onset_pairs)
    raw_mag = _weighted_median(mag_pairs)

    onset = _bounded(previous.response_onset_minutes if previous else None,
                     raw_onset, MAX_ONSET_CHANGE_MIN)
    magnitude = _bounded(previous.typical_temperature_response_c if previous else None,
                         raw_mag, MAX_MAGNITUDE_CHANGE_C)

    if onset is not None:
        onset = _clamp(onset, MIN_ONSET_MIN, MAX_ONSET_MIN)
    # Effective window = onset + a settling buffer (≈ one onset again), capped.
    effective = None
    if onset is not None:
        effective = _clamp(onset * 2.0, MIN_OBSERVATION_MIN, MAX_OBSERVATION_MIN)

    confidence = compute_model_confidence(
        sample_count=sample_count, distinct_days=distinct_days,
        onset_values=[v for v, _ in onset_pairs],
        unconfounded_sample_count=sample_count,
    )

    # Context sub-models (active only with enough evidence)
    by_ctx: dict[str, list[ThermalResponseObservation]] = {}
    for o in usable:
        by_ctx.setdefault(o.context_key, []).append(o)
    context_models: dict[str, ContextThermalModel] = {}
    for key, obs in by_ctx.items():
        c_days = len({o.started_at.date() for o in obs})
        c_onsets = [v for o in obs if (v := _onset_of(o)) is not None]
        c_active = len(obs) >= MIN_CONTEXT_SAMPLES and c_days >= MIN_CONTEXT_DISTINCT_DAYS
        c_onset = _median(c_onsets) if c_onsets else None
        if c_onset is not None:
            c_onset = _clamp(c_onset, MIN_ONSET_MIN, MAX_ONSET_MIN)
        c_eff = _clamp(c_onset * 2.0, MIN_OBSERVATION_MIN, MAX_OBSERVATION_MIN) if c_onset else None
        context_models[key] = ContextThermalModel(
            context_key=key,
            response_onset_minutes=c_onset,
            effective_observation_minutes=c_eff,
            typical_temperature_response_c=_median(
                [abs(d) for o in obs if (d := o.indoor_delta_c) is not None]
            ) if any(o.indoor_delta_c is not None for o in obs) else None,
            sample_count=len(obs), distinct_days=c_days,
            confidence=compute_model_confidence(
                sample_count=len(obs), distinct_days=c_days,
                onset_values=c_onsets, unconfounded_sample_count=len(obs)),
            active=c_active,
        )

    return ThermalResponseModel(
        zone_id=zone_id,
        response_onset_minutes=onset,
        effective_observation_minutes=effective,
        response_duration_minutes=effective,
        thermal_inertia_level=_inertia_from_onset(onset),
        typical_temperature_response_c=magnitude,
        expected_response_direction="cooling_or_hold",
        confidence=confidence,
        sample_count=sample_count, distinct_days=distinct_days,
        unconfounded_sample_count=sample_count,
        source_kind=usable[-1].source_kind,
        config_generation=config_generation,
        context_models=context_models,
        last_updated=now,
        fallback_reason=None,
    )


def compute_model_confidence(
    *, sample_count: int, distinct_days: int,
    onset_values: list[float], unconfounded_sample_count: int,
) -> float:
    """Confidence of the learned zone model (over many observations).

    Multiplicative: data-richness × day-diversity × consistency.  No magic
    weighted sum.  Returns [0,1].
    """
    if sample_count == 0:
        return 0.0
    richness = min(1.0, unconfounded_sample_count / MODEL_CONFIDENCE_SAMPLE_RAMP)
    day_div = min(1.0, distinct_days / MIN_CONTEXT_DISTINCT_DAYS)
    # Consistency: low spread of onset → high.
    if len(onset_values) >= 2:
        spread = max(onset_values) - min(onset_values)
        consistency = max(0.0, 1.0 - spread / float(MAX_ONSET_MIN))
    else:
        consistency = 0.5
    return max(0.0, min(1.0, richness * day_div * consistency))


# ---------------------------------------------------------------------------
# Active authority: observation-window selection
# ---------------------------------------------------------------------------

def _round_window(v: float) -> int:
    return int(round(v / WINDOW_ROUNDING_MIN) * WINDOW_ROUNDING_MIN)


def select_observation_window(
    model: ThermalResponseModel | None,
    context_key_value: str | None = None,
    *,
    temperature_available: bool = True,
) -> tuple[int, str]:
    """Return (observation_window_minutes, reason).

    Cold start / low confidence / no temperature → fixed 30-minute fallback.
    Medium confidence → bounded adjustment around 30 (±15).
    Full confidence → zone/context window within hard caps.
    """
    if not temperature_available:
        return COLD_START_WINDOW_MIN, "no_temperature_fallback"
    if model is None or model.effective_observation_minutes is None:
        return COLD_START_WINDOW_MIN, "cold_start_fallback"
    if model.confidence < CONFIDENCE_DIAGNOSTIC:
        return COLD_START_WINDOW_MIN, "low_confidence_diagnostic"

    # Prefer an active context sub-model when available and confident.
    effective = model.effective_observation_minutes
    used = "zone_model"
    ctx = model.context_models.get(context_key_value) if context_key_value else None
    if ctx is not None and ctx.active and ctx.effective_observation_minutes is not None \
            and ctx.confidence >= CONFIDENCE_DIAGNOSTIC:
        effective = ctx.effective_observation_minutes
        used = "context_model"

    if model.confidence < CONFIDENCE_FULL:
        lo = COLD_START_WINDOW_MIN - MEDIUM_CONFIDENCE_CAP_MIN
        hi = COLD_START_WINDOW_MIN + MEDIUM_CONFIDENCE_CAP_MIN
        win = _clamp(effective, lo, hi)
        return _round_window(win), f"medium_confidence_{used}"

    win = _clamp(effective, MIN_OBSERVATION_MIN, MAX_OBSERVATION_MIN)
    return _round_window(win), f"high_confidence_{used}"
