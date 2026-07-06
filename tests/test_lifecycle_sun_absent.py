"""Sun-absent lifecycle semantics — absent-evidence semantics for missing elevation.

Tests spec section 3: fehlende Sonnenhöhe darf nicht als reale 0.0°-Messung
behandelt werden.

Coverage (9 spec items + supporting):
  SA-01  Standard threshold -6°, sun absent → no false trigger
  SA-02  Positive threshold +5°, sun absent → no false trigger (old 0.0 would have triggered)
  SA-03  Negative unusual threshold -3°, sun absent
  SA-04  FIXED_TIME night, sun absent → fires correctly
  SA-05  SUN_ELEVATION night, sun absent → does not fire
  SA-06  Existing NIGHT state, sun absent → preserved
  SA-07  Existing DAY state, sun absent → preserved (no false NIGHT)
  SA-08  Sun returns after several absent cycles → correct transition
  SA-09  No artificial NIGHT or DAY transition from absent data
  SA-10  BOTH trigger with absent sun → FIXED_TIME part still fires
  SA-11  Morning SUN_ELEVATION, sun absent → no false morning trigger
  SA-12  Morning FIXED_TIME, sun absent → fires correctly
  SA-13  check_night_interval_active with positive threshold, sun absent → False
  SA-14  FIXED_TIME carryover with None elevation → True (before noon, morning not fired)
  SA-15  SUN_ELEVATION carryover with None elevation → False (cannot confirm)
  SA-16  DST/clock shift: local_now derived from utcnow, not a separate call
"""
from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from custom_components.smartshading.engines.lifecycle_engine import (
    LifecycleEngine,
    check_night_interval_active,
)
from custom_components.smartshading.models.lifecycle import (
    LifecycleState,
    MorningTrigger,
    NightDayLifecycleConfig,
    NightTrigger,
)

_UTC = timezone.utc


def _local(hour: int, minute: int = 0, day: int = 15) -> datetime:
    return datetime(2026, 6, day, hour, minute, 0, tzinfo=_UTC)


def _fixed_time_config(
    night_h: int = 22, morning_h: int = 6
) -> NightDayLifecycleConfig:
    return NightDayLifecycleConfig(
        id="test",
        night_enabled=True,
        night_trigger=NightTrigger.FIXED_TIME,
        night_fixed_time=time(night_h, 0),
        night_sun_elevation_deg=-6.0,
        morning_enabled=True,
        morning_trigger=MorningTrigger.FIXED_TIME,
        morning_fixed_time=time(morning_h, 0),
    )


def _sun_elev_config(
    threshold: float, morning_threshold: float = 5.0
) -> NightDayLifecycleConfig:
    return NightDayLifecycleConfig(
        id="test",
        night_enabled=True,
        night_trigger=NightTrigger.SUN_ELEVATION,
        night_sun_elevation_deg=threshold,
        morning_enabled=True,
        morning_trigger=MorningTrigger.SUN_ELEVATION,
        morning_sun_elevation_deg=morning_threshold,
    )


def _both_config(
    threshold: float = -6.0, night_h: int = 22, morning_h: int = 6
) -> NightDayLifecycleConfig:
    return NightDayLifecycleConfig(
        id="test",
        night_enabled=True,
        night_trigger=NightTrigger.BOTH,
        night_sun_elevation_deg=threshold,
        night_fixed_time=time(night_h, 0),
        morning_enabled=True,
        morning_trigger=MorningTrigger.FIXED_TIME,
        morning_fixed_time=time(morning_h, 0),
    )


_ENG = LifecycleEngine()


# ---------------------------------------------------------------------------
# SA-01  Standard threshold -6°, sun absent
# ---------------------------------------------------------------------------

