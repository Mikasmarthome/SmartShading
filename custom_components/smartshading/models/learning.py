"""Learning Foundation data models (Phase 9A).

These dataclasses define the complete observation vocabulary for the future
Learning Engine. They are pure domain models — no logic, no persistence, no
adaptive decisions, no Home Assistant dependencies.

Architecture invariants:
  - Learning is supplementary intelligence. Missing, corrupt, or absent
    learning data must never prevent normal shading decisions or cause any
    Coordinator failure. The Learning Foundation is additive only.
  - Learning may only influence Tier 4 and Tier 5 thresholds. Tiers 1
    (Storm/Wind Safety), 2 (Manual Override), and 3 (Lifecycle) are
    lernfest — the Learning Engine must never alter their behavior.
  - All positions stored here use internal convention (0=open, 100=shaded),
    consistent with the rest of the domain model.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ..models.multi_objective_outcome import MultiObjectiveOutcome
from ..state_machine.states import ShadingState

# Valid values for OverrideRecord.event_type.
# Provided as a module-level constant for runtime validation in Phase 9C.
OverrideEventType = Literal[
    "started", "expired", "renewed", "cleared_by_safety", "cleared_by_lifecycle"
]
OVERRIDE_EVENT_TYPES: tuple[str, ...] = (
    "started",
    "expired",
    "renewed",
    "cleared_by_safety",
    "cleared_by_lifecycle",   # Step 8c: Night/Morning lifecycle transition cleared the override
)


@dataclass(frozen=True)
class StateTransitionRecord:
    """Immutable record of a single ShadingState transition for one window.

    Captured whenever the active ShadingState changes (from_state != to_state).
    Provides the event backbone the Learning Engine uses to build a decision
    timeline and correlate conditions with outcomes.

    All sensor fields are optional because SmartShading operates with
    partial sensor coverage (fail-safe principle: missing data → no trigger).
    """

    timestamp: datetime
    window_id: str
    from_state: ShadingState
    to_state: ShadingState
    decided_by: str              # e.g. "HeatEvaluator", "ManualOverrideEvaluator"
    lifecycle_state: str
    absence_active: bool
    is_in_solar_sector: bool
    outdoor_temp_c: float | None = None
    indoor_temp_c: float | None = None
    solar_radiation_wm2: float | None = None
    wind_speed_ms: float | None = None
    # Step 9F1: sun position at transition time (from sun.sun + window.azimuth).
    sun_azimuth: float | None = None
    sun_elevation: float | None = None
    solar_relative_azimuth: float | None = None  # sun_azimuth − window.azimuth
    # Step 9F2: weather and exposure context at transition time.
    weather_condition: str | None = None
    cloud_cover_pct: float | None = None
    raw_solar_radiation_wm2: float | None = None   # ExposureEngine input (pre-impact-factor)
    effective_exposure_wm2: float | None = None    # ExposureEngine output (post-impact-factor)
    learned_solar_impact_factor: float | None = None  # 1.0 = no learning in effect


@dataclass(frozen=True)
class OverrideRecord:
    """Immutable record of a manual override lifecycle event.

    Captured on four occasions:
      "started"           — new override detected (position delta exceeded tolerance)
      "renewed"           — user moved cover again while override active
      "expired"           — override duration elapsed naturally
      "cleared_by_safety" — Tier-1 Safety (Storm/Wind) cleared the override

    override_duration_min is None on "started" and "renewed" events (duration
    is not yet known) and populated on "expired" / "cleared_by_safety".
    """

    timestamp: datetime
    window_id: str
    event_type: OverrideEventType
    lifecycle_state: str
    override_position: int | None = None         # internal convention (0=open, 100=shaded)
    overridden_state: ShadingState | None = None
    overridden_position: int | None = None       # internal convention
    override_duration_min: float | None = None   # None until the override ends
    outdoor_temp_c: float | None = None
    solar_radiation_wm2: float | None = None
    # Step 9F3: evaluator active at the time of the event.
    # Populated on started/renewed/cleared_by_safety (tier_decision available).
    # None on expired/cleared_by_lifecycle (pre-sun-branch, tier_decision absent).
    decided_by: str | None = None
    # Step 9F1: sun position at override-event time.
    sun_azimuth: float | None = None
    sun_elevation: float | None = None
    solar_relative_azimuth: float | None = None  # sun_azimuth − window.azimuth
    # Step 9F2: weather and exposure context at override-event time.
    weather_condition: str | None = None
    cloud_cover_pct: float | None = None
    raw_solar_radiation_wm2: float | None = None   # ExposureEngine input (pre-impact-factor)
    effective_exposure_wm2: float | None = None    # ExposureEngine output (post-impact-factor)
    learned_solar_impact_factor: float | None = None  # 1.0 = no learning in effect


@dataclass(frozen=True)
class WindowCycleSnapshot:
    """Immutable periodic state snapshot for one window.

    Captured every N coordinator cycles (default: 15, approximately once
    per 15 minutes at a 1-minute cycle interval) rather than every cycle,
    to limit storage volume. Provides the continuous-time signal the Learning
    Engine needs to correlate environmental conditions with shading outcomes.

    effective_exposure_wm2 is the ExposureEngine output after applying
    learned_solar_impact_factor and seasonal_factor — it captures the
    calibrated solar input the evaluators actually see.
    """

    timestamp: datetime
    window_id: str
    shading_state: ShadingState
    decided_by: str
    lifecycle_state: str
    absence_active: bool
    override_active: bool
    target_position: int | None = None
    outdoor_temp_c: float | None = None
    indoor_temp_c: float | None = None
    solar_radiation_wm2: float | None = None
    effective_exposure_wm2: float | None = None  # ExposureEngine output
    wind_speed_ms: float | None = None
    # Step 9F1: sun position at snapshot time.
    sun_azimuth: float | None = None
    sun_elevation: float | None = None
    solar_relative_azimuth: float | None = None  # sun_azimuth − window.azimuth
    # Step 9F2: weather and exposure context at snapshot time.
    # Note: effective_exposure_wm2 already exists above (original field).
    weather_condition: str | None = None
    cloud_cover_pct: float | None = None
    raw_solar_radiation_wm2: float | None = None   # ExposureEngine input (pre-impact-factor)
    learned_solar_impact_factor: float | None = None  # 1.0 = no learning in effect


@dataclass(frozen=True)
class DecisionOutcome:
    """Immutable record linking a shading decision to its observable outcome.

    The Learning Engine creates a DecisionOutcome when a state transition
    occurs. The outcome fields (override_occurred, indoor_temp_outcome_c,
    state_duration_min) are resolved later — a new DecisionOutcome instance
    replaces the pending one when resolution data becomes available.

    Outcome signals:
      override_occurred    — strongest signal: user corrected the decision
      override_delay_min   — shorter delay = stronger negative signal
      indoor_temp_outcome_c — heat protection effectiveness (requires sensor)
      state_duration_min   — how long the decision held before next transition

    indoor_temp_outcome_delay_min controls when the Coordinator reads
    indoor_temp_outcome_c (default: 30 minutes after the decision).
    """

    decision_timestamp: datetime
    window_id: str
    decided_state: ShadingState
    decided_by: str
    indoor_temp_outcome_delay_min: int = 30
    lifecycle_state: str = "day"             # Step 9F4b-5: phase at decision time
    from_state: ShadingState | None = None   # Step 9F4b-5: prior state before transition
    override_occurred: bool = False
    override_delay_min: float | None = None      # None if no override occurred
    # Step 9F4a: event type of the override that corrected this decision
    override_event_type: str | None = None       # "started" / "renewed" / None
    indoor_temp_at_decision: float | None = None
    indoor_temp_outcome_c: float | None = None   # None until resolved
    indoor_temp_delta_c: float | None = None     # outcome_c − at_decision_c
    state_duration_min: float | None = None      # None until next transition
    # Step 9F4a: outcome metadata resolved after the decision
    escalation_occurred: bool = False
    outcome_score: float | None = None           # -1.0 … +1.0, set on resolution
    resolution_status: str = "pending"           # pending/complete/partial_no_temp/…
    evaluation_timestamp: datetime | None = None # when outcome was resolved
    # LE 2.0 / P2 — authoritative link to the LearningDecisionRecord (v2).
    # None for legacy v1 outcomes, which use the isolated timestamp fallback.
    decision_id: str | None = None
    # LE 2.0 / P3 — additive multi-objective decomposition.  None for legacy v1
    # outcomes and for P2 records written before P3.  Has NO active learning
    # authority in P3 (the legacy outcome_score remains authoritative).
    multi_objective: MultiObjectiveOutcome | None = None

    @property
    def timestamp(self) -> datetime:
        """Alias for decision_timestamp — required by prune_by_age_and_count."""
        return self.decision_timestamp

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict (LE 2.0 / P2 — embeddable in a record).

        Self-contained: includes window_id so the outcome can be deserialized
        without external context.  Mirrors the v1 stream shape used by
        learning_persistence._serialize_outcome with window_id added.
        """
        return {
            "decision_timestamp": self.decision_timestamp.isoformat(),
            "window_id": self.window_id,
            "decided_state": self.decided_state.value,
            "decided_by": self.decided_by,
            "indoor_temp_outcome_delay_min": self.indoor_temp_outcome_delay_min,
            "lifecycle_state": self.lifecycle_state,
            "from_state": self.from_state.value if self.from_state is not None else None,
            "override_occurred": self.override_occurred,
            "override_delay_min": self.override_delay_min,
            "override_event_type": self.override_event_type,
            "indoor_temp_at_decision": self.indoor_temp_at_decision,
            "indoor_temp_outcome_c": self.indoor_temp_outcome_c,
            "indoor_temp_delta_c": self.indoor_temp_delta_c,
            "state_duration_min": self.state_duration_min,
            "escalation_occurred": self.escalation_occurred,
            "outcome_score": self.outcome_score,
            "resolution_status": self.resolution_status,
            "evaluation_timestamp": (
                self.evaluation_timestamp.isoformat()
                if self.evaluation_timestamp is not None else None
            ),
            "decision_id": self.decision_id,
            "multi_objective": (
                self.multi_objective.to_dict() if self.multi_objective is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DecisionOutcome":
        """Deserialize from to_dict() output.  Raises on missing required keys."""
        def _parse(ts: str | None) -> datetime | None:
            if ts is None:
                return None
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                from datetime import timezone
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        raw_from = d.get("from_state")
        return cls(
            decision_timestamp=_parse(d["decision_timestamp"]),  # type: ignore[arg-type]
            window_id=d["window_id"],
            decided_state=ShadingState(d["decided_state"]),
            decided_by=d["decided_by"],
            indoor_temp_outcome_delay_min=int(d.get("indoor_temp_outcome_delay_min", 30)),
            lifecycle_state=d.get("lifecycle_state", "day"),
            from_state=ShadingState(raw_from) if raw_from is not None else None,
            override_occurred=bool(d.get("override_occurred", False)),
            override_delay_min=d.get("override_delay_min"),
            override_event_type=d.get("override_event_type"),
            indoor_temp_at_decision=d.get("indoor_temp_at_decision"),
            indoor_temp_outcome_c=d.get("indoor_temp_outcome_c"),
            indoor_temp_delta_c=d.get("indoor_temp_delta_c"),
            state_duration_min=d.get("state_duration_min"),
            escalation_occurred=bool(d.get("escalation_occurred", False)),
            outcome_score=d.get("outcome_score"),
            resolution_status=d.get("resolution_status", "pending"),
            evaluation_timestamp=_parse(d.get("evaluation_timestamp")),
            decision_id=d.get("decision_id"),
            multi_objective=MultiObjectiveOutcome.from_dict(d.get("multi_objective")),
        )


@dataclass
class EvaluatorConfidenceRecord:
    """Running tally of an evaluator's decision quality for one window.

    Not frozen — the Learning Engine increments decision_count and
    override_count and recomputes override_rate as new outcomes arrive.

    Confidence model (Learning Engine, Phase 9+):
        base_confidence = 1.0 - override_rate
        sample_weight   = min(1.0, decision_count / 100)
        confidence      = base_confidence * sample_weight

    Records with fewer than ~30 decisions are statistically insignificant.
    The Learning Engine must not use them to adjust evaluator thresholds.
    """

    window_id: str
    evaluator_name: str
    last_updated: datetime
    decision_count: int = 0
    override_count: int = 0
    override_rate: float = 0.0
