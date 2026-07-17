"""Schedule clamp for lifecycle triggers — v1.2.0-beta.1, Beta.1-T3.

Architecture: night_not_before / night_not_after / morning_not_before /
morning_not_after are OPTIONAL bounds on NightDayLifecycleConfig, applied by
the pure helper clamp_time() as the LAST step inside _active_profile() —
after both the weekday/weekend profile selection and the T2 sun-event
override resolution. By the time _evaluate_trigger() runs, profile.
night_fixed_time / morning_fixed_time already IS the final clamped value,
indistinguishable from an unclamped fixed time — exactly the same
architectural pattern T2 established for sun events. No branch anywhere
needs to know whether a clamp happened, and BOTH automatically compares
against the clamped time on its time side with zero extra code.

Coverage:
  CL-01  No clamp configured -> fixed_time unchanged.
  CL-02  No clamp configured -> sun event unchanged.
  CL-03  not_before shifts a too-early fixed-time trigger.
  CL-04  not_before shifts a too-early sun event.
  CL-05  not_after pulls forward a too-late fixed-time trigger.
  CL-06  not_after pulls forward a too-late sun event.
  CL-07  Time within both bounds is unchanged.
  CL-08  Time before both bounds clamps to not_before.
  CL-09  Time after both bounds clamps to not_after.
  CL-10  not_before == not_after collapses to a fixed trigger time.
  CL-11  Inverted window (not_before > not_after) is a fail-safe no-clamp
         at the engine level (the OptionsFlow rejects and never stores one).
  CL-12  Night-only clamp (morning untouched).
  CL-13  Morning-only clamp (night untouched).
  CL-14  Different clamp values for night vs. morning simultaneously.
  CL-15  BOTH automatically uses the clamped time on its time side.
  CL-16  Compatible with active_months (T1).
  CL-17  Compatible with WEEKDAY_WEEKEND schedule_mode (clamp is shared).
  CL-18  Restart before the clamped trigger time.
  CL-19  Restart after the clamped trigger time (carryover).
  CL-20  Carryover logic remains correct with a clamp configured.
  CL-21  Missing sun-event data never produces a synthetic clamped trigger.
  CL-22  Storage round-trip: explicit bounds survive serialize/deserialize.
  CL-23  Storage round-trip: pre-T3 configs without the new keys default to
         None (no restriction) — full backward compatibility.
  CL-24  Invalid/malformed stored time values fall back safely to None.
  CL-25  const.py CONF_* keys for the new fields are distinct and stable —
         config_flow.py itself is not importable under this repo's HA-stub
         test harness (it imports the real homeassistant.helpers.selector;
         no test file in this repo imports config_flow.py, T1/T2 included),
         so schema-submission-level OptionsFlow coverage is out of scope
         here, same as it was for T1/T2.
  CL-26  clamp_time is pure wall-clock time.time comparison — no date/tz
         dependency, so a bound holds identically across a DST-adjacent date.
  CL-27  Bug-injection sanity check: prove clamp_time is actually applied,
         not a tautology.
  CL-28  No regression: T1 (active_months) and T2 (sun_event) keep working
         unchanged when no clamp is configured, and all three features
         compose correctly together.
"""
from __future__ import annotations

from datetime import datetime, time, timezone

from custom_components.smartshading.config_entry_data import (
    _lifecycle_config_from_storage,
    to_storage_dict,
    SmartShadingConfigEntryData,
)
from custom_components.smartshading.const import (
    CONF_MORNING_NOT_AFTER,
    CONF_MORNING_NOT_BEFORE,
    CONF_NIGHT_NOT_AFTER,
    CONF_NIGHT_NOT_BEFORE,
)
from custom_components.smartshading.engines.lifecycle_engine import (
    LifecycleEngine,
    SunEventTimes,
    clamp_time,
)
from custom_components.smartshading.models.lifecycle import (
    LifecycleScheduleMode,
    LifecycleState,
    MorningTrigger,
    NightDayLifecycleConfig,
    NightTrigger,
    SunEvent,
)

_UTC = timezone.utc


def _local(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, 0, tzinfo=_UTC)


