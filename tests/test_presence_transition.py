"""Presence transition tests: Away → Home while shading or safety is active.

Verifies the complete Away→Home decision pipeline via TierOrchestrator and
the guard-bypass rules in state_machine.transitions.

Key architectural properties confirmed here:

1. When sun exposure exceeds the absence floor (strong sun during absence):
   SolarEvaluator wins PositionResolver's max(); the active state is
   STRONG_SHADE, not ABSENCE_CLOSED.  Returning home leaves the state
   unchanged — no spurious open command is issued.

2. When no shading trigger is active on return home (no sun, no heat, no
   glare): TierOrchestrator falls back to OPEN.  The (ABSENCE_CLOSED → OPEN)
   transition is listed in LIFECYCLE_DIRECT_TRANSITIONS and bypasses the
   StateGuard minimum_state_duration, so the cover opens immediately.

3. When the absence floor is more restrictive than the sun target (unusual
   config — very tight absence position): ABSENCE_CLOSED wins during absence.
   On return home the proposed state is a sun-shade level.  This is a
   de-escalation in the priority hierarchy and is NOT in
   LIFECYCLE_DIRECT_TRANSITIONS, so StateGuard applies.  After the guard
   interval the cover moves to the appropriate sun-shade level — still more
   open than fully closed, never more open than necessary.

4. Manual Override (Tier 2) and Safety states (Tier 1) are not affected by
   presence changes: they remain dominant until their own exit conditions
   are met.

5. The absence state is never sticky across cycles.  Each call to
   TierOrchestrator.evaluate_window() derives the decision solely from the
   current absence_active flag and live sensor readings.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.smartshading.engines.exposure_engine import WindowExposure
from custom_components.smartshading.engines.weather_engine import WeatherCondition
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.manual_override import ManualOverride
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import ShadingState
from custom_components.smartshading.state_machine.transitions import bypasses_guard

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 19, 13, 30, tzinfo=timezone.utc)

# Comfort config with all goal evaluators disabled — isolates solar evaluation.
_NO_COMFORT = ComfortConfig(
    heat_protection_enabled=False,
    glare_protection_enabled=False,
    solar_gain_enabled=False,
)


@pytest.fixture()
def orchestrator() -> TierOrchestrator:
    return TierOrchestrator()


@pytest.fixture()
def south_window() -> WindowConfig:
    return WindowConfig(
        id="w-south", name="South Window", zone_id="z1",
        azimuth=180.0, floor_level=0, cover_group_id="cg-south",
    )


@pytest.fixture()
def zone() -> ZoneConfig:
    return ZoneConfig(id="z1", name="Living Room")


def _exposure(wm2: float) -> WindowExposure:
    return WindowExposure(
        window_id="w-south",
        timestamp=_NOW,
        sun_azimuth=180.0, sun_elevation=45.0,
        is_above_horizon=True, is_in_tolerance_window=True,
        azimuth_delta_deg=0.0, direct_radiation_factor=1.0,
        elevation_clipped=False,
        theoretical_exposure=wm2, learned_solar_impact_factor=1.0,
        seasonal_factor=1.0, effective_exposure=wm2,
    )


def _wdi(
    window: WindowConfig,
    zone: ZoneConfig,
    *,
    absence_active: bool = False,
    is_in_solar_sector: bool = False,
    exposure_wm2: float | None = None,
    absence_position_ha: int = 30,     # HA 30 → internal 70 (70% shaded / 30% open)
    strong_shade_ha: int = 10,         # HA 10 → internal 90
    normal_shade_ha: int = 25,         # HA 25 → internal 75
    light_shade_ha: int = 40,          # HA 40 → internal 60
    comfort_config: ComfortConfig | None = None,
    active_override: ManualOverride | None = None,
    weather_condition: WeatherCondition | None = None,
    wind_speed_ms: float | None = None,
    current_shading_state: ShadingState = ShadingState.OPEN,
):
    return build_window_decision_input(
        window=window,
        zone=zone,
        global_defaults=GlobalDefaults(absence_position=absence_position_ha),
        shade_position_defaults=ShadePositionDefaults(
            light_shade_position=light_shade_ha,
            normal_shade_position=normal_shade_ha,
            strong_shade_position=strong_shade_ha,
        ),
        lifecycle_config=NightDayLifecycleConfig(id="default"),
        lifecycle_state=LifecycleState.DAY,
        absence_active=absence_active,
        current_shading_state=current_shading_state,
        outdoor_temp_c=None,
        indoor_temp_c=None,
        exposure=_exposure(exposure_wm2) if exposure_wm2 is not None else None,
        is_in_solar_sector=is_in_solar_sector,
        comfort_config=comfort_config or _NO_COMFORT,
        active_override=active_override,
        weather_condition=weather_condition,
        wind_speed_ms=wind_speed_ms,
        storm_protection_enabled=weather_condition is not None or wind_speed_ms is not None,
    )


# ---------------------------------------------------------------------------
# 1. Away → Home with sunny window (main scenario)
# ---------------------------------------------------------------------------

class TestAwayToHomeWithSunnyWindow:
    """Core scenario: cover is shaded due to strong sun during absence.

    Property: strong sun (600 W/m²) produces STRONG_SHADE at 90 internal,
    which exceeds the absence floor of 70 internal.  PositionResolver picks
    STRONG_SHADE.  The active state during absence is STRONG_SHADE, not
    ABSENCE_CLOSED.  On return home the same sun conditions still apply —
    SolarEvaluator proposes STRONG_SHADE again; no state change, no open.
    """

    def test_during_absence_strong_sun_state_is_strong_shade_not_absence_closed(
        self, orchestrator: TierOrchestrator,
        south_window: WindowConfig, zone: ZoneConfig,
    ) -> None:
        wdi = _wdi(
            south_window, zone,
            absence_active=True,
            is_in_solar_sector=True, exposure_wm2=600.0,
            absence_position_ha=30,   # internal 70 — sun wins at 90
            strong_shade_ha=10,       # internal 90
        )
        result = orchestrator.evaluate_window(wdi)
        # SolarEvaluator (90) beats AbsenceEvaluator (70): solar wins
        assert result.shading_state is ShadingState.STRONG_SHADE
        assert result.target_position == 90
        assert result.decided_by == "SolarEvaluator"

    def test_away_to_home_recalculates_and_keeps_sunny_window_shaded(
        self, orchestrator: TierOrchestrator,
        south_window: WindowConfig, zone: ZoneConfig,
    ) -> None:
        # --- Cycle 1: away, strong sun ---
        wdi_away = _wdi(
            south_window, zone,
            absence_active=True,
            is_in_solar_sector=True, exposure_wm2=600.0,
            absence_position_ha=30, strong_shade_ha=10,
        )
        result_away = orchestrator.evaluate_window(wdi_away)
        assert result_away.shading_state is ShadingState.STRONG_SHADE
        assert result_away.target_position == 90

        # --- Cycle 2: home, same strong sun ---
        wdi_home = _wdi(
            south_window, zone,
            absence_active=False,      # presence returned
            is_in_solar_sector=True, exposure_wm2=600.0,
            strong_shade_ha=10,
        )
        result_home = orchestrator.evaluate_window(wdi_home)

        # Cover must remain at STRONG_SHADE — absence removal does NOT trigger open
        assert result_home.shading_state is ShadingState.STRONG_SHADE
        assert result_home.target_position == 90
        assert result_home.decided_by == "SolarEvaluator"

    def test_away_to_home_normal_shade_still_active(
        self, orchestrator: TierOrchestrator,
        south_window: WindowConfig, zone: ZoneConfig,
    ) -> None:
        """Moderate sun (350 W/m²) also beats absence floor → NORMAL_SHADE persists."""
        wdi_home = _wdi(
            south_window, zone,
            absence_active=False,
            is_in_solar_sector=True, exposure_wm2=350.0,
            absence_position_ha=30, normal_shade_ha=25,
        )
        result = orchestrator.evaluate_window(wdi_home)
        assert result.shading_state is ShadingState.NORMAL_SHADE
        assert result.target_position == 75

    def test_shading_target_not_fully_open_after_home_return(
        self, orchestrator: TierOrchestrator,
        south_window: WindowConfig, zone: ZoneConfig,
    ) -> None:
        """target_position after home return is the sun shade level, never 0 (open)."""
        wdi = _wdi(
            south_window, zone,
            absence_active=False,
            is_in_solar_sector=True, exposure_wm2=600.0,
            strong_shade_ha=10,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.target_position > 0
        assert result.shading_state is not ShadingState.OPEN


# ---------------------------------------------------------------------------
# 2. Away → Home: non-sunny window opens
# ---------------------------------------------------------------------------

class TestAwayToHomeWithoutSun:
    """When no shade trigger is active, the cover opens after home return.

    The (ABSENCE_CLOSED → OPEN) transition is a direct lifecycle transition
    that bypasses StateGuard minimum_state_duration.
    """

    def test_during_absence_no_sun_state_is_absence_closed(
        self, orchestrator: TierOrchestrator,
        south_window: WindowConfig, zone: ZoneConfig,
    ) -> None:
        wdi = _wdi(
            south_window, zone,
            absence_active=True,
            is_in_solar_sector=False,   # sun not facing this window
            absence_position_ha=30,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.ABSENCE_CLOSED
        assert result.target_position == 70

    def test_away_to_home_opens_non_sunny_window_if_no_shading_needed(
        self, orchestrator: TierOrchestrator,
        south_window: WindowConfig, zone: ZoneConfig,
    ) -> None:
        # Cycle 1: away, no sun
        wdi_away = _wdi(
            south_window, zone,
            absence_active=True,
            is_in_solar_sector=False,
        )
        result_away = orchestrator.evaluate_window(wdi_away)
        assert result_away.shading_state is ShadingState.ABSENCE_CLOSED

        # Cycle 2: home, still no sun, no other trigger
        wdi_home = _wdi(
            south_window, zone,
            absence_active=False,
            is_in_solar_sector=False,
        )
        result_home = orchestrator.evaluate_window(wdi_home)
        assert result_home.shading_state is ShadingState.OPEN
        assert result_home.target_position == 0

    def test_absence_closed_to_open_is_direct_transition(self) -> None:
        """(ABSENCE_CLOSED → OPEN) must bypass StateGuard minimum_state_duration."""
        assert bypasses_guard(ShadingState.ABSENCE_CLOSED, ShadingState.OPEN) is True

    def test_absence_closed_same_state_is_noop(self) -> None:
        assert bypasses_guard(ShadingState.ABSENCE_CLOSED, ShadingState.ABSENCE_CLOSED) is True


# ---------------------------------------------------------------------------
# 3. De-escalation: absence more restrictive than sun target
# ---------------------------------------------------------------------------

class TestAwayToHomeAbsenceMoreRestrictive:
    """When the absence floor exceeds the sun shade position (uncommon config).

    Example: absence_position_ha=5 (internal 95) vs. STRONG_SHADE=10 (internal 90).
    ABSENCE_CLOSED wins during absence.  On return home the pipeline proposes
    STRONG_SHADE (90 internal).

    In the priority hierarchy ABSENCE_CLOSED (rank 30) is higher than
    STRONG_SHADE (rank 40), so this transition is a de-escalation and is NOT
    in LIFECYCLE_DIRECT_TRANSITIONS.  StateGuard applies.  After its interval
    the cover moves to STRONG_SHADE — still well-shaded, never fully open.
    """

    def test_during_absence_high_floor_beats_strong_shade(
        self, orchestrator: TierOrchestrator,
        south_window: WindowConfig, zone: ZoneConfig,
    ) -> None:
        wdi = _wdi(
            south_window, zone,
            absence_active=True,
            is_in_solar_sector=True, exposure_wm2=600.0,
            absence_position_ha=5,    # internal 95 — more restrictive than strong shade 90
            strong_shade_ha=10,       # internal 90
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.ABSENCE_CLOSED
        assert result.target_position == 95

    def test_after_home_return_proposes_strong_shade_not_open(
        self, orchestrator: TierOrchestrator,
        south_window: WindowConfig, zone: ZoneConfig,
    ) -> None:
        wdi = _wdi(
            south_window, zone,
            absence_active=False,
            is_in_solar_sector=True, exposure_wm2=600.0,
            strong_shade_ha=10,       # internal 90
        )
        result = orchestrator.evaluate_window(wdi)
        # TierOrchestrator returns the sun-shade decision (StateGuard applied by Coordinator)
        assert result.shading_state is ShadingState.STRONG_SHADE
        assert result.target_position == 90
        # Critically: the result is NOT open
        assert result.shading_state is not ShadingState.OPEN

    def test_absence_closed_to_strong_shade_requires_state_guard(self) -> None:
        """De-escalation from ABSENCE_CLOSED to STRONG_SHADE is NOT a direct transition.

        The Coordinator's StateGuard must arbitrate this — bypasses_guard must
        return False so that minimum_state_duration applies.
        """
        assert bypasses_guard(ShadingState.ABSENCE_CLOSED, ShadingState.STRONG_SHADE) is False

    def test_absence_closed_to_normal_shade_requires_state_guard(self) -> None:
        assert bypasses_guard(ShadingState.ABSENCE_CLOSED, ShadingState.NORMAL_SHADE) is False

    def test_absence_closed_to_light_shade_requires_state_guard(self) -> None:
        assert bypasses_guard(ShadingState.ABSENCE_CLOSED, ShadingState.LIGHT_SHADE) is False


# ---------------------------------------------------------------------------
# 4. Safety and manual override survive presence change
# ---------------------------------------------------------------------------

class TestAwayToHomeDoesNotBreakHigherTiers:
    """Tier 1 (Safety) and Tier 2 (Manual Override) are not affected by presence."""

    def test_away_to_home_does_not_break_manual_override(
        self, orchestrator: TierOrchestrator,
        south_window: WindowConfig, zone: ZoneConfig,
    ) -> None:
        override = ManualOverride(
            window_id="w-south",
            override_position=50,   # internal: 50% shaded
            started_at=_NOW,
            expires_at=_NOW.replace(hour=_NOW.hour + 4),
            source="position_delta",
            overridden_state=ShadingState.NORMAL_SHADE,
            overridden_position=75,
        )
        # During absence with active override
        wdi_away = _wdi(
            south_window, zone,
            absence_active=True,
            active_override=override,
        )
        result_away = orchestrator.evaluate_window(wdi_away)
        assert result_away.shading_state is ShadingState.MANUAL_OVERRIDE

        # After home return — override still active
        wdi_home = _wdi(
            south_window, zone,
            absence_active=False,
            active_override=override,
        )
        result_home = orchestrator.evaluate_window(wdi_home)
        assert result_home.shading_state is ShadingState.MANUAL_OVERRIDE
        assert result_home.target_position == 50

    def test_away_to_home_does_not_override_safety(
        self, orchestrator: TierOrchestrator,
        south_window: WindowConfig, zone: ZoneConfig,
    ) -> None:
        # During absence with storm active
        wdi_away = _wdi(
            south_window, zone,
            absence_active=True,
            weather_condition=WeatherCondition.THUNDERSTORM,
        )
        result_away = orchestrator.evaluate_window(wdi_away)
        assert result_away.shading_state is ShadingState.STORM_SAFE

        # After home return — storm still active
        wdi_home = _wdi(
            south_window, zone,
            absence_active=False,
            weather_condition=WeatherCondition.THUNDERSTORM,
        )
        result_home = orchestrator.evaluate_window(wdi_home)
        assert result_home.shading_state is ShadingState.STORM_SAFE
        # Safety keeps cover OPEN (retracted), not closed
        assert result_home.target_position == 0

    def test_manual_override_beats_absence_regardless_of_presence(
        self, orchestrator: TierOrchestrator,
        south_window: WindowConfig, zone: ZoneConfig,
    ) -> None:
        override = ManualOverride(
            window_id="w-south",
            override_position=30,
            started_at=_NOW,
            expires_at=_NOW.replace(hour=_NOW.hour + 4),
            source="position_delta",
            overridden_state=ShadingState.ABSENCE_CLOSED,
            overridden_position=70,
        )
        for absence_active in (True, False):
            wdi = _wdi(
                south_window, zone,
                absence_active=absence_active,
                active_override=override,
            )
            result = orchestrator.evaluate_window(wdi)
            assert result.shading_state is ShadingState.MANUAL_OVERRIDE, (
                f"Expected MANUAL_OVERRIDE for absence_active={absence_active}"
            )


# ---------------------------------------------------------------------------
# 5. State not sticky — each cycle is independent
# ---------------------------------------------------------------------------

class TestPresenceStateNotSticky:
    """The pipeline derives state fresh each cycle; no caching across calls."""

    def test_away_state_not_reused_on_next_call_with_home(
        self, orchestrator: TierOrchestrator,
        south_window: WindowConfig, zone: ZoneConfig,
    ) -> None:
        # Call 1: away
        wdi1 = _wdi(south_window, zone, absence_active=True, is_in_solar_sector=False)
        r1 = orchestrator.evaluate_window(wdi1)
        assert r1.shading_state is ShadingState.ABSENCE_CLOSED

        # Call 2: home (same orchestrator instance, same window)
        wdi2 = _wdi(south_window, zone, absence_active=False, is_in_solar_sector=False)
        r2 = orchestrator.evaluate_window(wdi2)
        # State must be derived from current inputs, not cached from call 1
        assert r2.shading_state is ShadingState.OPEN

    def test_repeated_home_cycles_with_sun_stay_consistent(
        self, orchestrator: TierOrchestrator,
        south_window: WindowConfig, zone: ZoneConfig,
    ) -> None:
        """Each evaluation with same inputs returns same result (stateless evaluators)."""
        for _ in range(3):
            wdi = _wdi(
                south_window, zone,
                absence_active=False,
                is_in_solar_sector=True, exposure_wm2=600.0,
                strong_shade_ha=10,
            )
            result = orchestrator.evaluate_window(wdi)
            assert result.shading_state is ShadingState.STRONG_SHADE
            assert result.target_position == 90

    def test_oscillating_presence_sun_stays_shaded(
        self, orchestrator: TierOrchestrator,
        south_window: WindowConfig, zone: ZoneConfig,
    ) -> None:
        """Rapid away→home→away cycling while sun is strong: always STRONG_SHADE."""
        for absence_active in (True, False, True, False):
            wdi = _wdi(
                south_window, zone,
                absence_active=absence_active,
                is_in_solar_sector=True, exposure_wm2=600.0,
                strong_shade_ha=10,
            )
            result = orchestrator.evaluate_window(wdi)
            # Sun (90 internal) always beats absence floor (70 internal)
            assert result.shading_state is ShadingState.STRONG_SHADE, (
                f"Expected STRONG_SHADE for absence_active={absence_active}"
            )


# ---------------------------------------------------------------------------
# 6. Priority and transition guard: complete matrix for ABSENCE_CLOSED exits
# ---------------------------------------------------------------------------

class TestAbsenceClosedTransitionRules:
    """Verify the guard-bypass rules for all transitions out of ABSENCE_CLOSED.

    These rules govern how the Coordinator's StateGuard treats the transition
    when presence changes from away to home.
    """

    def test_absence_closed_to_open_bypasses_guard(self) -> None:
        assert bypasses_guard(ShadingState.ABSENCE_CLOSED, ShadingState.OPEN) is True

    def test_absence_closed_to_same_is_noop_bypasses_guard(self) -> None:
        assert bypasses_guard(ShadingState.ABSENCE_CLOSED, ShadingState.ABSENCE_CLOSED) is True

    def test_absence_closed_to_safety_escalation_bypasses_guard(self) -> None:
        assert bypasses_guard(ShadingState.ABSENCE_CLOSED, ShadingState.STORM_SAFE) is True
        assert bypasses_guard(ShadingState.ABSENCE_CLOSED, ShadingState.WIND_SAFE) is True

    def test_absence_closed_to_manual_override_escalation_bypasses_guard(self) -> None:
        assert bypasses_guard(ShadingState.ABSENCE_CLOSED, ShadingState.MANUAL_OVERRIDE) is True

    def test_absence_closed_to_night_closed_escalation_bypasses_guard(self) -> None:
        assert bypasses_guard(ShadingState.ABSENCE_CLOSED, ShadingState.NIGHT_CLOSED) is True

    def test_absence_closed_to_shade_levels_require_state_guard(self) -> None:
        for state in (ShadingState.STRONG_SHADE, ShadingState.NORMAL_SHADE, ShadingState.LIGHT_SHADE):
            assert bypasses_guard(ShadingState.ABSENCE_CLOSED, state) is False, (
                f"Expected bypasses_guard=False for ABSENCE_CLOSED → {state}"
            )