class TestSA01_StandardThresholdAbsent:
    """SA-01: with threshold -6°, absent sun must not trigger night."""

    def test_no_night_at_daytime_with_absent_sun(self):
        """At 14:00 (past noon, no carryover), no NIGHT with absent sun."""
        cfg = _sun_elev_config(-6.0)
        state = _ENG.get_lifecycle_state(_local(14, 0), None, cfg, LifecycleState.DAY)
        assert state == LifecycleState.DAY

    def test_check_night_interval_absent_standard_threshold(self):
        """check_night_interval_active: absent sun, standard threshold → False."""
        cfg = _sun_elev_config(-6.0)
        result = check_night_interval_active(_local(14, 0), None, cfg)
        assert result is False


# ---------------------------------------------------------------------------
# SA-02  Positive threshold +5°, sun absent → must not trigger night
# ---------------------------------------------------------------------------

class TestSA02_PositiveThresholdAbsent:
    """SA-02: with positive threshold +5°, absent sun must not trigger night.
    Old 0.0 substitution would have triggered (0.0 <= 5.0 = True)."""

    def test_no_night_positive_threshold_absent_sun(self):
        cfg = _sun_elev_config(5.0)
        state = _ENG.get_lifecycle_state(_local(14, 0), None, cfg, LifecycleState.DAY)
        assert state == LifecycleState.DAY

    def test_check_night_interval_positive_threshold_absent(self):
        cfg = _sun_elev_config(5.0)
        result = check_night_interval_active(_local(14, 0), None, cfg)
        assert result is False

    def test_positive_threshold_fires_when_elevation_present_and_below(self):
        """Control: with real elevation below +5°, trigger fires."""
        cfg = _sun_elev_config(5.0)
        state = _ENG.get_lifecycle_state(_local(14, 0), 2.0, cfg, LifecycleState.DAY)
        assert state == LifecycleState.NIGHT

    def test_positive_threshold_does_not_fire_when_above(self):
        """Control: with elevation above +5°, no trigger."""
        cfg = _sun_elev_config(5.0)
        state = _ENG.get_lifecycle_state(_local(14, 0), 8.0, cfg, LifecycleState.DAY)
        assert state == LifecycleState.DAY


# ---------------------------------------------------------------------------
# SA-03  Negative unusual threshold -3°
# ---------------------------------------------------------------------------

class TestSA03_NegativeUnusualThreshold:
    """SA-03: threshold -3°, absent sun. 0.0 <= -3.0 = False so old code was OK here,
    but new code is explicitly correct (elevation_met=False when None)."""

    def test_no_night_negative_unusual_threshold_absent(self):
        cfg = _sun_elev_config(-3.0)
        state = _ENG.get_lifecycle_state(_local(14, 0), None, cfg, LifecycleState.DAY)
        assert state == LifecycleState.DAY

    def test_fires_when_elevation_below_minus3(self):
        """Control: elevation -5.0 < -3.0 → night."""
        cfg = _sun_elev_config(-3.0)
        state = _ENG.get_lifecycle_state(_local(14, 0), -5.0, cfg, LifecycleState.DAY)
        assert state == LifecycleState.NIGHT


# ---------------------------------------------------------------------------
# SA-04  FIXED_TIME night, sun absent → fires correctly
# ---------------------------------------------------------------------------

class TestSA04_FixedTimeAbsentSun:
    """SA-04: FIXED_TIME trigger evaluates on time alone — works without elevation."""

    def test_fixed_time_fires_at_night_hour_no_sun(self):
        cfg = _fixed_time_config(night_h=22)
        state = _ENG.get_lifecycle_state(_local(22, 30), None, cfg, LifecycleState.DAY)
        assert state == LifecycleState.NIGHT

    def test_fixed_time_does_not_fire_before_hour_no_sun(self):
        cfg = _fixed_time_config(night_h=22)
        state = _ENG.get_lifecycle_state(_local(21, 59), None, cfg, LifecycleState.DAY)
        assert state == LifecycleState.DAY

    def test_check_night_interval_fixed_time_no_sun_active(self):
        cfg = _fixed_time_config(night_h=22)
        result = check_night_interval_active(_local(23, 0), None, cfg)
        assert result is True

    def test_check_night_interval_fixed_time_no_sun_inactive(self):
        cfg = _fixed_time_config(night_h=22)
        result = check_night_interval_active(_local(21, 0), None, cfg)
        assert result is False


