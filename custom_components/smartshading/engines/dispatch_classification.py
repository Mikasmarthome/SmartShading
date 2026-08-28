"""Target-aware Dispatch — Phase 1: pure semantic classification.

Answers exactly one question, for one already-fully-decided comfort target:
"which semantic dispatch class does this final target belong to?" — never
"what should the target be" (that is the coordinator's decision pipeline,
already computed before this module is ever called) and never "how should
this be dispatched" (service call, completion method, queue position,
timeout — all later phases / other modules).

Canonical input scale: HA cover-position convention, integers in [0, 100],
0 = closed, 100 = open (see cover_control/position_semantics.py — the single
existing authoritative source for this convention; HA_OPEN == 100). Callers
must resolve/clamp their target through the existing decision pipeline
(e.g. engines/decision_record.resolve_target_ha()) and pass the plain HA
value here — this module performs no shading-decision computation itself.

Reuses the existing central tolerance/comparison primitive
(cover_control.position_semantics.positions_within_tolerance) rather than
introducing a second, independently-maintained tolerance concept — the
caller supplies whatever position_tolerance value is already authoritative
for that cover (e.g. ExecutionCapability.position_tolerance, already widened
for low confidence where applicable) instead of this module hardcoding one.

No Home Assistant import. No coordinator import. No service-call/dispatch
logic. No queue/executor concept (Phase 2+).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from ..cover_control.position_semantics import (
    HA_CLOSED,
    HA_OPEN,
    clamp_position,
    positions_within_tolerance,
)

# The two CommandFilter block reasons that are "unconditional" (apply even to
# safety-priority items) per cover_control/command_filter.py's own priority
# order (BLOCKED_MANUAL_OVERRIDE / BLOCKED_COVER_UNAVAILABLE are checks #1/#2,
# evaluated before any is_safety exemption). Reproduced here as literals
# (not imported) to keep this module fully decoupled from command_filter.py's
# own dependency surface — see the module docstring and the architecture
# tests in tests/test_t22_phase1_dispatch_classification.py.
_SAFETY_HARD_BLOCK_REASONS = frozenset({"manual_override", "cover_unavailable"})

# The existing decision_record.resolve_dispatch_action() vocabulary already
# has a dedicated value for "target already matches current position" —
# reusing it here means this module never re-derives that judgment itself.
_DISPATCH_ACTION_UNCHANGED = "unchanged"


class DispatchTargetClass(str, Enum):
    """The five — and only the five — semantic dispatch classes. Stable,
    lowercase-string-serializable values (used directly in exports/
    diagnostics without a separate mapping table).

    B3-012: FULL_CLOSE added alongside FULL_OPEN so a fully-closed (0%)
    target is never dispatched through the same completion-wait chain as a
    genuine partial (INTERMEDIATE) target — see classify_dispatch_target()'s
    own docstring for the exact-value boundary rule."""

    FULL_OPEN = "full_open"
    FULL_CLOSE = "full_close"
    INTERMEDIATE = "intermediate"
    NO_MOVEMENT = "no_movement"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class DispatchClassification:
    """Immutable classification result. Only fields Phase 1 callers actually
    need — no speculative queue/plan/completion/trigger fields (those belong
    to the later DispatchPlan/executor phases)."""

    target_class: DispatchTargetClass
    reason: str
    normalized_target: int | None
    movement_required: bool


def _normalize_position(value) -> int | None:
    """Best-effort int-in-[0,100] normalization, or None when the input is
    missing/non-numeric/NaN/Infinity. Never raises. 0 is a fully valid,
    preserved result — never treated as "missing" (this function returns
    None only for genuinely absent/invalid input, not for a falsy-but-valid
    0)."""
    if value is None:
        return None
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(as_float) or math.isinf(as_float):
        return None
    return clamp_position(round(as_float))


def classify_dispatch_target(
    *,
    resolved_target_ha,
    current_position_ha=None,
    dispatch_action: str | None = None,
    blocked_reason: str | None = None,
    position_tolerance: int,
    is_safety: bool = False,
) -> DispatchClassification:
    """Classify one already-fully-decided comfort target.

    Priority order (first match wins, deterministic):
      1. BLOCKED       — a hard block already established elsewhere (a
                          CommandFilter blocked_reason, or no target at all).
      2. NO_MOVEMENT    — the decision pipeline already says nothing changed
                          (dispatch_action == "unchanged"), or the current
                          position is already within tolerance of the target.
                          This is the ONLY place position_tolerance affects
                          classification — see B3-012 note below.
      3. FULL_OPEN      — target is EXACTLY HA_OPEN (100).
      4. FULL_CLOSE     — target is EXACTLY HA_CLOSED (0).
      5. INTERMEDIATE   — everything else that requires movement (1-99).

    B3-012: FULL_OPEN/FULL_CLOSE are decided on the exact normalized target
    value, never blurred by position_tolerance — a target of 96 or 99 is
    INTERMEDIATE, not FULL_OPEN, even though position_tolerance's default
    (5) would consider it "close enough" for the separate NO_MOVEMENT /
    completion "already there" judgment above. Conflating the two would let
    a genuine near-boundary movement silently skip the completion-wait
    chain B3-013's INTERMEDIATE handling depends on. NO_MOVEMENT's own
    tolerance check runs first and is unaffected: a target within tolerance
    of the CURRENT position is still classified NO_MOVEMENT regardless of
    where it sits relative to 0/100.

    ``is_safety``: safety items are never part of a comfort DispatchPlan
    (Phase 2+), but if safety-sourced data is ever passed through this pure
    function anyway, it must not be silently swallowed as BLOCKED just
    because a comfort-only block reason is present. When True, only the two
    reasons that are unconditional even for safety in the existing
    CommandFilter priority order (manual_override, cover_unavailable) still
    produce BLOCKED; any other blocked_reason is ignored and classification
    proceeds on the target/position data alone.
    """
    if blocked_reason is not None and (not is_safety or blocked_reason in _SAFETY_HARD_BLOCK_REASONS):
        return DispatchClassification(
            DispatchTargetClass.BLOCKED, f"blocked:{blocked_reason}", None, False,
        )

    target = _normalize_position(resolved_target_ha)
    if target is None:
        return DispatchClassification(
            DispatchTargetClass.BLOCKED, "blocked:no_target_position", None, False,
        )

    if dispatch_action == _DISPATCH_ACTION_UNCHANGED:
        return DispatchClassification(
            DispatchTargetClass.NO_MOVEMENT, "no_movement:dispatch_action_unchanged",
            target, False,
        )

    current = _normalize_position(current_position_ha)
    if current is not None and positions_within_tolerance(current, target, position_tolerance):
        return DispatchClassification(
            DispatchTargetClass.NO_MOVEMENT, "no_movement:within_tolerance", target, False,
        )

    if target == HA_OPEN:
        return DispatchClassification(
            DispatchTargetClass.FULL_OPEN, "full_open:exact_target", target, True,
        )

    if target == HA_CLOSED:
        return DispatchClassification(
            DispatchTargetClass.FULL_CLOSE, "full_close:exact_target", target, True,
        )

    return DispatchClassification(
        DispatchTargetClass.INTERMEDIATE, "intermediate:partial_target", target, True,
    )
