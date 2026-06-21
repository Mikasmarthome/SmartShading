"""ConfidenceEngine — Phase 9F6.

Answers: how much should SmartShading trust a SimilarityResult?

Input:
  SimilarityResult  — from the Similarity Pipeline (9F5-6)
  total_resolved_outcomes  — all resolved outcomes for this window (from LearningStore)

Output:
  ConfidenceResult  — numerical score, level class, and all intermediate factors

---

Architecture
------------

Two orthogonal confidence dimensions are computed and combined:

  Global Confidence (G):
    How much data does SmartShading have about this window in total?
    G = min(1.0, total_resolved_outcomes / 50)
    Ramps from 0.0 → 1.0 as data accumulates. Below ~25 outcomes the
    system is in a low-confidence learning phase regardless of situational
    signal quality.

  Situational Confidence (S):
    How strong and consistent is the signal from the neighbourhood?
    S = sample_factor × core

    sample_factor = min(1.0, similar_count / 25)
      Gates all situational confidence. Zero similar neighbours → S = 0.

    core  = weighted average of four quality factors (weight redistribution
            for None factors, identical strategy to SimilarityCalculator):
      agreement_factor  0.30  — directional consensus of neighbours
      variance_factor   0.25  — outcome consistency
      override_factor   0.25  — absence of user corrections
      similarity_factor 0.20  — closeness of neighbours

  Final:
    score = clamp(G × S, 0.0, 1.0)

---

Factor formulas
---------------
  agreement_factor  = max(0.0, (agreement_rate  - 0.5) / 0.5)   | None → None
  variance_factor   = max(0.0, 1.0 - 2.0 × outcome_variance)    | None → None
  override_factor   = 1.0 - override_rate                        | None → None
  similarity_factor = avg_similarity                              | None → None

  agreement_rate < 0.50 → factor 0.0  (no consensus, not negative)
  outcome_variance > 0.50 → factor 0.0 (too noisy)
  override_rate > 1.0 is impossible; factor is clamped to [0.0, 1.0] via formula
    (1.0 - 0.0 = 1.0 … 1.0 - 1.0 = 0.0)

None redistribution:
  When a factor is None its weight is removed and remaining weights are
  re-normalized to 1.0 (same as SimilarityCalculator.calculate_similarity).
  All factors None → core = 0.0.

---

Tier safety
-----------
This module is purely computational. It has no access to, and no influence
over, Tier 1–3 logic (Safety, Override, Lifecycle). No evaluator thresholds
are modified here. The ConfidenceResult is a diagnostic/gate value for future
Learning Engine steps.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .outcome_aggregation import SimilarityResult


# ---------------------------------------------------------------------------
# Confidence level classification
# ---------------------------------------------------------------------------

class ConfidenceLevel(Enum):
    """Discrete classification of the numerical confidence score.

    Boundary convention: lower bound inclusive, upper bound exclusive,
    except VERY_HIGH which includes 1.0.

      VERY_LOW   [0.00, 0.20)
      LOW        [0.20, 0.40)
      MEDIUM     [0.40, 0.60)
      HIGH       [0.60, 0.80)
      VERY_HIGH  [0.80, 1.00]
    """

    VERY_LOW  = "very_low"
    LOW       = "low"
    MEDIUM    = "medium"
    HIGH      = "high"
    VERY_HIGH = "very_high"


def _classify(score: float) -> ConfidenceLevel:
    """Map a numerical score in [0.0, 1.0] to a ConfidenceLevel."""
    if score < 0.20:
        return ConfidenceLevel.VERY_LOW
    if score < 0.40:
        return ConfidenceLevel.LOW
    if score < 0.60:
        return ConfidenceLevel.MEDIUM
    if score < 0.80:
        return ConfidenceLevel.HIGH
    return ConfidenceLevel.VERY_HIGH


# ---------------------------------------------------------------------------
# Core weight table — architecture constants, sum to 1.0
# ---------------------------------------------------------------------------

# Each entry: (factor name in ConfidenceResult, weight)
_CORE_WEIGHTS: list[tuple[str, float]] = [
    ("agreement_factor",  0.30),
    ("variance_factor",   0.25),
    ("override_factor",   0.25),
    ("similarity_factor", 0.20),
]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfidenceInput:
    """Input to compute_confidence().

    result                 — aggregated neighbourhood signal from SimilarityPipeline
    total_resolved_outcomes — count of all resolved DecisionOutcomes for this window
                             (used to compute Global Confidence)
    """

    result: SimilarityResult
    total_resolved_outcomes: int


@dataclass(frozen=True)
class ConfidenceResult:
    """Output of compute_confidence().

    score                 — final confidence in [0.0, 1.0]  (G × S)
    level                 — discrete classification
    global_confidence     — G: how much data exists for this window
    situational_confidence — S: how strong the neighbourhood signal is

    Intermediate factors (all in [0.0, 1.0] or None when input metric absent):
      sample_factor      — similar_count / 25, always float (never None)
      agreement_factor   — directional consensus
      variance_factor    — outcome consistency
      override_factor    — absence of user corrections
      similarity_factor  — neighbourhood closeness
    """

    score: float
    level: ConfidenceLevel
    global_confidence: float
    situational_confidence: float
    sample_factor: float | None
    agreement_factor: float | None
    variance_factor: float | None
    override_factor: float | None
    similarity_factor: float | None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_core(
    agreement_factor: float | None,
    variance_factor: float | None,
    override_factor: float | None,
    similarity_factor: float | None,
) -> float:
    """Weighted average of the four quality factors with None redistribution.

    Missing (None) factors have their weight removed; remaining weights are
    re-normalized to 1.0 by dividing the weighted sum by total_available_weight.
    All factors None → 0.0.
    """
    factor_values = {
        "agreement_factor":  agreement_factor,
        "variance_factor":   variance_factor,
        "override_factor":   override_factor,
        "similarity_factor": similarity_factor,
    }

    total_weight = 0.0
    weighted_sum = 0.0

    for field, weight in _CORE_WEIGHTS:
        val = factor_values[field]
        if val is None:
            continue
        total_weight += weight
        weighted_sum += val * weight

    if total_weight == 0.0:
        return 0.0

    return weighted_sum / total_weight


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_confidence(inp: ConfidenceInput) -> ConfidenceResult:
    """Compute a ConfidenceResult from a ConfidenceInput.

    Pure function — no state, no randomness, no I/O.
    """
    result = inp.result

    # --- Global confidence ---------------------------------------------------
    G = min(1.0, inp.total_resolved_outcomes / 50)

    # --- Sample factor -------------------------------------------------------
    sample_factor: float = min(1.0, result.similar_count / 25)

    # --- Individual quality factors ------------------------------------------
    agreement_factor: float | None = (
        max(0.0, (result.agreement_rate - 0.5) / 0.5)
        if result.agreement_rate is not None else None
    )
    variance_factor: float | None = (
        max(0.0, 1.0 - 2.0 * result.outcome_variance)
        if result.outcome_variance is not None else None
    )
    override_factor: float | None = (
        1.0 - result.override_rate
        if result.override_rate is not None else None
    )
    similarity_factor: float | None = result.avg_similarity  # None propagates as-is

    # --- Core score ----------------------------------------------------------
    core = _compute_core(agreement_factor, variance_factor, override_factor, similarity_factor)

    # --- Situational confidence ----------------------------------------------
    S = sample_factor * core

    # --- Final confidence ----------------------------------------------------
    score = max(0.0, min(1.0, G * S))

    return ConfidenceResult(
        score=score,
        level=_classify(score),
        global_confidence=G,
        situational_confidence=S,
        sample_factor=sample_factor,
        agreement_factor=agreement_factor,
        variance_factor=variance_factor,
        override_factor=override_factor,
        similarity_factor=similarity_factor,
    )
