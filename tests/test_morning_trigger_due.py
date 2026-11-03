"""Tests for LifecycleEngine.is_morning_trigger_due() — B3-010 R12.

This is the exact, reused trigger-due semantics the coordinator's missed-
Morning-event check now consults (see coordinator.py's per-window loop) --
previously that check only asked "is Morning theoretically eligible today"
(enabled + month active), which could wrongly fire hours before the real
configured trigger time. is_morning_trigger_due() answers the narrower,
correct question: has today's trigger actually fired as of `now`, using
get_lifecycle_state()'s own _check_morning_trigger()/_active_profile(),
never a second, parallel computation.

Coverage:
  MTD-01  Fixed Time: before the trigger -> False.
  MTD-02  Fixed Time: exactly at the trigger -> True.
  MTD-03  Fixed Time: after the trigger -> True.
  MTD-04  Sun Elevation: below threshold -> False, at/above -> True.
  MTD-05  BOTH: either condition alone is sufficient (existing OR semantics).
  MTD-06  Configured sun-event override resolves into the same fixed-time slot.
  MTD-07  Weekday vs weekend profile selects the correct trigger time.
  MTD-08  Active month -> normal evaluation; inactive month -> always False.
  MTD-09  morning_enabled=False -> always False, regardless of time.
  MTD-10  MorningTrigger.DISABLED -> always False.
  MTD-11  Missing sun elevation data never fabricates a SUN_ELEVATION/BOTH trigger.
"""
from __future__ import annotations

from datetime import datetime, time, timezone

from custom_components.smartshading.engines.lifecycle_engine import LifecycleEngine
from custom_components.smartshading.models.lifecycle import (
    LifecycleScheduleMode,
    MorningTrigger,
    NightDayLifecycleConfig,
    NightTrigger,
)

_UTC = timezone.utc
_engine = LifecycleEngine()


def _local(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, 0, tzinfo=_UTC)


def _config(**overrides) -> NightDayLifecycleConfig:
    defaults = dict(
        id="test", schedule_mode=LifecycleScheduleMode.SAME_EVERY_DAY,
        night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(22, 0), night_position=0,
        morning_trigger=MorningTrigger.FIXED_TIME, morning_fixed_time=time(6, 30),
        morning_position=30, morning_enabled=True,
    )
    defaults.update(overrides)
    return NightDayLifecycleConfig(**defaults)


class TestFixedTime:
    def test_before_trigger_is_not_due(self):
        cfg = _config(morning_fixed_time=time(6, 30))
        assert _engine.is_morning_trigger_due(_local(6, 15, 6, 29), None, cfg) is False

    def test_exactly_at_trigger_is_due(self):
        cfg = _config(morning_fixed_time=time(6, 30))
        assert _engine.is_morning_trigger_due(_local(6, 15, 6, 30), None, cfg) is True

    def test_after_trigger_is_due(self):
        cfg = _config(morning_fixed_time=time(6, 30))
        assert _engine.is_morning_trigger_due(_local(6, 15, 12, 0), None, cfg) is True


class TestSunElevation:
    def test_below_threshold_not_due(self):
        cfg = _config(morning_trigger=MorningTrigger.SUN_ELEVATION, morning_sun_elevation_deg=0.0)
        assert _engine.is_morning_trigger_due(_local(6, 15, 6, 30), -1.0, cfg) is False

    def test_at_or_above_threshold_is_due(self):
        cfg = _config(morning_trigger=MorningTrigger.SUN_ELEVATION, morning_sun_elevation_deg=0.0)
        assert _engine.is_morning_trigger_due(_local(6, 15, 6, 30), 0.0, cfg) is True
        assert _engine.is_morning_trigger_due(_local(6, 15, 6, 30), 5.0, cfg) is True

    def test_missing_elevation_data_never_fires(self):
        cfg = _config(morning_trigger=MorningTrigger.SUN_ELEVATION, morning_sun_elevation_deg=0.0)
        assert _engine.is_morning_trigger_due(_local(6, 15, 12, 0), None, cfg) is False