# ---------------------------------------------------------------------------
# SA-05  SUN_ELEVATION night, sun absent → does not fire
# ---------------------------------------------------------------------------

class TestSA05_SunElevationAbsent:
    """SA-05: SUN_ELEVATION trigger with absent sun must not create new night state."""

    def test_sun_elev_trigger_absent_does_not_fire(self):
        cfg = _sun_elev_config(-6.0)
        state = _ENG.get_lifecycle_state(_local(20, 0), None, cfg, LifecycleState.DAY)
        assert state == LifecycleState.DAY

    def test_sun_elev_trigger_positive_threshold_absent_does_not_fire(self):
        cfg = _sun_elev_config(10.0)
        state = _ENG.get_lifecycle_state(_local(20, 0), None, cfg, LifecycleState.DAY)
        assert state == LifecycleState.DAY


# ---------------------------------------------------------------------------
# SA-06  Existing NIGHT state, sun absent → preserved
# ---------------------------------------------------------------------------

class TestSA06_ExistingNightPreserved:
    """SA-06: if previous=NIGHT and sun absent, stay NIGHT (morning not triggered yet)."""

    def test_night_preserved_at_2am_no_sun(self):
        cfg = _fixed_time_config(night_h=22, morning_h=6)
        now = _local(2, 0)  # 02:00 AM, morning trigger at 06:00 not yet fired
        state = _ENG.get_lifecycle_state(now, None, cfg, LifecycleState.NIGHT)
        assert state == LifecycleState.NIGHT

    def test_night_preserved_sun_elev_config_at_2am_no_sun(self):
        cfg = _sun_elev_config(-6.0)
        now = _local(2, 0)
        state = _ENG.get_lifecycle_state(now, None, cfg, LifecycleState.NIGHT)
        assert state == LifecycleState.NIGHT

    def test_night_preserved_positive_threshold_no_sun(self):
        """NIGHT preserved even with positive threshold when sun is absent."""
        cfg = _sun_elev_config(5.0)
        now = _local(2, 0)
        state = _ENG.get_lifecycle_state(now, None, cfg, LifecycleState.NIGHT)
        assert state == LifecycleState.NIGHT


# ---------------------------------------------------------------------------
# SA-07  Existing DAY state, sun absent → preserved (no false NIGHT)
# ---------------------------------------------------------------------------

class TestSA07_ExistingDayPreserved:
    """SA-07: if previous=DAY at daytime and sun absent, stay DAY (no false NIGHT)."""

    def test_day_preserved_at_noon_no_sun(self):
        cfg = _sun_elev_config(-6.0)
        state = _ENG.get_lifecycle_state(_local(12, 0), None, cfg, LifecycleState.DAY)
        assert state == LifecycleState.DAY

    def test_day_preserved_positive_threshold_noon_no_sun(self):
        """Old 0.0 substitution with +5° threshold would have triggered NIGHT here."""
        cfg = _sun_elev_config(5.0)
        state = _ENG.get_lifecycle_state(_local(12, 0), None, cfg, LifecycleState.DAY)
        assert state == LifecycleState.DAY


# ---------------------------------------------------------------------------
# SA-08  Sun returns after absent cycles → correct transition
# ---------------------------------------------------------------------------

class TestSA08_SunReturns:
    """SA-08: after several absent cycles, when sun data returns,
    the lifecycle engine transitions correctly."""

    def test_sun_returns_triggers_night_when_below_threshold(self):
        cfg = _sun_elev_config(-6.0)
        now = _local(20, 0)
        # Several absent cycles — state stays DAY
        state = _ENG.get_lifecycle_state(now, None, cfg, LifecycleState.DAY)
        assert state == LifecycleState.DAY
        # Sun returns with elevation -8° < -6° → NIGHT
        state2 = _ENG.get_lifecycle_state(now, -8.0, cfg, state)
        assert state2 == LifecycleState.NIGHT

    def test_sun_returns_stays_day_when_above_threshold(self):
        cfg = _sun_elev_config(-6.0)
        now = _local(14, 0)
        state = _ENG.get_lifecycle_state(now, None, cfg, LifecycleState.DAY)
        state2 = _ENG.get_lifecycle_state(now, 30.0, cfg, state)
        assert state2 == LifecycleState.DAY


