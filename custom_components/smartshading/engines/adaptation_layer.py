"""Adaptation Layer Foundation — Phase 9F10.

Answers: How would learning adjust the evaluator parameters for this window?

This module does NOT make runtime decisions.  It does NOT modify any threshold.
It does NOT connect to the Coordinator, HeatEvaluator, GlareEvaluator, or any
Tier-1–5 component.  It produces an AdaptiveProfile that describes the
direction and magnitude of potential parameter adjustments derived from the
current learning state.

An actual Adaptation Engine (future) will apply these factors inside the
Coordinator before parameters are forwarded to the Evaluators.

---

Inputs
------
  AdaptationInput.aggregate_result — LearningAggregateResult from 9F9
  AdaptationInput.override_result  — OverrideLearningResult from 9F7 (optional)
  AdaptationInput.solar_result     — SolarImpactResult from 9F8 (optional)

Outputs
-------
  AdaptiveProfile — six fields describing learning-suggested parameter
                    adjustments and the overall adaptation strength

---

Learning active gate
---------------------
  learning_active = aggregate_result.learning_ready

  False → all factors = 1.0 (no adaptation), adaptation_strength = 0.0
  True  → factors are computed from available signals

---

Adaptation strength
--------------------
  adaptation_strength = aggregate_result.learning_score  (already [0.0, 1.0])
  None → 0.0

---

Factor formulas
----------------
  Solar-driven factors (heat_sensitivity_factor, exposure_sensitivity_factor):

    normalized_solar = clamp(combined_solar_factor / 5.0, 0.0, 1.0)
                     → 0.0 when solar_result is None or combined_solar_factor is None

    factor = 1.0 + (normalized_solar × adaptation_strength × _MAX_STEP_FACTOR)
    factor = clamp(factor, _FACTOR_MIN, _FACTOR_MAX)

  Override-driven factor (preferred_shade_position_factor):

    override_score = override_result.learning_score
                   → 0.0 when override_result is None or learning_score is None

    factor = 1.0 + (override_score × adaptation_strength × _MAX_STEP_FACTOR)
    factor = clamp(factor, _FACTOR_MIN, _FACTOR_MAX)

  Bound: 0.90 ≤ factor ≤ 1.10  (maximum ±10 % from baseline)

  Both heat_sensitivity_factor and exposure_sensitivity_factor use the same
  formula and solar source, producing equal values in this foundation step.
  Future steps may differentiate the two by adding azimuth or elevation weights.

---

Confidence level
-----------------
  Derived from aggregate_result.confidence_score (float [0.0, 1.0] or None)
  via _classify_confidence_level().  Boundaries mirror ConfidenceEngine exactly:

    [0.00, 0.20) → "very_low"
    [0.20, 0.40) → "low"
    [0.40, 0.60) → "medium"
    [0.60, 0.80) → "high"
    [0.80, 1.00] → "very_high"
    None          → "very_low"

  The string values match ConfidenceLevel.value so downstream consumers can
  compare without importing ConfidenceLevel.

---

Tier safety
-----------
Purely descriptive.  No Tier 1–5 logic is touched.  No thresholds are modified.
No runtime state is read or written.
"""
from __future__ import annotations

from dataclasses import dataclass

from .learning_signal_aggregator import LearningAggregateResult
from .override_learning import OverrideLearningResult
from .solar_impact_learning import SolarImpactResult


# ---------------------------------------------------------------------------
# Architecture constants
# ---------------------------------------------------------------------------

# Hard bounds on any sensitivity factor.  No single adaptation cycle can move
# a parameter more than ±10 % from the neutral baseline of 1.0.
_FACTOR_MIN: float = 0.90
_FACTOR_MAX: float = 1.10

# Maximum fractional step applied per cycle.  Multiplied by the signal and
# adaptation_strength; at full strength (both = 1.0) the step is exactly 0.10.
_MAX_STEP_FACTOR: float = 0.10

# Reference scale for combined_solar_factor (°C → [0.0, 1.0]).
# Mirrors _SOLAR_NORM_SCALE in learning_signal_aggregator.py.
_SOLAR_NORM_SCALE: float = 5.0

# Solar threshold adaptation: bidirectional signed signal.
# Neutral point: 2.0°C indoor temp rise from sun is considered "normal".
# Below neutral → negative signal → W/m² thresholds RISE (shade later).
# Above neutral → positive signal → W/m² thresholds FALL (shade earlier).
# ±4°C from neutral maps to ±1.0 signal.
_SOLAR_THRESHOLD_NEUTRAL_C:    float = 2.0
_SOLAR_THRESHOLD_NORM_SCALE:   float = 4.0

# Score boundaries for confidence_level string labels.
# Lower bound inclusive, upper bound exclusive — mirrors ConfidenceLevel thresholds.
_CONFIDENCE_THRESHOLDS: list[tuple[float, str]] = [
    (0.20, "very_low"),
    (0.40, "low"),
    (0.60, "medium"),
    (0.80, "high"),
]
_CONFIDENCE_TOP: str = "very_high"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdaptationInput:
    """Input to compute_adaptive_profile().

    aggregate_result — LearningAggregateResult from 9F9 (required)
    override_result  — OverrideLearningResult from 9F7; None → no override signal
    solar_result     — SolarImpactResult from 9F8; None → no solar signal
    """

    aggregate_result: LearningAggregateResult
    override_result:  OverrideLearningResult | None
    solar_result:     SolarImpactResult | None


