"""RainEvaluator — Tier 1 Safety Guard: Rain Protection.

Returns RAIN_SAFE (at the configured rain-safe position) when rain is detected
and rain protection is enabled for this window.

Rain Protection is opt-in per window.  Hardware-type defaults:
  - AWNING:          on  (rain damages awning fabric)
  - EXTERIOR_SCREEN: on  (water retention damages screen mesh)
  - ROLLER_SHUTTER:  off (structurally unaffected by rain; may even deter condensation)
  - VENETIAN_BLIND:  off
  - GENERIC:         off

Fail-safe: if rain sensor is UNKNOWN (unavailable/stale), no new RAIN_SAFE
trigger is produced.  Any active RAIN_SAFE hold (hysteresis + dry cooldown)
is maintained by the coordinator's SafetyHold tracker.

Compare:
  Storm Protection — always-on; structural damage at storm-level forces.
  Wind Protection  — opt-in; wind damage risk is cover-type-dependent.
"""
from __future__ import annotations

from ..engines.rain_engine import RainStatus
from ..models.window_decision import WindowDecision
from ..models.window_decision_input import WindowDecisionInput
from ..state_machine.states import ShadingState


class RainEvaluator:
    """Tier 1 Safety Guard: RAIN_SAFE.

    Fires when ALL of the following hold:
      - effective_behavior.rain_protection_enabled is True (opt-in), AND
      - wdi.rain_status is RainStatus.RAINING (confirmed, not UNKNOWN).

    Returns None when:
      - rain_protection_enabled is False, OR
      - rain_status is None or UNKNOWN (fail-safe: absent data → no trigger).
    """

    def evaluate(self, wdi: WindowDecisionInput) -> WindowDecision | None:
        if not wdi.effective_behavior.rain_protection_enabled:
            return None

        rain_status = wdi.rain_status
        if rain_status is None or rain_status is not RainStatus.RAINING:
            return None

        safe_pos = wdi.effective_behavior.rain_safe_position
        if safe_pos is None:
            # rain_protection_enabled=True but no safe position resolved — fall back to 0
            safe_pos = 0

        return WindowDecision(
            window_id=wdi.window_config.id,
            shading_state=ShadingState.RAIN_SAFE,
            target_position=safe_pos,
            decided_by="RainEvaluator",
        )
