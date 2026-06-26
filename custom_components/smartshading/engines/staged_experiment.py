"""Bounded staged experiments — LE 2.0 / Increment 3H (pure).

Defines the safe escalation contract for a bounded experiment key:

    supported proposal
    → conservative Stage-1 (5 pp close-more)
    → complete, attributable, NON-degraded outcome
    → only then, on a LATER distinct day in the SAME context family, after the
      real cooldown:  Stage-2 (10 pp TOTAL close-more vs the authoritative
      baseline — never 5+10)
    → never a Stage 3
    → never more than EXPERIMENT_CUMULATIVE_CAP_HA cumulative deviation

and the shade-level monotonicity guard (strong < normal < light in HA
convention, with a minimum spacing between levels) so a close-more experiment on
one intensity can never cross the next-stronger configured level.

No Home Assistant import.  Pure functions / frozen dataclasses.  HA position
convention (0=closed, 100=open).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..models.bounded_experiment import (
    EVAL_IMPROVED,
    EVAL_NO_DEGRADATION,
    EXPERIMENT_MAX_STAGE,
    STATUS_ACCEPTED_FOR_P8,
    STATUS_COMPLETED,
    stage_step_ha,
)

# Minimum gap (HA pp) that must remain between two adjacent shade levels so an
# experiment never collapses the strong/normal/light ordering.
MIN_SHADE_LEVEL_SPACING_HA: int = 5

_NON_NEGATIVE = (EVAL_IMPROVED, EVAL_NO_DEGRADATION)
_ATTRIBUTABLE_STATUSES = (STATUS_COMPLETED, STATUS_ACCEPTED_FOR_P8)


@dataclass(frozen=True)
class StageDecision:
    """The stage a NEW experiment for a key should take, plus lineage/audit."""

    stage: int
    target_step_ha: int
    previous_experiment_id: str | None
    previous_stage_evaluation: str | None
    escalation_eligible: bool          # True iff this is a real Stage-2 escalation
    block_reason: str | None           # why escalation did NOT happen (stays Stage 1)


def evaluate_stage_escalation(
    *,
    terminal_experiments_for_key: list,
    now: datetime,
) -> StageDecision:
    """Decide the stage for a NEW experiment of one (window,intensity,context) key.

    ``terminal_experiments_for_key`` are the already-completed BoundedExperiments
    that share the key (any terminal status).  Escalation to Stage 2 is allowed
    only when the most recent terminal experiment was a complete, attributable,
    non-confounded, NON-degraded outcome on an EARLIER distinct day.  Otherwise a
    fresh Stage 1 is returned (with a block_reason when a prior existed).
    """
    prior = None
    for e in terminal_experiments_for_key:
        if getattr(e, "completed_at", None) is None:
            continue
        if prior is None or e.completed_at > prior.completed_at:
            prior = e

    if prior is None:
        return StageDecision(1, stage_step_ha(1), None, None, False, None)

    prev_eval = getattr(prior.evaluation, "decision", None)

    # Already at the maximum stage → never escalate further (no Stage 3); a new
    # experiment continues at the max stage to accumulate evidence.
    if prior.stage >= EXPERIMENT_MAX_STAGE and prev_eval in _NON_NEGATIVE:
        if prior.completed_at.date() >= now.date():
            return StageDecision(1, stage_step_ha(1), None, prev_eval, False,
                                 "same_day_no_escalation")
        return StageDecision(
            EXPERIMENT_MAX_STAGE, stage_step_ha(EXPERIMENT_MAX_STAGE),
            prior.experiment_id, prev_eval, True, None)

    if prev_eval not in _NON_NEGATIVE:
        return StageDecision(1, stage_step_ha(1), None, prev_eval, False,
                             "prior_not_non_negative")
    if prior.status not in _ATTRIBUTABLE_STATUSES:
        return StageDecision(1, stage_step_ha(1), None, prev_eval, False,
                             "prior_not_attributable")
    if getattr(prior.evaluation, "confounders", ()):
        return StageDecision(1, stage_step_ha(1), None, prev_eval, False,
                             "prior_confounded")
    if prior.completed_at.date() >= now.date():
        return StageDecision(1, stage_step_ha(1), None, prev_eval, False,
                             "same_day_no_escalation")

    new_stage = min(EXPERIMENT_MAX_STAGE, prior.stage + 1)
    return StageDecision(
        new_stage, stage_step_ha(new_stage),
        prior.experiment_id, prev_eval, new_stage > 1, None)


def enforce_monotonic_spacing(
    *,
    intensity_level: str,
    candidate_ha: int,
    stronger_neighbor_ha: int | None,
    min_spacing_ha: int = MIN_SHADE_LEVEL_SPACING_HA,
) -> tuple[int, bool]:
    """Clamp a close-more experiment target so it never crosses (or comes within
    ``min_spacing_ha`` of) the next-stronger configured shade level.

    HA convention: lower value = more closed, so strong_ha < normal_ha < light_ha.
    A close-more experiment lowers the value of its own level; it must remain at
    least ``min_spacing_ha`` ABOVE the stronger neighbour.  ``strong`` has no
    stronger neighbour and is returned unchanged.  Returns (clamped, was_clamped).
    """
    if stronger_neighbor_ha is None:
        return candidate_ha, False
    floor = stronger_neighbor_ha + max(0, min_spacing_ha)
    if candidate_ha < floor:
        return floor, True
    return candidate_ha, False
