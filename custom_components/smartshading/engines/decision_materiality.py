"""Decision materiality / deduplication — LE 2.0 / Phase P2.2.

A pure, deterministic gate that decides whether the decision computed this
coordinator cycle is *materially* different from the last persisted decision
for the same window.  Only material decisions create a LearningDecisionRecord;
unchanged cycles create nothing, preventing an event flood while still
capturing same-state target changes.

No Home Assistant imports.  No state.  Same inputs → same result.
"""
from __future__ import annotations

from ..models.decision_provenance import DecisionCandidate, DecisionSummary

# Minimum HA-position change (percentage points) that counts as material.
# Aligned with CommandFilter tolerance / position dead-band conventions.
MATERIAL_TARGET_DELTA_HA: int = 3


def _target_changed(prev: int | None, cur: int | None) -> bool:
    """True when a target moved materially.

    A transition between None (hold / no target) and a concrete value is
    always material.  Two concrete values differ materially when their
    absolute difference is >= MATERIAL_TARGET_DELTA_HA.
    """
    if prev is None and cur is None:
        return False
    if prev is None or cur is None:
        return True
    return abs(cur - prev) >= MATERIAL_TARGET_DELTA_HA


def is_material_learning_decision(
    previous: DecisionSummary | None,
    current: DecisionCandidate,
) -> bool:
    """Return True when *current* warrants a new persistent decision record.

    Material when ANY of:
      - previous is None (first decision for this window)
      - shading state changed
      - baseline target changed materially (>= 3 pp, or None<->value)
      - final requested target changed materially
      - the set of adaptation sources changed
      - a dispatch was attempted this cycle with a new target or changed status
      - the command-filter reason or suppression reason changed
      - the shadow/experiment status changed

    Not material → no record (unchanged cycle).
    """
    if previous is None:
        return True

    if current.shading_state != previous.shading_state:
        return True

    if _target_changed(previous.baseline_target_ha, current.baseline_target_ha):
        return True

    if _target_changed(previous.final_target_ha, current.final_target_ha):
        return True

    if current.adaptation_sources != previous.adaptation_sources:
        return True

    # Dispatch became relevant: a new attempt, or a changed dispatch status.
    if current.dispatch_attempted and (
        not previous.dispatch_attempted
        or current.dispatch_status != previous.dispatch_status
    ):
        return True

    if current.filter_reason != previous.filter_reason:
        return True

    if current.suppression_reason != previous.suppression_reason:
        return True

    if current.shadow_experiment_status != previous.shadow_experiment_status:
        return True

    return False
