"""GlareEvaluator — Tier 4 Protection Floor: LIGHT_SHADE on glare.

Responsibility: return a WindowDecision encoding a LIGHT_SHADE floor when
the sun is within the window's azimuth tolerance sector and glare protection
is enabled.

This is a 1:1 migration of the ComfortAwareStateEvaluator / ComfortEngine
glare-protection logic (comfort_engine.py, Rule 2) into the Tier 4 floor
pattern.  The effective result is identical:
    ComfortAwareStateEvaluator: _most_shading(proposed, LIGHT_SHADE)
    GlareEvaluator:             WindowDecision(LIGHT_SHADE, light_shade_position)
Because PositionResolver takes max() of all Tier 4 floors, the "at least
LIGHT_SHADE" semantic is preserved.

Key difference from SolarEvaluator:
    SolarEvaluator gates on both is_in_solar_sector AND effective_exposure ≥ 150 W/m².
    GlareEvaluator gates on is_in_solar_sector alone — glare can occur even
    when cloud cover damps effective_exposure below the solar threshold
    (diffuse glare, low sun angle, thin cloud layer).

Scope:
  - Reads only wdi.is_in_solar_sector and
    wdi.effective_behavior.glare_protection_enabled / light_shade_position.
  - No lifecycle state, absence, heat, solar exposure, or config hierarchy (INV-18).
  - No HA dependency.
"""
from __future__ import annotations

from ..const import LOW_ANGLE_GLARE_MIN_MEASURED_WM2
from ..models.window_decision import WindowDecision
from ..models.window_decision_input import WindowDecisionInput
from ..state_machine.states import ShadingState


def is_low_angle_direct_sun(wdi: WindowDecisionInput) -> bool:
    """True when low-angle direct sun should trigger glare on a vertical window.

    Independent glare entry condition for the case where the sun is low and in
    the window's sector, the measured solar beam is real (not dusk/diffuse), and
    the vertical-window direct-glare estimate clears the glare threshold — even
    though the standard horizontal-projected effective exposure is below it.

    Caller has already verified glare is enabled, not solar-gain suppressed, the
    window is in the solar sector (which also implies the sun reaches the glazing
    — not floor-clipped, not obstructed), and exposure is available.
    """
    exposure = wdi.exposure
    if exposure is None:
        return False
    if exposure.measured_solar_wm2 < LOW_ANGLE_GLARE_MIN_MEASURED_WM2:
        return False
    # low_angle_direct_glare_wm2 is 0.0 outside the low-elevation band, so this
    # naturally excludes normal/high-sun cases (handled by the effective path).
    return (
        exposure.low_angle_direct_glare_wm2
        >= wdi.effective_behavior.glare_min_exposure_wm2
    )


class GlareEvaluator:
    """Tier 4 Protection Floor: LIGHT_SHADE when sun is in window's sector.

    Returns a WindowDecision for LIGHT_SHADE when glare protection is enabled
    and the sun is within the window's azimuth tolerance window.

    Returns None when:
      - glare_protection_enabled is False (disabled for this window).
      - is_in_solar_sector is False (sun not facing this window).
      - effective window exposure is below glare_min_exposure_wm2 (geometry alone
        is NOT enough — the window must be meaningfully lit).  This uses the
        authoritative effective window exposure (measured solar source when valid;
        diagnosed fallback otherwise), never the ignored raw weather brightness,
        and never a second cloud reduction.
    """

    def evaluate(self, wdi: WindowDecisionInput) -> WindowDecision | None:
        if not wdi.effective_behavior.glare_protection_enabled:
            return None

        if wdi.effective_behavior.solar_gain_suppresses_shading:
            return None

        if not wdi.is_in_solar_sector:
            return None

        # Glare must not fire on geometry alone — require a meaningful effective
        # window exposure.  Exposure unavailable (sun.sun missing) → no glare.
        if wdi.exposure is None:
            return None

        # Two independent entry conditions:
        #  1. Normal path: the standard effective window exposure clears the
        #     threshold (the existing behaviour, unchanged).
        #  2. Low-angle direct-sun path: at a low sun angle the horizontal-
        #     projected effective exposure under-represents vertical-window glare,
        #     so a vertical-incidence estimate is used instead (real east/west
        #     morning/evening sun) — gated by a minimum measured beam.
        _normal = (
            wdi.exposure.effective_exposure
            >= wdi.effective_behavior.glare_min_exposure_wm2
        )
        if not (_normal or is_low_angle_direct_sun(wdi)):
            return None

        return WindowDecision(
            window_id=wdi.window_config.id,
            shading_state=ShadingState.LIGHT_SHADE,
            target_position=wdi.effective_behavior.light_shade_position,
            decided_by="GlareEvaluator",
        )
