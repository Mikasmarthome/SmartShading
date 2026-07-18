"""Tests for ManualOverridePolicy (engines/manual_override_policy.py) — T7.

Pure decision-matrix tests, no HA/coordinator dependency.

Coverage (T7 review points 7-16):
  MOP-01  No active override → any category unchanged.
  MOP-02  Legacy defaults (allow_comfort=False, allow_protection=False)
          block Comfort.
  MOP-03  Legacy defaults block Protection.
  MOP-04  Safety always allowed regardless of flags.
  MOP-05  Comfort allowed individually when allow_comfort=True.
  MOP-06  Protection allowed individually when allow_protection=True.
  MOP-07  Both allowed together.
  MOP-08  Lifecycle always blocked while override active — no allow-switch
          exists for it, regardless of flag combination.
  MOP-09  An allowed candidate is returned exactly as produced by its own
          evaluator (position/shading_state/decided_by unchanged) — the
          policy does not mutate it.
  MOP-10  A blocked candidate produces the same shape as the pre-T7
          ManualOverrideEvaluator result (shading_state=MANUAL_OVERRIDE,
          decided_by="ManualOverrideEvaluator", position = override's own
          position) — legacy hold semantics preserved byte-for-byte.
  MOP-11  Hold candidates pass through unchanged regardless of override
          state or flags (nothing to gate).
  MOP-12  The policy does not mutate active_override (frozen dataclass,
          but explicitly proven no replacement/clear happens).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.smartshading.engines.manual_override_policy import (
    evaluate_manual_override_policy,
)
from custom_components.smartshading.models.manual_override import ManualOverride
from custom_components.smartshading.models.window_decision import WindowDecision
from custom_components.smartshading.state_machine.states import DecisionCategory, ShadingState


def _override(position: int = 20) -> ManualOverride:
    return ManualOverride(
        window_id="w1",
        override_position=position,
        started_at=datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc),
        source="position_delta",
        overridden_state=ShadingState.OPEN,
        overridden_position=0,
    )


def _candidate(category: DecisionCategory, position: int = 70, decided_by: str = "SomeEvaluator") -> WindowDecision:
    return WindowDecision(
        window_id="w1",
        shading_state=ShadingState.NORMAL_SHADE,
        target_position=position,
        decided_by=decided_by,
        category=category,
    )


class TestNoActiveOverride:
    @pytest.mark.parametrize("category", list(DecisionCategory))
    def test_every_category_unchanged_when_no_override(self, category: DecisionCategory) -> None:
        candidate = _candidate(category)
        result = evaluate_manual_override_policy(
            active_override=None, candidate=candidate,
            allow_comfort=False, allow_protection=False,
        )
        assert result is candidate


class TestLegacyDefaults:
    def test_comfort_blocked_by_default(self) -> None:
        candidate = _candidate(DecisionCategory.COMFORT)
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=False,
        )
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE
        assert result.target_position == 20

    def test_protection_blocked_by_default(self) -> None:
        candidate = _candidate(DecisionCategory.PROTECTION)
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=False,
        )
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE
        assert result.target_position == 20


class TestSafetyAlwaysAllowed:
    def test_safety_allowed_with_all_flags_false(self) -> None:
        candidate = _candidate(DecisionCategory.SAFETY, decided_by="StormEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=False,
        )
        assert result is candidate


class TestIndividualAllowFlags:
    def test_comfort_allowed_individually(self) -> None:
        candidate = _candidate(DecisionCategory.COMFORT, decided_by="SolarEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=True, allow_protection=False,
        )
        assert result is candidate

    def test_protection_allowed_individually(self) -> None:
        candidate = _candidate(DecisionCategory.PROTECTION, decided_by="HeatEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=True,
        )
        assert result is candidate

    def test_comfort_still_blocked_when_only_protection_allowed(self) -> None:
        candidate = _candidate(DecisionCategory.COMFORT, decided_by="SolarEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=True,
        )
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE

    def test_protection_still_blocked_when_only_comfort_allowed(self) -> None:
        candidate = _candidate(DecisionCategory.PROTECTION, decided_by="HeatEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=True, allow_protection=False,
        )
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE

    def test_both_allowed_together(self) -> None:
        comfort = _candidate(DecisionCategory.COMFORT, decided_by="SolarEvaluator")
        protection = _candidate(DecisionCategory.PROTECTION, decided_by="HeatEvaluator")
        result_comfort = evaluate_manual_override_policy(
            active_override=_override(20), candidate=comfort,
            allow_comfort=True, allow_protection=True,
        )
        result_protection = evaluate_manual_override_policy(
            active_override=_override(20), candidate=protection,
            allow_comfort=True, allow_protection=True,
        )
        assert result_comfort is comfort
        assert result_protection is protection


class TestLifecycleNeverGetsAllowSwitch:
    @pytest.mark.parametrize("allow_comfort", [False, True])
    @pytest.mark.parametrize("allow_protection", [False, True])
    def test_lifecycle_always_blocked_while_override_active(
        self, allow_comfort: bool, allow_protection: bool
    ) -> None:
        candidate = _candidate(DecisionCategory.LIFECYCLE, decided_by="NightEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=allow_comfort, allow_protection=allow_protection,
        )
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE
        assert result.decided_by == "ManualOverrideEvaluator"


class TestAllowedCandidateShapePreserved:
    def test_allowed_protection_returned_exactly_as_produced(self) -> None:
        candidate = WindowDecision(
            window_id="w7", shading_state=ShadingState.ABSENCE_CLOSED,
            target_position=70, decided_by="AbsenceEvaluator",
            category=DecisionCategory.PROTECTION,
        )
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=True,
        )
        assert result is candidate
        assert result.shading_state is ShadingState.ABSENCE_CLOSED
        assert result.target_position == 70
        assert result.decided_by == "AbsenceEvaluator"


class TestBlockedCandidateMatchesLegacyShape:
    def test_blocked_shape_matches_pre_t7_manual_override_evaluator(self) -> None:
        ov = _override(35)
        candidate = _candidate(DecisionCategory.COMFORT, decided_by="SolarEvaluator")
        result = evaluate_manual_override_policy(
            active_override=ov, candidate=candidate,
            allow_comfort=False, allow_protection=False,
        )
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE
        assert result.target_position == 35
        assert result.decided_by == "ManualOverrideEvaluator"
        assert result.window_id == candidate.window_id
        assert result.target_tilt is None


class TestHoldCandidateHandling:
    def test_hold_candidate_unchanged_when_no_override_active(self) -> None:
        """A HOLD candidate (e.g. PresenceUncertain:hold) is unaffected by
        the policy when there is no override to gate against."""
        candidate = WindowDecision(
            window_id="w1", shading_state=ShadingState.OPEN,
            target_position=None, decided_by="PresenceUncertain:hold",
            category=DecisionCategory.HOLD,
        )
        result = evaluate_manual_override_policy(
            active_override=None, candidate=candidate,
            allow_comfort=False, allow_protection=False,
        )
        assert result is candidate

    def test_hold_candidate_blocked_like_everything_else_when_override_active(self) -> None:
        """Legacy-parity requirement: pre-T7, an active override ALWAYS won
        via Tier 2's early exit, regardless of what Tier 3-5 would have
        decided — including an inert PresenceUncertain hold. A HOLD
        candidate must therefore be converted to the MANUAL_OVERRIDE hold
        exactly like a blocked Comfort/Protection/Lifecycle candidate, not
        passed through unchanged — otherwise this specific edge case would
        leak a non-override decision through an active override."""
        candidate = WindowDecision(
            window_id="w1", shading_state=ShadingState.OPEN,
            target_position=None, decided_by="PresenceUncertain:hold",
            category=DecisionCategory.HOLD,
        )
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=True, allow_protection=True,
        )
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE
        assert result.target_position == 20


class TestNoMutationOfActiveOverride:
    def test_override_object_identity_unaffected_by_policy_call(self) -> None:
        ov = _override(20)
        candidate = _candidate(DecisionCategory.COMFORT)
        evaluate_manual_override_policy(
            active_override=ov, candidate=candidate,
            allow_comfort=False, allow_protection=False,
        )
        # ManualOverride is frozen — this call could not have mutated it even
        # if it tried; assert its fields still read as constructed.
        assert ov.override_position == 20
        assert ov.expires_at == datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
