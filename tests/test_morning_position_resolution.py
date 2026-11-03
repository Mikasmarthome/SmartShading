"""Morning position resolution — v1.2.0-beta.3, B3-010.

Tests the ALREADY-EXISTING, unmodified LifecycleEngine.active_profile()
weekday/weekend/shared position-selection logic specifically for
morning_position (previously untested for either night_position or
morning_position at the position level — only the *time* side, via
test_lifecycle_clamp.py, had coverage). This file establishes that coverage
and then proves the real end-to-end path: LifecycleEngine.get_lifecycle_state()
-> LifecycleEngine.active_profile() -> build_window_decision_input() ->
TierOrchestrator.evaluate_window() -- the exact same sequence
coordinator.py's real cycle runs (coordinator.py ~3757-3763), for both a
weekday and a weekend date, across multiple windows in one zone.

Established, unchanged semantics being verified (not invented here):
  - SAME_EVERY_DAY: config.morning_position applies every day.
  - WEEKDAY_WEEKEND: config.weekday_morning_position (Mon-Fri) or
    config.weekend_morning_position (Sat-Sun) applies -- both are plain
    non-Optional ints with their own defaults (100), unlike the *_fixed_time
    fields which are Optional and fall back to the shared field when unset.
    There is therefore no runtime "None -> fall back to shared position"
    branch for positions (engines/lifecycle_engine.py:_active_profile) --
    the "fallback" is simply each field's own sensible default.
  - active_months / clamp_time only ever affect the resolved *time*, never
    the position fields -- verified explicitly here so a future change
    cannot silently start clamping/restricting positions too.
"""
from __future__ import annotations

from datetime import datetime, time, timezone

from custom_components.smartshading.engines.lifecycle_engine import LifecycleEngine
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import (
    LifecycleScheduleMode,
    LifecycleState,
    MorningTrigger,
    NightDayLifecycleConfig,
    NightTrigger,
)
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import ShadingState

_UTC = timezone.utc
_NO_COMFORT = ComfortConfig(
    heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False,
)


def _weekday_datetime(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 3, hour, minute, 0, tzinfo=_UTC)  # Monday


def _weekend_datetime(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 8, hour, minute, 0, tzinfo=_UTC)  # Saturday


# ---------------------------------------------------------------------------
# SAME_EVERY_DAY: shared morning_position applies every day
# ---------------------------------------------------------------------------

class TestSameEveryDayMorningPosition:
    def test_shared_position_used_on_a_weekday(self):
        config = NightDayLifecycleConfig(
            id="default", schedule_mode=LifecycleScheduleMode.SAME_EVERY_DAY,
            morning_position=55,
        )
        profile = LifecycleEngine().active_profile(_weekday_datetime(7), config)
        assert profile.morning_position == 55

    def test_shared_position_used_on_a_weekend_day(self):
        """SAME_EVERY_DAY ignores day-of-week entirely -- same value both days."""
        config = NightDayLifecycleConfig(
            id="default", schedule_mode=LifecycleScheduleMode.SAME_EVERY_DAY,
            morning_position=55,
        )
        profile = LifecycleEngine().active_profile(_weekend_datetime(7), config)
        assert profile.morning_position == 55


# ---------------------------------------------------------------------------
# WEEKDAY_WEEKEND: correct selection on weekdays vs. weekends
# ---------------------------------------------------------------------------

