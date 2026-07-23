"""Tests for engines/override_release.py — the central release-strategy
resolver (v1.2.0-beta.1, T10).
"""
from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from custom_components.smartshading.engines.override_release import (
    NO_SAFETY_TIMEOUT,
    compute_expiry,
    extends_on_renewal,
    resolve_candidate_release,
    uses_post_expiry_baseline,
)
from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy
from custom_components.smartshading.state_machine.states import DecisionCategory

_NOW = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)


class TestComputeExpiryDuration:
    def test_duration_strategy_uses_now_plus_minutes(self) -> None:
        expiry = compute_expiry(
            strategy=OverrideReleaseStrategy.DURATION, now=_NOW, now_local=None,
            duration_min=90, fixed_until=None, safety_timeout_enabled=True,
        )
        assert expiry == _NOW.replace(minute=0) + __import__("datetime").timedelta(hours=1, minutes=30)

    def test_duration_ignores_safety_timeout_flag(self) -> None:
        expiry = compute_expiry(
            strategy=OverrideReleaseStrategy.DURATION, now=_NOW, now_local=None,
            duration_min=60, fixed_until=None, safety_timeout_enabled=False,
        )
        assert (expiry - _NOW).total_seconds() == 3600


class TestComputeExpiryFixedTime:
    def test_uses_fixed_time_when_now_local_supplied(self) -> None:
        now_local = _NOW.astimezone(timezone.utc)  # local == utc for this test
        expiry = compute_expiry(
            strategy=OverrideReleaseStrategy.FIXED_TIME, now=_NOW, now_local=now_local,
            duration_min=120, fixed_until=time(8, 0, 0), safety_timeout_enabled=True,
        )
        # 08:00 has already passed at 10:00, so it rolls to the next day.
        assert expiry.hour == 8
        assert expiry.date() == _NOW.date() + __import__("datetime").timedelta(days=1)

    def test_falls_back_to_duration_when_fixed_until_missing(self) -> None:
        expiry = compute_expiry(
            strategy=OverrideReleaseStrategy.FIXED_TIME, now=_NOW, now_local=_NOW,
            duration_min=45, fixed_until=None, safety_timeout_enabled=True,
        )
        assert (expiry - _NOW).total_seconds() == 45 * 60

    def test_falls_back_to_duration_when_now_local_missing(self) -> None:
        expiry = compute_expiry(
            strategy=OverrideReleaseStrategy.FIXED_TIME, now=_NOW, now_local=None,
            duration_min=45, fixed_until=time(8, 0, 0), safety_timeout_enabled=True,
        )
        assert (expiry - _NOW).total_seconds() == 45 * 60


class TestComputeExpirySafetyTimeoutStrategies:
    @pytest.mark.parametrize("strategy", [
        OverrideReleaseStrategy.LIFECYCLE,
        OverrideReleaseStrategy.FIRST_COMFORT,
        OverrideReleaseStrategy.FIRST_PROTECTION,
        OverrideReleaseStrategy.FIRST_ANY_DECISION,
        OverrideReleaseStrategy.MANUAL,
    ])
    def test_safety_timeout_enabled_uses_duration(self, strategy) -> None:
        expiry = compute_expiry(
            strategy=strategy, now=_NOW, now_local=None,
            duration_min=720, fixed_until=None, safety_timeout_enabled=True,
        )
        assert (expiry - _NOW).total_seconds() == 720 * 60

    @pytest.mark.parametrize("strategy", [
        OverrideReleaseStrategy.LIFECYCLE,
        OverrideReleaseStrategy.FIRST_COMFORT,
        OverrideReleaseStrategy.FIRST_PROTECTION,
        OverrideReleaseStrategy.FIRST_ANY_DECISION,
        OverrideReleaseStrategy.MANUAL,
    ])
    def test_safety_timeout_disabled_uses_sentinel(self, strategy) -> None:
        expiry = compute_expiry(
            strategy=strategy, now=_NOW, now_local=None,
            duration_min=720, fixed_until=None, safety_timeout_enabled=False,
        )
        assert expiry == NO_SAFETY_TIMEOUT
        assert expiry > _NOW  # sentinel is always "not yet expired"


