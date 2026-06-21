"""ManualOverrideEvaluator — Tier 2 gate for active manual overrides.

If WindowDecisionInput.active_override is set, this evaluator returns a
MANUAL_OVERRIDE decision that holds the cover at the user's chosen position.

This evaluator is intentionally thin — all override lifecycle logic (detection,
expiry, renewal) lives in OverrideDetector (engines/override_detector.py).
The evaluator is stateless and only acts as the Tier 2 pipeline stage.

Priority invariant: MANUAL_OVERRIDE (rank 10) is checked after Tier 1 Safety
Guards (ranks 1, 2). Storm/Wind always beat a manual override.
"""
from __future__ import annotations

from ..models.window_decision import WindowDecision
from ..models.window_decision_input import WindowDecisionInput
from ..state_machine.states import ShadingState


class ManualOverrideEvaluator:
    """Tier 2: return MANUAL_OVERRIDE when an active override is present.

    Usage:
        evaluator = ManualOverrideEvaluator()
        result = evaluator.evaluate(wdi)  # None or WindowDecision(MANUAL_OVERRIDE)
    """

    def evaluate(self, wdi: WindowDecisionInput) -> WindowDecision | None:
        """Return MANUAL_OVERRIDE at the user's position, or None if no override."""
        if wdi.active_override is None:
            return None
        return WindowDecision(
            window_id=wdi.window_config.id,
            shading_state=ShadingState.MANUAL_OVERRIDE,
            target_position=wdi.active_override.override_position,
            decided_by="ManualOverrideEvaluator",
        )
