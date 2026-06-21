"""Comfort Engine (ARCHITECTURE.md §5.5).

Stateless data provider for comfort-goal assessment. Used by the
Coordinator to populate WindowObservation.comfort_assessment (sensor
attributes and build_reason() labels). Evaluation decisions are made
by HeatEvaluator and GlareEvaluator in the Tier 4 pipeline.

Evaluates three comfort goals per window per cycle:
  1. Heat protection  — outdoor or indoor temperature at/above configured threshold
  2. Glare protection — sun is currently in the window's solar sector
  3. Solar gain       — cold outside, sun active, no conflicting comfort goal

HA-independent: takes only pre-computed float/bool inputs.
"""
from __future__ import annotations

from ..models.comfort import ComfortAssessment, ComfortConfig
from ..models.state import ReasonCode


class ComfortEngine:
    """ARCHITECTURE.md §5.5 - stateless, single-call comfort assessment.

    assess() never raises: all inputs except `config` may be None or
    unavailable. The same "never crash on absent data" principle used
    throughout the weather/lifecycle path applies here.
    """

    @staticmethod
    def assess(
        outdoor_temp: float | None,
        indoor_temp: float | None,
        is_in_solar_sector: bool,
        sun_elevation: float | None,  # noqa: ARG004 – reserved for a later glare angle check
        config: ComfortConfig,
    ) -> ComfortAssessment:
        """Evaluate all three comfort goals for one window.

        Args:
            outdoor_temp: current outdoor temperature in °C, or None.
            indoor_temp: current indoor temperature in °C, or None.
            is_in_solar_sector: True when the sun is within the window's
                azimuth tolerance window (SunEngine output).
            sun_elevation: current sun elevation in degrees, or None.
                Reserved for a future per-goal elevation filter; not used
                in this version - glare detection relies solely on
                is_in_solar_sector.
            config: flat comfort configuration collected by the Config Flow.
        """
        # Heat protection: outdoor temp OR indoor temp at/above threshold.
        heat_protection_needed = False
        if config.heat_protection_enabled:
            if outdoor_temp is not None and outdoor_temp >= config.heat_protection_outdoor_temp_c:
                heat_protection_needed = True
            if indoor_temp is not None and indoor_temp >= config.heat_protection_indoor_temp_c:
                heat_protection_needed = True

        # Glare protection: sun is in the window's azimuth tolerance window.
        # The is_in_solar_sector flag already encodes that the sun is at a
        # meaningful angle for this window, so no separate elevation check is
        # needed for this version.
        glare_protection_needed = (
            config.glare_protection_enabled and is_in_solar_sector
        )

        # Solar gain: cold outside, sun active, no conflicting comfort goal.
        solar_gain_beneficial = (
            config.solar_gain_enabled
            and not heat_protection_needed
            and not glare_protection_needed
            and is_in_solar_sector
            and outdoor_temp is not None
            and outdoor_temp < config.solar_gain_max_outdoor_temp_c
        )

        if heat_protection_needed:
            reason_code = ReasonCode.HEAT_PROTECTION.value
            reason = "Heat protection"
        elif glare_protection_needed:
            reason_code = ReasonCode.GLARE_PROTECTION.value
            reason = "Glare protection"
        elif solar_gain_beneficial:
            reason_code = ReasonCode.SOLAR_GAIN.value
            reason = "Solar gain"
        else:
            reason_code = ReasonCode.COMFORT_NEUTRAL.value
            reason = "No active comfort goal"

        return ComfortAssessment(
            heat_protection_needed=heat_protection_needed,
            glare_protection_needed=glare_protection_needed,
            solar_gain_beneficial=solar_gain_beneficial,
            indoor_temperature=indoor_temp,
            indoor_temp_available=indoor_temp is not None,
            reason=reason,
            reason_code=reason_code,
        )