class TestExtendsOnRenewal:
    def test_only_duration_extends(self) -> None:
        assert extends_on_renewal(OverrideReleaseStrategy.DURATION) is True

    @pytest.mark.parametrize("strategy", [
        OverrideReleaseStrategy.FIXED_TIME,
        OverrideReleaseStrategy.LIFECYCLE,
        OverrideReleaseStrategy.FIRST_COMFORT,
        OverrideReleaseStrategy.FIRST_PROTECTION,
        OverrideReleaseStrategy.FIRST_ANY_DECISION,
        OverrideReleaseStrategy.MANUAL,
    ])
    def test_others_do_not_extend(self, strategy) -> None:
        assert extends_on_renewal(strategy) is False


class TestUsesPostExpiryBaseline:
    def test_duration_does_not_use_baseline(self) -> None:
        assert uses_post_expiry_baseline(OverrideReleaseStrategy.DURATION) is False

    @pytest.mark.parametrize("strategy", [
        OverrideReleaseStrategy.FIXED_TIME,
        OverrideReleaseStrategy.LIFECYCLE,
        OverrideReleaseStrategy.FIRST_COMFORT,
        OverrideReleaseStrategy.FIRST_PROTECTION,
        OverrideReleaseStrategy.FIRST_ANY_DECISION,
        OverrideReleaseStrategy.MANUAL,
    ])
    def test_others_use_baseline(self, strategy) -> None:
        assert uses_post_expiry_baseline(strategy) is True


class TestResolveCandidateRelease:
    def test_first_comfort_releases_on_comfort_only(self) -> None:
        assert resolve_candidate_release(
            strategy=OverrideReleaseStrategy.FIRST_COMFORT, category=DecisionCategory.COMFORT
        ) is True
        assert resolve_candidate_release(
            strategy=OverrideReleaseStrategy.FIRST_COMFORT, category=DecisionCategory.PROTECTION
        ) is False

    def test_first_protection_releases_on_protection_only(self) -> None:
        assert resolve_candidate_release(
            strategy=OverrideReleaseStrategy.FIRST_PROTECTION, category=DecisionCategory.PROTECTION
        ) is True
        assert resolve_candidate_release(
            strategy=OverrideReleaseStrategy.FIRST_PROTECTION, category=DecisionCategory.COMFORT
        ) is False

    def test_first_any_decision_releases_on_either(self) -> None:
        assert resolve_candidate_release(
            strategy=OverrideReleaseStrategy.FIRST_ANY_DECISION, category=DecisionCategory.COMFORT
        ) is True
        assert resolve_candidate_release(
            strategy=OverrideReleaseStrategy.FIRST_ANY_DECISION, category=DecisionCategory.PROTECTION
        ) is True

    @pytest.mark.parametrize("category", [
        DecisionCategory.SAFETY, DecisionCategory.LIFECYCLE, DecisionCategory.HOLD,
    ])
    def test_first_any_decision_never_releases_on_safety_lifecycle_hold(self, category) -> None:
        assert resolve_candidate_release(
            strategy=OverrideReleaseStrategy.FIRST_ANY_DECISION, category=category
        ) is False

    @pytest.mark.parametrize("strategy", [
        OverrideReleaseStrategy.DURATION,
        OverrideReleaseStrategy.FIXED_TIME,
        OverrideReleaseStrategy.LIFECYCLE,
        OverrideReleaseStrategy.MANUAL,
    ])
    @pytest.mark.parametrize("category", [DecisionCategory.COMFORT, DecisionCategory.PROTECTION])
    def test_non_candidate_strategies_never_release_via_this_function(self, strategy, category) -> None:
        """DURATION/FIXED_TIME/LIFECYCLE/MANUAL are governed by their own
        mechanisms (expires_at / lifecycle_guard / explicit user action) —
        this function must never fire a release for them, regardless of
        which candidate category is checked."""
        assert resolve_candidate_release(strategy=strategy, category=category) is False