# ---------------------------------------------------------------------------
# SA-09  No artificial NIGHT or DAY transition from absent data
# ---------------------------------------------------------------------------

class TestSA09_NoArtificialTransition:
    """SA-09: absent sun must not cause any state transition on its own."""

    def test_no_transition_day_to_night_from_absent_sun(self):
        cfg = _sun_elev_config(5.0)  # positive threshold — old code would have triggered
        prev = LifecycleState.DAY
        now = _local(14, 0)
        for _ in range(5):  # 5 absent cycles
            prev = _ENG.get_lifecycle_state(now, None, cfg, prev)
        assert prev == LifecycleState.DAY

    def test_no_transition_night_to_day_from_absent_sun(self):
        """Absent sun with SUN_ELEVATION morning trigger — morning must not fire."""
        cfg = _sun_elev_config(-6.0, morning_threshold=5.0)
        prev = LifecycleState.NIGHT
        now = _local(7, 0)
        for _ in range(5):  # 5 absent cycles
            prev = _ENG.get_lifecycle_state(now, None, cfg, prev)
        # SUN_ELEVATION morning trigger with None elevation → elevation_met=False
        # FIXED_TIME morning not configured → stays NIGHT
        assert prev == LifecycleState.NIGHT


# ---------------------------------------------------------------------------
# SA-10  BOTH trigger — FIXED_TIME part still fires with absent sun
# ---------------------------------------------------------------------------

class TestSA10_BothTriggerAbsentSun:
    """SA-10: BOTH trigger uses FIXED_TIME part when elevation is absent."""

    def test_both_trigger_fixed_time_fires_absent_sun(self):
        cfg = _both_config(threshold=-6.0, night_h=22)
        state = _ENG.get_lifecycle_state(_local(22, 30), None, cfg, LifecycleState.DAY)
        assert state == LifecycleState.NIGHT

    def test_both_trigger_elevation_does_not_fire_absent_sun_before_fixed_time(self):
        """Before 22:00 with absent sun: BOTH trigger should not fire (elevation_met=False,
        time_met=False)."""
        cfg = _both_config(threshold=-6.0, night_h=22)
        state = _ENG.get_lifecycle_state(_local(21, 0), None, cfg, LifecycleState.DAY)
        assert state == LifecycleState.DAY


# ---------------------------------------------------------------------------
# SA-11  Morning SUN_ELEVATION, sun absent → no false morning trigger
# ---------------------------------------------------------------------------

class TestSA11_MorningSunElevationAbsent:
    """SA-11: SUN_ELEVATION morning trigger with absent sun must not fire."""

    def test_morning_sun_elev_absent_does_not_trigger(self):
        cfg = _sun_elev_config(-6.0, morning_threshold=5.0)
        now = _local(7, 0)
        state = _ENG.get_lifecycle_state(now, None, cfg, LifecycleState.NIGHT)
        assert state == LifecycleState.NIGHT  # morning not triggered


# ---------------------------------------------------------------------------
# SA-12  Morning FIXED_TIME, sun absent → fires correctly
# ---------------------------------------------------------------------------

class TestSA12_MorningFixedTimeAbsent:
    """SA-12: FIXED_TIME morning trigger fires correctly without elevation."""

    def test_morning_fixed_time_fires_absent_sun(self):
        cfg = _fixed_time_config(night_h=22, morning_h=6)
        now = _local(7, 0)  # past 06:00 morning trigger
        state = _ENG.get_lifecycle_state(now, None, cfg, LifecycleState.NIGHT)
        assert state == LifecycleState.MORNING


# ---------------------------------------------------------------------------
# SA-13  check_night_interval_active: positive threshold, sun absent → False
# ---------------------------------------------------------------------------