def _config(**overrides) -> NightDayLifecycleConfig:
    """FIXED_TIME triggers with clamp bounds set on both sides by default."""
    defaults = dict(
        id="test",
        night_enabled=True,
        night_trigger=NightTrigger.FIXED_TIME,
        night_fixed_time=time(22, 0),
        night_not_before=time(21, 0),
        night_not_after=time(23, 0),
        morning_enabled=True,
        morning_trigger=MorningTrigger.FIXED_TIME,
        morning_fixed_time=time(6, 30),
        morning_not_before=time(6, 0),
        morning_not_after=time(7, 30),
    )
    defaults.update(overrides)
    return NightDayLifecycleConfig(**defaults)


# ---------------------------------------------------------------------------
# CL-01 / CL-02 — no clamp configured leaves the resolved time unchanged.
# ---------------------------------------------------------------------------

class TestNoClampUnchanged:
    def test_fixed_time_unclamped(self):
        assert clamp_time(time(22, 0), None, None) == time(22, 0)

    def test_sun_event_unclamped(self):
        engine = LifecycleEngine()
        cfg = NightDayLifecycleConfig(
            id="t", night_enabled=True, night_trigger=NightTrigger.FIXED_TIME,
            night_sun_event=SunEvent.SUNSET, morning_enabled=False,
        )
        sun_times = SunEventTimes(next_sunset=_local(6, 15, 21, 15))
        profile = engine.active_profile(_local(6, 15, 12, 0), cfg, sun_times)
        assert profile.night_fixed_time == time(21, 15)


# ---------------------------------------------------------------------------
# CL-03 / CL-04 — not_before shifts a too-early trigger.
# ---------------------------------------------------------------------------

class TestNotBeforeShiftsEarlyTrigger:
    def test_not_before_shifts_fixed_time(self):
        assert clamp_time(time(5, 20), time(7, 30), None) == time(7, 30)

    def test_not_before_shifts_sun_event(self):
        engine = LifecycleEngine()
        cfg = NightDayLifecycleConfig(
            id="t", night_enabled=False, morning_enabled=True,
            morning_trigger=MorningTrigger.FIXED_TIME,
            morning_sun_event=SunEvent.SUNRISE,
            morning_not_before=time(7, 30),
        )
        sun_times = SunEventTimes(next_sunrise=_local(6, 15, 5, 20))
        profile = engine.active_profile(_local(6, 15, 4, 0), cfg, sun_times)
        assert profile.morning_fixed_time == time(7, 30)


# ---------------------------------------------------------------------------
# CL-05 / CL-06 — not_after pulls forward a too-late trigger.
# ---------------------------------------------------------------------------

class TestNotAfterPullsBackLateTrigger:
    def test_not_after_pulls_back_fixed_time(self):
        assert clamp_time(time(9, 10), None, time(8, 30)) == time(8, 30)

    def test_not_after_pulls_back_sun_event(self):
        engine = LifecycleEngine()
        cfg = NightDayLifecycleConfig(
            id="t", night_enabled=False, morning_enabled=True,
            morning_trigger=MorningTrigger.FIXED_TIME,
            morning_sun_event=SunEvent.SUNRISE,
            morning_not_after=time(8, 30),
        )
        sun_times = SunEventTimes(next_sunrise=_local(6, 15, 9, 10))
        profile = engine.active_profile(_local(6, 15, 4, 0), cfg, sun_times)
        assert profile.morning_fixed_time == time(8, 30)


# ---------------------------------------------------------------------------
# CL-07 .. CL-10 — within-window / boundary semantics.
# ---------------------------------------------------------------------------

class TestWindowSemantics:
    def test_within_both_bounds_unchanged(self):
        assert clamp_time(time(7, 55), time(7, 30), time(8, 30)) == time(7, 55)

    def test_before_both_bounds_clamps_to_not_before(self):
        assert clamp_time(time(5, 0), time(7, 30), time(8, 30)) == time(7, 30)

    def test_after_both_bounds_clamps_to_not_after(self):
        assert clamp_time(time(9, 0), time(7, 30), time(8, 30)) == time(8, 30)

    def test_not_before_equals_not_after_collapses_to_fixed_time(self):
        assert clamp_time(time(5, 0), time(7, 30), time(7, 30)) == time(7, 30)
        assert clamp_time(time(9, 0), time(7, 30), time(7, 30)) == time(7, 30)
        assert clamp_time(time(7, 30), time(7, 30), time(7, 30)) == time(7, 30)