class TestBothOrSemantics:
    def test_time_alone_is_sufficient(self):
        cfg = _config(
            morning_trigger=MorningTrigger.BOTH, morning_fixed_time=time(6, 30),
            morning_sun_elevation_deg=10.0,
        )
        assert _engine.is_morning_trigger_due(_local(6, 15, 6, 30), None, cfg) is True

    def test_elevation_alone_is_sufficient(self):
        cfg = _config(
            morning_trigger=MorningTrigger.BOTH, morning_fixed_time=time(23, 59),
            morning_sun_elevation_deg=0.0,
        )
        assert _engine.is_morning_trigger_due(_local(6, 15, 6, 0), 1.0, cfg) is True

    def test_neither_condition_met_not_due(self):
        cfg = _config(
            morning_trigger=MorningTrigger.BOTH, morning_fixed_time=time(23, 59),
            morning_sun_elevation_deg=10.0,
        )
        assert _engine.is_morning_trigger_due(_local(6, 15, 6, 0), -5.0, cfg) is False


class TestWeekdayWeekend:
    def test_weekday_uses_weekday_profile(self):
        # 2026-06-15 is a Monday.
        cfg = _config(
            schedule_mode=LifecycleScheduleMode.WEEKDAY_WEEKEND,
            weekday_morning_fixed_time=time(6, 30), weekend_morning_fixed_time=time(9, 0),
            morning_fixed_time=None,
        )
        assert _engine.is_morning_trigger_due(_local(6, 15, 6, 15), None, cfg) is False
        assert _engine.is_morning_trigger_due(_local(6, 15, 6, 30), None, cfg) is True

    def test_weekend_uses_weekend_profile(self):
        # 2026-06-20 is a Saturday.
        cfg = _config(
            schedule_mode=LifecycleScheduleMode.WEEKDAY_WEEKEND,
            weekday_morning_fixed_time=time(6, 30), weekend_morning_fixed_time=time(9, 0),
            morning_fixed_time=None,
        )
        assert _engine.is_morning_trigger_due(_local(6, 20, 6, 30), None, cfg) is False
        assert _engine.is_morning_trigger_due(_local(6, 20, 9, 0), None, cfg) is True


class TestMonthGate:
    def test_active_month_evaluates_normally(self):
        cfg = _config(morning_fixed_time=time(6, 30), active_months=[6, 7, 8])
        assert _engine.is_morning_trigger_due(_local(6, 15, 12, 0), None, cfg) is True

    def test_inactive_month_never_due(self):
        cfg = _config(morning_fixed_time=time(6, 30), active_months=[12, 1, 2])
        assert _engine.is_morning_trigger_due(_local(6, 15, 12, 0), None, cfg) is False


class TestMorningDisabled:
    def test_morning_enabled_false_never_due(self):
        cfg = _config(morning_fixed_time=time(6, 30), morning_enabled=False)
        assert _engine.is_morning_trigger_due(_local(6, 15, 12, 0), None, cfg) is False

    def test_morning_trigger_disabled_never_due(self):
        cfg = _config(morning_trigger=MorningTrigger.DISABLED)
        assert _engine.is_morning_trigger_due(_local(6, 15, 12, 0), None, cfg) is False


class TestConfiguredSunEventOverride:
    def test_sun_event_resolves_into_fixed_time_slot(self):
        """A configured morning_sun_event override resolves into the same
        fixed-time comparison slot every trigger type already uses --
        proven here via SunEventTimes, the same mechanism
        get_lifecycle_state() itself consults."""
        from custom_components.smartshading.engines.lifecycle_engine import SunEventTimes
        from custom_components.smartshading.models.lifecycle import SunEvent

        cfg = _config(
            morning_trigger=MorningTrigger.FIXED_TIME, morning_fixed_time=time(6, 30),
            morning_sun_event=SunEvent.SUNRISE,
        )
        # next_sunrise still points to TODAY -> "still pending today, use
        # its time as-is" (see _resolve_sun_event_time's own docstring).
        sun_times = SunEventTimes(next_sunrise=_local(6, 15, 7, 15))
        # Before the resolved sunrise time -> not due.
        assert _engine.is_morning_trigger_due(_local(6, 15, 7, 0), None, cfg, sun_times) is False
        # At/after the resolved sunrise time -> due.
        assert _engine.is_morning_trigger_due(_local(6, 15, 7, 15), None, cfg, sun_times) is True
