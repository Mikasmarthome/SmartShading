"""StormEvaluator — Tier 1 Safety Guard: Storm Protection.

Returns STORM_SAFE (position 0 = retracted/open) when storm conditions are
detected via weather_condition code or wind speed/gust exceeding the
storm threshold.

Storm Protection is always active when storm_protection_enabled is True
(the default).  It is NOT cover-type dependent — structural storm damage
affects all exterior cover types at the intensity levels defined by
STORM_CONDITIONS and DEFAULT_STORM_WIND_THRESHOLD_MS.

Compare:
  Wind Protection  — opt-in; cover-type-dependent wind risk.
  Frost Protection — deferred; requires a Cover-Type model in WindowConfig.

Fail-safe: if both weather_condition and wind data are unavailable,
no decision is produced (consistent with "missing data = no trigger"
across all evaluators).
"""
from __future__ import annotations

from ..engines.weather_engine import DEFAULT_STORM_WIND_THRESHOLD_MS, STORM_CONDITIONS
from ..models.window_decision import WindowDecision
from ..models.window_decision_input import WindowDecisionInput
from ..state_machine.states import DecisionCategory, ShadingState

_STORM_SAFE_POSITION = 0  # internal convention: 0 = fully open / retracted


class StormEvaluator:
    """Tier 1 Safety Guard: STORM_SAFE.

    Fires when storm_protection_enabled is True AND either:
      - wdi.weather_condition is in STORM_CONDITIONS (STORM, THUNDERSTORM, HAIL), OR
      - effective wind (gust if available, else sustained speed) >=
        DEFAULT_STORM_WIND_THRESHOLD_MS (20 m/s).

    Returns None when:
      - effective_behavior.storm_protection_enabled is False, OR
      - both weather_condition and wind data are None (fail-safe).
    """

    def evaluate(self, wdi: WindowDecisionInput) -> WindowDecision | None:
        if not wdi.effective_behavior.storm_protection_enabled:
            return None

        condition = wdi.weather_condition
        effective_wind = (
            wdi.wind_gust_ms
            if wdi.wind_gust_ms is not None
            else wdi.wind_speed_ms
        )

        # Fail-safe: missing sensor data → no trigger.
        if condition is None and effective_wind is None:
            return None

        is_storm = (
            (condition is not None and condition in STORM_CONDITIONS)
            or (effective_wind is not None and effective_wind >= DEFAULT_STORM_WIND_THRESHOLD_MS)
        )

        if not is_storm:
            return None

        return WindowDecision(
            window_id=wdi.window_config.id,
            shading_state=ShadingState.STORM_SAFE,
            target_position=_STORM_SAFE_POSITION,
            decided_by="StormEvaluator",
            category=DecisionCategory.SAFETY,
        )
