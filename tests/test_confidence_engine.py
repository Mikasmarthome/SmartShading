"""Tests for ConfidenceEngine (T12).

ConfidenceEngine is the sole confidence authority in SmartShading. This
covers:
  - compute_confidence(): global/situational confidence, factor
    redistribution, classification boundaries.
  - compute_sample_maturity_confidence(): the shared evidence-maturity ramp
    used both for Global Confidence (G) and for shadow-candidate maturity
    (coordinator._maybe_shadow) — proven here to be exactly one formula.
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.engines.confidence_engine import (
    ConfidenceInput,
    ConfidenceLevel,
    compute_confidence,
    compute_sample_maturity_confidence,
)
from custom_components.smartshading.engines.outcome_aggregation import SimilarityResult


def _result(**overrides) -> SimilarityResult:
    base = dict(
        similar_count=25,
        expected_score=0.5,
        outcome_variance=0.1,
        agreement_rate=0.9,
        override_rate=0.1,
        avg_similarity=0.8,
    )
    base.update(overrides)
    return SimilarityResult(**base)


class TestComputeSampleMaturityConfidence:
    def test_ramps_linearly_to_target(self) -> None:
        assert compute_sample_maturity_confidence(0, count_target=50) == 0.0
        assert compute_sample_maturity_confidence(25, count_target=50) == pytest.approx(0.5)
        assert compute_sample_maturity_confidence(50, count_target=50) == 1.0

    def test_saturates_beyond_target(self) -> None:
        assert compute_sample_maturity_confidence(500, count_target=50) == 1.0

    def test_negative_count_clamped_to_zero(self) -> None:
        assert compute_sample_maturity_confidence(-5, count_target=50) == 0.0

    def test_day_factor_gates_result(self) -> None:
        # Full count but zero distinct days → zero confidence.
        assert compute_sample_maturity_confidence(
            8, count_target=8.0, distinct_days=0, day_target=3.0,
        ) == 0.0

    def test_matches_previous_shadow_formula_exactly(self) -> None:
        # Historical ad-hoc formula: min(1, neg_count/8) * min(1, days/3).
        for neg_count in range(0, 12):
            for days in range(0, 6):
                expected = min(1.0, neg_count / 8.0) * min(1.0, days / 3.0)
                actual = compute_sample_maturity_confidence(
                    neg_count, count_target=8.0, distinct_days=days, day_target=3.0,
                )
                assert actual == pytest.approx(expected)

    def test_matches_previous_global_confidence_formula_exactly(self) -> None:
        # Historical formula: min(1.0, total_resolved_outcomes / 50).
        for count in (0, 1, 10, 25, 49, 50, 51, 200):
            expected = min(1.0, count / 50)
            actual = compute_sample_maturity_confidence(count, count_target=50)
            assert actual == pytest.approx(expected)

    def test_omitting_day_target_ignores_days(self) -> None:
        assert compute_sample_maturity_confidence(
            50, count_target=50, distinct_days=0,
        ) == 1.0


class TestComputeConfidence:
    def test_full_signal_yields_high_confidence(self) -> None:
        result = compute_confidence(ConfidenceInput(
            result=_result(), total_resolved_outcomes=60,
        ))
        assert result.score > 0.5
        assert result.level in (ConfidenceLevel.HIGH, ConfidenceLevel.VERY_HIGH)

    def test_zero_similar_count_yields_zero_situational_confidence(self) -> None:
        result = compute_confidence(ConfidenceInput(
            result=_result(similar_count=0), total_resolved_outcomes=60,
        ))
        assert result.situational_confidence == 0.0
        assert result.score == 0.0

    def test_low_global_data_dampens_score_even_with_strong_situational_signal(self) -> None:
        result = compute_confidence(ConfidenceInput(
            result=_result(), total_resolved_outcomes=1,
        ))
        assert result.global_confidence < 0.05
        assert result.score < 0.05

    def test_all_none_factors_yield_zero_core(self) -> None:
        result = compute_confidence(ConfidenceInput(
            result=_result(
                agreement_rate=None, outcome_variance=None,
                override_rate=None, avg_similarity=None,
            ),
            total_resolved_outcomes=60,
        ))
        assert result.score == 0.0
        assert result.agreement_factor is None
        assert result.variance_factor is None

    def test_partial_none_factors_renormalize_weights(self) -> None:
        # Only override_factor and similarity_factor present.
        result = compute_confidence(ConfidenceInput(
            result=_result(agreement_rate=None, outcome_variance=None,
                            override_rate=0.0, avg_similarity=1.0),
            total_resolved_outcomes=60,
        ))
        assert result.agreement_factor is None
        assert result.variance_factor is None
        assert result.override_factor == 1.0
        assert result.similarity_factor == 1.0
        # core should equal 1.0 since both present factors are maximal.
        # situational = sample_factor * core = 1.0 * 1.0
        assert result.situational_confidence == pytest.approx(1.0)

    def test_score_never_exceeds_unit_interval(self) -> None:
        result = compute_confidence(ConfidenceInput(
            result=_result(
                similar_count=1000, agreement_rate=1.0, outcome_variance=0.0,
                override_rate=0.0, avg_similarity=1.0,
            ),
            total_resolved_outcomes=100000,
        ))
        assert 0.0 <= result.score <= 1.0

    @pytest.mark.parametrize("score,level", [
        (0.0, ConfidenceLevel.VERY_LOW),
        (0.19, ConfidenceLevel.VERY_LOW),
        (0.20, ConfidenceLevel.LOW),
        (0.39, ConfidenceLevel.LOW),
        (0.40, ConfidenceLevel.MEDIUM),
        (0.59, ConfidenceLevel.MEDIUM),
        (0.60, ConfidenceLevel.HIGH),
        (0.79, ConfidenceLevel.HIGH),
        (0.80, ConfidenceLevel.VERY_HIGH),
        (1.00, ConfidenceLevel.VERY_HIGH),
    ])
    def test_classification_boundaries(self, score: float, level: ConfidenceLevel) -> None:
        from custom_components.smartshading.engines.confidence_engine import _classify
        assert _classify(score) is level

    def test_pure_function_deterministic(self) -> None:
        inp = ConfidenceInput(result=_result(), total_resolved_outcomes=60)
        first = compute_confidence(inp)
        second = compute_confidence(inp)
        assert first == second
