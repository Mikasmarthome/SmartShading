"""Active manual override for one window.

Produced by OverrideDetector (engines/override_detector.py), consumed by
ManualOverrideEvaluator (evaluators/manual_override_evaluator.py, Tier 2).

In-memory only — not persisted across HA restarts. If HA restarts while an
override is active, the override is lost and SmartShading resumes normal
evaluation after the warmup period. Persistence via hass.storage is a
planned Phase 2 extension.
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
    """

    window_id: str
    override_position: int
    started_at: datetime
    expires_at: datetime
    source: str
    overridden_state: ShadingState
    overridden_position: int | None
