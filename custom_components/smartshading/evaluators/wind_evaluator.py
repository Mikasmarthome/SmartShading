"""WindEvaluator — Tier 1 Safety Guard: Wind Protection.

Returns WIND_SAFE (position 0 = retracted/open) when sustained wind or
gusts exceed the configured threshold and wind protection is enabled.

Wind Protection is opt-in (wind_protection_enabled = False by default).
Wind damage risk depends on cover type and mounting situation — an
installation with only roller shutters does not need wind protection.

Compare:
  Storm Protection — always-on; structural damage at storm-level forces.
  Frost Protection — deferred; requires a Cover-Type model in WindowConfig.

Fail-safe: if wind data is unavailable, no decision is produced.
"""
from __future__ import annotations

from ..models.window_decision import WindowDecision
from ..models.window_decision_input import WindowDecisionInput
from ..state_machine.states import ShadingState

_WIND_SAFE_POSITION = 0  # internal convention: 0 = fully open / retracted


class WindEvaluator:
    """Tier 1 Safety Guard: WIND_SAFE.

    Fires when ALL of the following hold:
      - effective_behavior.wind_protection_enabled is True (opt-in), AND
      - effective wind (gust if available, else sustained speed) >=
        effective_behavior.wind_threshold_ms (default 14 m/s, Beaufort 6).

    Returns None when:
      - wind_protection_enabled is False (the default), OR
      - both wdi.wind_gust_ms and wdi.wind_speed_ms are None (fail-safe).
    """

    def evaluate(self, wdi: WindowDecisionInput) -> WindowDecision | None:
        if not wdi.effective_behavior.wind_protection_enabled:
            return None

        effective_wind = (
            wdi.wind_gust_ms
            if wdi.wind_gust_ms is not None
            else wdi.wind_speed_ms
        )

        # Fail-safe: missing sensor data → no trigger.
        if effective_wind is None:
            return None

        if effective_wind < wdi.effective_behavior.wind_threshold_ms:
            return None

        return WindowDecision(
            window_id=wdi.window_config.id,
            shading_state=ShadingState.WIND_SAFE,
            target_position=_WIND_SAFE_POSITION,
            decided_by="WindEvaluator",
        )
