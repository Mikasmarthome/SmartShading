"""PendingOutcome dataclass — Phase 9F4b-1.

A PendingOutcome is created at each qualifying StateTransition and holds the
minimal context needed to later resolve a DecisionOutcome. It is a pure
observation structure — no scoring, no resolution, no persistence.

Design constraints:
  - Frozen: after creation, the observation context cannot change.
  - No weather or solar fields: those are already captured in the
    corresponding StateTransitionRecord (linked by window_id + decision_timestamp).
  - No redundancy with StateTransitionRecord.
  - No Home Assistant imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..state_machine.states import ShadingState


@dataclass(frozen=True)
class PendingOutcome:
    """Immutable observation context for a shading decision under evaluation.

    Lifecycle:
        created  → PendingOutcomeQueue.create()  (at StateTransition)
        active   → observation window (max indoor_temp_outcome_delay_min minutes)
        resolved → PendingOutcomeQueue.remove()  (by Resolution logic, 9F4b-2)

    One PendingOutcome exists per window at most. A new StateTransition while
    one is active triggers replace() — the old one is returned for resolution
    before the new one is stored.
    """

    window_id: str
    decision_timestamp: datetime
    from_state: ShadingState
    to_state: ShadingState
    decided_by: str
    lifecycle_state: str
    indoor_temp_outcome_delay_min: int
    target_position: int | None = None
    indoor_temp_at_decision: float | None = None
    outdoor_temp_at_decision: float | None = None
