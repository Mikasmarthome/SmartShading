"""Learning Signal Aggregator — Phase 9F9.

Answers: How robust is the combined learning state for this window right now?

This module does NOT make runtime decisions.  It produces a diagnostic
LearningAggregateResult that describes the overall quality of the available
learning signals.  An Adaptation Layer (future) will decide what to do with it.

Input:
  LearningAggregateInput — up to four optional results from the learning stack

Output:
  LearningAggregateResult — weighted learning score, readiness flag, per-signal
                            scores, signal count, and consistency measure

---

Signal normalisation
--------------------

All three participating signals must be on the same [0.0, 1.0] scale:

  confidence_score  = ConfidenceResult.score          (already [0.0, 1.0])
  override_score    = OverrideLearningResult.learning_score (already [0.0, 1.0])
  solar_score       = clamp(SolarImpactResult.combined_solar_factor / 5.0, 0.0, 1.0)

Solar normalisation justification:
  combined_solar_factor is in °C.  5 °C is the reference for "strong solar
  impact": a +5 °C rise in the observation window is a meaningful thermal
  event in typical residential spaces.  This makes 0 °C → 0.0 ("no thermal
  signal") and 5 °C → 1.0 ("strong signal") with linear interpolation.
  Negative values (rare cooling effects) are clamped to 0.0 since they do
  not represent a positive solar burden signal.  Values above 5 °C are
  clamped to 1.0.
  The scale constant (_SOLAR_NORM_SCALE = 5.0) is a named architecture
  constant and can be adjusted as real-world data accumulates.

SimilarityResult is accepted as input for completeness but is not a direct
scoring signal in this step.  It provides context (similar_count, avg_similarity)
for future Adaptation Layer logic.

---

Learning score formula
-----------------------

Weights:  confidence=0.40, override=0.35, solar=0.25   (sum = 1.00)

None redistribution: identical to SimilarityCalculator and ConfidenceEngine.
When a signal is unavailable (None), its weight is dropped and the remaining
weights are re-normalised to 1.0.  All signals None → learning_score = None.

---

Consistency score
-----------------

  consistency_score = max(available_scores) − min(available_scores)

  0.0 → all signals agree (perfectly consistent)
  1.0 → signals span the full range (strongly contradictory)

  Requires ≥ 2 available signals; None otherwise.

  Interpretation:
    Low consistency_score + high learning_score  → strong, coherent signal
    High consistency_score + medium learning_score → mixed picture; diagnose
    High consistency_score + low learning_score  → contradictory; no action

---

Learning ready
--------------

  learning_ready = True  when:
    1. confidence_result is not None
    2. confidence_result.level ∈ {MEDIUM, HIGH, VERY_HIGH}
    3. signal_count ≥ 2

  Rationale:
    - Confidence is the foundational gate: it measures overall data richness.
      Without sufficient data (< MEDIUM), no aggregate signal is meaningful.
    - signal_count ≥ 2 ensures at least one domain signal (Override or Solar)
      is present alongside Confidence.  A single signal cannot be cross-validated.
    - Override or Solar alone (no Confidence) can indicate domain learning but
      not a reliable enough system state for Adaptation-Layer decisions.

---

Tier safety
-----------

Purely diagnostic.  No Tier 1–5 logic is touched.  No thresholds are modified.
"""
from __future__ import annotations

from dataclasses import dataclass

from .confidence_engine import ConfidenceLevel, ConfidenceResult
from .outcome_aggregation import SimilarityResult
from .override_learning import OverrideLearningResult
from .solar_impact_learning import SolarImpactResult


# ---------------------------------------------------------------------------
# Architecture constants
# ---------------------------------------------------------------------------

# Per-signal weights for the learning score (sum = 1.00).
_SIGNAL_WEIGHTS: list[tuple[str, float]] = [
    ("confidence_score", 0.40),
    ("override_score",   0.35),
    ("solar_score",      0.25),
]

# Normalisation scale for SolarImpactResult.combined_solar_factor (°C → [0, 1]).
_SOLAR_NORM_SCALE: float = 5.0

# Confidence levels that open the learning-ready gate.
_CONFIDENCE_READY_LEVELS: frozenset[ConfidenceLevel] = frozenset(
    {ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH, ConfidenceLevel.VERY_HIGH}
)

