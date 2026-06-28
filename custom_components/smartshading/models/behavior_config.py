"""Pre-resolved behavior configuration for one evaluation cycle.

Built by build_window_decision_input() and stored in WindowDecisionInput
as `effective_behavior`.  Evaluators read only from this object — never
from WindowConfig / ZoneConfig / GlobalDefaults directly (INV-18).

All position values use the integration-internal convention:
  0  = fully open  (no shading)
  100 = fully shaded / closed

CoverController (cover_control/cover_controller.py) is the only place that
converts these values back to the HA cover convention (0=closed, 100=open)
when actually commanding a physical device.  Nothing else in the integration
should perform this conversion.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BehaviorConfig:
    """Resolved shading positions for one window, ready for evaluators.

    None for a position field means that mode is disabled for this window
    (e.g. night_position=None → night shading turned off via per-window or
    global config).  Evaluators receiving None must not produce a decision
    for that mode.
    """

    # --- Tier 1: Safety Guards ------------------------------------------------

    # Storm Protection: always enabled by default.  Fires on STORM_CONDITIONS
    # weather codes or wind speed/gust >= DEFAULT_STORM_WIND_THRESHOLD_MS.
    storm_protection_enabled: bool = True


    # Wind Protection: opt-in (default off) because wind damage risk depends on
    # cover type and mounting (e.g. irrelevant for roller shutters without awnings).
    wind_protection_enabled: bool = False

    # Gust/speed threshold that triggers WIND_SAFE (m/s; Beaufort 6 ≈ 14 m/s).
    # Must be below the storm threshold (DEFAULT_STORM_WIND_THRESHOLD_MS = 20 m/s).
    wind_threshold_ms: float = 14.0

    # Rain Protection: opt-in; default on for awnings/exterior screens (high rain
    # exposure), off for roller shutters/venetian blinds/generic (rain safe by design).
    # build_window_decision_input() resolves per-window override vs hardware default.
    rain_protection_enabled: bool = False

    # Target position (internal convention: 0=open, 100=shaded) for RAIN_SAFE.
    # Resolved by the coordinator from rain_safe_position_ha via hardware-type lookup.
    # None = not applicable (rain protection disabled for this window).
    rain_safe_position: int | None = None

    # Minutes to wait after rain stops before releasing RAIN_SAFE and allowing
    # normal automatic control to resume.  Passed dynamically to SafetyHold.update().
    rain_release_delay_min: int = 30

    # --- Tier 2: Manual Override ----------------------------------------------

    # Duration (minutes) an override stays active before SmartShading resumes
    # normal evaluation.  Default 4 hours.  Configurable per window/zone/global.
    override_duration_min: int = 240

    # Minimum position delta (internal units 0–100) before a position deviation
    # is declared a manual override.  Suppresses sensor drift and small
    # mechanical deviations.  Default 10 ≈ 10 % of travel range.
    override_detection_tolerance: int = 10

    # When True, any lifecycle state transition (e.g. DAY→NIGHT, NIGHT→MORNING)
    # clears an active manual override so SmartShading resumes normal evaluation
    # for that phase.  Power users who want overrides to survive phase boundaries
    # can opt out by setting this to False.
    override_break_on_lifecycle: bool = True

    # --- Tier 3: Lifecycle Phase Gate positions --------------------------------

    # Target position while in NIGHT lifecycle phase.
    # None = night shading disabled (window stays at whatever position it was).
    night_position: int | None = 100

    # Target position on the MORNING transition (one-cycle event).
    # None = no explicit morning position override (pipeline takes over immediately).
    # Phase 2 only; MorningEvaluator is not part of this version.
    morning_position: int | None = None

    # --- Tier 4: Protection Floor positions -----------------------------------

    # Minimum position floor while ABSENCE_CLOSED is active.
    # None = absence shading disabled.
    absence_position: int | None = 70

    # Heat protection temperature thresholds.
    # None on a threshold = that sensor is not checked (not that heat is disabled).
    # Both None = heat protection disabled entirely.
    # Pre-resolved from ComfortConfig by build_window_decision_input().
    heat_outdoor_threshold_c: float | None = 26.0
    heat_indoor_threshold_c: float | None = 24.0

    # Glare protection enabled flag.
    # False = GlareEvaluator always returns None for this window.
    # Pre-resolved from ComfortConfig by build_window_decision_input().
    glare_protection_enabled: bool = True

    # Solar gain suppression flag.
    # True = GlareEvaluator and SolarEvaluator both return None so that
    # winter sunlight can provide beneficial heat gain rather than being
    # blocked by shading.  Set by build_window_decision_input() when
    # solar_gain_enabled=True, outdoor_temp < solar_gain_max_outdoor_temp_c,
    # and no heat-protection threshold is currently triggered.
    # Always False when heat protection is active.
    solar_gain_suppresses_shading: bool = False

    # --- Tier 3 (cont.): Night Contact Behavior --------------------------------

    # Option A: block the automatic night move while the window contact is open.
    # When True, the NightEvaluator result is suppressed if contact is OPEN.
    # A catch-up move to night_position fires exactly once when contact closes.
    night_block_on_window_open: bool = False

    # Option B: drive to window_open_night_position when contact opens after the
    # night move was already performed; return to night_position on close.
    # Only effective when night_block_on_window_open is True.
    night_lift_on_window_open: bool = False

    # Target internal position for NIGHT_VENT state (Option B).
    # 0 = fully open (ventilation-friendly default).
    # Resolved from window_open_night_position_ha by coordinator before WDI build.
    window_open_night_position: int = 0

    # --- Tier 5: Comfort Pipeline positions ----------------------------------

    light_shade_position: int = 60
    normal_shade_position: int = 75
    strong_shade_position: int = 90

    # Solar entry thresholds (W/m²) for Tier 5 classification.
    # Defaults match the hardcoded constants that SolarEvaluator previously used
    # (_LIGHT_SHADE_WM2 = 150, _NORMAL_SHADE_WM2 = 300, _STRONG_SHADE_WM2 = 500).
    # Adaptation Application (9F17+) may replace these with learned values bounded
    # within hard clamps; BehaviorConfig.defaults guarantee backward compatibility.
    light_shade_threshold_wm2: float = 150.0
    normal_shade_threshold_wm2: float = 300.0
    strong_shade_threshold_wm2: float = 500.0
