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

from ..const import HEAT_MIN_EFFECTIVE_EXPOSURE_WM2
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

        # Sector gate: only shade for heat when the sun is confirmed in this
        # window's effective solar sector. is_in_solar_sector already incorporates
        # the manual sector override and obstruction zones. Using the automatic
        # tolerance sector (via exposure.effective_exposure) would incorrectly
        # trigger when a manual sector blocks the sun but the auto-tolerance
        # sector still matches — the exposure engine is driven by the automatic
        # geometry, not by the manual sector override.
        if not wdi.is_in_solar_sector:
            return None

        # Effective-exposure gate: heat protection blocks SOLAR heat gain, so a
        # window that is only GEOMETRICALLY in the sun sector but receives almost
        # no solar energy (e.g. heavy cloud damps effective exposure to near zero)
        # has no solar heat to protect against.  When a measured/effective exposure
        # reading is available and is below the meaningful-solar floor, do not shade
        # for heat.  A missing exposure reading (no sun data at all) keeps the prior
        # temperature+sector behaviour — this gate only ever SUPPRESSES a shade, so
        # it makes HeatEvaluator more conservative on weak/uncertain solar input,
        # never more aggressive.
        if (
            wdi.exposure is not None
            and wdi.exposure.effective_exposure < HEAT_MIN_EFFECTIVE_EXPOSURE_WM2
        ):
            return None

        return WindowDecision(
            window_id=wdi.window_config.id,
            shading_state=ShadingState.NORMAL_SHADE,
            target_position=b.normal_shade_position,
            decided_by="HeatEvaluator",
        )
