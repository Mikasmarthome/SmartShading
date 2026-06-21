"""OutcomeAggregation — Phase 9F5-5.

Aggregates a list of SimilarityMatches into a SimilarityResult that
summarises the historical outcome signal for the current situation.

Input  : list[SimilarityMatch]   (from SimilarityEngine.find_similar_situations)
Output : SimilarityResult        (frozen dataclass — pure data, no HA)

---

Resolution-status filter decision
----------------------------------
Three status values exist:
  "complete"           — TIMEOUT trigger + indoor-temp available
  "partial_no_temp"    — TIMEOUT trigger + no indoor-temp sensor
  "partial_early_exit" — all other triggers (OVERRIDE, STATE_CHANGE,
                          LIFECYCLE, SAFETY)

This function includes all three status values by default, for one
structural reason: every OVERRIDE-trigger outcome carries status
"partial_early_exit" AND override_occurred=True.  Filtering out
"partial_early_exit" would silently zero out override_rate — removing
the strongest negative learning signal in the system.

Callers that want to restrict the input (e.g. "only TIMEOUT outcomes")
can pre-filter the SimilarityMatch list before calling aggregate_outcomes().

---

Metric definitions
------------------
expected_score   : similarity-weighted mean of outcome_scores.
                   Higher-similarity neighbours count proportionally more.
                   None when the total similarity weight is 0.0.

outcome_variance : similarity-weighted variance of outcome_scores around
                   expected_score.  Measures spread of outcomes in the
                   neighbourhood.  0.0 = perfect consensus; 1.0 = maximum
                   spread (bounded by score range [-1, +1]).
                   None when expected_score is None.

agreement_rate   : fraction of neighbours whose outcome_score has the same
                   sign as expected_score.
                   expected_score > 0 → agree if score > 0
                   expected_score < 0 → agree if score < 0
                   expected_score = 0 → agree if score = 0 (exact)
                   None when expected_score is None.

override_rate    : fraction of neighbours where override_occurred=True.
                   Always computed; None only on empty input.

avg_similarity   : unweighted mean of similarity scores.
                   None only on empty input.
"""
from __future__ import annotations

from dataclasses import dataclass

from .similarity_engine import SimilarityMatch


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SimilarityResult:
    """Aggregated outcome signal from a neighbourhood of similar situations.

    All metric fields are None when the input was empty or numerically
    degenerate (e.g. all similarity weights are 0.0).
    """

    similar_count: int
    expected_score: float | None
    outcome_variance: float | None
    agreement_rate: float | None
    override_rate: float | None
    avg_similarity: float | None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_outcomes(matches: list[SimilarityMatch]) -> SimilarityResult:
    """Aggregate *matches* into a single SimilarityResult.

    Empty input → similar_count=0, all metrics None.
    """
    if not matches:
        return SimilarityResult(
            similar_count=0,
            expected_score=None,
            outcome_variance=None,
            agreement_rate=None,
            override_rate=None,
            avg_similarity=None,
        )

    n = len(matches)
    similarities = [m.similarity for m in matches]
    scores = [m.situation.outcome_score for m in matches]
    total_weight = sum(similarities)

    # --- expected_score: similarity-weighted mean ----------------------------
    if total_weight > 0.0:
        expected_score: float | None = (
            sum(sim * score for sim, score in zip(similarities, scores)) / total_weight
        )
    else:
        expected_score = None

    # --- outcome_variance: similarity-weighted variance ---------------------
    if expected_score is not None:
        weighted_sq_diff = sum(
            sim * (score - expected_score) ** 2
            for sim, score in zip(similarities, scores)
        )
        outcome_variance: float | None = weighted_sq_diff / total_weight
    else:
        outcome_variance = None

    # --- agreement_rate: fraction matching the sign of expected_score -------
    if expected_score is not None:
        if expected_score > 0.0:
            agreeing = sum(1 for sc in scores if sc > 0.0)
        elif expected_score < 0.0:
            agreeing = sum(1 for sc in scores if sc < 0.0)
        else:
            agreeing = sum(1 for sc in scores if sc == 0.0)
        agreement_rate: float | None = agreeing / n
    else:
        agreement_rate = None

    # --- override_rate: fraction with override_occurred=True ----------------
    override_rate: float | None = (
        sum(1 for m in matches if m.situation.override_occurred) / n
    )

    # --- avg_similarity: unweighted mean of similarity scores ---------------
    avg_similarity: float | None = sum(similarities) / n

    return SimilarityResult(
        similar_count=n,
        expected_score=expected_score,
        outcome_variance=outcome_variance,
        agreement_rate=agreement_rate,
        override_rate=override_rate,
        avg_similarity=avg_similarity,
    )
