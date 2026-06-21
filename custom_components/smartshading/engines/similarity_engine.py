"""SimilarityEngine — Phase 9F5-4.

Nearest-neighbour search over historical SituationRecords.

Pipeline per candidate:
  SituationRecord
    → normalize_situation   (FeatureNormalizer)
    → calculate_similarity  (SimilarityCalculator)
    → SimilarityMatch

Only matches at or above min_similarity are returned.  Results are sorted
by similarity descending so the closest historical situation comes first.

Self-exclusion:
  A historical record with the same (window_id, decision_timestamp) as the
  current situation is silently skipped — it is the same decision and would
  trivially score 1.0 (or 0.0 after a mandatory-filter mismatch, which is
  harmless but still misleading).  Identity is defined by the join key, not
  Python object identity.

No outcome aggregation, no confidence, no adaptive decisions — this step
produces only a ranked list of similar situations.  Higher-level components
interpret what those neighbours mean.
"""
from __future__ import annotations

from dataclasses import dataclass

from .feature_normalizer import normalize_situation
from .similarity_calculator import calculate_similarity
from .situation_joiner import SituationRecord


@dataclass(frozen=True)
class SimilarityMatch:
    """One result from find_similar_situations.

    situation — the historical record that was found similar
    similarity — score in [0.0, 1.0]; at least min_similarity
    """

    situation: SituationRecord
    similarity: float


def find_similar_situations(
    current: SituationRecord,
    historical: list[SituationRecord],
    *,
    min_similarity: float = 0.50,
    max_results: int = 25,
) -> list[SimilarityMatch]:
    """Return the top-k historical situations most similar to *current*.

    Steps:
      1. Normalize *current*.
      2. For each record in *historical*:
           a. Skip if same (window_id, decision_timestamp) → self-exclusion.
           b. Normalize the candidate.
           c. Compute similarity score.
           d. Discard if score < min_similarity.
      3. Sort remaining matches by similarity descending.
         Tiebreaker: decision_timestamp descending (prefer newer records).
      4. Return the first max_results entries.

    Returns an empty list when no match meets the threshold, or when
    *historical* is empty.
    """
    norm_current = normalize_situation(current)

    matches: list[SimilarityMatch] = []
    for hist in historical:
        # Self-exclusion — same decision is not a "neighbour"
        if (
            hist.window_id == current.window_id
            and hist.decision_timestamp == current.decision_timestamp
        ):
            continue

        norm_hist = normalize_situation(hist)
        score = calculate_similarity(norm_current, norm_hist)
        if score >= min_similarity:
            matches.append(SimilarityMatch(situation=hist, similarity=score))

    # Primary: similarity descending; secondary: timestamp descending (deterministic tiebreak)
    matches.sort(
        key=lambda m: (m.similarity, m.situation.decision_timestamp),
        reverse=True,
    )
    return matches[:max_results]