# ---------------------------------------------------------------------------
# CL-11 — inverted window fail-safe.
# ---------------------------------------------------------------------------

class TestInvertedWindowFailSafe:
    def test_not_before_greater_than_not_after_returns_unclamped(self):
        """The OptionsFlow validates and refuses to store not_before >
        not_after (see config_flow.py night_clamp_window_invalid /
        morning_clamp_window_invalid), but clamp_time() itself must never
        guess an interpretation if corrupted/hand-edited storage ever
        contains one anyway — "no clamp" is the only outcome that cannot
        itself introduce a new bug."""
        assert clamp_time(time(22, 0), time(23, 0), time(21, 0)) == time(22, 0)
        assert clamp_time(time(20, 0), time(23, 0), time(21, 0)) == time(20, 0)


# ---------------------------------------------------------------------------
# CL-12 .. CL-14 — night/morning independence.
# ---------------------------------------------------------------------------

class TestNightMorningIndependence:
    def test_night_only_clamp_leaves_morning_untouched(self):
        engine = LifecycleEngine()
        cfg = _config(morning_not_before=None, morning_not_after=None)
        profile = engine.active_profile(_local(6, 15, 12, 0), cfg)
        assert profile.night_fixed_time == time(22, 0)  # within [21:00, 23:00] -> unchanged
        assert profile.morning_fixed_time == time(6, 30)  # unclamped default preserved

    def test_morning_only_clamp_leaves_night_untouched(self):
        engine = LifecycleEngine()
        cfg = _config(night_not_before=None, night_not_after=None, night_fixed_time=time(5, 0))
        profile = engine.active_profile(_local(6, 15, 12, 0), cfg)
        assert profile.night_fixed_time == time(5, 0)  # unclamped
        assert profile.morning_fixed_time == time(6, 30)  # within [6:00, 7:30] -> unchanged

    def test_different_clamp_values_for_night_and_morning(self):
        engine = LifecycleEngine()
        cfg = _config(
            night_fixed_time=time(20, 0), night_not_before=time(21, 30), night_not_after=None,
            morning_fixed_time=time(8, 0), morning_not_before=None, morning_not_after=time(7, 0),
        )
        profile = engine.active_profile(_local(6, 15, 12, 0), cfg)
        assert profile.night_fixed_time == time(21, 30)
        assert profile.morning_fixed_time == time(7, 0)


# ---------------------------------------------------------------------------
# CL-15 — BOTH uses the clamped time on its time side.
# ---------------------------------------------------------------------------

class TestBothUsesClampedTime:
    def test_both_fires_via_clamped_time_when_elevation_not_met(self):
        engine = LifecycleEngine()
        cfg = NightDayLifecycleConfig(
            id="t", night_enabled=True, night_trigger=NightTrigger.BOTH,
            night_sun_elevation_deg=-10.0,  # strict — elevation not yet met
            night_fixed_time=time(20, 0), night_not_before=time(21, 30),
            morning_enabled=True, morning_trigger=MorningTrigger.DISABLED,
        )
        # 21:35 >= clamped 21:30 (not >= raw 20:00 only) — clamp is what fires it.
        now = _local(6, 15, 21, 35)
        state = engine.get_lifecycle_state(now, -3.0, cfg, LifecycleState.DAY, None)
        assert state is LifecycleState.NIGHT

    def test_both_does_not_fire_before_clamped_time(self):
        engine = LifecycleEngine()
        cfg = NightDayLifecycleConfig(
            id="t", night_enabled=True, night_trigger=NightTrigger.BOTH,
            night_sun_elevation_deg=-10.0,
            night_fixed_time=time(20, 0), night_not_before=time(21, 30),
            morning_enabled=True, morning_trigger=MorningTrigger.DISABLED,
        )
        # 20:30 has passed the raw fixed_time (20:00) but not the clamp (21:30).
        now = _local(6, 15, 20, 30)
        state = engine.get_lifecycle_state(now, -3.0, cfg, LifecycleState.DAY, None)
        assert state is LifecycleState.DAY


# ---------------------------------------------------------------------------
# CL-16 — compatible with active_months (T1).
# ---------------------------------------------------------------------------

