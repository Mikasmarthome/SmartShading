"""Active months per lifecycle schedule — v1.2.0-beta.1, Beta.1-T1.

Lets a night/morning schedule be restricted to a subset of calendar
months (e.g. September through March for a winter schedule), following the
same additive pattern as LifecycleScheduleMode (weekday/weekend): a single
NightDayLifecycleConfig field, resolved once per cycle, with no zone/window
plumbing and no revival of the unused lifecycle_config_id mechanism.

Coverage:
  AM-01  Default (active_months=None) behaves exactly like before — regression.
  AM-02  Current month included → night trigger fires normally.
  AM-03  Current month excluded → night trigger does not fire, state stays DAY.
  AM-04  Year boundary: Sep-Mar list includes December and January correctly.
  AM-05  Year boundary: Sep-Mar list excludes a mid-list-boundary month (April).
  AM-06  check_night_interval_active() is also gated by active_months.
  AM-07  Bootstrap carryover is not created for an inactive month.
  AM-08  A NIGHT state carried into a newly-inactive month releases to DAY,
         not stuck waiting for a morning trigger that may never fire.
  AM-09  Weekday/Weekend schedule_mode combined with active_months still
         resolves the correct weekday/weekend profile when the month is active.
  AM-10  Storage round-trip: missing "active_months" key (pre-beta configs)
         deserializes to None (unrestricted) — backward compatibility.
  AM-11  Storage round-trip: an explicit month list survives serialize/deserialize.
"""
from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from custom_components.smartshading.config_entry_data import (
    _lifecycle_config_from_storage,
    to_storage_dict,
    SmartShadingConfigEntryData,
)
from custom_components.smartshading.engines.lifecycle_engine import (
    LifecycleEngine,
    check_night_interval_active,
)
from custom_components.smartshading.models.lifecycle import (
    LifecycleScheduleMode,
    LifecycleState,
    MorningTrigger,
    NightDayLifecycleConfig,
    NightTrigger,
)

_UTC = timezone.utc


def _local(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, 0, tzinfo=_UTC)


def _config(active_months: list[int] | None = None, **overrides) -> NightDayLifecycleConfig:
    defaults = dict(
        id="test",
        night_enabled=True,
        night_trigger=NightTrigger.FIXED_TIME,
        night_fixed_time=time(22, 0),
        morning_enabled=True,
        morning_trigger=MorningTrigger.FIXED_TIME,
        morning_fixed_time=time(6, 0),
        active_months=active_months,
    )
    defaults.update(overrides)
    return NightDayLifecycleConfig(**defaults)


# ---------------------------------------------------------------------------
# AM-01 — Default behaves exactly like before.
# ---------------------------------------------------------------------------

class TestDefaultUnrestricted:
    def test_active_months_default_is_none(self):
        assert NightDayLifecycleConfig(id="x").active_months is None

    def test_none_active_months_fires_night_in_any_month(self):
        engine = LifecycleEngine()
        cfg = _config(active_months=None)
        for month in (1, 6, 12):
            state = engine.get_lifecycle_state(
                _local(month, 15, 23, 0), None, cfg, LifecycleState.DAY,
            )
            assert state is LifecycleState.NIGHT, month


# ---------------------------------------------------------------------------
# AM-02 / AM-03 — active_months gates night trigger.
# ---------------------------------------------------------------------------

class TestActiveMonthGating:
    def test_current_month_included_fires_normally(self):
        engine = LifecycleEngine()
        cfg = _config(active_months=[9, 10, 11, 12, 1, 2, 3])
        state = engine.get_lifecycle_state(
            _local(12, 15, 23, 0), None, cfg, LifecycleState.DAY,
        )
        assert state is LifecycleState.NIGHT

    def test_current_month_excluded_never_fires(self):
        engine = LifecycleEngine()
        cfg = _config(active_months=[9, 10, 11, 12, 1, 2, 3])
        state = engine.get_lifecycle_state(
            _local(6, 15, 23, 0), None, cfg, LifecycleState.DAY,
        )
        assert state is LifecycleState.DAY


# ---------------------------------------------------------------------------
# AM-04 / AM-05 — year boundary correctness.
# ---------------------------------------------------------------------------

class TestYearBoundary:
    def test_december_and_january_both_active_for_winter_rule(self):
        engine = LifecycleEngine()
        cfg = _config(active_months=[9, 10, 11, 12, 1, 2, 3])
        dec_state = engine.get_lifecycle_state(
            _local(12, 31, 23, 0), None, cfg, LifecycleState.DAY,
        )
        jan_state = engine.get_lifecycle_state(
            _local(1, 1, 23, 0), None, cfg, LifecycleState.DAY,
        )
        assert dec_state is LifecycleState.NIGHT
        assert jan_state is LifecycleState.NIGHT

    def test_april_excluded_from_winter_rule(self):
        engine = LifecycleEngine()
        cfg = _config(active_months=[9, 10, 11, 12, 1, 2, 3])
        state = engine.get_lifecycle_state(
            _local(4, 1, 23, 0), None, cfg, LifecycleState.DAY,
        )
        assert state is LifecycleState.DAY

    def test_summer_rule_apr_aug_active_in_july(self):
        engine = LifecycleEngine()
        cfg = _config(active_months=[4, 5, 6, 7, 8])
        state = engine.get_lifecycle_state(
            _local(7, 15, 23, 0), None, cfg, LifecycleState.DAY,
        )
        assert state is LifecycleState.NIGHT

    def test_summer_rule_apr_aug_inactive_in_december(self):
        engine = LifecycleEngine()
        cfg = _config(active_months=[4, 5, 6, 7, 8])
        state = engine.get_lifecycle_state(
            _local(12, 15, 23, 0), None, cfg, LifecycleState.DAY,
        )
        assert state is LifecycleState.DAY