@dataclass(frozen=True)
class AdaptiveProfile:
    """Output of compute_adaptive_profile().

    learning_active                 — True when learning_ready gate passed
    confidence_level                — string label derived from confidence_score
    heat_sensitivity_factor         — solar-driven multiplier for heat threshold  [0.90, 1.10]
    exposure_sensitivity_factor     — solar-driven multiplier for exposure limit  [0.90, 1.10]
    preferred_shade_position_factor — override-driven multiplier for position     [0.90, 1.10]
    solar_escalation_factor          — BIDIRECTIONAL W/m² threshold multiplier    [0.90, 1.10]
                                      factor < 1.0 → thresholds RISE (uncritical solar impact)
                                      factor > 1.0 → thresholds FALL (strong solar impact)
                                      Neutral (2°C indoor delta) → exactly 1.0
    adaptation_strength             — normalised aggregate learning weight        [0.00, 1.00]
    """

    learning_active:                 bool
    confidence_level:                str
    heat_sensitivity_factor:         float
    exposure_sensitivity_factor:     float
    preferred_shade_position_factor: float
    solar_escalation_factor:          float
    adaptation_strength:             float


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _classify_confidence_level(score: float | None) -> str:
    """Map a confidence score [0.0, 1.0] to a string label.

    None or values in [0.0, 0.20) → "very_low".
    Mirrors ConfidenceEngine._classify() boundaries.
    """
    if score is None:
        return "very_low"
    for threshold, label in _CONFIDENCE_THRESHOLDS:
        if score < threshold:
            return label
    return _CONFIDENCE_TOP


def _normalise_solar(solar_result: SolarImpactResult | None) -> float:
    """Extract and normalise combined_solar_factor to [0.0, 1.0].

    Returns 0.0 when solar_result is None or combined_solar_factor is None.
    Negative solar factors (rare cooling effects) are clamped to 0.0.
    """
    if solar_result is None or solar_result.combined_solar_factor is None:
        return 0.0
    return max(0.0, min(1.0, solar_result.combined_solar_factor / _SOLAR_NORM_SCALE))


def _normalise_solar_for_thresholds(solar_result: SolarImpactResult | None) -> float:
    """Compute a SIGNED [-1.0, 1.0] signal for W/m² threshold adaptation.

    Below neutral (2°C) → negative → thresholds rise → window stays open longer.
    Above neutral (2°C) → positive → thresholds fall → window shades earlier.
    Returns 0.0 when no solar data is available.
    """
    if solar_result is None or solar_result.combined_solar_factor is None:
        return 0.0
    delta = solar_result.combined_solar_factor - _SOLAR_THRESHOLD_NEUTRAL_C
    return max(-1.0, min(1.0, delta / _SOLAR_THRESHOLD_NORM_SCALE))


def _extract_override_score(override_result: OverrideLearningResult | None) -> float:
    """Extract learning_score from OverrideLearningResult.

    Returns 0.0 when override_result is None or learning_score is None.
    """
    if override_result is None or override_result.learning_score is None:
        return 0.0
    return override_result.learning_score


def _compute_factor(signal: float, adaptation_strength: float) -> float:
    """Compute a single sensitivity factor and clamp to [_FACTOR_MIN, _FACTOR_MAX].

    formula: 1.0 + (signal × adaptation_strength × _MAX_STEP_FACTOR)
    Both signal and adaptation_strength are non-negative [0.0, 1.0], so the
    raw result is always ≥ 1.0.  The lower clamp (_FACTOR_MIN = 0.90) guards
    against future extensions that allow negative signal directions.
    """
    raw = 1.0 + (signal * adaptation_strength * _MAX_STEP_FACTOR)
    return max(_FACTOR_MIN, min(_FACTOR_MAX, raw))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_adaptive_profile(inp: AdaptationInput) -> AdaptiveProfile:
    """Compute an AdaptiveProfile from an AdaptationInput.

    Pure function — no state, no randomness, no I/O.
    """
    aggregate = inp.aggregate_result
    confidence_level = _classify_confidence_level(aggregate.confidence_score)

    # ------------------------------------------------------------------
    # Learning active gate
    # ------------------------------------------------------------------
    if not aggregate.learning_ready:
        return AdaptiveProfile(
            learning_active=False,
            confidence_level=confidence_level,
            heat_sensitivity_factor=1.0,
            exposure_sensitivity_factor=1.0,
            preferred_shade_position_factor=1.0,
            solar_escalation_factor=1.0,
            adaptation_strength=0.0,
        )

    # ------------------------------------------------------------------
    # Adaptation strength
    # ------------------------------------------------------------------
    adaptation_strength: float = (
        max(0.0, min(1.0, aggregate.learning_score))
        if aggregate.learning_score is not None
        else 0.0
    )

    # ------------------------------------------------------------------
    # Signal extraction
    # ------------------------------------------------------------------
    normalized_solar          = _normalise_solar(inp.solar_result)
    signed_solar_for_threshold = _normalise_solar_for_thresholds(inp.solar_result)
    override_score             = _extract_override_score(inp.override_result)

    # ------------------------------------------------------------------
    # Factor computation
    # ------------------------------------------------------------------
    heat_factor               = _compute_factor(normalized_solar,           adaptation_strength)
    exposure_factor           = _compute_factor(normalized_solar,           adaptation_strength)
    position_factor           = _compute_factor(override_score,             adaptation_strength)
    solar_escalation_factor    = _compute_factor(signed_solar_for_threshold, adaptation_strength)

    return AdaptiveProfile(
        learning_active=True,
        confidence_level=confidence_level,
        heat_sensitivity_factor=heat_factor,
        exposure_sensitivity_factor=exposure_factor,
        preferred_shade_position_factor=position_factor,
        solar_escalation_factor=solar_escalation_factor,
        adaptation_strength=adaptation_strength,
    )
