"""Window attribution (solo-event gate) — LE 2.0 / Phase P5 (pure).

Deterministic classification of a zone thermal observation into:
    unknown | zone_shared | window_candidate | window_isolated

Conservative by design: window_isolated requires a confirmed real position
change of exactly one window, all other windows stable, no harmonization of
multiple windows, no external movement, no thermal confounder, and a mature,
sufficiently reliable thermal observation.  A SENT service call alone never
proves a shading change.  No Home Assistant import.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models.window_contribution import (
    ATTR_UNKNOWN,
    ATTR_WINDOW_CANDIDATE,
    ATTR_WINDOW_ISOLATED,
    ATTR_ZONE_SHARED,
    POS_ASSUMED,
    POS_CONFIRMED,
    POS_NONE,
    POS_UNCONFIRMED,
    WindowAttributionResult,
)

# Materiality / tolerance for an observed position change (HA/internal points).
_POSITION_TOLERANCE: int = 3


@dataclass(frozen=True)
class WindowEventFacts:
    """Per-window facts for one zone observation (coordinator-supplied)."""

    window_id: str
    material_change: bool
    command_status: str            # "sent" | "blocked" | "failed" | "not_attempted" | "none"
    has_reliable_feedback: bool
    active_control: bool
    start_position_internal: int | None = None
    end_position_internal: int | None = None
    target_internal: int | None = None
    harmonized: bool = False
    external_movement_detected: bool = False


def position_change_class(f: WindowEventFacts, tolerance: int = _POSITION_TOLERANCE) -> str:
    """Classify the observed position change of a window.

    POS_NONE        target not materially different, or cover did not move.
    POS_CONFIRMED   reliable feedback + observed material move toward target.
    POS_ASSUMED     no reliable feedback but a command was sent.
    POS_UNCONFIRMED command sent, reliable feedback, but movement not observed.
    """
    if f.command_status != "sent":
        return POS_NONE
    if f.target_internal is not None and f.start_position_internal is not None \
            and abs(f.target_internal - f.start_position_internal) < tolerance:
        return POS_NONE  # already at/near target — no effective change
    if not f.has_reliable_feedback:
        return POS_ASSUMED
    if f.start_position_internal is None or f.end_position_internal is None:
        return POS_UNCONFIRMED
    moved = abs(f.end_position_internal - f.start_position_internal)
    if moved < tolerance:
        return POS_UNCONFIRMED  # sent but no material movement observed
    # Direction must be toward the target.
    if f.target_internal is not None:
        toward = (f.target_internal - f.start_position_internal)
        observed = (f.end_position_internal - f.start_position_internal)
        if toward * observed <= 0:
            return POS_UNCONFIRMED  # moved the wrong way → not a valid contribution
    return POS_CONFIRMED


def _effective_contributor(f: WindowEventFacts, tolerance: int) -> bool:
    return f.material_change and position_change_class(f, tolerance) in (POS_CONFIRMED, POS_ASSUMED)


def classify_window_attribution(
    window_facts: list[WindowEventFacts],
    *,
    thermal_available: bool,
    thermal_mature: bool,
    thermal_reliability: float,
    confounded: bool,
    tolerance: int = _POSITION_TOLERANCE,
) -> WindowAttributionResult:
    """Classify one zone observation.  Pure and deterministic."""
    if not thermal_available:
        return WindowAttributionResult(ATTR_UNKNOWN, disqualifiers=("thermal_unavailable",))
    if confounded:
        return WindowAttributionResult(ATTR_UNKNOWN, disqualifiers=("thermal_confounded",))

    harmonized_count = sum(1 for f in window_facts if f.harmonized)
    material = [f for f in window_facts if f.material_change]
    contributors = [f for f in window_facts if _effective_contributor(f, tolerance)]
    external_any = any(f.external_movement_detected for f in window_facts)

    # Harmonization moved ≥2 windows together → shared.
    if harmonized_count >= 2:
        return WindowAttributionResult(
            ATTR_ZONE_SHARED,
            contributing_window_ids=tuple(f.window_id for f in window_facts if f.harmonized),
            disqualifiers=("harmonized_multi_window",),
            model_eligible=True,
        )

    if not material:
        return WindowAttributionResult(ATTR_UNKNOWN, disqualifiers=("no_material_change",))

    if len(contributors) == 0:
        # Material decisions but no confirmed/assumed actuation (filtered, failed,
        # already at target, recommendation-only without movement) → not attributable.
        return WindowAttributionResult(
            ATTR_ZONE_SHARED,
            contributing_window_ids=tuple(f.window_id for f in material),
            disqualifiers=("no_confirmed_actuation",),
            model_eligible=True,
        )

    if len(contributors) >= 2:
        return WindowAttributionResult(
            ATTR_ZONE_SHARED,
            contributing_window_ids=tuple(f.window_id for f in contributors),
            disqualifiers=("multiple_contributors",),
            model_eligible=True,
        )

    # Exactly one effective contributor.  A window that had a material decision
    # but did NOT actuate (blocked/failed/not_attempted/no movement) stayed
    # physically stable and therefore does NOT disqualify isolation; only a real
    # external movement does.
    c = contributors[0]
    other_material_decided = [
        f for f in material
        if f.window_id != c.window_id  # decided but not an effective contributor
    ]

    if external_any:
        return WindowAttributionResult(
            ATTR_ZONE_SHARED, candidate_window_id=c.window_id,
            contributing_window_ids=(c.window_id,),
            excluded_window_ids=tuple(f.window_id for f in other_material_decided),
            disqualifiers=("external_movement",), model_eligible=True,
        )

    pos = position_change_class(c, tolerance)
    isolated = (
        pos == POS_CONFIRMED and c.has_reliable_feedback and c.active_control and thermal_mature
    )
    if isolated:
        return WindowAttributionResult(
            ATTR_WINDOW_ISOLATED, candidate_window_id=c.window_id,
            contributing_window_ids=(c.window_id,), solo_event=True,
            isolation_confidence=max(0.0, min(1.0, thermal_reliability)),
            evidence=("single_contributor", "confirmed_movement", "all_others_stable",
                      "thermal_mature"),
            model_eligible=True,
        )

    # Single contributor but residual uncertainty → candidate.
    ev = ["single_contributor"]
    if pos == POS_ASSUMED:
        ev.append("assumed_movement")
    if not c.active_control:
        ev.append("recommendation_only")
    if not thermal_mature:
        ev.append("not_mature")
    return WindowAttributionResult(
        ATTR_WINDOW_CANDIDATE, candidate_window_id=c.window_id,
        contributing_window_ids=(c.window_id,),
        isolation_confidence=max(0.0, min(1.0, thermal_reliability * 0.5)),
        evidence=tuple(ev), model_eligible=True,
    )
