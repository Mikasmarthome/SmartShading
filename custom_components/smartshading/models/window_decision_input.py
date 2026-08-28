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

from ..engines.contact_engine import ContactStatus
from ..engines.exposure_engine import WindowExposure
from ..engines.rain_engine import RainStatus
from ..engines.weather_engine import WeatherCondition
from ..models.comfort import ComfortConfig
from ..models.lifecycle import LifecycleState, NightDayLifecycleConfig
from ..models.manual_override import ManualOverride, OverrideReleaseStrategy
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
    # consumed by evaluate_manual_override_policy (engines/manual_override_policy.py).
    # In-memory only — not persisted across HA restarts (Phase 2 extension).
    active_override: ManualOverride | None = None

    # Tier 1 Safety inputs (Step 7) — all optional so existing call sites need
    # no changes; build_window_decision_input() always populates them.
    wind_speed_ms: float | None = None       # sustained wind speed from sensor or weather entity
    wind_gust_ms: float | None = None        # peak gust speed; preferred over wind_speed_ms when available
    weather_condition: WeatherCondition | None = None  # parsed WeatherCondition enum

    # Rain status — normalized by rain_engine.RainEngine before WDI is built.
    # None when no rain sensor is configured or no reading available.
    # RainEvaluator checks this field; UNKNOWN means "no trigger" (fail-safe).
    rain_status: RainStatus | None = None

    # Contact sensor reading for night-contact behavior (Option A / Option B).
    # None when no sensor is configured. UNKNOWN means fail-safe (no block).
    contact_status: ContactStatus | None = None

    # True when presence is configured but its state cannot currently be
    # determined (every configured presence entity is unknown/unavailable, e.g.
    # right after an HA restart before they hydrate).  The TierOrchestrator uses
    # this only to hold instead of driving the non-protective daytime fallback to
    # fully open — it never affects safety/manual/night/absence/heat/glare/solar.
    presence_uncertain: bool = False

    # True when HeatEvaluator's hysteresis-resolved decision was active on the
    # PRIOR evaluation cycle for this window (v1.2.0-beta.1, T9). Populated by
    # the Coordinator (engines/heat_hysteresis.py owns the state-transition
    # logic; the Coordinator only persists the resulting boolean across
    # cycles — see coordinator.py's self._heat_active). Defaults to False so
    # every existing call site (including all pre-T9 tests) reproduces the
    # exact legacy "fires only at/above the entry threshold" behavior
    # unchanged. Never touched by anything other than the Coordinator.
    heat_previously_active: bool = False

    # B3-010 correction: the window's best-known CURRENT cover position,
    # already in SmartShading internal convention (0=open, 100=shaded) —
    # the Coordinator resolves this from AssumedStateManager (reliable
    # actual feedback, or an assumed position ONLY when
    # AssumedStateManager.is_position_trustworthy() is True) BEFORE
    # building this WDI, so no invert_position conversion is needed here
    # (AssumedStateManager already stores/returns internal-convention
    # values regardless of the physical cover's invert quirk). None when
    # genuinely unknown or not (yet) trustworthy — TierOrchestrator's
    # presence_uncertain direction check treats None as the conservative
    # "cannot confirm a closing move" case, never a numeric guess.
    current_position_internal: int | None = None

    # B3-010: the still-pending Morning Reconciliation target for this
    # window, in internal convention, or None when there is nothing
    # pending for today (satisfied/superseded/invalidated/stale/never
    # started). Resolved by the Coordinator from
    # engines.morning_reconciliation.pending_target_position_ha() BEFORE
    # building this WDI. MorningEvaluator re-proposes this target every
    # DAY cycle while it is not None, replacing the old "MorningEvaluator
    # fires once, then ComfortMovementHold throttles the fallback" design
    # — the commitment now stays in the SAME Tier 3/4/5 arbitration pool
    # every cycle until genuinely satisfied or superseded, not merely
    # throttled by a timer.
    morning_reconciliation_pending_target_internal: int | None = None


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
    # Rain protection (Tier 1 — rank 3)
    rain_status: RainStatus | None = None,
    rain_protection_enabled: bool = False,
    rain_safe_position: int | None = None,
    rain_release_delay_min: int = 30,
    # Tier 2: Manual Override inputs (Step 8)
    active_override: ManualOverride | None = None,
    override_detection_tolerance: int = 10,
    override_release_strategy: OverrideReleaseStrategy = OverrideReleaseStrategy.LIFECYCLE,
    override_allow_comfort_actions: bool = False,
    override_allow_protection_actions: bool = False,
    # Tier 3 extension: night contact behavior (v1.1.0)
    night_block_on_window_open: bool = False,
    night_lift_on_window_open: bool = False,
    window_open_night_position: int = 0,
    contact_status: ContactStatus | None = None,
    presence_uncertain: bool = False,
    # Heat protection hysteresis (v1.2.0-beta.1, T9) — see WindowDecisionInput
    # .heat_previously_active docstring.
    heat_previously_active: bool = False,
    # B3-010 — see WindowDecisionInput.current_position_internal docstring.
    current_position_internal: int | None = None,
    # B3-010 — see WindowDecisionInput.morning_reconciliation_pending_target_internal docstring.
    morning_reconciliation_pending_target_internal: int | None = None,
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
    heat_hysteresis_c = _comfort.heat_protection_hysteresis_c

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
    # F23: per-window night-close position override.  Night position has no
    # zone-level tier (unlike absence_position), so this is a direct
    # window-only override of the resolved lifecycle value, not a
    # ConfigResolver Window>Zone>Global chain.
    night_position_ha: int = (
        window.night_position
        if window.night_position is not None
        else lifecycle_config.night_position
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
        rain_protection_enabled=rain_protection_enabled,
        rain_safe_position=rain_safe_position,
        rain_release_delay_min=rain_release_delay_min,
        override_detection_tolerance=override_detection_tolerance,
        override_release_strategy=override_release_strategy,
        override_allow_comfort_actions=override_allow_comfort_actions,
        override_allow_protection_actions=override_allow_protection_actions,
        night_position=(
            _ha_to_internal(night_position_ha)
            if night_shading_enabled
            else None
        ),
        # NightDayLifecycleConfig.morning_position is typed `int` (not
        # Optional) with a real default -- every valid load path (config_flow's
        # vol.Required, config_entry_data.py's raw.get(key, default)) keeps it
        # a real int. The `is not None` guard is purely defensive against a
        # corrupted/hand-edited stored payload with an explicit null for this
        # key (raw.get() only substitutes the default when the KEY is absent,
        # never when it is present with value None) -- coordinator.py already
        # treats this same field as possibly-None elsewhere in the identical
        # cycle (see its own `if _active_lc_profile.morning_position is not
        # None else None` a few dozen lines below its _active_profile() call).
        # No fabricated default: an unexpected None here degrades to "no
        # explicit morning position override" (BehaviorConfig.morning_position
        # stays None), the same documented state as morning_enabled=False,
        # letting the existing shared arbitration pipeline take over --
        # never a crash, never an invented 0/100/light/normal/strong position.
        morning_position=(
            _ha_to_internal(lifecycle_config.morning_position)
            if lifecycle_config.morning_enabled
            and lifecycle_config.morning_position is not None
            else None
        ),
        absence_position=(
            _ha_to_internal(absence_position_ha)
            if absence_shading_enabled
            else None
        ),
        heat_outdoor_threshold_c=heat_outdoor_threshold_c,
        heat_indoor_threshold_c=heat_indoor_threshold_c,
        heat_hysteresis_c=heat_hysteresis_c,
        glare_protection_enabled=_comfort.glare_protection_enabled,
        glare_min_exposure_wm2=_comfort.glare_min_exposure_wm2,
        solar_gain_suppresses_shading=_solar_gain_suppresses,
        night_block_on_window_open=night_block_on_window_open,
        night_lift_on_window_open=night_lift_on_window_open,
        window_open_night_position=window_open_night_position,
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
        rain_status=rain_status,
        contact_status=contact_status,
        presence_uncertain=presence_uncertain,
        heat_previously_active=heat_previously_active,
        current_position_internal=current_position_internal,
        morning_reconciliation_pending_target_internal=morning_reconciliation_pending_target_internal,
    )
