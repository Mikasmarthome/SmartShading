"""GlareEvaluator — Tier 4 Protection Floor: glare shading at scaled intensity.

Responsibility: return a WindowDecision when the sun is within the window's
azimuth tolerance sector, glare protection is enabled, and the window is
meaningfully lit.  The reason is always glare; the *intensity* (LIGHT / NORMAL /
STRONG shade, with the matching configured-or-learned position) scales with how
strongly THIS window is lit, classified with the same light/normal/strong
thresholds the SolarEvaluator uses.  Glare therefore no longer stays pinned to
LIGHT_SHADE when a window faces genuinely strong direct sun — without having to
re-label the reason as heat or solar.

The intensity is always per-window: it is derived only from this window's own
exposure and thresholds, never from any other window or floor (no cross-window
derivation).  It is monotonic — never weaker than the LIGHT_SHADE floor — so
moderate-glare behaviour (the historical LIGHT_SHADE result) is unchanged.
Because PositionResolver takes max() of all Tier 4 floors, the floor semantic is
preserved.

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

from ..const import (
    GLARE_INTENSITY_NORMAL_RATIO,
    GLARE_INTENSITY_STRONG_EXIT_RATIO,
    GLARE_INTENSITY_STRONG_RATIO,
    LOW_ANGLE_GLARE_MIN_MEASURED_WM2,
)
from ..models.window_decision import WindowDecision
from ..models.window_decision_input import WindowDecisionInput
from ..state_machine.states import DecisionCategory, ShadingState


def _low_angle_glare_value(wdi: WindowDecisionInput) -> float:
    """Vertical-window low-angle direct-glare estimate, gated by a real measured
    beam (0.0 when below the minimum measured beam or outside the low-sun band).
    """
    exposure = wdi.exposure
    if exposure is None:
        return 0.0
    # getattr defaults keep older exposure objects (e.g. test fakes predating the
    # beta.7 fields) working — they simply contribute no low-angle glare.
    if getattr(exposure, "measured_solar_wm2", 0.0) < LOW_ANGLE_GLARE_MIN_MEASURED_WM2:
        return 0.0
    return getattr(exposure, "low_angle_direct_glare_wm2", 0.0)


def glare_exposure_wm2(wdi: WindowDecisionInput) -> float:
    """The glare-relevant exposure for this window: the larger of the standard
    effective exposure and the gated low-angle vertical-window estimate.  Both
    use the authoritative measured source value; no second cloud reduction."""
    exposure = wdi.exposure
    if exposure is None:
        return 0.0
    return max(exposure.effective_exposure, _low_angle_glare_value(wdi))


def is_low_angle_direct_sun(wdi: WindowDecisionInput) -> bool:
    """True when low-angle direct sun alone would trigger glare on a vertical
    window (real low east/west beam clears the glare threshold even though the
    horizontal-projected effective exposure does not).  Diagnostic helper."""
    return _low_angle_glare_value(wdi) >= wdi.effective_behavior.glare_min_exposure_wm2


class GlareEvaluator:
    """Tier 4 Protection Floor: glare shading at LIGHT/NORMAL/STRONG intensity.

    Fires when glare protection is enabled and the sun is in the window's sector
    and the window is meaningfully lit.  The shade intensity scales with this
    window's glare-relevant exposure (the larger of the standard effective
    exposure and the gated low-angle vertical estimate) against the configured-
    or-learned light/normal/strong thresholds.

    Returns None when:
      - glare_protection_enabled is False (disabled for this window).
      - solar gain suppresses shading (cold-weather opt-out).
      - is_in_solar_sector is False (sun not facing this window).
      - the glare-relevant exposure is below glare_min_exposure_wm2 (geometry
        alone is NOT enough — the window must be meaningfully lit).  Uses the
        authoritative measured solar source value (or diagnosed fallback), never
        raw weather brightness, and never a second cloud reduction.
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

        behavior = wdi.effective_behavior
        # Glare-relevant exposure for the entry gate: the larger of the standard
        # effective exposure (normal path) and the gated low-angle vertical
        # estimate (low east/west sun the horizontal projection under-represents).
        if glare_exposure_wm2(wdi) < behavior.glare_min_exposure_wm2:
            return None

        # Intensity scaling is driven ONLY by the low-angle vertical estimate —
        # the part the SolarEvaluator under-counts at a low sun angle.  For
        # ordinary high-sun exposure the SolarEvaluator already classifies
        # NORMAL/STRONG (and owns that attribution); glare there stays the
        # LIGHT_SHADE floor, so this never overrides solar's reason or changes
        # existing high-sun behaviour.  The stage is taken from how far the real
        # low-angle beam exceeds the glare threshold (a geometry-weighted ratio,
        # since the estimate already encodes cot(elevation)·cos(azimuth_delta)).
        # Per window only — derived solely from this window's own exposure; never
        # from any other window.  Monotonic: never weaker than the light floor.
        low_angle = _low_angle_glare_value(wdi)
        glare_min = behavior.glare_min_exposure_wm2
        ratio = (low_angle / glare_min) if glare_min > 0 else 0.0

        # Exit hysteresis (v1.1.1): once already in STRONG_SHADE, require the
        # ratio to drop below the lower GLARE_INTENSITY_STRONG_EXIT_RATIO —
        # not just back below the entry ratio — before de-escalating. Entry
        # into STRONG stays instant and unaffected (see const.py). Currently
        # STRONG + ratio still >= the exit ratio (even if below the entry
        # ratio) holds at STRONG rather than flapping to NORMAL/LIGHT.
        if (
            wdi.current_shading_state is ShadingState.STRONG_SHADE
            and GLARE_INTENSITY_STRONG_EXIT_RATIO <= ratio < GLARE_INTENSITY_STRONG_RATIO
        ):
            state = ShadingState.STRONG_SHADE
            position = behavior.strong_shade_position
        elif ratio >= GLARE_INTENSITY_STRONG_RATIO:
            state = ShadingState.STRONG_SHADE
            position = behavior.strong_shade_position
        elif ratio >= GLARE_INTENSITY_NORMAL_RATIO:
            state = ShadingState.NORMAL_SHADE
            position = behavior.normal_shade_position
        else:
            state = ShadingState.LIGHT_SHADE
            position = behavior.light_shade_position

        return WindowDecision(
            window_id=wdi.window_config.id,
            shading_state=state,
            target_position=position,
            decided_by="GlareEvaluator",
            category=DecisionCategory.PROTECTION,
        )
