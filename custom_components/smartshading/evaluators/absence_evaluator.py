"""AbsenceEvaluator — Tier 4 Protection Floor.

Responsibility: return a WindowDecision encoding the minimum shading floor
when the occupant is absent and absence shading is configured.

This is a FLOOR, not an absolute target.  The PositionResolver (Step 3)
collects the target_position values from all active Tier 4 evaluators and
takes max(), so the cover never goes below the most restrictive floor but
may be pushed further closed by another active Tier 4 evaluator (e.g. heat
protection demanding a higher position than absence alone).

Scope:
  - Reads only wdi.absence_active and wdi.effective_behavior.absence_position.
  - No knowledge of lifecycle state, Manual Override, Heat, Glare, or any
    other tier.  The orchestrator decides in which order tiers are called
    and whether to apply early exits.
  - No dependency on Coordinator, StateGuard, HA state, or config hierarchy
    (INV-18: all config was pre-resolved in WindowDecisionInput).
"""
from __future__ import annotations

from ..models.window_decision import WindowDecision
from ..models.window_decision_input import WindowDecisionInput
from ..state_machine.states import DecisionCategory, ShadingState


class AbsenceEvaluator:
    """Tier 4 Protection Floor: ABSENCE_CLOSED.

    Returns a WindowDecision for ABSENCE_CLOSED — encoding the minimum
    position floor — when absence is active and the window has a configured
    absence_position.

    Returns None when:
      - absence is not active (wdi.absence_active is False)
      - effective_behavior.absence_position is None (absence shading disabled
        for this window via global/zone/window config)

    The caller (PositionResolver / TierOrchestrator) is responsible for
    treating the returned target_position as a floor, not an absolute target.
    """

    def evaluate(self, wdi: WindowDecisionInput) -> WindowDecision | None:
        if not wdi.absence_active:
            return None

        absence_position = wdi.effective_behavior.absence_position
        if absence_position is None:
            return None

        return WindowDecision(
            window_id=wdi.window_config.id,
            shading_state=ShadingState.ABSENCE_CLOSED,
            target_position=absence_position,
            decided_by="AbsenceEvaluator",
            category=DecisionCategory.PROTECTION,
        )
