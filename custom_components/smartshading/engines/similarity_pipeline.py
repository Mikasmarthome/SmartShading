"""Similarity Pipeline — Phase 9F5-6.

Connects the individual Similarity Foundation components into a single
callable that the Coordinator can invoke once per evaluation cycle.

Pipeline stages:
  1. build_situations(transitions, outcomes)
       → joins StateTransitionRecord + DecisionOutcome by (window_id, decision_timestamp)
       → silently discards unresolved outcomes (outcome_score is None)
  2. filter by window_id
       → narrows the joined pool to the specific window being evaluated
  3. find_similar_situations(current, historical, ...)
       → nearest-neighbour search with min_similarity and max_results
       → self-exclusion: (window_id, decision_timestamp) match → skipped
  4. aggregate_outcomes(matches)
       → similarity-weighted expected_score, variance, agreement_rate,
          override_rate, avg_similarity

API choice:
  compute_similarity_result() with an explicit window_id parameter.

  The window_id is required even though current.window_id contains the same
  value in normal usage.  Making it explicit keeps the call site self-describing
  and avoids surprises when the caller passes a current situation from a
  different context.

  compute_similarity_result_from_situations() is not provided — the caller
  always has access to the raw LearningStore lists, so the pre-join convenience
  is unnecessary complexity.

Graceful degradation:
  Any empty or unmatched input produces:
    SimilarityResult(similar_count=0, expected_score=None, ...)
  No exception is raised.

No HA dependencies. No caching. No adaptive decisions.
"""
from __future__ import annotations

from ..models.learning import DecisionOutcome, StateTransitionRecord
from .outcome_aggregation import SimilarityResult, aggregate_outcomes
from .similarity_engine import find_similar_situations
from .situation_joiner import SituationRecord, build_situations


def compute_similarity_result(
    *,
    window_id: str,
    current: SituationRecord,
    transitions: list[StateTransitionRecord],
    outcomes: list[DecisionOutcome],
    min_similarity: float = 0.50,
    max_results: int = 25,
) -> SimilarityResult:
    """Run the full Similarity Pipeline for *window_id* and return a SimilarityResult.

    Steps:
      1. Join *transitions* and *outcomes* into SituationRecords.
      2. Filter to *window_id* (window-scoped search).
      3. Find the nearest neighbours of *current*.
      4. Aggregate their outcome signals.

    Returns an empty SimilarityResult (similar_count=0, all metrics None) when:
      - *transitions* or *outcomes* is empty
      - no matching join partner is found
      - no historical situation meets min_similarity
    """
    all_situations = build_situations(transitions, outcomes)
    historical = [s for s in all_situations if s.window_id == window_id]
    matches = find_similar_situations(
        current,
        historical,
        min_similarity=min_similarity,
        max_results=max_results,
    )
    return aggregate_outcomes(matches)
