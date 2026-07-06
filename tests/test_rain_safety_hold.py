"""Unit tests for SafetyHold rain behavior.

Tests the dry-cooldown via dynamic hold_s override and the RAIN_HOLD_S constant.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from custom_components.smartshading.engines.safety_hold import (
    SafetyHold,
    RAIN_HOLD_S,
)


_T0 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _now(seconds_offset: float) -> datetime:
    return _T0 + timedelta(seconds=seconds_offset)


# ---------------------------------------------------------------------------
# RAIN_HOLD_S constant
# ---------------------------------------------------------------------------

class TestRainHoldConstants:
    def test_rain_hold_s_is_60(self):
        assert RAIN_HOLD_S == 60.0


# ---------------------------------------------------------------------------
# Basic rain hold lifecycle
# ---------------------------------------------------------------------------

class TestRainHoldBasic:
    def test_not_held_before_trigger(self):
        hold = SafetyHold(_hold_s=RAIN_HOLD_S)
        assert hold.update(evaluator_triggered=False, now=_T0) is False

    def test_held_immediately_after_trigger(self):
        hold = SafetyHold(_hold_s=RAIN_HOLD_S)
        assert hold.update(evaluator_triggered=True, now=_T0) is True

    def test_hold_expires_after_hold_s(self):
        hold = SafetyHold(_hold_s=RAIN_HOLD_S)
        hold.update(evaluator_triggered=True, now=_T0)
        assert hold.update(evaluator_triggered=False, now=_now(RAIN_HOLD_S + 1)) is False

    def test_hold_still_active_before_expiry(self):
        hold = SafetyHold(_hold_s=RAIN_HOLD_S)
        hold.update(evaluator_triggered=True, now=_T0)
        assert hold.update(evaluator_triggered=False, now=_now(RAIN_HOLD_S - 1)) is True


# ---------------------------------------------------------------------------
# Dynamic hold_s override for dry-cooldown
# ---------------------------------------------------------------------------

class TestDryCooldowOverride:
    def test_custom_hold_s_extends_hold(self):
        dry_cooldown_s = 30 * 60  # 30 min in seconds
        hold = SafetyHold(_hold_s=RAIN_HOLD_S)
        hold.update(evaluator_triggered=True, now=_T0)
        # After 5 min (RAIN_HOLD_S expired, but dry_cooldown not yet)
        still_held = hold.update(
            evaluator_triggered=False,
            now=_now(5 * 60),
            hold_s=dry_cooldown_s,
        )
        assert still_held is True

    def test_custom_hold_s_expires(self):
        dry_cooldown_s = 30 * 60
        hold = SafetyHold(_hold_s=RAIN_HOLD_S)
        hold.update(evaluator_triggered=True, now=_T0)
        released = hold.update(
            evaluator_triggered=False,
            now=_now(dry_cooldown_s + 1),
            hold_s=dry_cooldown_s,
        )
        assert released is False

    def test_hold_s_none_falls_back_to_instance_hold(self):
        hold = SafetyHold(_hold_s=RAIN_HOLD_S)
        hold.update(evaluator_triggered=True, now=_T0)
        # Without override, uses RAIN_HOLD_S (60 s)
        assert hold.update(evaluator_triggered=False, now=_now(30), hold_s=None) is True
        assert hold.update(evaluator_triggered=False, now=_now(61), hold_s=None) is False


# ---------------------------------------------------------------------------
# Sensor unavailable while held: hold is extended (fail-safe)
# ---------------------------------------------------------------------------

class TestSensorUnavailableExtend:
    def test_unavailable_resets_hold_timer(self):
        hold = SafetyHold(_hold_s=RAIN_HOLD_S)
        hold.update(evaluator_triggered=True, now=_T0)
        # Sensor goes unavailable just before hold would expire
        hold.update(evaluator_triggered=False, now=_now(55), sensor_unavailable=True)
        # 30 s after that, the hold should still be active (reset at t=55, hold=60)
        assert hold.update(evaluator_triggered=False, now=_now(85)) is True

    def test_unavailable_only_extends_when_latch_active(self):
        hold = SafetyHold(_hold_s=RAIN_HOLD_S)
        # Never triggered — unavailable should not latch the hold
        assert hold.update(evaluator_triggered=False, now=_T0, sensor_unavailable=True) is False


# ---------------------------------------------------------------------------
# seconds_held / is_held accessors
# ---------------------------------------------------------------------------

class TestSecondHeldAccessor:
    def test_seconds_held_none_when_not_latched(self):
        hold = SafetyHold(_hold_s=RAIN_HOLD_S)
        assert hold.seconds_held(_T0) is None

    def test_seconds_held_returns_elapsed(self):
        hold = SafetyHold(_hold_s=RAIN_HOLD_S)
        hold.update(evaluator_triggered=True, now=_T0)
        assert hold.seconds_held(_now(10)) == pytest.approx(10.0)

    def test_is_held_false_initially(self):
        hold = SafetyHold(_hold_s=RAIN_HOLD_S)
        assert hold.is_held is False

    def test_is_held_true_after_trigger(self):
        hold = SafetyHold(_hold_s=RAIN_HOLD_S)
        hold.update(evaluator_triggered=True, now=_T0)
        assert hold.is_held is True
