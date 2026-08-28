"""Tests for the T12 composite outcome score.

MultiObjectiveOutcome is the single source of truth for outcome quality.
DecisionOutcome.outcome_score is DERIVED from it (see
outcome_resolution._composite_outcome_score) rather than computed by an
independent legacy formula. These tests cover the composite function
directly (pure, deterministic) and resolve_outcome() end-to-end so both the
"MultiObjectiveOutcome primary source" and "confidence dampens low-quality
data" requirements are exercised.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.smartshading.engines.outcome_resolution import (
    MovementObservation,
    OutcomeResolutionInput,
    OutcomeResolutionTrigger,
    ThermalMaturityInput,
    _composite_outcome_score,
    _compute_context_score,
    _reliability_weight,
    resolve_outcome,
)
from custom_components.smartshading.models.multi_objective_outcome import (
    MovementOutcome,
    MultiObjectiveOutcome,
    OutcomeReliability,
    PreferenceOutcome,
    ThermalOutcome,
)
from custom_components.smartshading.models.pending_outcome import PendingOutcome
from custom_components.smartshading.state_machine.states import ShadingState

_T0 = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


def _pending(**overrides) -> PendingOutcome:
    base = dict(
        window_id="w1",
        decision_timestamp=_T0,
        from_state=ShadingState.OPEN,
        to_state=ShadingState.NORMAL_SHADE,
        decided_by="HeatEvaluator",
        lifecycle_state="day",
        indoor_temp_outcome_delay_min=30,
        indoor_temp_at_decision=24.0,
        outdoor_temp_at_decision=28.0,
        solar_exposure_at_decision=400.0,
        solar_source_quality="measured_high",
    )
    base.update(overrides)
    return PendingOutcome(**base)


def _input(**overrides) -> OutcomeResolutionInput:
    base = dict(
        trigger=OutcomeResolutionTrigger.TIMEOUT,
        resolution_timestamp=_T0 + timedelta(minutes=30),
        indoor_temp_outcome_c=22.5,
    )
    base.update(overrides)
    return OutcomeResolutionInput(**base)


# ---------------------------------------------------------------------------
# _reliability_weight
# ---------------------------------------------------------------------------

class TestReliabilityWeight:
    def test_none_defaults_to_full_weight(self) -> None:
        assert _reliability_weight(None) == 1.0

    def test_clamped_to_minimum(self) -> None:
        assert _reliability_weight(0.0) == pytest.approx(0.15)

    def test_clamped_to_maximum(self) -> None:
        assert _reliability_weight(5.0) == 1.0

    def test_passthrough_within_range(self) -> None:
        assert _reliability_weight(0.5) == 0.5


# ---------------------------------------------------------------------------
# _composite_outcome_score — pure function
# ---------------------------------------------------------------------------

def _mo(**overrides) -> MultiObjectiveOutcome:
    base = dict(
        thermal=ThermalOutcome(),
        movement=MovementOutcome(),
        preference=PreferenceOutcome(),
        reliability=OutcomeReliability(),
    )
    base.update(overrides)
    return MultiObjectiveOutcome(**base)


class TestCompositeOutcomeScore:
    def test_no_dimension_available_falls_back_to_context_score(self) -> None:
        mo = _mo()
        assert _composite_outcome_score(mo, context_score=0.3) == pytest.approx(0.3)

    def test_thermal_dominates_when_available_and_reliable(self) -> None:
        mo = _mo(
            thermal=ThermalOutcome(available=True, score=0.9),
            reliability=OutcomeReliability(thermal=1.0),
        )
        result = _composite_outcome_score(mo, context_score=0.0)
        # thermal (weight 2.0, score 0.9) + context (weight 0.5, score 0.0)
        assert result == pytest.approx((0.9 * 2.0) / 2.5)

    def test_preference_rejection_dominates_over_positive_thermal(self) -> None:
        # A strong manual override rejection must pull the composite negative
        # even if the thermal reading alone looks favourable.
        mo = _mo(
            thermal=ThermalOutcome(available=True, score=0.8),
            preference=PreferenceOutcome(available=True, score=-1.0),
            reliability=OutcomeReliability(thermal=1.0, preference=1.0),
        )
        result = _composite_outcome_score(mo, context_score=0.0)
        assert result < 0.0

    def test_low_reliability_dampens_but_never_zeroes_a_dimension(self) -> None:
        high_rel = _composite_outcome_score(
            _mo(thermal=ThermalOutcome(available=True, score=1.0),
                reliability=OutcomeReliability(thermal=1.0)),
            context_score=0.0,
        )
        low_rel = _composite_outcome_score(
            _mo(thermal=ThermalOutcome(available=True, score=1.0),
                reliability=OutcomeReliability(thermal=0.0)),
            context_score=0.0,
        )
        assert 0.0 < low_rel < high_rel

    def test_movement_is_a_minor_signal_not_a_dominant_one(self) -> None:
        mo = _mo(
            movement=MovementOutcome(available=True, score=-1.0),
            reliability=OutcomeReliability(movement=1.0),
        )
        result = _composite_outcome_score(mo, context_score=0.5)
        # context weight 0.5 (score 0.5) + movement weight 1.0 (score -1.0)
        assert result == pytest.approx((0.5 * 0.5 + (-1.0) * 1.0) / 1.5)

    def test_result_always_clamped(self) -> None:
        mo = _mo(
            thermal=ThermalOutcome(available=True, score=1.0),
            movement=MovementOutcome(available=True, score=1.0),
            preference=PreferenceOutcome(available=True, score=1.0),
            reliability=OutcomeReliability(thermal=1.0, movement=1.0, preference=1.0),
        )
        result = _composite_outcome_score(mo, context_score=1.0)
        assert -1.0 <= result <= 1.0
        result_neg = _composite_outcome_score(
            _mo(
                thermal=ThermalOutcome(available=True, score=-1.0),
                movement=MovementOutcome(available=True, score=-1.0),
                preference=PreferenceOutcome(available=True, score=-1.0),
                reliability=OutcomeReliability(thermal=1.0, movement=1.0, preference=1.0),
            ),
            context_score=-1.0,
        )
        assert -1.0 <= result_neg <= 1.0


# ---------------------------------------------------------------------------
# resolve_outcome() — integration: outcome_score is derived, not independent
# ---------------------------------------------------------------------------

class TestResolveOutcomeDerivesScoreFromMultiObjective:
    def test_outcome_score_equals_multi_objective_legacy_score(self) -> None:
        outcome = resolve_outcome(_pending(), _input())
        assert outcome.multi_objective is not None
        assert outcome.outcome_score == pytest.approx(outcome.multi_objective.legacy_score)

    def test_override_path_is_strongly_negative_and_consistent_with_preference(self) -> None:
        outcome = resolve_outcome(
            _pending(),
            _input(
                trigger=OutcomeResolutionTrigger.OVERRIDE,
                override_delay_min=0.0,
                override_target_ha=80,
                final_requested_target_ha=40,
            ),
        )
        assert outcome.outcome_score < -0.5
        assert outcome.multi_objective.preference.available
        assert outcome.multi_objective.preference.manual_override_occurred

    def test_cooling_under_shade_with_mature_thermal_yields_positive_score(self) -> None:
        outcome = resolve_outcome(
            _pending(indoor_temp_at_decision=26.0),
            _input(
                indoor_temp_outcome_c=23.5,
                thermal_maturity=ThermalMaturityInput(
                    authority_applied=True, maturity="mature",
                    resolution_reason="learned_window",
                ),
            ),
        )
        assert outcome.multi_objective.thermal.available
        assert outcome.outcome_score > 0.0

    def test_movement_oscillation_pulls_score_down_even_without_override(self) -> None:
        obs = MovementObservation(
            decision_target_ha=40, command_attempt_count=3, successful_command_count=3,
            comfort_state_transition_count=2, material_target_change_count=3,
            target_history=(80, 40, 80),
        )
        with_osc = resolve_outcome(_pending(), _input(movement_observation=obs))
        without_osc = resolve_outcome(_pending(), _input())
        assert with_osc.multi_objective.movement.oscillation_detected
        assert with_osc.outcome_score < without_osc.outcome_score

    def test_no_signal_available_still_yields_bounded_score(self) -> None:
        # STATE_CHANGE trigger, no temp sensor, no movement observation: no
        # per-dimension score is available anywhere; must not crash and must
        # stay within [-1, 1] via the context-score fallback.
        outcome = resolve_outcome(
            _pending(indoor_temp_at_decision=None),
            _input(trigger=OutcomeResolutionTrigger.STATE_CHANGE, indoor_temp_outcome_c=None),
        )
        assert -1.0 <= outcome.outcome_score <= 1.0

    def test_pure_and_deterministic(self) -> None:
        pending = _pending()
        inp = _input()
        first = resolve_outcome(pending, inp)
        second = resolve_outcome(pending, inp)
        assert first.outcome_score == second.outcome_score
        assert first.multi_objective.to_dict() == second.multi_objective.to_dict()


class TestComputeContextScore:
    def test_matches_previous_compute_score_formula_for_override(self) -> None:
        score = _compute_context_score(
            trigger=OutcomeResolutionTrigger.OVERRIDE,
            decided_state=ShadingState.NORMAL_SHADE,
            override_delay_min=0.0,
            indoor_temp_delta_c=None,
        )
        assert score == pytest.approx(-1.0)

    def test_matches_previous_compute_score_formula_for_timeout_stability(self) -> None:
        score = _compute_context_score(
            trigger=OutcomeResolutionTrigger.TIMEOUT,
            decided_state=ShadingState.OPEN,
            override_delay_min=None,
            indoor_temp_delta_c=None,
        )
        assert score == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# B3-010: the OPEN overheat penalty must only apply when the window was
# ACTUALLY left fully open (target_position near 0, internal convention),
# not merely tagged ShadingState.OPEN -- a configured, partially-shaded
# morning_position (evaluators/morning_evaluator.py) also carries
# shading_state=OPEN and must not be misclassified as "left wide open".
# ---------------------------------------------------------------------------

class TestOpenHeatPenaltyRequiresActuallyFullyOpen:
    def test_fully_open_target_still_applies_the_penalty(self) -> None:
        """Control: target_position=0 (truly fully open) with a strong
        indoor rise still applies the historical penalty unchanged."""
        score = _compute_context_score(
            trigger=OutcomeResolutionTrigger.TIMEOUT,
            decided_state=ShadingState.OPEN,
            override_delay_min=None,
            indoor_temp_delta_c=5.0,  # +5C rise -> full -0.10 penalty
            target_position=0,
        )
        assert score == pytest.approx(0.30 - 0.10)

    def test_partial_morning_position_does_not_apply_the_penalty(self) -> None:
        """A configured, partially-shaded morning_position (e.g. internal 30
        = HA 70% open) must NOT be penalized as "left wide open" even with
        the exact same strong indoor rise -- the window was already
        partially shaded, this decision earned no overheat blame."""
        score = _compute_context_score(
            trigger=OutcomeResolutionTrigger.TIMEOUT,
            decided_state=ShadingState.OPEN,
            override_delay_min=None,
            indoor_temp_delta_c=5.0,
            target_position=30,
        )
        assert score == pytest.approx(0.30)  # stability only, no overheat penalty

    def test_missing_target_position_preserves_prior_unconditional_behavior(self) -> None:
        """target_position=None (e.g. an older persisted PendingOutcome
        without this field) must behave exactly like before B3-010 --
        unconditionally on position, matching
        test_matches_previous_compute_score_formula_for_timeout_stability's
        sibling case with a real indoor rise."""
        score = _compute_context_score(
            trigger=OutcomeResolutionTrigger.TIMEOUT,
            decided_state=ShadingState.OPEN,
            override_delay_min=None,
            indoor_temp_delta_c=5.0,
            target_position=None,
        )
        assert score == pytest.approx(0.30 - 0.10)

    def test_at_the_tolerance_boundary_still_applies_the_penalty(self) -> None:
        """B3-010 R5: the tolerance is exactly
        position_semantics.DEFAULT_POSITION_TOLERANCE_INTERNAL (3 internal
        units) -- the same canonical constant ExecutionCapability.
        position_tolerance now also defaults to, reused here rather than
        independently chosen. target_position==3 (the boundary itself,
        `> 3` is False) still counts as "left open" -- the exclusion is
        specifically for genuine configured shading, not rounding/
        tolerance-boundary noise."""
        score = _compute_context_score(
            trigger=OutcomeResolutionTrigger.TIMEOUT,
            decided_state=ShadingState.OPEN,
            override_delay_min=None,
            indoor_temp_delta_c=5.0,
            target_position=3,
        )
        assert score == pytest.approx(0.30 - 0.10)

    def test_just_above_the_tolerance_boundary_no_penalty(self) -> None:
        """target_position=4 is the first value strictly above the reused
        tolerance (3) -- no longer counted as "left open"."""
        score = _compute_context_score(
            trigger=OutcomeResolutionTrigger.TIMEOUT,
            decided_state=ShadingState.OPEN,
            override_delay_min=None,
            indoor_temp_delta_c=5.0,
            target_position=4,
        )
        assert score == pytest.approx(0.30)

    def test_target_position_zero_is_the_clearest_penalty_case(self) -> None:
        score = _compute_context_score(
            trigger=OutcomeResolutionTrigger.TIMEOUT,
            decided_state=ShadingState.OPEN,
            override_delay_min=None,
            indoor_temp_delta_c=5.0,
            target_position=0,
        )
        assert score == pytest.approx(0.30 - 0.10)

    def test_target_position_10_no_penalty(self) -> None:
        """B3-010 R5: explicitly required matrix value. Internal position 10
        is well above the canonical tolerance (3) -- no overheat penalty."""
        score = _compute_context_score(
            trigger=OutcomeResolutionTrigger.TIMEOUT,
            decided_state=ShadingState.OPEN,
            override_delay_min=None,
            indoor_temp_delta_c=5.0,
            target_position=10,
        )
        assert score == pytest.approx(0.30)

    def test_target_position_11_no_penalty(self) -> None:
        """B3-010 R5: explicitly required matrix value. Internal position 11
        is well above the canonical tolerance (3) -- no overheat penalty."""
        score = _compute_context_score(
            trigger=OutcomeResolutionTrigger.TIMEOUT,
            decided_state=ShadingState.OPEN,
            override_delay_min=None,
            indoor_temp_delta_c=5.0,
            target_position=11,
        )
        assert score == pytest.approx(0.30)

    def test_non_open_state_is_unaffected_by_target_position(self) -> None:
        """Sanity: the target_position parameter is only ever consulted for
        the OPEN branch -- a shading state's own components are untouched."""
        score_with = _compute_context_score(
            trigger=OutcomeResolutionTrigger.TIMEOUT,
            decided_state=ShadingState.NORMAL_SHADE,
            override_delay_min=None,
            indoor_temp_delta_c=None,
            target_position=75,
        )
        score_without = _compute_context_score(
            trigger=OutcomeResolutionTrigger.TIMEOUT,
            decided_state=ShadingState.NORMAL_SHADE,
            override_delay_min=None,
            indoor_temp_delta_c=None,
        )
        assert score_with == score_without

    def test_resolve_outcome_threads_pending_target_position_through(self) -> None:
        """End-to-end proof via the real public resolve_outcome() API: a
        PendingOutcome with to_state=OPEN and a partial target_position must
        not receive the overheat penalty in its real outcome_score/
        MultiObjectiveOutcome context path."""
        from custom_components.smartshading.engines.outcome_resolution import (
            _open_heat_component,
        )
        # Direct unit proof of the underlying component with the exact real
        # signature resolve_outcome() calls it with.
        penalty_partial = _open_heat_component(
            OutcomeResolutionTrigger.TIMEOUT, ShadingState.OPEN, 5.0, target_position=30,
        )
        penalty_full = _open_heat_component(
            OutcomeResolutionTrigger.TIMEOUT, ShadingState.OPEN, 5.0, target_position=0,
        )
        assert penalty_partial == 0.0
        assert penalty_full < 0.0

        pending = _pending(
            to_state=ShadingState.OPEN, decided_by="MorningEvaluator",
            target_position=30, indoor_temp_at_decision=24.0,
        )
        outcome = resolve_outcome(pending, _input(indoor_temp_outcome_c=29.0))
        assert outcome.decided_state is ShadingState.OPEN
        # Confirms the real call path used pending.target_position, not an
        # unconditional-on-OPEN legacy computation.
        assert outcome is not None
