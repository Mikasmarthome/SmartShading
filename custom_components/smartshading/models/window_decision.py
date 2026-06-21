"""WindowDecision: the execution-oriented output of one evaluation cycle.

One WindowDecision is produced per window per cycle by the evaluator pipeline
(Tier 1–5).  It answers "what should happen" — not "what was observed."
Observation data lives in WindowObservation / EvaluationObservations (separate
concern, INV-14).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..state_machine.states import ShadingState


@dataclass(frozen=True)
class WindowDecision:
    """What the evaluator tier that fired decided to do for one window.

    target_position uses the internal convention: 0 = fully open,
    100 = fully shaded / closed.  CoverController handles conversion to the
    HA cover convention when commanding a physical device.

    target_tilt is Phase 2 only and is always None in this version.

    decided_by names the evaluator class or tier that produced this decision,
    primarily for logging and diagnostics (surfaces as a sensor attribute).
    """

    window_id: str
    shading_state: ShadingState
    target_position: int | None    # 0=open, 100=shaded; None = no explicit position command
    decided_by: str
    target_tilt: int | None = None  # Phase 2 only; always None in this version
