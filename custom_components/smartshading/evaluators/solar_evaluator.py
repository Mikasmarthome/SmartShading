"""SolarEvaluator — Tier 5 Comfort Pipeline (solar exposure classification).

Responsibility: classify the current solar exposure level for one window
and return a WindowDecision with the appropriate shade position.

The classification uses entry thresholds (in W/m²) that match the existing
observability_evaluator.HYSTERESIS_THRESHOLDS entry values, so the evaluation
boundary is consistent with the existing runtime until the chain is removed in
Step 6.

Note on hysteresis: SolarEvaluator does NOT implement hysteresis.  The
StateGuard (wired in Step 5) is responsible for suppressing rapid
state oscillations.  SolarEvaluator only classifies the current exposure.

Position convention:
    Internal: 0 = open, 100 = fully shaded.
    All positions come from wdi.effective_behavior (pre-resolved, INV-18).
    No HA-convention conversion happens here.

Scope:
  - Reads only wdi.exposure (effective_exposure in W/m²),
    wdi.is_in_solar_sector, and wdi.effective_behavior.
  - No astronomy / azimuth calculation.
  - No config inheritance traversal.
  - No HA dependency.
"""
from __future__ import annotations

from ..models.window_decision import WindowDecision
from ..models.window_decision_input import WindowDecisionInput
from ..state_machine.states import ShadingState

class SolarEvaluator:
    """Tier 5 Comfort Pipeline: solar-based shade classification.

    Returns a WindowDecision with one of {LIGHT_SHADE, NORMAL_SHADE,
    STRONG_SHADE} when the sun is in the window's solar sector and the
    effective exposure meets the corresponding threshold.

    Entry thresholds are read from wdi.effective_behavior, which carries the
    defaults (150 / 300 / 500 W/m²) or learned values supplied by
    AdaptationApplication (bounded, confidence-gated).

    Returns None when:
      - is_in_solar_sector is False (sun not facing this window)
      - exposure is None (sun.sun entity unavailable)
      - effective_exposure < light_shade_threshold_wm2 (sun too low — prefer OPEN)
    """

    def evaluate(self, wdi: WindowDecisionInput) -> WindowDecision | None:
        if wdi.effective_behavior.solar_gain_suppresses_shading:
            return None

        if not wdi.is_in_solar_sector:
            return None

        if wdi.exposure is None:
            return None

        exposure_wm2 = wdi.exposure.effective_exposure
        behavior = wdi.effective_behavior

        if exposure_wm2 >= behavior.strong_shade_threshold_wm2:
            return WindowDecision(
                window_id=wdi.window_config.id,
                shading_state=ShadingState.STRONG_SHADE,
                target_position=behavior.strong_shade_position,
                decided_by="SolarEvaluator",
            )
        if exposure_wm2 >= behavior.normal_shade_threshold_wm2:
            return WindowDecision(
                window_id=wdi.window_config.id,
                shading_state=ShadingState.NORMAL_SHADE,
                target_position=behavior.normal_shade_position,
                decided_by="SolarEvaluator",
            )
        if exposure_wm2 >= behavior.light_shade_threshold_wm2:
            return WindowDecision(
                window_id=wdi.window_config.id,
                shading_state=ShadingState.LIGHT_SHADE,
                target_position=behavior.light_shade_position,
                decided_by="SolarEvaluator",
            )

        return None  # below threshold → OPEN
