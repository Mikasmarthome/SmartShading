"""Tests for evaluate_manual_override_policy's candidate-triggered release
behavior (v1.2.0-beta.1, T10) — engines/manual_override_policy.py.

Complements test_manual_override_policy.py (T7's allow_comfort/allow_protection
matrix, unaffected by this ticket) and test_override_release.py (pure
resolve_candidate_release() truth table). This file proves the two are wired
together correctly through evaluate_manual_override_policy(), including the
release_override flag it now sets on the returned WindowDecision.

Coverage:
  RS-01  FIRST_COMFORT + COMFORT candidate -> passes through with
         release_override=True, even when allow_comfort=False.
  RS-02  FIRST_PROTECTION + PROTECTION candidate -> passes through with
         release_override=True, even when allow_protection=False.
  RS-03  FIRST_ANY_DECISION releases on both COMFORT and PROTECTION.
  RS-04  FIRST_COMFORT does NOT release on a PROTECTION candidate (still
         blocked, unless allow_protection independently allows it).
  RS-05  FIRST_PROTECTION does NOT release on a COMFORT candidate.
  RS-06  LIFECYCLE / DURATION / FIXED_TIME / MANUAL strategies never set
         release_override, regardless of category (lifecycle-break and
         explicit-clear are handled entirely outside this function).
  RS-07  A blocked candidate (no release, allow flag false) never carries
         release_override=True.
  RS-08  An allow-flag-permitted passthrough (independent of strategy)
         carries release_override=False, not True — passthrough and release
         are orthogonal; only a strategy-qualifying candidate ends the
         override.
  RS-09  SAFETY candidates are unaffected by release_strategy (still the
         always-allowed early return, release_override untouched/default).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.smartshading.engines.manual_override_policy import (
    evaluate_manual_override_policy,
)
from custom_components.smartshading.models.manual_override import (
    ManualOverride,
    OverrideReleaseStrategy,
)
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


class TestFirstComfortReleasesOnComfort:
    def test_releases_even_when_allow_comfort_false(self) -> None:
        candidate = _candidate(DecisionCategory.COMFORT, decided_by="SolarEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=False,
            release_strategy=OverrideReleaseStrategy.FIRST_COMFORT,
        )
        assert result is not None
        assert result.shading_state is not ShadingState.MANUAL_OVERRIDE
        assert result.release_override is True

    def test_does_not_release_on_protection_candidate(self) -> None:
        candidate = _candidate(DecisionCategory.PROTECTION, decided_by="HeatEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=False,
            release_strategy=OverrideReleaseStrategy.FIRST_COMFORT,
        )
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE
        assert result.release_override is False


class TestFirstProtectionReleasesOnProtection:
    def test_releases_even_when_allow_protection_false(self) -> None:
        candidate = _candidate(DecisionCategory.PROTECTION, decided_by="HeatEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=False,
            release_strategy=OverrideReleaseStrategy.FIRST_PROTECTION,
        )
        assert result.release_override is True
        assert result.shading_state is not ShadingState.MANUAL_OVERRIDE

    def test_does_not_release_on_comfort_candidate(self) -> None:
        candidate = _candidate(DecisionCategory.COMFORT, decided_by="SolarEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=False,
            release_strategy=OverrideReleaseStrategy.FIRST_PROTECTION,
        )
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE


class TestFirstAnyDecisionReleasesOnBoth:
    @pytest.mark.parametrize("category", [DecisionCategory.COMFORT, DecisionCategory.PROTECTION])
    def test_releases_on_comfort_and_protection(self, category: DecisionCategory) -> None:
        candidate = _candidate(category, decided_by="AnyEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=False,
            release_strategy=OverrideReleaseStrategy.FIRST_ANY_DECISION,
        )
        assert result.release_override is True
        assert result.shading_state is not ShadingState.MANUAL_OVERRIDE

    def test_still_blocks_lifecycle(self) -> None:
        candidate = _candidate(DecisionCategory.LIFECYCLE, decided_by="NightEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=False,
            release_strategy=OverrideReleaseStrategy.FIRST_ANY_DECISION,
        )
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE


class TestNonCandidateStrategiesNeverRelease:
    @pytest.mark.parametrize(
        "strategy",
        [
            OverrideReleaseStrategy.DURATION,
            OverrideReleaseStrategy.FIXED_TIME,
            OverrideReleaseStrategy.LIFECYCLE,
            OverrideReleaseStrategy.MANUAL,
        ],
    )
    @pytest.mark.parametrize("category", [DecisionCategory.COMFORT, DecisionCategory.PROTECTION])
    def test_blocked_candidate_never_carries_release_override(
        self, strategy: OverrideReleaseStrategy, category: DecisionCategory
    ) -> None:
        candidate = _candidate(category, decided_by="SomeEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=False,
            release_strategy=strategy,
        )
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE
        assert result.release_override is False


class TestAllowFlagPassthroughIsOrthogonalToRelease:
    def test_allow_comfort_passthrough_does_not_set_release_override(self) -> None:
        candidate = _candidate(DecisionCategory.COMFORT, decided_by="SolarEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=True, allow_protection=False,
            release_strategy=OverrideReleaseStrategy.LIFECYCLE,
        )
        assert result is candidate
        assert result.release_override is False

    def test_allow_protection_passthrough_does_not_set_release_override(self) -> None:
        candidate = _candidate(DecisionCategory.PROTECTION, decided_by="HeatEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=True,
            release_strategy=OverrideReleaseStrategy.MANUAL,
        )
        assert result is candidate
        assert result.release_override is False


class TestSafetyUnaffectedByReleaseStrategy:
    @pytest.mark.parametrize("strategy", list(OverrideReleaseStrategy))
    def test_safety_always_passes_through_regardless_of_strategy(
        self, strategy: OverrideReleaseStrategy
    ) -> None:
        candidate = _candidate(DecisionCategory.SAFETY, decided_by="StormEvaluator")
        result = evaluate_manual_override_policy(
            active_override=_override(20), candidate=candidate,
            allow_comfort=False, allow_protection=False,
            release_strategy=strategy,
        )
        assert result is candidate


class TestNoActiveOverrideStrategyIrrelevant:
    @pytest.mark.parametrize("strategy", list(OverrideReleaseStrategy))
    def test_no_override_candidate_unchanged_regardless_of_strategy(
        self, strategy: OverrideReleaseStrategy
    ) -> None:
        candidate = _candidate(DecisionCategory.COMFORT)
        result = evaluate_manual_override_policy(
            active_override=None, candidate=candidate,
            allow_comfort=False, allow_protection=False,
            release_strategy=strategy,
        )
        assert result is candidate
