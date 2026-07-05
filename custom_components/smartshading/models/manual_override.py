"""Active manual override for one window.

Produced by OverrideDetector (engines/override_detector.py), consumed by
ManualOverrideEvaluator (evaluators/manual_override_evaluator.py, Tier 2).

An active override is now persisted across HA restart/reload (to_dict/from_dict
below) so a manual movement is not silently re-asserted after a restart.  Stale
overrides are dropped on restore via the ``expires_at`` bound.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..state_machine.states import ShadingState


@dataclass(frozen=True)
class ManualOverride:
    """A user-initiated override that keeps a window at a manually chosen position.

    Created when OverrideDetector observes that the cover position deviates
    from SmartShading's evaluation target beyond override_detection_tolerance.

    All positions use the integration-internal convention (0 = open, 100 = shaded).

    Fields:
        window_id:            Window this override belongs to.
        override_position:    Position the user moved to (internal convention).
        started_at:           UTC timestamp when the override was first detected.
        expires_at:           UTC timestamp when the override expires
                              (started_at + override_duration_min).
        source:               How the override was detected.  "position_delta"
                              is the only source in this version; "service_call" is
                              reserved for a future explicit override API.
        overridden_state:     The ShadingState SmartShading held before the
                              override — kept for the Learning Engine.
        overridden_position:  The target_position SmartShading would have held
                              (internal convention) — kept for the Learning Engine.
                              None if the previous state had no target (e.g. OPEN).
        scope:                "daytime" or "night" — which duration policy produced
                              this override's expires_at (v1.1.3).  "daytime": a
                              fixed duration from started_at (default 120 min).
                              "night": held until the Morning lifecycle transition;
                              expires_at is a generous safety-net far beyond any
                              real night, not the real release mechanism (see
                              engines/override_detector.py / lifecycle_guard.py).
                              Defaults to "daytime" for entries persisted before
                              this field existed (the pre-v1.1.3 flat duration was
                              closer in spirit to the daytime policy).
    """

    window_id: str
    override_position: int
    started_at: datetime
    expires_at: datetime
    source: str
    overridden_state: ShadingState
    overridden_position: int | None
    scope: str = "daytime"

    def to_dict(self) -> dict:
        """JSON-safe serialization for restart-safe persistence."""
        return {
            "window_id": self.window_id,
            "override_position": self.override_position,
            "started_at": self.started_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "source": self.source,
            "overridden_state": self.overridden_state.value,
            "overridden_position": self.overridden_position,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ManualOverride":
        return cls(
            window_id=d["window_id"],
            override_position=int(d["override_position"]),
            started_at=datetime.fromisoformat(d["started_at"]),
            expires_at=datetime.fromisoformat(d["expires_at"]),
            source=d.get("source", "position_delta"),
            overridden_state=ShadingState(d["overridden_state"]),
            overridden_position=d.get("overridden_position"),
            scope=d.get("scope", "daytime"),
        )