class TestWeekdayWeekendMorningPositionSelection:
    def _config(self, **overrides) -> NightDayLifecycleConfig:
        defaults = dict(
            id="default", schedule_mode=LifecycleScheduleMode.WEEKDAY_WEEKEND,
            morning_position=100,             # shared field -- must NOT be used in this mode
            weekday_morning_position=40,
            weekend_morning_position=80,
        )
        defaults.update(overrides)
        return NightDayLifecycleConfig(**defaults)

    def test_monday_uses_weekday_position(self):
        profile = LifecycleEngine().active_profile(_weekday_datetime(7), self._config())
        assert profile.morning_position == 40

    def test_friday_uses_weekday_position(self):
        friday = datetime(2026, 8, 7, 7, 0, 0, tzinfo=_UTC)
        profile = LifecycleEngine().active_profile(friday, self._config())
        assert profile.morning_position == 40

    def test_saturday_uses_weekend_position(self):
        profile = LifecycleEngine().active_profile(_weekend_datetime(7), self._config())
        assert profile.morning_position == 80

    def test_sunday_uses_weekend_position(self):
        sunday = datetime(2026, 8, 9, 7, 0, 0, tzinfo=_UTC)
        profile = LifecycleEngine().active_profile(sunday, self._config())
        assert profile.morning_position == 80

    def test_shared_morning_position_is_not_used_in_weekday_weekend_mode(self):
        """The shared field must be completely bypassed once schedule_mode is
        WEEKDAY_WEEKEND -- confirms no accidental fallback-to-shared exists
        for positions (unlike the Optional *_fixed_time fields)."""
        weekday_profile = LifecycleEngine().active_profile(_weekday_datetime(7), self._config())
        weekend_profile = LifecycleEngine().active_profile(_weekend_datetime(7), self._config())
        assert weekday_profile.morning_position != 100
        assert weekend_profile.morning_position != 100

    def test_changing_weekday_position_does_not_affect_weekend_selection(self):
        config = self._config(weekday_morning_position=10)
        weekend_profile = LifecycleEngine().active_profile(_weekend_datetime(7), config)
        assert weekend_profile.morning_position == 80  # unchanged

    def test_changing_weekend_position_does_not_affect_weekday_selection(self):
        config = self._config(weekend_morning_position=90)
        weekday_profile = LifecycleEngine().active_profile(_weekday_datetime(7), config)
        assert weekday_profile.morning_position == 40  # unchanged

    def test_default_weekday_and_weekend_positions_when_never_customized(self):
        """Neither weekday_morning_position nor weekend_morning_position was
        explicitly overridden -- both fall back to their own dataclass
        default (100), NOT to each other or to the shared field."""
        config = NightDayLifecycleConfig(
            id="default", schedule_mode=LifecycleScheduleMode.WEEKDAY_WEEKEND,
        )
        weekday_profile = LifecycleEngine().active_profile(_weekday_datetime(7), config)
        weekend_profile = LifecycleEngine().active_profile(_weekend_datetime(7), config)
        assert weekday_profile.morning_position == 100
        assert weekend_profile.morning_position == 100


# ---------------------------------------------------------------------------
# active_months / schedule clamp: positions are never affected
# ---------------------------------------------------------------------------

class TestActiveMonthsAndClampNeverTouchPositions:
    def test_active_profile_position_unaffected_by_active_months(self):
        """active_months only gates whether MORNING is entered at all
        (get_lifecycle_state) -- active_profile() itself has no month
        awareness and always resolves the same position regardless."""
        config_restricted = NightDayLifecycleConfig(
            id="default", morning_position=42, active_months=(12, 1, 2),
        )
        config_unrestricted = NightDayLifecycleConfig(id="default", morning_position=42)
        july = datetime(2026, 7, 15, 7, 0, 0, tzinfo=_UTC)
        assert (
            LifecycleEngine().active_profile(july, config_restricted).morning_position
            == LifecycleEngine().active_profile(july, config_unrestricted).morning_position
            == 42
        )

    def test_get_lifecycle_state_never_reaches_morning_outside_active_months(self):
        """The actual gate: outside active_months, NIGHT releases straight to
        DAY -- MORNING (and therefore morning_position) is never reached."""
        config = NightDayLifecycleConfig(
            id="default",
            night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(22, 0),
            morning_trigger=MorningTrigger.FIXED_TIME, morning_fixed_time=time(6, 30),
            active_months=(12, 1, 2),  # winter only
        )
        july_morning = datetime(2026, 7, 15, 7, 0, 0, tzinfo=_UTC)  # past both triggers, July
        state = LifecycleEngine().get_lifecycle_state(
            july_morning, sun_elevation_deg=None, config=config,
            previous_lifecycle_state=LifecycleState.NIGHT,
        )
        assert state is LifecycleState.DAY  # not MORNING

    def test_active_profile_position_unaffected_by_time_clamp(self):
        """morning_not_before/morning_not_after clamp the resolved *time*
        only -- clamp_time() never touches morning_position."""
        config = NightDayLifecycleConfig(
            id="default", morning_position=42,
            morning_trigger=MorningTrigger.FIXED_TIME, morning_fixed_time=time(6, 30),
            morning_not_before=time(7, 0), morning_not_after=time(8, 0),
        )
        profile = LifecycleEngine().active_profile(_weekday_datetime(7), config)
        assert profile.morning_position == 42
        assert profile.morning_fixed_time == time(7, 0)  # the TIME was clamped up


