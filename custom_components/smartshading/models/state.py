"""Data records describing a window's current shading state. See
ARCHITECTURE.md §3.5.

The ShadingState enum itself (with priorities) lives in
state_machine/states.py, per the original module split in ARCHITECTURE.md
§2 (state_machine/states.py = "ShadingState-Enum, Prioritäten" vs.
models/shade_state.py = "ShadeState, StateRecord, StateLock"). This module
only holds data *about* a state instance, not the state space itself.
"""
from __future__ import annotations

from enum import Enum


class ReasonCode(Enum):
    """Machine-readable reason for the current ShadeState (ARCHITECTURE.md
    §3.5). Kept minimal for the current states; extended as later phases
    (Forecast/Comfort/Learning Engine) add new reasons.
    """

    SUN_EXPOSURE_THRESHOLD = "sun_exposure_threshold"
    HYSTERESIS_EXIT = "hysteresis_exit"
    MANUAL_INTERVENTION_DETECTED = "manual_intervention_detected"
    MANUAL_OVERRIDE_CLEARED = "manual_override_cleared"
    STORM_DETECTED = "storm_detected"
    STORM_CLEARED = "storm_cleared"
    WIND_DETECTED = "wind_detected"
    WIND_CLEARED = "wind_cleared"
    RAIN_DETECTED = "rain_detected"
    RAIN_CLEARED = "rain_cleared"
    NIGHT_LIFECYCLE = "night_lifecycle"
    MORNING_LIFECYCLE = "morning_lifecycle"
    ABSENCE_DETECTED = "absence_detected"
    PRESENCE_DETECTED = "presence_detected"
    GUARD_LOCKED = "guard_locked"
    # Comfort Engine reasons (Comfort Engine phase, 2026-06-17)
    HEAT_PROTECTION = "heat_protection"
    GLARE_PROTECTION = "glare_protection"
    SOLAR_GAIN = "solar_gain"
    COMFORT_NEUTRAL = "comfort_neutral"
