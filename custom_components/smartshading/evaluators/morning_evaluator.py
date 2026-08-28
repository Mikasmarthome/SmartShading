"""MorningEvaluator — Tier 3 Lifecycle Phase Gate counterpart to NightEvaluator.

Responsibility: return a WindowDecision candidate for the configured morning
position on the MORNING lifecycle transition cycle, AND keep re-proposing
that same target on every following DAY cycle for as long as
engines/morning_reconciliation.py's per-window commitment for today's
occurrence is still `pending` (not yet confirmed reached, not superseded by
a genuinely stronger protective tier, not invalidated by a real changed
fact). This is the B3-010 Morning Reconciliation design: the commitment is
an explicit, persisted, fact-based state (pending/satisfied/superseded/
invalidated), not a bare one-cycle candidate throttled only by
ComfortMovementHold's timer.

Not an early-exit tier, unlike NightEvaluator. NightEvaluator's NIGHT
decision always wins outright (night protection is never optional). Morning
is the opposite: it is only the correct answer when nothing more protective
is required at that exact moment. The orchestrator therefore folds this
evaluator's result into the SAME PositionResolver arbitration pool as Tier 4
(Absence/Heat/Glare) and Tier 5 (Solar) -- the existing max(target_position)
rule already picks whichever position is more shaded, so a Heat/Glare/
Absence floor or an already-required Solar shade naturally wins over the
Morning target without any special-case comparison code, and that same win
is what the Coordinator records as `superseded` for the reconciliation
record (see morning_reconciliation.supersede()) — no second arbitration
path, no duplicated "is this stronger" comparison.

Scope:
  - Reads only wdi.lifecycle_state, wdi.effective_behavior.morning_position,
    and wdi.morning_reconciliation_pending_target_internal.
  - Has no knowledge of Manual Override, Absence, Heat, Glare, Solar, or any
    other tier — exactly like NightEvaluator (INV-18).
  - Has no dependency on Coordinator, StateGuard, HA state, or config
    hierarchy. effective_behavior.morning_position is already the resolved
    weekday/weekend/shared value (LifecycleEngine.active_profile(), applied
    by the coordinator before build_window_decision_input() — see
    coordinator.py) and already HA->internal converted, exactly like
    night_position. morning_reconciliation_pending_target_internal is
    likewise fully pre-resolved by the Coordinator from the real
    persisted reconciliation record — this evaluator never touches that
    record's lifecycle itself (create/satisfy/supersede/invalidate all
    happen in coordinator.py, see engines/morning_reconciliation.py).
  - Windows in a behavior mode that must not receive lifecycle control at
    all (ABSENCE_ONLY, DISABLED_AUTOMATIC) never reach this evaluator with a
    non-None morning_position in the first place — coordinator.py's
    _apply_window_behavior_mode() nulls effective_behavior.morning_position
    for exactly those two modes (mirrors how it already nulls heat/glare/
    absence for them), so this evaluator itself needs no behavior-mode
    awareness.
"""
from __future__ import annotations

from ..models.window_decision import WindowDecision
from ..models.window_decision_input import WindowDecisionInput
from ..models.lifecycle import LifecycleState
from ..state_machine.states import DecisionCategory, ShadingState


class MorningEvaluator:
    """Tier 3 Lifecycle Phase Gate: Morning transition candidate (non-early-
    exit, one-cycle only).

    Returns a WindowDecision candidate for ShadingState.OPEN at
    morning_position exactly when the lifecycle state is MORNING and the
    window has a configured morning_position. ShadingState.OPEN is reused
    rather than a new enum value — MORNING_OPEN is documented
    (state_machine/states.py) as a tracking-only label for this lifecycle
    transition, not a real ShadingState.

    DecisionCategory.LIFECYCLE, exactly like NightEvaluator's NIGHT decision
    — an active Manual Override always blocks it (manual_override_policy.py:
    LIFECYCLE candidates are always blocked while an override is active),
    matching how it already ends the lifecycle transition's own bypass for
    Night.

    Returns None in all other cases, including:
      - lifecycle_state is not MORNING (DAY, NIGHT, EVENING)
      - effective_behavior.morning_position is None (no morning position
        configured for this window/zone, or masked out by behavior mode)
    """

    def evaluate(self, wdi: WindowDecisionInput) -> WindowDecision | None:
        if wdi.lifecycle_state is LifecycleState.MORNING:
            morning_position = wdi.effective_behavior.morning_position
            if morning_position is not None:
                return WindowDecision(
                    window_id=wdi.window_config.id,
                    shading_state=ShadingState.OPEN,
                    target_position=morning_position,
                    decided_by="MorningEvaluator",
                    category=DecisionCategory.LIFECYCLE,
                )

        # B3-010 Morning Reconciliation: while a prior MORNING transition's
        # commitment for this window is still `pending` (not yet satisfied,
        # superseded, or invalidated -- see engines/morning_reconciliation.py),
        # keep re-proposing its target every DAY cycle. This candidate stays
        # in the SAME Tier 3/4/5 arbitration pool as any other, so a
        # genuinely stronger Heat/Glare/Solar/Absence need still naturally
        # wins via the existing max(target_position) rule -- no special-case
        # comparison code, no second dispatch pipeline. The Coordinator is
        # responsible for resolving this field from the real reconciliation
        # record BEFORE building the WDI, gated on is_current_occurrence()
        # (today's date only -- no cross-day catch-up) and status=="pending".
        if wdi.morning_reconciliation_pending_target_internal is not None:
            return WindowDecision(
                window_id=wdi.window_config.id,
                shading_state=ShadingState.OPEN,
                target_position=wdi.morning_reconciliation_pending_target_internal,
                decided_by="MorningEvaluator",
                category=DecisionCategory.LIFECYCLE,
            )

        return None
