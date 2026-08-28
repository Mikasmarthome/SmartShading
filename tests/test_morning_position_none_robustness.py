"""Morning robustness fix -- morning_enabled=True with morning_position=None
must never crash.

Root cause (found while auditing B3-011): NightDayLifecycleConfig.
morning_position (and its weekday_/weekend_ variants) are typed `int`, not
Optional, with real defaults (100) -- every valid load path keeps them a
real int (config_flow.py's CONF_MORNING_POSITION is vol.Required;
config_entry_data.py's raw.get("morning_position", defaults.morning_position)
only substitutes the default when the KEY is absent, never when it is
present with an explicit null). But Python dataclasses do not enforce field
types at runtime, and coordinator.py itself already treats this same field
as possibly-None elsewhere in the identical cycle (its own
`if _active_lc_profile.morning_position is not None else None` guard right
after the active_profile() call) -- proving the codebase's own authors
already anticipated a corrupted/hand-edited stored payload with an explicit
null could reach here despite the type annotation.

models/window_decision_input.py's build_window_decision_input() previously
converted lifecycle_config.morning_position with _ha_to_internal() guarded
ONLY on morning_enabled, not on the value itself being non-None --
`_ha_to_internal(None)` raises TypeError ("unsupported operand type(s) for
-: 'int' and 'NoneType'"), which coordinator.py's real _async_update_data()
never catches, so it propagates all the way out and permanently fails
config-entry setup ("... not ready yet ... Retrying in 5 seconds", forever).

The fix mirrors the SAME `is not None` guard already used at the sibling
call site: morning_position is converted only when morning_enabled AND the
resolved value is not None; otherwise it stays None -- the exact same,
already-documented "no explicit morning position override" state
BehaviorConfig.morning_position's own docstring describes for
morning_enabled=False. No fabricated default, no invented 0/100/light/
normal/strong position -- the existing shared arbitration pipeline (Heat/
Glare/Solar/Absence/fallback) takes over exactly as it already does
whenever morning_enabled is False.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

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


def _zone() -> ZoneConfig:
    return ZoneConfig(id="z1", name="Living")


def _window() -> WindowConfig:
    return WindowConfig(id="w1", name="South", zone_id="z1",
                         azimuth=180.0, floor_level=0, cover_group_id="cg1")


def _build_wdi(*, lifecycle_config, lifecycle_state) -> object:
    return build_window_decision_input(
        window=_window(), zone=_zone(),
        global_defaults=GlobalDefaults(),
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=lifecycle_config,
        lifecycle_state=lifecycle_state,
        absence_active=False,
        current_shading_state=ShadingState.NIGHT_CLOSED,
        outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
        is_in_solar_sector=False,
        comfort_config=_NO_COMFORT,
    )


class TestMorningEnabledWithNonePosition:
    """Scenario 1: morning_enabled=True, morning_position=None."""

    def test_no_crash_building_wdi(self):
        config = NightDayLifecycleConfig(
            id="default", morning_enabled=True, morning_position=None,
        )
        wdi = _build_wdi(lifecycle_config=config, lifecycle_state=LifecycleState.MORNING)
        assert wdi is not None

    def test_effective_morning_position_stays_none(self):
        config = NightDayLifecycleConfig(
            id="default", morning_enabled=True, morning_position=None,
        )
        wdi = _build_wdi(lifecycle_config=config, lifecycle_state=LifecycleState.MORNING)
        assert wdi.effective_behavior.morning_position is None

    def test_morning_evaluator_yields_no_artificial_candidate(self):
        """MorningEvaluator must return None -- never fabricate a target from
        a missing position, never default to fully open."""
        config = NightDayLifecycleConfig(
            id="default", morning_enabled=True, morning_position=None,
        )
        wdi = _build_wdi(lifecycle_config=config, lifecycle_state=LifecycleState.MORNING)
        from custom_components.smartshading.evaluators.morning_evaluator import MorningEvaluator
        assert MorningEvaluator().evaluate(wdi) is None

    def test_shared_pipeline_resolves_via_fallback_not_morning(self):
        """The real TierOrchestrator path: with no Morning candidate and no
        other protection active, the plain existing fallback (OPEN, category
        COMFORT, decided_by TierOrchestrator:fallback) applies -- proving the
        shared arbitration pipeline genuinely took over, not a crash-avoidance
        stub."""
        config = NightDayLifecycleConfig(
            id="default", morning_enabled=True, morning_position=None,
        )
        wdi = _build_wdi(lifecycle_config=config, lifecycle_state=LifecycleState.MORNING)
        decision = TierOrchestrator().evaluate_window(wdi)
        assert decision.decided_by != "MorningEvaluator"
        assert decision.shading_state is ShadingState.OPEN


class TestMorningEnabledWithValidPositionUnaffected:
    """Scenario 2: morning_enabled=True with a real position -- the fix must
    not change the existing, correct behavior at all."""

    def test_valid_position_converts_exactly_as_before(self):
        config = NightDayLifecycleConfig(
            id="default", morning_enabled=True, morning_position=70,
        )
        wdi = _build_wdi(lifecycle_config=config, lifecycle_state=LifecycleState.MORNING)
        assert wdi.effective_behavior.morning_position == 30  # HA 70 -> internal 30

    def test_morning_evaluator_still_dispatches_the_real_target(self):
        config = NightDayLifecycleConfig(
            id="default", morning_enabled=True, morning_position=70,
        )
        wdi = _build_wdi(lifecycle_config=config, lifecycle_state=LifecycleState.MORNING)
        decision = TierOrchestrator().evaluate_window(wdi)
        assert decision.decided_by == "MorningEvaluator"
        assert decision.target_position == 30


class TestMorningDisabled:
    """Scenario 3: morning_enabled=False -- morning_position must stay None
    regardless of the configured value (pre-existing behavior, unaffected
    by the fix)."""

    def test_effective_morning_position_is_none_even_with_a_configured_value(self):
        config = NightDayLifecycleConfig(
            id="default", morning_enabled=False, morning_position=70,
        )
        wdi = _build_wdi(lifecycle_config=config, lifecycle_state=LifecycleState.DAY)
        assert wdi.effective_behavior.morning_position is None

    def test_effective_morning_position_is_none_when_also_none(self):
        config = NightDayLifecycleConfig(
            id="default", morning_enabled=False, morning_position=None,
        )
        wdi = _build_wdi(lifecycle_config=config, lifecycle_state=LifecycleState.DAY)
        assert wdi.effective_behavior.morning_position is None


class TestWeekdayWeekendProfileWithMissingPosition:
    """Scenario 4: the real coordinator.py sequence
    (active_profile() -> replace() the effective config -> build_window_
    decision_input()) with a weekday/weekend profile whose selected position
    field was corrupted to None -- mirrors test_morning_position_resolution.py's
    own _run_real_cycle() helper exactly, so this is the SAME real path the
    coordinator runs, not a synthetic shortcut."""

    def _config(self) -> NightDayLifecycleConfig:
        from datetime import time as _time
        return NightDayLifecycleConfig(
            id="default", schedule_mode=LifecycleScheduleMode.WEEKDAY_WEEKEND,
            night_trigger=NightTrigger.FIXED_TIME,
            weekday_night_fixed_time=_time(22, 0), weekend_night_fixed_time=_time(23, 0),
            morning_trigger=MorningTrigger.FIXED_TIME,
            weekday_morning_fixed_time=_time(6, 30), weekend_morning_fixed_time=_time(8, 30),
            weekday_morning_position=None,  # simulates a corrupted stored payload
            weekend_morning_position=60,
        )

    def _run_real_cycle(self, now: datetime, config: NightDayLifecycleConfig):
        engine = LifecycleEngine()
        state = engine.get_lifecycle_state(
            now, sun_elevation_deg=None, config=config,
            previous_lifecycle_state=LifecycleState.NIGHT,
        )
        profile = engine.active_profile(now, config)
        effective_config = replace(
            config, night_position=profile.night_position,
            morning_position=profile.morning_position,
        )
        wdi = _build_wdi(lifecycle_config=effective_config, lifecycle_state=state)
        return state, TierOrchestrator().evaluate_window(wdi)

    def test_weekday_with_corrupted_none_position_does_not_crash(self):
        now = datetime(2026, 8, 3, 6, 30, 0, tzinfo=_UTC)  # Monday
        state, decision = self._run_real_cycle(now, self._config())
        assert state is LifecycleState.MORNING
        assert decision.decided_by != "MorningEvaluator"
        assert decision.shading_state is ShadingState.OPEN

    def test_weekend_with_the_other_profiles_valid_position_is_unaffected(self):
        """The weekday corruption must not leak into the weekend profile --
        proves the None-guard is scoped to the resolved value only."""
        now = datetime(2026, 8, 8, 8, 30, 0, tzinfo=_UTC)  # Saturday
        state, decision = self._run_real_cycle(now, self._config())
        assert state is LifecycleState.MORNING
        assert decision.decided_by == "MorningEvaluator"
        assert decision.target_position == 40  # HA 60 -> internal 40


class TestNoRegressionOnB3010AndB3011:
    """The fix touches exactly one conversion guard -- confirm the sibling
    night_position/absence_position conversions (same function) and the
    already-correct morning_reconciliation_pending_target_internal path
    (B3-010) are completely untouched."""

    def test_night_position_conversion_still_unconditional_on_shading_enabled(self):
        config = NightDayLifecycleConfig(id="default", night_position=0)
        wdi = _build_wdi(lifecycle_config=config, lifecycle_state=LifecycleState.NIGHT)
        assert wdi.effective_behavior.night_position == 100  # HA 0 -> internal 100

    def test_morning_reconciliation_pending_target_path_independent_of_this_guard(self):
        """B3-010's own re-proposal path reads a DIFFERENT WDI field
        (morning_reconciliation_pending_target_internal, resolved by the
        coordinator before building the WDI) -- unaffected by
        lifecycle_config.morning_position being None."""
        config = NightDayLifecycleConfig(
            id="default", morning_enabled=True, morning_position=None,
        )
        wdi = build_window_decision_input(
            window=_window(), zone=_zone(),
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=config,
            lifecycle_state=LifecycleState.DAY,
            absence_active=False,
            current_shading_state=ShadingState.NIGHT_CLOSED,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=False,
            comfort_config=_NO_COMFORT,
            morning_reconciliation_pending_target_internal=25,
        )
        from custom_components.smartshading.evaluators.morning_evaluator import MorningEvaluator
        decision = MorningEvaluator().evaluate(wdi)
        assert decision is not None
        assert decision.target_position == 25
