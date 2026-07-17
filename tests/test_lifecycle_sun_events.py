"""Selectable sun events per lifecycle schedule — v1.2.0-beta.1, Beta.1-T2.

Architecture (post architecture-review, before push): night_sun_event /
morning_sun_event are OPTIONAL OVERRIDES on NightDayLifecycleConfig, not a
5th value on NightTrigger/MorningTrigger. NightTrigger/MorningTrigger stay
at their original 4 values (DISABLED/SUN_ELEVATION/FIXED_TIME/BOTH),
unchanged since before this beta. None (default) leaves night_fixed_time/
morning_fixed_time exactly as entered. Set to a SunEvent, it resolves that
astronomical event (via HA's sun.sun `next_rising`/`next_setting`/
`next_dawn`/`next_dusk` attributes) into the SAME night_fixed_time /
morning_fixed_time slot every trigger type already compares against — so
_check_night_trigger(), _check_morning_trigger(), _is_night_carryover(),
and _evaluate_trigger() need zero sun-event-specific branches, and BOTH
(elevation OR time) automatically gets "elevation OR sun event" without a
new enum value.

Coverage:
  SE-01  Default behavior (no sun_event override) is unchanged.
  SE-02  Sunrise override as MORNING trigger (via FIXED_TIME).
  SE-03  Sunset override as NIGHT trigger (via FIXED_TIME).
  SE-04  Dawn override as MORNING trigger.
  SE-05  Dusk override as NIGHT trigger.
  SE-06  Different events selected for night vs. morning simultaneously.
  SE-07  Compatible with active_months (T1).
  SE-08  Compatible with WEEKDAY_WEEKEND schedule_mode (sun events shared,
         not weekday/weekend-specific).
  SE-09  Restart after the event has already happened today (carryover).
  SE-10  Restart before a still-pending future event.
  SE-11  Missing/unavailable sun data does not cause a false trigger.
  SE-12  Storage round-trip: explicit sun events survive serialize/deserialize.
  SE-13  Storage round-trip: pre-beta configs without the new keys default
         correctly (backward compatibility) — and explicit None round-trips too.
  SE-14  const.py: NightTrigger/MorningTrigger enum values are UNCHANGED
         (still exactly 4) — the core architectural claim of this design.
  SE-15  Invalid/unknown stored enum values fall back safely (to None), never raise.
  SE-16  BOTH automatically uses a configured sun_event as its time side —
         the exact limitation the earlier (superseded) design had.
  SE-17  Midnight-boundary event resolution (event just after midnight).
  SE-18  Bug-injection sanity check: prove the resolution mechanism is real,
         not a tautology.
"""
from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from custom_components.smartshading.config_entry_data import (
    _lifecycle_config_from_storage,
    to_storage_dict,
    SmartShadingConfigEntryData,
)
from custom_components.smartshading.const import LIFECYCLE_TRIGGER_OPTIONS, SUN_EVENT_OPTIONS
from custom_components.smartshading.engines.lifecycle_engine import (
    LifecycleEngine,
    SunEventTimes,
    _resolve_sun_event_time,
    check_night_interval_active,
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
    """FIXED_TIME triggers (the mechanism the sun-event override plugs into)
    with a sun_event override set for both directions by default."""
    defaults = dict(
        id="test",
        night_enabled=True,
        night_trigger=NightTrigger.FIXED_TIME,
        night_sun_event=SunEvent.SUNSET,
        morning_enabled=True,
        morning_trigger=MorningTrigger.FIXED_TIME,
        morning_sun_event=SunEvent.SUNRISE,
    )
    defaults.update(overrides)
    return NightDayLifecycleConfig(**defaults)


# ---------------------------------------------------------------------------
# SE-01 — default behavior unchanged when no override is configured.
# ---------------------------------------------------------------------------

class TestDefaultBehaviorUnchanged:
    def test_fixed_time_config_without_override_ignores_sun_event_times(self):
        """A plain FIXED_TIME config (no sun_event override) must behave
        identically whether or not sun_event_times is supplied."""
        engine = LifecycleEngine()
        cfg = NightDayLifecycleConfig(
            id="t", night_enabled=True, night_trigger=NightTrigger.FIXED_TIME,
            night_fixed_time=time(22, 0), morning_enabled=True,
            morning_trigger=MorningTrigger.FIXED_TIME, morning_fixed_time=time(6, 0),
        )
        now = _local(6, 15, 23, 0)
        sun_times = SunEventTimes(
            next_sunrise=_local(6, 16, 5, 0), next_sunset=_local(6, 15, 6, 0),
            next_dawn=None, next_dusk=None,
        )
        without = engine.get_lifecycle_state(now, None, cfg, LifecycleState.DAY, None)
        with_times = engine.get_lifecycle_state(now, None, cfg, LifecycleState.DAY, sun_times)
        assert without == with_times == LifecycleState.NIGHT

    def test_night_sun_event_default_is_none(self):
        assert NightDayLifecycleConfig(id="x").night_sun_event is None

    def test_morning_sun_event_default_is_none(self):
        assert NightDayLifecycleConfig(id="x").morning_sun_event is None


# ---------------------------------------------------------------------------
# SE-02..SE-05 — each event type fires its respective trigger via the override.
# ---------------------------------------------------------------------------

class TestEventTypesFireCorrectly:
    def test_sunrise_override_as_morning_trigger(self):
        engine = LifecycleEngine()
        cfg = _config(
            night_trigger=NightTrigger.DISABLED,
            morning_trigger=MorningTrigger.FIXED_TIME, morning_sun_event=SunEvent.SUNRISE,
        )
        now = _local(6, 15, 7, 0)
        sun_times = SunEventTimes(next_sunrise=_local(6, 16, 5, 30))  # already passed today
        state = engine.get_lifecycle_state(now, None, cfg, LifecycleState.NIGHT, sun_times)
        assert state is LifecycleState.MORNING

    def test_sunset_override_as_night_trigger(self):
        engine = LifecycleEngine()
        cfg = _config(
            night_trigger=NightTrigger.FIXED_TIME, night_sun_event=SunEvent.SUNSET,
            morning_trigger=MorningTrigger.DISABLED,
        )
        now = _local(6, 15, 22, 0)
        sun_times = SunEventTimes(next_sunset=_local(6, 16, 21, 30))  # already passed today
        state = engine.get_lifecycle_state(now, None, cfg, LifecycleState.DAY, sun_times)
        assert state is LifecycleState.NIGHT

    def test_dawn_override_as_morning_trigger(self):
        engine = LifecycleEngine()
        cfg = _config(
            night_trigger=NightTrigger.DISABLED,
            morning_trigger=MorningTrigger.FIXED_TIME, morning_sun_event=SunEvent.DAWN,
        )
        now = _local(6, 15, 5, 30)
        sun_times = SunEventTimes(next_dawn=_local(6, 16, 4, 45))  # already passed today
        state = engine.get_lifecycle_state(now, None, cfg, LifecycleState.NIGHT, sun_times)
        assert state is LifecycleState.MORNING

    def test_dusk_override_as_night_trigger(self):
        engine = LifecycleEngine()
        cfg = _config(
            night_trigger=NightTrigger.FIXED_TIME, night_sun_event=SunEvent.DUSK,
            morning_trigger=MorningTrigger.DISABLED,
        )
        now = _local(6, 15, 22, 30)
        sun_times = SunEventTimes(next_dusk=_local(6, 16, 22, 0))  # already passed today
        state = engine.get_lifecycle_state(now, None, cfg, LifecycleState.DAY, sun_times)
        assert state is LifecycleState.NIGHT


# ---------------------------------------------------------------------------
# SE-06 — independent selection for night and morning.
# ---------------------------------------------------------------------------

class TestIndependentNightMorningEvents:
    def test_night_dusk_morning_sunrise(self):
        engine = LifecycleEngine()
        cfg = _config(night_sun_event=SunEvent.DUSK, morning_sun_event=SunEvent.SUNRISE)
        # Evening: dusk already passed -> NIGHT.
        evening = _local(9, 20, 20, 0)
        sun_times_evening = SunEventTimes(
            next_dusk=_local(9, 21, 19, 30), next_sunrise=_local(9, 21, 7, 0),
        )
        assert engine.get_lifecycle_state(
            evening, None, cfg, LifecycleState.DAY, sun_times_evening
        ) is LifecycleState.NIGHT

        # Morning: sunrise already passed -> MORNING.
        morning = _local(9, 21, 7, 30)
        sun_times_morning = SunEventTimes(
            next_dusk=_local(9, 21, 19, 25), next_sunrise=_local(9, 22, 7, 2),
        )
        assert engine.get_lifecycle_state(
            morning, None, cfg, LifecycleState.NIGHT, sun_times_morning
        ) is LifecycleState.MORNING


# ---------------------------------------------------------------------------
# SE-07 — compatible with active_months (T1).
# ---------------------------------------------------------------------------

class TestCompatibleWithActiveMonths:
    def test_sun_event_override_respects_inactive_month(self):
        engine = LifecycleEngine()
        cfg = _config(active_months=[9, 10, 11, 12, 1, 2, 3])
        now = _local(6, 15, 23, 0)  # June — not in active_months
        sun_times = SunEventTimes(next_sunset=_local(6, 16, 21, 30))
        state = engine.get_lifecycle_state(now, None, cfg, LifecycleState.DAY, sun_times)
        assert state is LifecycleState.DAY

    def test_sun_event_override_fires_in_active_month(self):
        engine = LifecycleEngine()
        cfg = _config(active_months=[9, 10, 11, 12, 1, 2, 3])
        now = _local(12, 15, 20, 0)  # December — in active_months
        sun_times = SunEventTimes(next_sunset=_local(12, 16, 16, 30))
        state = engine.get_lifecycle_state(now, None, cfg, LifecycleState.DAY, sun_times)
        assert state is LifecycleState.NIGHT


# ---------------------------------------------------------------------------
# SE-08 — compatible with WEEKDAY_WEEKEND (sun events are shared, not split).
# ---------------------------------------------------------------------------

class TestCompatibleWithWeekdayWeekend:
    def test_sun_event_resolves_identically_on_weekday_and_weekend(self):
        engine = LifecycleEngine()
        cfg = _config(schedule_mode=LifecycleScheduleMode.WEEKDAY_WEEKEND)
        # 2026-09-19 is a Saturday (weekend), 2026-09-18 is a Friday (weekday).
        # Each next_sunset is dated to match its own "now" (still pending
        # today), matching HA's actual next_* invariant.
        weekend_sun_times = SunEventTimes(next_sunset=_local(9, 19, 19, 30))
        weekday_sun_times = SunEventTimes(next_sunset=_local(9, 18, 19, 32))
        weekend_profile = engine.active_profile(_local(9, 19, 12, 0), cfg, weekend_sun_times)
        weekday_profile = engine.active_profile(_local(9, 18, 12, 0), cfg, weekday_sun_times)
        # Same resolution mechanism regardless of weekday/weekend branch —
        # both correctly pick up their own day's still-pending sunset time.
        assert weekend_profile.night_fixed_time == time(19, 30)
        assert weekday_profile.night_fixed_time == time(19, 32)


# ---------------------------------------------------------------------------
# SE-09 / SE-10 — restart around an event.
# ---------------------------------------------------------------------------

class TestRestartAroundEvent:
    def test_restart_after_event_already_passed_bootstraps_night(self):
        """Fresh restart (previous=DAY) shortly after sunset already fired —
        must bootstrap NIGHT via the carryover path, not wait until tomorrow."""
        engine = LifecycleEngine()
        cfg = _config(
            night_trigger=NightTrigger.FIXED_TIME, night_sun_event=SunEvent.SUNSET,
            morning_trigger=MorningTrigger.FIXED_TIME, morning_fixed_time=time(6, 30),
            morning_sun_event=None,
        )
        now = _local(6, 16, 0, 20)  # post-midnight, before morning
        sun_times = SunEventTimes(next_sunset=_local(6, 16, 21, 40))  # already tonight's next -> passed
        state = engine.get_lifecycle_state(now, None, cfg, LifecycleState.DAY, sun_times)
        assert state is LifecycleState.NIGHT

    def test_restart_before_future_event_stays_day(self):
        engine = LifecycleEngine()
        cfg = _config(
            night_trigger=NightTrigger.FIXED_TIME, night_sun_event=SunEvent.SUNSET,
            morning_trigger=MorningTrigger.FIXED_TIME, morning_fixed_time=time(6, 30),
            morning_sun_event=None,
        )
        now = _local(6, 15, 18, 0)  # before tonight's sunset
        sun_times = SunEventTimes(next_sunset=_local(6, 15, 21, 40))  # still pending today
        state = engine.get_lifecycle_state(now, None, cfg, LifecycleState.DAY, sun_times)
        assert state is LifecycleState.DAY


# ---------------------------------------------------------------------------
# SE-11 — missing sun data never causes a false trigger.
# ---------------------------------------------------------------------------

class TestMissingSunDataFailsSafe:
    def test_sun_event_times_none_never_fires(self):
        engine = LifecycleEngine()
        cfg = _config()
        state = engine.get_lifecycle_state(_local(6, 15, 23, 0), None, cfg, LifecycleState.DAY, None)
        assert state is LifecycleState.DAY

    def test_specific_field_none_never_fires(self):
        engine = LifecycleEngine()
        cfg = _config(night_trigger=NightTrigger.FIXED_TIME, night_sun_event=SunEvent.DUSK)
        sun_times = SunEventTimes(next_dusk=None)  # dusk unavailable this cycle
        state = engine.get_lifecycle_state(_local(6, 15, 23, 0), None, cfg, LifecycleState.DAY, sun_times)
        assert state is LifecycleState.DAY

    def test_resolve_sun_event_time_none_input_returns_none(self):
        assert _resolve_sun_event_time(_local(6, 15, 12, 0), None) is None

    def test_check_night_interval_active_with_missing_data(self):
        cfg = _config(night_trigger=NightTrigger.FIXED_TIME, night_sun_event=SunEvent.SUNSET)
        assert check_night_interval_active(_local(6, 15, 23, 0), None, cfg, None) is False


# ---------------------------------------------------------------------------
# SE-12 / SE-13 — storage round-trip.
# ---------------------------------------------------------------------------

class TestStorageRoundTrip:
    def test_explicit_sun_events_survive_round_trip(self):
        data = SmartShadingConfigEntryData(
            name="Test", use_home_location=True,
            lifecycle_config=NightDayLifecycleConfig(
                id="default", night_sun_event=SunEvent.DUSK, morning_sun_event=SunEvent.DAWN,
            ),
        )
        stored = to_storage_dict(data)
        restored = _lifecycle_config_from_storage(stored["lifecycle_config"])
        assert restored.night_sun_event is SunEvent.DUSK
        assert restored.morning_sun_event is SunEvent.DAWN

    def test_explicit_none_survives_round_trip(self):
        data = SmartShadingConfigEntryData(
            name="Test", use_home_location=True,
            lifecycle_config=NightDayLifecycleConfig(
                id="default", night_sun_event=None, morning_sun_event=None,
            ),
        )
        stored = to_storage_dict(data)
        assert stored["lifecycle_config"]["night_sun_event"] is None
        assert stored["lifecycle_config"]["morning_sun_event"] is None
        restored = _lifecycle_config_from_storage(stored["lifecycle_config"])
        assert restored.night_sun_event is None
        assert restored.morning_sun_event is None

    def test_missing_keys_default_to_none(self):
        raw = {"id": "default", "night_enabled": True}  # pre-beta config, no keys at all
        cfg = _lifecycle_config_from_storage(raw)
        assert cfg.night_sun_event is None
        assert cfg.morning_sun_event is None


# ---------------------------------------------------------------------------
# SE-14 — NightTrigger/MorningTrigger enum values are UNCHANGED.
# ---------------------------------------------------------------------------

class TestTriggerEnumUnchanged:
    def test_night_trigger_still_has_exactly_four_values(self):
        assert {t.value for t in NightTrigger} == {"disabled", "sun_elevation", "fixed_time", "both"}

    def test_morning_trigger_still_has_exactly_four_values(self):
        assert {t.value for t in MorningTrigger} == {"disabled", "sun_elevation", "fixed_time", "both"}

    def test_lifecycle_trigger_options_unchanged_at_four(self):
        assert set(LIFECYCLE_TRIGGER_OPTIONS) == {"disabled", "fixed_time", "sun_elevation", "both"}

    def test_sun_event_options_are_exactly_the_four_events(self):
        assert set(SUN_EVENT_OPTIONS) == {"sunrise", "sunset", "dawn", "dusk"}

    def test_sun_event_options_values_match_enum(self):
        assert set(SUN_EVENT_OPTIONS) == {e.value for e in SunEvent}


# ---------------------------------------------------------------------------
# SE-15 — invalid stored enum values fall back safely (to None).
# ---------------------------------------------------------------------------

class TestInvalidStoredValues:
    def test_unknown_night_sun_event_falls_back_to_none(self):
        raw = {"id": "default", "night_sun_event": "solar_noon_bogus"}
        cfg = _lifecycle_config_from_storage(raw)
        assert cfg.night_sun_event is None

    def test_unknown_morning_sun_event_falls_back_to_none(self):
        raw = {"id": "default", "morning_sun_event": "not_a_real_event"}
        cfg = _lifecycle_config_from_storage(raw)
        assert cfg.morning_sun_event is None

    def test_unknown_value_never_raises(self):
        # Whole-storage smoke: garbage in every enum-ish field must not raise.
        raw = {
            "id": "default",
            "night_trigger": "garbage",
            "morning_trigger": "garbage",
            "schedule_mode": "garbage",
            "night_sun_event": "garbage",
            "morning_sun_event": "garbage",
        }
        cfg = _lifecycle_config_from_storage(raw)
        assert cfg.night_sun_event is None
        assert cfg.morning_sun_event is None


# ---------------------------------------------------------------------------
# SE-16 — BOTH automatically uses a configured sun_event override.
#
# This is the architectural upgrade over the superseded SUN_EVENT-as-
# trigger-value design (which structurally COULD NOT express "elevation OR
# sun event" without a 6th enum value — see engines/lifecycle_engine.py
# module docstring "Sun events"). Under the override design, BOTH just
# reads whatever night_fixed_time _active_profile() resolved, exactly like
# FIXED_TIME does, with zero extra code.
# ---------------------------------------------------------------------------

class TestBothTriggerUsesSunEventOverride:
    def test_both_fires_via_sun_event_when_elevation_not_yet_met(self):
        engine = LifecycleEngine()
        cfg = NightDayLifecycleConfig(
            id="t", night_enabled=True, night_trigger=NightTrigger.BOTH,
            night_sun_elevation_deg=-10.0,  # strict — elevation not yet met
            night_fixed_time=None,
            night_sun_event=SunEvent.DUSK,  # BOTH now uses this automatically
            morning_enabled=True, morning_trigger=MorningTrigger.DISABLED,
        )
        now = _local(6, 15, 22, 0)
        sun_times = SunEventTimes(next_dusk=_local(6, 16, 21, 40))  # already passed today
        # Elevation well above -10° (not met), but dusk already passed.
        state = engine.get_lifecycle_state(now, -3.0, cfg, LifecycleState.DAY, sun_times)
        assert state is LifecycleState.NIGHT

    def test_both_still_fires_via_elevation_when_sun_event_not_yet_passed(self):
        """The reverse: elevation condition met, sun event still pending —
        BOTH's OR-semantics still work in the other direction too."""
        engine = LifecycleEngine()
        cfg = NightDayLifecycleConfig(
            id="t", night_enabled=True, night_trigger=NightTrigger.BOTH,
            night_sun_elevation_deg=-2.0,  # lenient — easily met
            night_fixed_time=None,
            night_sun_event=SunEvent.DUSK,
            morning_enabled=True, morning_trigger=MorningTrigger.DISABLED,
        )
        now = _local(6, 15, 20, 0)
        sun_times = SunEventTimes(next_dusk=_local(6, 15, 21, 40))  # still pending tonight
        state = engine.get_lifecycle_state(now, -5.0, cfg, LifecycleState.DAY, sun_times)
        assert state is LifecycleState.NIGHT  # via elevation, not the (still-pending) sun event


# ---------------------------------------------------------------------------
# SE-17 — midnight-boundary event resolution.
# ---------------------------------------------------------------------------

class TestMidnightBoundary:
    def test_event_shortly_after_midnight_resolves_correctly(self):
        """A dusk event that happens to fall just after midnight (extreme
        latitude edge case) must still resolve via the same date-comparison,
        without any special-casing."""
        engine = LifecycleEngine()
        cfg = _config(
            night_trigger=NightTrigger.FIXED_TIME, night_sun_event=SunEvent.DUSK,
            morning_trigger=MorningTrigger.DISABLED, morning_sun_event=None,
        )
        # A concrete (non-None) elevation is supplied so the carryover check's
        # None-elevation fail-safe (which conservatively assumes carryover
        # for ANY configured night trigger when elevation is unknown — a
        # pre-existing, deliberate behavior shared with FIXED_TIME, not
        # specific to sun events) does not mask the new-trigger check this
        # test targets. 0.5° (just above the horizon) makes the carryover's
        # own elevation cross-check (< 0.0°) false either way.
        # Just before the (very late) dusk on 2026-06-16.
        before = _local(6, 16, 0, 5)
        sun_times_before = SunEventTimes(next_dusk=_local(6, 16, 0, 10))
        assert engine.get_lifecycle_state(
            before, 0.5, cfg, LifecycleState.DAY, sun_times_before
        ) is LifecycleState.DAY

        # Just after — next_dusk has rolled over to tomorrow.
        after = _local(6, 16, 0, 15)
        sun_times_after = SunEventTimes(next_dusk=_local(6, 17, 0, 9))
        assert engine.get_lifecycle_state(
            after, 0.5, cfg, LifecycleState.DAY, sun_times_after
        ) is LifecycleState.NIGHT


# ---------------------------------------------------------------------------
# SE-18 — bug-injection sanity check.
# ---------------------------------------------------------------------------

class TestSanityCheck:
    def test_resolution_is_real_not_tautological(self):
        """Prove the sun-event resolution mechanism actually drives the
        result: swapping which event is configured must change the outcome
        for an otherwise-identical scenario. Dawn always precedes sunrise
        chronologically, so a "now" between the two gives a coherent
        real-world case where one has fired and the other hasn't yet."""
        engine = LifecycleEngine()
        now = _local(6, 15, 6, 15)  # between dawn (~06:00) and sunrise (~06:35)
        sun_times = SunEventTimes(
            next_sunrise=_local(6, 15, 6, 35),  # still pending today
            next_dawn=_local(6, 16, 5, 58),     # already passed today -> rolled to tomorrow
        )
        cfg_sunrise = _config(
            night_trigger=NightTrigger.DISABLED,
            morning_trigger=MorningTrigger.FIXED_TIME, morning_sun_event=SunEvent.SUNRISE,
        )
        cfg_dawn = _config(
            night_trigger=NightTrigger.DISABLED,
            morning_trigger=MorningTrigger.FIXED_TIME, morning_sun_event=SunEvent.DAWN,
        )
        state_sunrise = engine.get_lifecycle_state(now, None, cfg_sunrise, LifecycleState.NIGHT, sun_times)
        state_dawn = engine.get_lifecycle_state(now, None, cfg_dawn, LifecycleState.NIGHT, sun_times)
        assert state_sunrise is LifecycleState.NIGHT    # sunrise still pending -> no transition
        assert state_dawn is LifecycleState.MORNING     # dawn already passed -> releases
        assert state_sunrise != state_dawn
