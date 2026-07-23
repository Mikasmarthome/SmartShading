"""OverrideDetector.tick() behavior per release_strategy (v1.2.0-beta.1, T10).

Complements test_override_detector.py (generic warmup/threshold/renewal/clear
mechanics, all strategy-agnostic) and test_override_release.py (pure
compute_expiry/extends_on_renewal/uses_post_expiry_baseline truth tables).
This file proves tick() actually wires those pure functions correctly for a
NEW override's expires_at and for RENEWAL semantics per strategy.

Coverage:
  DS-01  DURATION: new override expiry = now + duration_min. Renewal extends
         expires_at to (renewal-time + duration_min).
  DS-02  FIXED_TIME: new override expiry uses the configured local clock time,
         not duration_min, when now_local is supplied.
  DS-03  LIFECYCLE / FIRST_COMFORT / FIRST_PROTECTION / FIRST_ANY_DECISION /
         MANUAL with safety_timeout_enabled=True: new override expiry =
         now + duration_min (the defensive safety-net).
  DS-04  Same five strategies with safety_timeout_enabled=False: expiry is
         the far-future NO_SAFETY_TIMEOUT sentinel (never naturally expires).
  DS-05  Renewal for every non-DURATION strategy does NOT move expires_at —
         only the override_position/started_at update.
  DS-06  release_strategy is persisted onto the created/renewed ManualOverride
         (readable back via detector.get()).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.smartshading.engines.override_detector import OverrideDetector
from custom_components.smartshading.engines.override_release import NO_SAFETY_TIMEOUT
from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy
from custom_components.smartshading.state_machine.states import ShadingState

_NON_DURATION_UNBOUNDED_STRATEGIES = [
    OverrideReleaseStrategy.LIFECYCLE,
    OverrideReleaseStrategy.FIRST_COMFORT,
    OverrideReleaseStrategy.FIRST_PROTECTION,
    OverrideReleaseStrategy.FIRST_ANY_DECISION,
    OverrideReleaseStrategy.MANUAL,
]


def _detect(detector: OverrideDetector, window_id: str, position: int, target: int, now: datetime, **kw) -> None:
    detector.tick(
        window_id=window_id, observed_position=position, smartshading_target=target,
        prev_state=ShadingState.OPEN, tolerance=10, duration_min=kw.pop("duration_min", 60),
        now=now, **kw,
    )


class TestDurationStrategyExpiry:
    def test_new_override_expires_after_duration_min(self) -> None:
        d = OverrideDetector()
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        _detect(d, "w1", 0, 0, t0)  # warmup
        t1 = t0 + timedelta(minutes=1)
        _detect(d, "w1", 40, 0, t1, release_strategy=OverrideReleaseStrategy.DURATION, duration_min=90)
        ov = d.get("w1", t1)
        assert ov is not None
        assert ov.expires_at == t1 + timedelta(minutes=90)
        assert ov.release_strategy == "duration"

    def test_renewal_extends_expiry(self) -> None:
        d = OverrideDetector()
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        _detect(d, "w1", 0, 0, t0)
        t1 = t0 + timedelta(minutes=1)
        _detect(d, "w1", 40, 0, t1, release_strategy=OverrideReleaseStrategy.DURATION, duration_min=90)
        t2 = t1 + timedelta(minutes=5)
        _detect(d, "w1", 55, 0, t2, release_strategy=OverrideReleaseStrategy.DURATION, duration_min=90)
        ov = d.get("w1", t2)
        assert ov.expires_at == t2 + timedelta(minutes=90)
        assert ov.started_at == t2


class TestFixedTimeStrategyExpiry:
    def test_new_override_uses_configured_local_clock_time(self) -> None:
        d = OverrideDetector()
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        _detect(d, "w1", 0, 0, t0)
        t1 = t0 + timedelta(minutes=1)
        from datetime import time as dtime
        _detect(
            d, "w1", 40, 0, t1,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, duration_min=90,
            fixed_until=dtime(8, 0), now_local=t1,
        )
        ov = d.get("w1", t1)
        assert ov is not None
        assert ov.expires_at.hour == 8
        assert ov.expires_at.minute == 0
        # NOT the duration_min fallback.
        assert ov.expires_at != t1 + timedelta(minutes=90)


class TestUnboundedStrategiesSafetyTimeoutEnabled:
    @pytest.mark.parametrize("strategy", _NON_DURATION_UNBOUNDED_STRATEGIES)
    def test_expiry_is_now_plus_duration_min_when_safety_timeout_enabled(
        self, strategy: OverrideReleaseStrategy
    ) -> None:
        d = OverrideDetector()
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        _detect(d, "w1", 0, 0, t0)
        t1 = t0 + timedelta(minutes=1)
        _detect(
            d, "w1", 40, 0, t1,
            release_strategy=strategy, duration_min=180, safety_timeout_enabled=True,
        )
        ov = d.get("w1", t1)
        assert ov is not None
        assert ov.expires_at == t1 + timedelta(minutes=180)
        assert ov.release_strategy == strategy.value


class TestUnboundedStrategiesSafetyTimeoutDisabled:
    @pytest.mark.parametrize("strategy", _NON_DURATION_UNBOUNDED_STRATEGIES)
    def test_expiry_is_sentinel_when_safety_timeout_disabled(
        self, strategy: OverrideReleaseStrategy
    ) -> None:
        d = OverrideDetector()
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        _detect(d, "w1", 0, 0, t0)
        t1 = t0 + timedelta(minutes=1)
        _detect(
            d, "w1", 40, 0, t1,
            release_strategy=strategy, duration_min=180, safety_timeout_enabled=False,
        )
        ov = d.get("w1", t1)
        assert ov is not None
        assert ov.expires_at == NO_SAFETY_TIMEOUT


class TestNonDurationStrategiesDoNotExtendOnRenewal:
    @pytest.mark.parametrize("strategy", _NON_DURATION_UNBOUNDED_STRATEGIES)
    def test_renewal_does_not_move_expires_at(self, strategy: OverrideReleaseStrategy) -> None:
        d = OverrideDetector()
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        _detect(d, "w1", 0, 0, t0)
        t1 = t0 + timedelta(minutes=1)
        _detect(
            d, "w1", 40, 0, t1, release_strategy=strategy, duration_min=180, safety_timeout_enabled=True,
        )
        original = d.get("w1", t1)
        t2 = t1 + timedelta(minutes=5)
        _detect(
            d, "w1", 55, 0, t2, release_strategy=strategy, duration_min=180, safety_timeout_enabled=True,
        )
        renewed = d.get("w1", t2)
        assert renewed.expires_at == original.expires_at
        # Position/started_at DO update on a genuine renewed movement.
        assert renewed.override_position == 55
        assert renewed.started_at == t2