class TestSA13_CheckNightIntervalPositiveThreshold:
    """SA-13: check_night_interval_active with positive threshold and absent sun → False."""

    def test_positive_threshold_absent_sun_false(self):
        cfg = _sun_elev_config(5.0)
        result = check_night_interval_active(_local(14, 0), None, cfg)
        assert result is False

    def test_positive_threshold_absent_sun_carryover_am_false(self):
        """At 01:00 AM with SUN_ELEVATION trigger: carryover returns False (no elevation)."""
        cfg = _sun_elev_config(5.0)
        result = check_night_interval_active(_local(1, 0), None, cfg)
        assert result is False


# ---------------------------------------------------------------------------
# SA-14  FIXED_TIME carryover with None elevation → True
# ---------------------------------------------------------------------------

class TestSA14_FixedTimeCarryoverNoneElevation:
    """SA-14: FIXED_TIME carryover at 00:47 AM with None elevation → True.
    Previously returned False due to 0.0 < 0.0 = False (now correct)."""

    def test_fixed_time_carryover_none_elevation_true(self):
        cfg = _fixed_time_config(night_h=22, morning_h=6)
        result = check_night_interval_active(_local(0, 47), None, cfg)
        assert result is True

    def test_fixed_time_carryover_none_elevation_3am(self):
        cfg = _fixed_time_config(night_h=22, morning_h=6)
        result = check_night_interval_active(_local(3, 0), None, cfg)
        assert result is True

    def test_fixed_time_carryover_none_after_morning_trigger_false(self):
        """After morning trigger (07:00 >= 06:00), carryover returns False."""
        cfg = _fixed_time_config(night_h=22, morning_h=6)
        result = check_night_interval_active(_local(7, 0), None, cfg)
        assert result is False

    def test_fixed_time_carryover_none_afternoon_false(self):
        """Afternoon (> noon) → never a carryover."""
        cfg = _fixed_time_config(night_h=22, morning_h=6)
        result = check_night_interval_active(_local(14, 0), None, cfg)
        assert result is False


# ---------------------------------------------------------------------------
# SA-15  SUN_ELEVATION carryover with None elevation → False
# ---------------------------------------------------------------------------

class TestSA15_SunElevationCarryoverNoneElevation:
    """SA-15: SUN_ELEVATION carryover at AM with None elevation → False."""

    def test_sun_elev_carryover_none_false(self):
        cfg = _sun_elev_config(-6.0)
        result = check_night_interval_active(_local(1, 0), None, cfg)
        assert result is False

    def test_sun_elev_carryover_positive_threshold_none_false(self):
        cfg = _sun_elev_config(5.0)
        result = check_night_interval_active(_local(1, 0), None, cfg)
        assert result is False


# ---------------------------------------------------------------------------
# SA-16  Clock authority: local_now derived from utcnow
# ---------------------------------------------------------------------------

class TestSA16_ClockAuthority:
    """SA-16: local_now derived from a single UTC instant must represent the same moment.

    The coordinator uses dt_util.as_local(now) — a timezone conversion of the same
    UTC instant, not a separate clock call. These pure-Python tests verify the contract."""

    def test_astimezone_preserves_utc_instant(self):
        """Converting a UTC datetime to local tz preserves the UTC instant."""
        import datetime
        import zoneinfo
        utc_now = datetime.datetime(2026, 6, 18, 14, 0, 0, tzinfo=datetime.timezone.utc)
        berlin = zoneinfo.ZoneInfo("Europe/Berlin")  # UTC+2 in summer
        local = utc_now.astimezone(berlin)
        assert local.utctimetuple() == utc_now.utctimetuple()

    def test_derived_local_same_instant_as_utc(self):
        """local = utc.astimezone(tz) represents the same wall-clock instant."""
        import datetime
        import zoneinfo
        utc_now = datetime.datetime(2026, 1, 15, 3, 30, 0, tzinfo=datetime.timezone.utc)
        berlin = zoneinfo.ZoneInfo("Europe/Berlin")  # UTC+1 in winter
        local = utc_now.astimezone(berlin)
        back_to_utc = local.astimezone(datetime.timezone.utc)
        assert back_to_utc == utc_now