class TestCompatibleWithActiveMonths:
    def test_clamp_still_applies_in_active_month(self):
        engine = LifecycleEngine()
        cfg = _config(night_fixed_time=time(20, 0), active_months=[6, 7, 8])
        now = _local(6, 15, 21, 5)  # >= clamped 21:00, June is active
        state = engine.get_lifecycle_state(now, None, cfg, LifecycleState.DAY, None)
        assert state is LifecycleState.NIGHT

    def test_clamp_irrelevant_outside_active_month(self):
        engine = LifecycleEngine()
        cfg = _config(night_fixed_time=time(20, 0), active_months=[12, 1, 2])
        now = _local(6, 15, 23, 30)  # well past clamped 23:00, but June is inactive
        state = engine.get_lifecycle_state(now, None, cfg, LifecycleState.DAY, None)
        assert state is LifecycleState.DAY


# ---------------------------------------------------------------------------
# CL-17 — compatible with WEEKDAY_WEEKEND (clamp is shared, not per-profile).
# ---------------------------------------------------------------------------

class TestCompatibleWithWeekdayWeekend:
    def test_clamp_applies_identically_on_weekday_and_weekend(self):
        engine = LifecycleEngine()
        cfg = NightDayLifecycleConfig(
            id="t", schedule_mode=LifecycleScheduleMode.WEEKDAY_WEEKEND,
            night_enabled=True, night_trigger=NightTrigger.FIXED_TIME,
            weekday_night_fixed_time=time(20, 0), weekend_night_fixed_time=time(19, 0),
            night_not_before=time(21, 0),
            morning_enabled=False,
        )
        # 2026-09-18 is a Friday (weekday), 2026-09-19 is a Saturday (weekend).
        weekday_profile = engine.active_profile(_local(9, 18, 12, 0), cfg)
        weekend_profile = engine.active_profile(_local(9, 19, 12, 0), cfg)
        assert weekday_profile.night_fixed_time == time(21, 0)  # 20:00 clamped up
        assert weekend_profile.night_fixed_time == time(21, 0)  # 19:00 clamped up, same bound


# ---------------------------------------------------------------------------
# CL-18 / CL-19 / CL-20 — restart before/after the clamped trigger.
# ---------------------------------------------------------------------------

class TestRestartAroundClampedTrigger:
    def test_restart_before_clamped_trigger_stays_day(self):
        engine = LifecycleEngine()
        cfg = _config(night_fixed_time=time(20, 0), night_not_before=time(21, 30))
        now = _local(6, 15, 21, 0)  # after raw fixed_time, before the clamp
        state = engine.get_lifecycle_state(now, None, cfg, LifecycleState.DAY, None)
        assert state is LifecycleState.DAY

    def test_restart_after_clamped_trigger_bootstraps_night(self):
        """Fresh restart just after midnight, after last night's clamped
        trigger fired but before this morning's trigger -> carryover."""
        engine = LifecycleEngine()
        cfg = _config(
            night_fixed_time=time(20, 0), night_not_before=time(21, 30),
            morning_fixed_time=time(6, 30), morning_not_before=None, morning_not_after=None,
        )
        now = _local(6, 16, 0, 15)  # post-midnight, before morning
        state = engine.get_lifecycle_state(now, None, cfg, LifecycleState.DAY, None)
        assert state is LifecycleState.NIGHT

    def test_carryover_respects_clamped_morning_trigger(self):
        """Carryover must not release to DAY/MORNING before the clamped
        morning time, even though the raw morning_fixed_time has passed."""
        engine = LifecycleEngine()
        cfg = _config(
            night_fixed_time=time(20, 0), night_not_before=None, night_not_after=None,
            morning_fixed_time=time(5, 0), morning_not_before=time(6, 30), morning_not_after=None,
        )
        now = _local(6, 16, 5, 30)  # past raw morning (5:00) but before clamp (6:30)
        state = engine.get_lifecycle_state(now, None, cfg, LifecycleState.NIGHT, None)
        assert state is LifecycleState.NIGHT  # still waiting for the clamped morning trigger

        now_after_clamp = _local(6, 16, 6, 45)
        state_after = engine.get_lifecycle_state(now_after_clamp, None, cfg, LifecycleState.NIGHT, None)
        assert state_after is LifecycleState.MORNING