# ---------------------------------------------------------------------------
# Real end-to-end path: lifecycle event -> final zone decision, weekday vs.
# weekend, multiple windows in one zone -- mirrors coordinator.py's exact
# resolution sequence (see coordinator.py ~3745-3841).
# ---------------------------------------------------------------------------

class TestRealPathLifecycleEventToFinalZoneDecision:
    def _windows(self) -> list[WindowConfig]:
        return [
            WindowConfig(id="w-south", name="South", zone_id="z1",
                         azimuth=180.0, floor_level=0, cover_group_id="cg-south"),
            WindowConfig(id="w-east", name="East", zone_id="z1",
                         azimuth=90.0, floor_level=0, cover_group_id="cg-east"),
        ]

    def _zone(self) -> ZoneConfig:
        return ZoneConfig(id="z1", name="Living")

    def _config(self) -> NightDayLifecycleConfig:
        return NightDayLifecycleConfig(
            id="default", schedule_mode=LifecycleScheduleMode.WEEKDAY_WEEKEND,
            night_trigger=NightTrigger.FIXED_TIME,
            weekday_night_fixed_time=time(22, 0), weekend_night_fixed_time=time(23, 0),
            weekday_night_position=0, weekend_night_position=0,
            morning_trigger=MorningTrigger.FIXED_TIME,
            weekday_morning_fixed_time=time(6, 30), weekend_morning_fixed_time=time(8, 30),
            weekday_morning_position=100,   # weekday: fully open
            weekend_morning_position=60,    # weekend: partially shaded (sleep-in)
        )

    def _run_real_cycle(self, now: datetime, zone, windows, config, *, prev=LifecycleState.NIGHT):
        """Reproduces coordinator.py's real per-cycle resolution sequence:
        LifecycleEngine.get_lifecycle_state() -> active_profile() -> patch
        the config with the resolved night/morning positions ->
        build_window_decision_input() per window -> TierOrchestrator."""
        engine = LifecycleEngine()
        state = engine.get_lifecycle_state(
            now, sun_elevation_deg=None, config=config, previous_lifecycle_state=prev,
        )
        profile = engine.active_profile(now, config)
        from dataclasses import replace
        effective_config = replace(
            config,
            night_position=profile.night_position,
            morning_position=profile.morning_position,
        )
        orchestrator = TierOrchestrator()
        results = {}
        for window in windows:
            wdi = build_window_decision_input(
                window=window, zone=zone,
                global_defaults=GlobalDefaults(),
                shade_position_defaults=ShadePositionDefaults(),
                lifecycle_config=effective_config,
                lifecycle_state=state,
                absence_active=False,
                current_shading_state=ShadingState.NIGHT_CLOSED,
                outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
                is_in_solar_sector=False,
                comfort_config=_NO_COMFORT,
            )
            results[window.id] = orchestrator.evaluate_window(wdi)
        return state, results

    def test_weekday_morning_transition_reaches_weekday_position_both_windows(self):
        now = datetime(2026, 8, 3, 6, 30, 0, tzinfo=_UTC)  # Monday, exactly at trigger
        state, results = self._run_real_cycle(now, self._zone(), self._windows(), self._config())
        assert state is LifecycleState.MORNING
        for window_id, decision in results.items():
            assert decision.decided_by == "MorningEvaluator", window_id
            assert decision.shading_state is ShadingState.OPEN
            assert decision.target_position == 0  # HA 100 -> internal 0, weekday

    def test_weekend_morning_transition_reaches_weekend_position_both_windows(self):
        now = datetime(2026, 8, 8, 8, 30, 0, tzinfo=_UTC)  # Saturday, exactly at trigger
        state, results = self._run_real_cycle(now, self._zone(), self._windows(), self._config())
        assert state is LifecycleState.MORNING
        for window_id, decision in results.items():
            assert decision.decided_by == "MorningEvaluator", window_id
            assert decision.target_position == 40  # HA 60 -> internal 40, weekend

    def test_weekday_night_still_reaches_night_closed_unaffected(self):
        """Regression guard: the real Night path (unchanged by B3-010) still
        works correctly through this same real-cycle helper."""
        now = datetime(2026, 8, 3, 22, 30, 0, tzinfo=_UTC)  # Monday night
        state, results = self._run_real_cycle(
            now, self._zone(), self._windows(), self._config(), prev=LifecycleState.DAY,
        )
        assert state is LifecycleState.NIGHT
        for window_id, decision in results.items():
            assert decision.decided_by == "NightEvaluator", window_id
            assert decision.shading_state is ShadingState.NIGHT_CLOSED
