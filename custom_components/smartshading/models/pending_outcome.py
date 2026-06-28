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
    # LE 2.0 / P3 — solar exposure at decision time (W/m²), thermal context.
    solar_exposure_at_decision: float | None = None
    # LE 2.0 / P2 — link to the LearningDecisionRecord this observation belongs to.
    decision_id: str | None = None
    # P2.6 — config fingerprint captured at decision time; on restart-restore a
    # mismatch invalidates the pending observation (no false outcome).
    config_fingerprint: str | None = None
    # P2.6 — restart/interruption tracking carried across persistence.
    created_at_utc: datetime | None = None
    restart_count: int = 0
    # P4 — thermal observation-window authority applied at decision time.
    thermal_authority_applied: bool = False
    thermal_confidence_at_decision: float | None = None
    # P7 — bounded-experiment linkage (None for normal decisions).  The outcome
    # is attached to the experiment by decision_id/experiment_id, never by time.
    experiment_id: str | None = None
    # Rain context — recorded at decision time so rain-coincident outcomes can be
    # flagged as confounded during resolution without re-reading sensor state.
    # "unknown" when no rain sensor is configured (RainStatus.UNKNOWN.value).
    rain_status_at_decision: str | None = None  # RainStatus.value or None
    rain_safe_active_at_decision: bool = False
    # Night contact context — recorded at decision time for outcome resolution.
    night_contact_blocked_at_decision: bool = False
    night_vent_active_at_decision: bool = False

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict (P2 — restart-safe persistence)."""
        return {
            "window_id": self.window_id,
            "decision_timestamp": self.decision_timestamp.isoformat(),
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "decided_by": self.decided_by,
            "lifecycle_state": self.lifecycle_state,
            "indoor_temp_outcome_delay_min": self.indoor_temp_outcome_delay_min,
            "target_position": self.target_position,
            "indoor_temp_at_decision": self.indoor_temp_at_decision,
            "outdoor_temp_at_decision": self.outdoor_temp_at_decision,
            "solar_exposure_at_decision": self.solar_exposure_at_decision,
            "decision_id": self.decision_id,
            "config_fingerprint": self.config_fingerprint,
            "created_at_utc": self.created_at_utc.isoformat() if self.created_at_utc else None,
            "restart_count": self.restart_count,
            "thermal_authority_applied": self.thermal_authority_applied,
            "thermal_confidence_at_decision": self.thermal_confidence_at_decision,
            "experiment_id": self.experiment_id,
            "rain_status_at_decision": self.rain_status_at_decision,
            "rain_safe_active_at_decision": self.rain_safe_active_at_decision,
            "night_contact_blocked_at_decision": self.night_contact_blocked_at_decision,
            "night_vent_active_at_decision": self.night_vent_active_at_decision,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PendingOutcome":
        from datetime import timezone

        def _p(ts: str | None) -> datetime | None:
            if ts is None:
                return None
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        return cls(
            window_id=d["window_id"],
            decision_timestamp=_p(d["decision_timestamp"]),  # type: ignore[arg-type]
            from_state=ShadingState(d["from_state"]),
            to_state=ShadingState(d["to_state"]),
            decided_by=d["decided_by"],
            lifecycle_state=d["lifecycle_state"],
            indoor_temp_outcome_delay_min=int(d.get("indoor_temp_outcome_delay_min", 30)),
            target_position=d.get("target_position"),
            indoor_temp_at_decision=d.get("indoor_temp_at_decision"),
            outdoor_temp_at_decision=d.get("outdoor_temp_at_decision"),
            solar_exposure_at_decision=d.get("solar_exposure_at_decision"),
            decision_id=d.get("decision_id"),
            config_fingerprint=d.get("config_fingerprint"),
            created_at_utc=_p(d.get("created_at_utc")),
            restart_count=int(d.get("restart_count", 0)),
            thermal_authority_applied=bool(d.get("thermal_authority_applied", False)),
            thermal_confidence_at_decision=d.get("thermal_confidence_at_decision"),
            experiment_id=d.get("experiment_id"),
            rain_status_at_decision=d.get("rain_status_at_decision"),
            rain_safe_active_at_decision=bool(d.get("rain_safe_active_at_decision", False)),
            night_contact_blocked_at_decision=bool(d.get("night_contact_blocked_at_decision", False)),
            night_vent_active_at_decision=bool(d.get("night_vent_active_at_decision", False)),
        )
