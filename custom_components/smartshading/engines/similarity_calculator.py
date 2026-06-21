"""SimilarityCalculator — Phase 9F5-3.

Computes a similarity score in [0.0, 1.0] between two NormalizedSituations.

Algorithm:
  1. Mandatory filters (return 0.0 immediately on mismatch):
       lifecycle_state — "day" ≠ "night" → 0.0
       decided_by      — "HeatEvaluator" ≠ "NightEvaluator" → 0.0

  2. Per-feature weighted distance:
       distance_i = abs(a_i - b_i)         →  [0.0, 1.0]
       weighted_sum += distance_i * weight_i

  3. Weight redistribution for missing features:
       If either side carries None for a feature, that feature is skipped and
       its weight is removed. The remaining weights are re-normalized to 1.0
       by dividing every weighted_sum term by total_available_weight.

       weighted_distance = weighted_sum / total_available_weight

  4. Similarity:
       similarity = 1.0 - weighted_distance
       clamped to [0.0, 1.0]

  5. Special case — no feature comparable (all None):
       similarity = 0.0

Feature weights (sum to 1.0):
  effective_exposure_norm      0.35  — strongest single signal
  solar_relative_azimuth_norm  0.25  — window-relative sun angle
  sun_elevation_norm           0.15  — solar intensity
  outdoor_temp_norm            0.15  — thermal context
  indoor_temp_norm             0.10  — heat-evaluator context (optional sensor)

Design invariants:
  - Pure function: no state, no randomness, no I/O.
  - Deterministic: same pair → same score always.
  - Symmetric: calculate_similarity(a, b) == calculate_similarity(b, a).
"""
from __future__ import annotations

from .feature_normalizer import NormalizedSituation

# ---------------------------------------------------------------------------
# Feature weights — architecture constants, sum to 1.0
# ---------------------------------------------------------------------------

# Each tuple: (NormalizedSituation field name, weight)
_FEATURE_WEIGHTS: list[tuple[str, float]] = [
    ("effective_exposure_norm",     0.35),
    ("solar_relative_azimuth_norm", 0.25),
    ("sun_elevation_norm",          0.15),
    ("outdoor_temp_norm",           0.15),
    ("indoor_temp_norm",            0.10),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_similarity(a: NormalizedSituation, b: NormalizedSituation) -> float:
    """Return a similarity score in [0.0, 1.0] for the pair (a, b).

    Returns 0.0 immediately when the mandatory filters are not satisfied.
    Returns 0.0 when no feature is comparable (all None on either side).
    """
    # Mandatory filters — different decision domains are incomparable
    if a.lifecycle_state != b.lifecycle_state:
        return 0.0
    if a.decided_by != b.decided_by:
        return 0.0

    total_weight = 0.0
    weighted_sum = 0.0

    for field, weight in _FEATURE_WEIGHTS:
        val_a: float | None = getattr(a, field)
        val_b: float | None = getattr(b, field)
        if val_a is None or val_b is None:
            continue  # skip and remove weight from pool
        total_weight += weight
        weighted_sum += abs(val_a - val_b) * weight

    if total_weight == 0.0:
        return 0.0  # all features missing — not comparable

    # Divide by total_weight re-normalizes the remaining weights to 1.0
    weighted_distance = weighted_sum / total_weight
    return max(0.0, min(1.0, 1.0 - weighted_distance))


def calculate_distance(a: NormalizedSituation, b: NormalizedSituation) -> float:
    """Return 1.0 - calculate_similarity(a, b).

    A distance of 0.0 means identical; 1.0 means maximally different.
    Satisfies the same filter and None-handling rules as calculate_similarity.
    """
    return 1.0 - calculate_similarity(a, b)
