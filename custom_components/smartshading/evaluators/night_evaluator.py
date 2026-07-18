"""NightEvaluator — Tier 3 Lifecycle Phase Gate.

Responsibility: return a WindowDecision when the current lifecycle phase
is NIGHT and night shading is configured for this window.

Scope:
  - Reads only wdi.lifecycle_state and wdi.effective_behavior.night_position.
  - Has no knowledge of Manual Override, Absence, Heat, Glare, or any
    other tier.  The orchestrator (TierOrchestrator, Step 4) ensures that
    Tier 2 (Manual Override) is checked before this evaluator is called.
  - Has no dependency on Coordinator, StateGuard, HA state, or config
    hierarchy (INV-18: all config was pre-resolved in WindowDecisionInput).
"""
from __future__ import annotations

from ..models.window_decision import WindowDecision
from ..models.window_decision_input import WindowDecisionInput
from ..models.lifecycle import LifecycleState
from ..state_machine.states import DecisionCategory, ShadingState


class NightEvaluator:
    """Tier 3 Lifecycle Phase Gate: NIGHT.

    Returns a WindowDecision for NIGHT_CLOSED when the lifecycle state is
    NIGHT and the window has a configured night_position.

    Returns None in all other cases, including:
      - lifecycle_state is DAY, MORNING, or EVENING
      - effective_behavior.night_position is None (night shading disabled
        for this window via global/zone/window config)
    """

    def evaluate(self, wdi: WindowDecisionInput) -> WindowDecision | None:
        if wdi.lifecycle_state is not LifecycleState.NIGHT:
            return None

        night_position = wdi.effective_behavior.night_position
        if night_position is None:
            return None

        return WindowDecision(
            window_id=wdi.window_config.id,
            shading_state=ShadingState.NIGHT_CLOSED,
            target_position=night_position,
            decided_by="NightEvaluator",
            category=DecisionCategory.LIFECYCLE,
        )