_MIN_SIGNAL_COUNT_FOR_READY: int = 2


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LearningAggregateInput:
    """Input to aggregate_learning_signals().

    All four fields are optional.  Any combination is accepted; the aggregator
    degrades gracefully when signals are absent.

    similarity_result   — from SimilarityPipeline; not a direct scoring signal
                          in 9F9 but accepted for future Adaptation Layer context
    confidence_result   — from ConfidenceEngine; drives the learning_ready gate
    override_result     — from OverrideLearningEngine; None-score = no signal
    solar_result        — from SolarImpactEngine; None combined_factor = no signal
    """

    similarity_result: SimilarityResult | None
    confidence_result: ConfidenceResult | None
    override_result:   OverrideLearningResult | None
    solar_result:      SolarImpactResult | None


@dataclass(frozen=True)
class LearningAggregateResult:
    """Output of aggregate_learning_signals().

    learning_score     — weighted average of available normalised signals [0.0, 1.0];
                         None when no signal is available
    learning_ready     — True when learning state is reliable enough for downstream use
    confidence_score   — normalised confidence signal [0.0, 1.0] | None
    override_score     — normalised override learning signal [0.0, 1.0] | None
    solar_score        — normalised solar impact signal [0.0, 1.0] | None
    signal_count       — number of available (non-None) scoring signals (0–3)
    consistency_score  — max−min spread of available signals [0.0, 1.0];
                         None when fewer than 2 signals are available
    """

    learning_score:    float | None
    learning_ready:    bool
    confidence_score:  float | None
    override_score:    float | None
    solar_score:       float | None
    signal_count:      int
    consistency_score: float | None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalise_solar(combined_solar_factor: float) -> float:
    """Map combined_solar_factor (°C) to [0.0, 1.0] via _SOLAR_NORM_SCALE."""
    return max(0.0, min(1.0, combined_solar_factor / _SOLAR_NORM_SCALE))


def _compute_learning_score(
    confidence_score: float | None,
    override_score:   float | None,
    solar_score:      float | None,
) -> float | None:
    """Weighted average with None redistribution.

    Absent signals have their weight dropped; remaining weights are
    re-normalised to 1.0.  All absent → None.
    """
    score_map = {
        "confidence_score": confidence_score,
        "override_score":   override_score,
        "solar_score":      solar_score,
    }

    total_weight  = 0.0
    weighted_sum  = 0.0

    for name, weight in _SIGNAL_WEIGHTS:
        val = score_map[name]
        if val is None:
            continue
        total_weight += weight
        weighted_sum += val * weight

    if total_weight == 0.0:
        return None

    return max(0.0, min(1.0, weighted_sum / total_weight))


def _compute_consistency_score(scores: list[float]) -> float | None:
    """Spread of available scores: max − min.

    0.0 → perfectly consistent; 1.0 → strongly contradictory.
    None when fewer than 2 scores are available.
    """
    if len(scores) < 2:
        return None
    return max(scores) - min(scores)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def aggregate_learning_signals(inp: LearningAggregateInput) -> LearningAggregateResult:
    """Aggregate all available learning signals into a LearningAggregateResult.

    Pure function — no state, no randomness, no I/O.
    """
    # ------------------------------------------------------------------
    # Extract and normalise per-signal scores
    # ------------------------------------------------------------------
    confidence_score: float | None = (
        inp.confidence_result.score
        if inp.confidence_result is not None else None
    )

    override_score: float | None = (
        inp.override_result.learning_score
        if inp.override_result is not None else None
    )

    solar_score: float | None = None
    if (inp.solar_result is not None
            and inp.solar_result.combined_solar_factor is not None):
        solar_score = _normalise_solar(inp.solar_result.combined_solar_factor)

    # ------------------------------------------------------------------
    # Collect available scores for aggregate computations
    # ------------------------------------------------------------------
    available_scores: list[float] = [
        s for s in (confidence_score, override_score, solar_score) if s is not None
    ]
    signal_count = len(available_scores)

    # ------------------------------------------------------------------
    # Learning score and consistency
    # ------------------------------------------------------------------
    learning_score     = _compute_learning_score(confidence_score, override_score, solar_score)
    consistency_score  = _compute_consistency_score(available_scores)

    # ------------------------------------------------------------------
    # Learning ready
    # ------------------------------------------------------------------
    learning_ready = (
        inp.confidence_result is not None
        and inp.confidence_result.level in _CONFIDENCE_READY_LEVELS
        and signal_count >= _MIN_SIGNAL_COUNT_FOR_READY
    )

    return LearningAggregateResult(
        learning_score=learning_score,
        learning_ready=learning_ready,
        confidence_score=confidence_score,
        override_score=override_score,
        solar_score=solar_score,
        signal_count=signal_count,
        consistency_score=consistency_score,
    )
