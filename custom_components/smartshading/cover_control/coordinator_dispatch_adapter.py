"""Target-aware Dispatch — Phase 4a: coordinator adapter to DispatchPlanItem.

Pure projection layer between the coordinator's already-finalized per-cycle
decision data (CoverIntent, live current position, dispatch generation) and
the Phase 1/2 DispatchTargetClass/DispatchPlanItem/DispatchPlan model.

This module computes NOTHING new:
  - no target position (already decided by CommandFilter/build_execution_plan)
  - no authority/decision re-evaluation
  - no classification re-derivation beyond the pure Phase 1 mapping
  - no service capability decision
  - no dispatch execution, no HA import, no coordinator import

Safety intents must never reach build_dispatch_plan_item() — callers filter
by `not intent.is_safety` beforehand (see the Phase 4 callsite audit: safety
keeps its existing, unmodified fastlane path).
"""
from __future__ import annotations

from ..engines.dispatch_classification import (
    DispatchClassification,
    DispatchTargetClass,
    classify_dispatch_target,
)
from .dispatch_completion import DEFAULT_POSITION_TOLERANCE
from .dispatch_plan import DispatchPlan, DispatchPlanItem, build_dispatch_plan
from .execution_plan import CoverCommandType, CoverIntent

# Diagnostic-only classes — reason is preserved for explainability, never
# consulted by the executor to decide whether/how to dispatch (see
# T22 Phase 3's decision_ref/reason passivity tests).
_DIAGNOSTIC_ONLY_CLASSES = frozenset(
    {DispatchTargetClass.NO_MOVEMENT, DispatchTargetClass.BLOCKED}
)


def classify_cover_intent(
    intent: CoverIntent,
    *,
    current_position_ha: int | None,
    position_tolerance: int = DEFAULT_POSITION_TOLERANCE,
) -> DispatchClassification:
    """Project one already-decided CoverIntent onto a DispatchTargetClass.

    Pure field mapping onto classify_dispatch_target() — reuses exactly the
    values CommandFilter/build_execution_plan already produced:
      resolved_target_ha <- intent.target_position_ha
      blocked_reason      <- intent.blocked_reason, but ONLY when
                               command_type is BLOCKED. build_execution_plan's
                               own _command_type_from_filter() already
                               distinguishes a genuine hard block
                               (CoverCommandType.BLOCKED) from an
                               allowed=False result that is really "already
                               at target / no target" (CoverCommandType.NO_OP,
                               for BLOCKED_SAME_POSITION / BLOCKED_NO_TARGET_
                               POSITION) — passing blocked_reason through for
                               the NO_OP case would wrongly reclassify a
                               same-position item as BLOCKED instead of
                               NO_MOVEMENT.
      dispatch_action      <- "unchanged" when intent.command_type is NO_OP
                               (build_execution_plan's own existing signal
                               that nothing changed / no useful action
                               exists — never re-derived here)
      is_safety             <- intent.is_safety

    position_tolerance defaults to dispatch_completion.DEFAULT_POSITION_TOLERANCE
    (the existing HA-scale "already at target" tolerance already used by
    wait_for_travel_completion) — the same scale classify_dispatch_target
    operates on, so no second tolerance concept is introduced.
    """
    blocked_reason = (
        intent.blocked_reason if intent.command_type is CoverCommandType.BLOCKED else None
    )
    dispatch_action = (
        "unchanged" if intent.command_type is CoverCommandType.NO_OP else None
    )
    return classify_dispatch_target(
        resolved_target_ha=intent.target_position_ha,
        current_position_ha=current_position_ha,
        dispatch_action=dispatch_action,
        blocked_reason=blocked_reason,
        position_tolerance=position_tolerance,
        is_safety=intent.is_safety,
    )


def build_dispatch_plan_item(
    *,
    zone_id: str,
    zone_index: int,
    cover_index: int,
    intent: CoverIntent,
    classification: DispatchClassification,
    decision_ref: str,
    zone_generation: int,
) -> DispatchPlanItem:
    """Turn one already-classified CoverIntent into a DispatchPlanItem.

    Never recomputes target, authority, or classification — only maps
    already-finalized values into the Phase 2 shape.
    """
    return DispatchPlanItem(
        zone_id=zone_id,
        zone_index=zone_index,
        cover_entity_id=intent.cover_entity_id,
        cover_index=cover_index,
        target_ha=classification.normalized_target,
        target_class=classification.target_class,
        decision_ref=decision_ref,
        zone_generation=zone_generation,
        reason=(
            classification.reason
            if classification.target_class in _DIAGNOSTIC_ONLY_CLASSES
            else None
        ),
    )


def build_cycle_dispatch_plan(
    *,
    entry_id: str,
    cycle_counter: int,
    items,
    trigger: str,
    created_at,
) -> DispatchPlan:
    """Build the one DispatchPlan for this coordinator cycle.

    Plan-ID is derived from already-stable existing IDs
    (`<entry_id>:<cycle_counter>`) — no hidden module-level counter, no
    UUID, no persistence. Unique per running plan because `cycle_counter`
    is a per-coordinator monotonic counter already incremented once per
    _async_update_data() call (coordinator.py's self._cycle_counter).
    """
    plan_id = f"{entry_id}:{cycle_counter}"
    return build_dispatch_plan(
        plan_id=plan_id, trigger=trigger, items=items, created_at=created_at,
    )
