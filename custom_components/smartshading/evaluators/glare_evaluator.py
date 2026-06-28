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

from ..models.window_decision import WindowDecision
from ..models.window_decision_input import WindowDecisionInput
from ..state_machine.states import ShadingState


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
        if wdi.exposure.effective_exposure < wdi.effective_behavior.glare_min_exposure_wm2:
            return None

        return WindowDecision(
            window_id=wdi.window_config.id,
            shading_state=ShadingState.LIGHT_SHADE,
            target_position=wdi.effective_behavior.light_shade_position,
            decided_by="GlareEvaluator",
        )
