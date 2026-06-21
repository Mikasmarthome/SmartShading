"""HeatEvaluator — Tier 4 Protection Floor: NORMAL_SHADE on heat.

Responsibility: return a WindowDecision encoding a NORMAL_SHADE floor when
the configured heat protection thresholds are exceeded by the measured
outdoor or indoor temperature.

This is a 1:1 migration of the ComfortAwareStateEvaluator / ComfortEngine
heat-protection logic (comfort_engine.py, Rule 1) into the Tier 4 floor
pattern.  The effective result is identical:
    ComfortAwareStateEvaluator: _most_shading(proposed, NORMAL_SHADE)
    HeatEvaluator:              WindowDecision(NORMAL_SHADE, normal_shade_position)
Because PositionResolver takes max() of all Tier 4 floors, the "at least
NORMAL_SHADE" semantic is preserved.

Scope:
  - Reads only wdi.outdoor_temp_c, wdi.indoor_temp_c, and
    wdi.effective_behavior.heat_outdoor_threshold_c /
    heat_indoor_threshold_c / normal_shade_position.
  - No lifecycle state, absence, glare, solar, or config hierarchy (INV-18).
  - No HA dependency.

Thresholds (from BehaviorConfig, pre-resolved by build_window_decision_input()):
  heat_outdoor_threshold_c: None → outdoor check disabled
  heat_indoor_threshold_c:  None → indoor check disabled
  Both None                 → heat protection disabled; evaluator always returns None.
"""
from __future__ import annotations

from ..models.window_decision import WindowDecision
from ..models.window_decision_input import WindowDecisionInput
from ..state_machine.states import ShadingState


class HeatEvaluator:
    """Tier 4 Protection Floor: NORMAL_SHADE when heat thresholds are exceeded.

    Returns a WindowDecision for NORMAL_SHADE when heat protection is active
    and at least one temperature measurement exceeds its configured threshold.

    Returns None when:
      - Both heat_outdoor_threshold_c and heat_indoor_threshold_c are None
        (heat protection disabled for this window).
      - Neither measurement exceeds its threshold (heat not needed now).
      - A threshold is set but the corresponding sensor value is None
        (sensor unavailable — fail-safe: do not trigger from missing data).
    """

    def evaluate(self, wdi: WindowDecisionInput) -> WindowDecision | None:
        b = wdi.effective_behavior

        # Fast exit: both thresholds None → heat protection entirely disabled.
        if b.heat_outdoor_threshold_c is None and b.heat_indoor_threshold_c is None:
            return None

        heat_needed = False

        if (
            b.heat_outdoor_threshold_c is not None
            and wdi.outdoor_temp_c is not None
            and wdi.outdoor_temp_c >= b.heat_outdoor_threshold_c
        ):
            heat_needed = True

        if (
            b.heat_indoor_threshold_c is not None
            and wdi.indoor_temp_c is not None
            and wdi.indoor_temp_c >= b.heat_indoor_threshold_c
        ):
            heat_needed = True

        if not heat_needed:
            return None

        # Exposure gate: only shade for heat when the sun currently has a path
        # to this window's glazing. Without solar exposure the window is not
        # contributing to heat gain via radiation, so closing it for heat
        # protection would block airflow with no thermal benefit.
        if not wdi.is_in_solar_sector and (
            wdi.exposure is None or wdi.exposure.effective_exposure <= 0
        ):
            return None

        return WindowDecision(
            window_id=wdi.window_config.id,
            shading_state=ShadingState.NORMAL_SHADE,
            target_position=b.normal_shade_position,
            decided_by="HeatEvaluator",
        )