# ---------------------------------------------------------------------------
# AM-06 — check_night_interval_active() gated too.
# ---------------------------------------------------------------------------

class TestCheckNightIntervalActive:
    def test_active_month_returns_true(self):
        cfg = _config(active_months=[12])
        assert check_night_interval_active(_local(12, 15, 23, 0), None, cfg) is True

    def test_inactive_month_returns_false(self):
        cfg = _config(active_months=[12])
        assert check_night_interval_active(_local(6, 15, 23, 0), None, cfg) is False


# ---------------------------------------------------------------------------
# AM-07 — bootstrap carryover suppressed for an inactive month.
# ---------------------------------------------------------------------------

class TestCarryoverGating:
    def test_no_carryover_bootstrap_in_inactive_month(self):
        engine = LifecycleEngine()
        cfg = _config(active_months=[12])
        # After-midnight, before-morning window that would normally bootstrap
        # NIGHT via carryover detection — but June is not in active_months.
        state = engine.get_lifecycle_state(
            _local(6, 15, 0, 15), None, cfg, LifecycleState.DAY,
        )
        assert state is LifecycleState.DAY

    def test_carryover_bootstrap_still_works_in_active_month(self):
        engine = LifecycleEngine()
        cfg = _config(active_months=[12])
        state = engine.get_lifecycle_state(
            _local(12, 15, 0, 15), None, cfg, LifecycleState.DAY,
        )
        assert state is LifecycleState.NIGHT


# ---------------------------------------------------------------------------
# AM-08 — NIGHT carried into a newly-inactive month releases to DAY.
# ---------------------------------------------------------------------------

class TestReleaseOnMonthRollover:
    def test_night_releases_to_day_when_month_becomes_inactive(self):
        engine = LifecycleEngine()
        cfg = _config(active_months=[12])
        # Previous cycle: still NIGHT (e.g. entered on Dec 31). Now it's
        # January — the rule is inactive, morning may never trigger this
        # month, so NIGHT must not persist indefinitely.
        state = engine.get_lifecycle_state(
            _local(1, 1, 3, 0), None, cfg, LifecycleState.NIGHT,
        )
        assert state is LifecycleState.DAY


# ---------------------------------------------------------------------------
# AM-09 — combines cleanly with WEEKDAY_WEEKEND schedule_mode.
# ---------------------------------------------------------------------------

class TestCombinedWithWeekdayWeekend:
    def test_weekend_profile_still_used_when_month_active(self):
        engine = LifecycleEngine()
        # 2026-12-19 is a Saturday.
        cfg = _config(
            active_months=[12],
            schedule_mode=LifecycleScheduleMode.WEEKDAY_WEEKEND,
            weekend_night_fixed_time=time(23, 0),
        )
        profile = engine.active_profile(_local(12, 19, 12, 0), cfg)
        assert profile.night_fixed_time == time(23, 0)

    def test_weekday_weekend_schedule_still_unaffected_when_no_active_months(self):
        engine = LifecycleEngine()
        cfg = _config(
            active_months=None,
            schedule_mode=LifecycleScheduleMode.WEEKDAY_WEEKEND,
            weekend_night_fixed_time=time(23, 0),
        )
        profile = engine.active_profile(_local(12, 19, 12, 0), cfg)
        assert profile.night_fixed_time == time(23, 0)


# ---------------------------------------------------------------------------
# AM-10 / AM-11 — storage round-trip / backward compatibility.
# ---------------------------------------------------------------------------

class TestStorageRoundTrip:
    def test_missing_key_defaults_to_none(self):
        raw = {"id": "default", "night_enabled": True}  # pre-beta config, no key
        cfg = _lifecycle_config_from_storage(raw)
        assert cfg.active_months is None

    def test_explicit_list_survives_round_trip(self):
        data = SmartShadingConfigEntryData(
            name="Test",
            use_home_location=True,
            lifecycle_config=NightDayLifecycleConfig(
                id="default", active_months=[9, 10, 11, 12, 1, 2, 3],
            ),
        )
        stored = to_storage_dict(data)
        restored = _lifecycle_config_from_storage(stored["lifecycle_config"])
        assert restored.active_months == [9, 10, 11, 12, 1, 2, 3]

    def test_none_survives_round_trip(self):
        data = SmartShadingConfigEntryData(
            name="Test",
            use_home_location=True,
            lifecycle_config=NightDayLifecycleConfig(id="default", active_months=None),
        )
        stored = to_storage_dict(data)
        restored = _lifecycle_config_from_storage(stored["lifecycle_config"])
        assert restored.active_months is None
