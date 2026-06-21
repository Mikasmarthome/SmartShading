"""SituationJoiner — Phase 9F5-1.

Joins StateTransitionRecord and DecisionOutcome into SituationRecord objects
that provide the full environmental + outcome context needed by the Similarity
Engine.

Join rules:
  - Key: (window_id, decision_timestamp) — exact match, no fuzzy/time-window
  - Both sides must be present; unmatched records are silently discarded
  - Records where outcome_score is None (unresolved) are silently discarded
  - Result is sorted newest-first, mirroring LearningStore query conventions

SituationRecord is a frozen, HA-independent dataclass. It is a transient
runtime object — never persisted and never stored in a LearningStore buffer.

Duplicate handling:
  If multiple DecisionOutcome records share the same (window_id,
  decision_timestamp) key the last one in the input list wins (dict overwrite
  semantics). In practice the ring buffer guarantees uniqueness per window.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..models.learning import DecisionOutcome, StateTransitionRecord
from ..state_machine.states import ShadingState


@dataclass(frozen=True)
class SituationRecord:
    """Full environmental + outcome context for one evaluator decision.

    Built by joining StateTransitionRecord (environmental context) with
    DecisionOutcome (outcome signal).  All optional fields are None when
    the corresponding sensor was not configured at decision time.
    """

    # Identification
    window_id: str
    decision_timestamp: datetime

    # Outcome context (from DecisionOutcome)
    from_state: ShadingState | None
    decided_state: ShadingState
    decided_by: str
    lifecycle_state: str

    # Outcome signals (from DecisionOutcome)
    outcome_score: float                # always set — None records are excluded at join time
    override_occurred: bool
    override_delay_min: float | None
    resolution_status: str

    # Solar context (from StateTransitionRecord)
    effective_exposure_wm2: float | None
    sun_elevation: float | None
    solar_relative_azimuth: float | None

    # Temperature context (decision-side from DecisionOutcome; outdoor from Transition)
    indoor_temp_at_decision: float | None
    outdoor_temp_c: float | None

    # Presence context (from StateTransitionRecord)
    absence_active: bool

    # Thermal outcome (from DecisionOutcome; None when resolution was not "complete")
    indoor_temp_delta_c: float | None = None  # indoor_temp_outcome_c − indoor_temp_at_decision


def build_situations(
    transitions: list[StateTransitionRecord],
    outcomes: list[DecisionOutcome],
) -> list[SituationRecord]:
    """Join *transitions* and *outcomes* into SituationRecords, newest first.

    Both lists may contain records for multiple windows — they are matched
    exclusively by (window_id, decision_timestamp).

    Records that have no matching counterpart, or where outcome_score is None,
    are silently discarded. No exception is raised for unmatched records.
    """
    # Build an outcome lookup keyed by the exact join key.
    # Later entries overwrite earlier ones on duplicate keys (see module docstring).
    outcome_lookup: dict[tuple[str, datetime], DecisionOutcome] = {}
    for outcome in outcomes:
        if outcome.outcome_score is None:
            continue  # unresolved — not yet useful for similarity
        outcome_lookup[(outcome.window_id, outcome.decision_timestamp)] = outcome

    situations: list[SituationRecord] = []

    for transition in transitions:
        key = (transition.window_id, transition.timestamp)
        outcome = outcome_lookup.get(key)
        if outcome is None:
            continue  # no matching outcome for this transition

        situations.append(SituationRecord(
            window_id=transition.window_id,
            decision_timestamp=transition.timestamp,
            from_state=outcome.from_state,
            decided_state=outcome.decided_state,
            decided_by=outcome.decided_by,
            lifecycle_state=outcome.lifecycle_state,
            outcome_score=outcome.outcome_score,
            override_occurred=outcome.override_occurred,
            override_delay_min=outcome.override_delay_min,
            resolution_status=outcome.resolution_status,
            effective_exposure_wm2=transition.effective_exposure_wm2,
            sun_elevation=transition.sun_elevation,
            solar_relative_azimuth=transition.solar_relative_azimuth,
            indoor_temp_at_decision=outcome.indoor_temp_at_decision,
            outdoor_temp_c=transition.outdoor_temp_c,
            absence_active=transition.absence_active,
            indoor_temp_delta_c=outcome.indoor_temp_delta_c,
        ))

    situations.sort(key=lambda s: s.decision_timestamp, reverse=True)
    return situations