# ---------------------------------------------------------------------------
# CL-21 — missing sun-event data never produces a synthetic clamped trigger.
# ---------------------------------------------------------------------------

class TestMissingSunEventDataNoSyntheticTrigger:
    def test_absent_sun_event_data_stays_unresolved_after_clamp(self):
        engine = LifecycleEngine()
        cfg = NightDayLifecycleConfig(
            id="t", night_enabled=True, night_trigger=NightTrigger.FIXED_TIME,
            night_sun_event=SunEvent.SUNSET,
            night_not_before=time(0, 0), night_not_after=time(23, 59),  # would clamp ANY real time
            morning_enabled=False,
        )
        # sun_event_times omitted entirely -> night_fixed_time resolves to None
        # before clamp_time() ever runs; clamp_time(None, ...) must stay None.
        profile = engine.active_profile(_local(6, 15, 12, 0), cfg, None)
        assert profile.night_fixed_time is None
        state = engine.get_lifecycle_state(_local(6, 15, 23, 0), None, cfg, LifecycleState.DAY, None)
        assert state is LifecycleState.DAY

    def test_clamp_time_with_none_base_returns_none_regardless_of_bounds(self):
        assert clamp_time(None, time(0, 0), time(23, 59)) is None
        assert clamp_time(None, None, None) is None


# ---------------------------------------------------------------------------
# CL-22 / CL-23 / CL-24 — storage round-trip.
# ---------------------------------------------------------------------------

class TestStorageRoundTrip:
    def test_explicit_bounds_survive_round_trip(self):
        data = SmartShadingConfigEntryData(
            name="Test", use_home_location=True,
            lifecycle_config=NightDayLifecycleConfig(
                id="default",
                night_not_before=time(21, 0), night_not_after=time(23, 0),
                morning_not_before=time(6, 0), morning_not_after=time(7, 30),
            ),
        )
        stored = to_storage_dict(data)
        lc = stored["lifecycle_config"]
        assert lc["night_not_before"] == "21:00:00"
        assert lc["night_not_after"] == "23:00:00"
        assert lc["morning_not_before"] == "06:00:00"
        assert lc["morning_not_after"] == "07:30:00"
        restored = _lifecycle_config_from_storage(lc)
        assert restored.night_not_before == time(21, 0)
        assert restored.night_not_after == time(23, 0)
        assert restored.morning_not_before == time(6, 0)
        assert restored.morning_not_after == time(7, 30)

    def test_missing_keys_default_to_none(self):
        """Pre-T3 configs (no clamp keys at all) -> unrestricted, byte-for-
        byte the pre-T3 behavior."""
        raw = {"id": "default", "night_enabled": True}
        cfg = _lifecycle_config_from_storage(raw)
        assert cfg.night_not_before is None
        assert cfg.night_not_after is None
        assert cfg.morning_not_before is None
        assert cfg.morning_not_after is None

    def test_malformed_stored_time_falls_back_to_none(self):
        raw = {
            "id": "default",
            "night_not_before": "not-a-time",
            "night_not_after": 12345,
            "morning_not_before": None,
            "morning_not_after": "",
        }
        cfg = _lifecycle_config_from_storage(raw)
        assert cfg.night_not_before is None
        assert cfg.night_not_after is None
        assert cfg.morning_not_before is None
        assert cfg.morning_not_after is None


# ---------------------------------------------------------------------------
# CL-25 — const.py CONF_* keys for the new clamp fields.
#
# config_flow.py is not importable under this repo's HA-stub test harness
# (it imports the real homeassistant.helpers.selector; no test file in this
# repo imports config_flow.py, T1/T2 included — see conftest.py, which only
# stubs modules other test files actually need). Schema-submission-level
# OptionsFlow coverage is therefore out of scope here, same as for T1/T2.
# This covers the one piece of the OptionsFlow wiring that IS import-safe:
# the storage key constants themselves.
# ---------------------------------------------------------------------------

class TestConfigFlowConfKeys:
    def test_conf_keys_are_distinct_and_expected(self):
        assert {CONF_NIGHT_NOT_BEFORE, CONF_NIGHT_NOT_AFTER, CONF_MORNING_NOT_BEFORE, CONF_MORNING_NOT_AFTER} == {
            "night_not_before", "night_not_after", "morning_not_before", "morning_not_after",
        }


