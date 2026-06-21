"""WindowDecisionInput and build_window_decision_input().

WindowDecisionInput is the pre-built runtime contract that is passed to every
evaluator in the Tier 1–5 pipeline.  It contains all inputs an evaluator needs,
already resolved, so that evaluators never need to traverse the config hierarchy
or read HA state themselves (INV-18).

build_window_decision_input() is the ONLY place where:
  - ConfigResolver.resolve() is called
  - HA cover-position convention (0=closed, 100=open) is converted to the
    integration-internal convention (0=open, 100=shaded)

After this function returns, evaluators operate on plain Python values with
no dependency on WindowConfig / ZoneConfig / GlobalDefaults internals.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..engines.exposure_engine import WindowExposure
from ..engines.weather_engine import WeatherCondition
from ..models.comfort import ComfortConfig
from ..models.lifecycle import LifecycleState, NightDayLifecycleConfig
from ..models.manual_override import ManualOverride
from ..models.window import WindowConfig
from ..models.zone import ZoneConfig
from ..state_machine.states import ShadingState
from .behavior_config import BehaviorConfig
from .config import ConfigResolver, GlobalDefaults, ShadePositionDefaults


# ---------------------------------------------------------------------------
# Convention helper
# ---------------------------------------------------------------------------

def _ha_to_internal(ha_position: int) -> int:
    """Convert a cover position from HA convention to internal convention.

    HA:       0 = closed,  100 = open
    Internal: 0 = open,    100 = shaded / closed

    Called only inside build_window_decision_input() — nowhere else.
    """
    return 100 - ha_position


# ---------------------------------------------------------------------------
# Runtime contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowDecisionInput:
    """Pre-resolved runtime contract for one window evaluation cycle (INV-18).

    Constructed once per window per update cycle by build_window_decision_input().
    Evaluators read effective_behavior and the sensor/state fields below —
    they must not access window_config or zone_config for behavior parameters.

    active_override is reserved for Phase 2 (ManualOverride detection) and is
    always None in this version.  It is intentionally placed here, not on individual
    evaluator inputs, so that the orchestrator — not individual Tier evaluators —
    controls override logic (INV-18).
    """

    window_config: WindowConfig
    zone_config: ZoneConfig

    # Pre-resolved behavior — evaluators read only this, never raw config (INV-18)
    effective_behavior: BehaviorConfig

    # Lifecycle / presence
    lifecycle_state: LifecycleState       # from LifecycleEngine.get_lifecycle_state()
    absence_active: bool                  # from PresenceDebouncer.is_absence_active()

    # Current state — used for hysteresis in the solar evaluator (Tier 5)
    current_shading_state: ShadingState

    # Sensor readings
    outdoor_temp_c: float | None          # from WeatherEngine / outdoor sensor; None if unavailable
    indoor_temp_c: float | None           # from optional indoor temperature sensor; None if unset
    exposure: WindowExposure | None       # from ExposureEngine.calculate(); None if sun.sun unavailable
    is_in_solar_sector: bool              # True when sun is within azimuth tolerance for this window

    # Active manual override, or None when no override is in effect (Tier 2).
    # Populated each cycle by OverrideDetector (engines/override_detector.py);
    # consumed by ManualOverrideEvaluator (evaluators/manual_override_evaluator.py).
    # In-memory only — not persisted across HA restarts (Phase 2 extension).
    active_override: ManualOverride | None = None

    # Tier 1 Safety inputs (Step 7) — all optional so existing call sites need
    # no changes; build_window_decision_input() always populates them.
    wind_speed_ms: float | None = None       # sustained wind speed from sensor or weather entity
    wind_gust_ms: float | None = None        # peak gust speed; preferred over wind_speed_ms when available
    weather_condition: WeatherCondition | None = None  # parsed WeatherCondition enum


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_window_decision_input(
    *,
    window: WindowConfig,
    zone: ZoneConfig,
    global_defaults: GlobalDefaults,
    shade_position_defaults: ShadePositionDefaults,
    lifecycle_config: NightDayLifecycleConfig,
    lifecycle_state: LifecycleState,
    absence_active: bool,
    current_shading_state: ShadingState,
    outdoor_temp_c: float | None,
    indoor_temp_c: float | None,
    exposure: WindowExposure | None,
    is_in_solar_sector: bool,
    comfort_config: ComfortConfig | None = None,
    # Tier 1: Safety inputs (Step 7)
    wind_speed_ms: float | None = None,
    wind_gust_ms: float | None = None,
    weather_condition: WeatherCondition | None = None,
    storm_protection_enabled: bool = True,
    wind_protection_enabled: bool = False,
    wind_threshold_ms: float = 14.0,
    # Tier 2: Manual Override inputs (Step 8)
    active_override: ManualOverride | None = None,
    override_duration_min: int = 240,
    override_detection_tolerance: int = 10,
    override_break_on_lifecycle: bool = True,
) -> WindowDecisionInput:
    """Assemble a WindowDecisionInput for one window evaluation cycle.

    This is the single authoritative place where:
      1. The three-level config inheritance chain (Window > Zone > GlobalDefaults)
         is resolved via ConfigResolver — evaluators must not call it themselves.
      2. HA cover positions (0=closed, 100=open) are converted to the
         integration-internal convention (0=open, 100=shaded) via _ha_to_internal().

    All keyword-only arguments enforce clarity at the call site and prevent
    accidental argument-order mistakes.
    """
    # --- Resolve comfort config (INV-18: thresholds land in BehaviorConfig) --
    _comfort = comfort_config or ComfortConfig()
    if _comfort.heat_protection_enabled:
        heat_outdoor_threshold_c: float | None = _comfort.heat_protection_outdoor_temp_c
        heat_indoor_threshold_c: float | None = _comfort.heat_protection_indoor_temp_c
    else:
        heat_outdoor_threshold_c = None
        heat_indoor_threshold_c = None

    # --- Resolve inherited behavior flags and positions ---------------------
    night_shading_enabled: bool = ConfigResolver.resolve(
        window, zone, global_defaults, "night_shading_enabled"
    )
    absence_shading_enabled: bool = ConfigResolver.resolve(
        window, zone, global_defaults, "absence_shading_enabled"
    )
    absence_position_ha: int = ConfigResolver.resolve(
        window, zone, global_defaults, "absence_position"
    )

    # --- Solar gain suppression (preventive shading opt-out for winter sun) --
    # When solar gain is enabled and the outdoor temperature is cold enough,
    # suppress both GlareEvaluator and SolarEvaluator so the window stays
    # open for beneficial winter heat gain.  Safety guard: never active when
    # a heat-protection threshold is currently exceeded.
    _heat_triggered_outdoor = (
        heat_outdoor_threshold_c is not None
        and outdoor_temp_c is not None
        and outdoor_temp_c >= heat_outdoor_threshold_c
    )
    _heat_triggered_indoor = (
        heat_indoor_threshold_c is not None
        and indoor_temp_c is not None
        and indoor_temp_c >= heat_indoor_threshold_c
    )
    _solar_gain_suppresses = (
        _comfort.solar_gain_enabled
        and outdoor_temp_c is not None
        and outdoor_temp_c < _comfort.solar_gain_max_outdoor_temp_c
        and not _heat_triggered_outdoor
        and not _heat_triggered_indoor
    )

    # --- Build pre-resolved BehaviorConfig (internal convention) -----------
    effective_behavior = BehaviorConfig(
        storm_protection_enabled=storm_protection_enabled,
        wind_protection_enabled=wind_protection_enabled,
        wind_threshold_ms=wind_threshold_ms,
        override_duration_min=override_duration_min,
        override_detection_tolerance=override_detection_tolerance,
        override_break_on_lifecycle=override_break_on_lifecycle,
        night_position=(
            _ha_to_internal(lifecycle_config.night_position)
            if night_shading_enabled
            else None
        ),
        morning_position=(
            _ha_to_internal(lifecycle_config.morning_position)
            if lifecycle_config.morning_enabled
            else None
        ),
        absence_position=(
            _ha_to_internal(absence_position_ha)
            if absence_shading_enabled
            else None
        ),
        heat_outdoor_threshold_c=heat_outdoor_threshold_c,
        heat_indoor_threshold_c=heat_indoor_threshold_c,
        glare_protection_enabled=_comfort.glare_protection_enabled,
        solar_gain_suppresses_shading=_solar_gain_suppresses,
        light_shade_position=_ha_to_internal(
            window.light_shade_position
            if window.light_shade_position is not None
            else shade_position_defaults.light_shade_position
        ),
        normal_shade_position=_ha_to_internal(
            window.normal_shade_position
            if window.normal_shade_position is not None
            else shade_position_defaults.normal_shade_position
        ),
        strong_shade_position=_ha_to_internal(
            window.strong_shade_position
            if window.strong_shade_position is not None
            else shade_position_defaults.strong_shade_position
        ),
    )

    return WindowDecisionInput(
        window_config=window,
        zone_config=zone,
        effective_behavior=effective_behavior,
        lifecycle_state=lifecycle_state,
        absence_active=absence_active,
        current_shading_state=current_shading_state,
        outdoor_temp_c=outdoor_temp_c,
        indoor_temp_c=indoor_temp_c,
        exposure=exposure,
        is_in_solar_sector=is_in_solar_sector,
        active_override=active_override,
        wind_speed_ms=wind_speed_ms,
        wind_gust_ms=wind_gust_ms,
        weather_condition=weather_condition,
    )