# ---------------------------------------------------------------------------
# CL-26 — no date/timezone dependency (DST-adjacent boundary).
# ---------------------------------------------------------------------------

class TestNoDstOrTimezoneDependency:
    def test_clamp_holds_identically_across_a_dst_adjacent_date(self):
        """clamp_time operates purely on datetime.time (wall-clock, no date/
        tzinfo component) — the same bound must produce the same result
        regardless of which calendar date (e.g. either side of a DST
        transition) the resolved time came from."""
        engine = LifecycleEngine()
        cfg = _config(night_fixed_time=time(20, 0), night_not_before=time(21, 30))
        before_dst = engine.active_profile(_local(3, 28, 12, 0), cfg)  # before EU DST switch
        after_dst = engine.active_profile(_local(3, 30, 12, 0), cfg)   # after EU DST switch
        assert before_dst.night_fixed_time == time(21, 30)
        assert after_dst.night_fixed_time == time(21, 30)


# ---------------------------------------------------------------------------
# CL-27 — bug-injection sanity check.
# ---------------------------------------------------------------------------

class TestSanityCheck:
    def test_clamp_actually_changes_the_outcome(self):
        """Prove clamp_time is actually driving the result: an otherwise
        identical scenario flips from DAY to NIGHT purely because a
        not_before bound is added, at a moment strictly between the raw
        fixed_time and the clamp bound."""
        engine = LifecycleEngine()
        now = _local(6, 15, 20, 45)  # between raw 20:00 and clamp 21:00
        cfg_unclamped = _config(
            night_fixed_time=time(20, 0), night_not_before=None, night_not_after=None,
        )
        cfg_clamped = _config(
            night_fixed_time=time(20, 0), night_not_before=time(21, 0), night_not_after=None,
        )
        state_unclamped = engine.get_lifecycle_state(now, None, cfg_unclamped, LifecycleState.DAY, None)
        state_clamped = engine.get_lifecycle_state(now, None, cfg_clamped, LifecycleState.DAY, None)
        assert state_unclamped is LifecycleState.NIGHT   # raw fixed_time already passed
        assert state_clamped is LifecycleState.DAY        # clamp defers it to 21:00
        assert state_unclamped != state_clamped


# ---------------------------------------------------------------------------
# CL-28 — no regression: T1 + T2 + T3 compose correctly together.
# ---------------------------------------------------------------------------

class TestNoRegressionCombinedFeatures:
    def test_active_months_and_sun_event_unaffected_when_clamp_unset(self):
        engine = LifecycleEngine()
        cfg = NightDayLifecycleConfig(
            id="t", night_enabled=True, night_trigger=NightTrigger.FIXED_TIME,
            night_sun_event=SunEvent.SUNSET, active_months=[6, 7, 8],
            morning_enabled=False,
        )
        sun_times = SunEventTimes(next_sunset=_local(6, 15, 21, 15))
        state = engine.get_lifecycle_state(_local(6, 15, 21, 30), None, cfg, LifecycleState.DAY, sun_times)
        assert state is LifecycleState.NIGHT

    def test_active_months_sun_event_and_clamp_compose_correctly(self):
        engine = LifecycleEngine()
        cfg = NightDayLifecycleConfig(
            id="t", night_enabled=True, night_trigger=NightTrigger.FIXED_TIME,
            night_sun_event=SunEvent.SUNSET, night_not_before=time(22, 0),
            active_months=[6, 7, 8], morning_enabled=False,
        )
        sun_times = SunEventTimes(next_sunset=_local(6, 15, 21, 15))  # would fire at 21:15
        # Before the clamp: still DAY even though sunset already passed.
        early = engine.get_lifecycle_state(_local(6, 15, 21, 30), None, cfg, LifecycleState.DAY, sun_times)
        assert early is LifecycleState.DAY
        # At/after the clamp: NIGHT.
        late = engine.get_lifecycle_state(_local(6, 15, 22, 5), None, cfg, LifecycleState.DAY, sun_times)
        assert late is LifecycleState.NIGHT
        # Outside active_months: stays DAY regardless of the clamped time.
        inactive_month = engine.get_lifecycle_state(_local(1, 15, 23, 0), None, cfg, LifecycleState.DAY, sun_times)
        assert inactive_month is LifecycleState.DAY
