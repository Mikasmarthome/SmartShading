"""Cover Execution Plan — Phase 9G2.

Semantic command layer between WindowDecision / CommandFilter and the
low-level CoverCommand in cover_controller.py.

No Home Assistant dependency.  No service calls.  No async.

LAYER OVERVIEW
--------------
  WindowDecision            evaluator output: state + target_position (internal)
  ↓
  CommandFilter             gating: is execution allowed?  (9G1)
  CommandFilterResult       → allowed, blocked_reason, target_position_ha
  ↓
  CoverIntent               semantic intent: WHAT to do, with full context  ← THIS MODULE
  ExecutionPlan             all intents for one window (1 per cover entity)  ← THIS MODULE
  ↓
  CoverController           HOW to execute: SET_POSITION vs OPEN/CLOSE       (existing)
  CoverCommand (low-level)  action + target in HA convention                 (existing)
  ↓
  HAServiceAdapter          hass.services.async_call                          (Step 9G3)

NAMING RATIONALE
----------------
The low-level execution primitive is CoverCommand (cover_controller.py), which
represents the HA service call strategy (SET_POSITION / OPEN / CLOSE / STOP).

CoverIntent is the semantic counterpart at a higher abstraction level: it
captures the *decision* (what was chosen and why) independently of *how* the
hardware is driven.  A single CoverIntent fans out to one CoverCommand per
execution; the mapping depends on CoverCapability (supports_position, etc.).

POSITION CONVENTION
-------------------
CoverIntent carries BOTH conventions so callers never need to re-derive them:
  target_position_internal:  SmartShading convention (0=open, 100=shaded)
  target_position_ha:        HA convention (0=closed, 100=open)

Both are derived from CommandFilterResult, which already performs the
conversion via position_semantics.to_ha_position().

COVER COMMAND TYPES
-------------------
  NO_OP                 Nothing to do.  The cover is already at the target
                        position (within tolerance), or no target exists.
                        Diagnostic context is still available in the intent.

  MOVE_TO_POSITION      Drive the cover to target_position_ha.  Only set
                        when allowed=True and target_position_ha is not None.

  MOVE_TO_TILT          Tilt only — Phase 2, never set in this version.

  MOVE_TO_POSITION_AND_TILT  Both — Phase 2, never set in this version.

  STOP                  Stop the cover mid-travel — future use (not yet wired
                        from any evaluator path).

  BLOCKED               A meaningful action was calculated, but execution was
                        prevented (recommendation_only, guard_action_interval,
                        manual_override, cover_unavailable).  The target
                        positions are still populated for diagnostics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Sequence

from .command_filter import (
    BLOCKED_NO_TARGET_POSITION,
    BLOCKED_SAME_POSITION,
    CommandFilterResult,
)


# ---------------------------------------------------------------------------
# CoverCommandType
# ---------------------------------------------------------------------------

class CoverCommandType(Enum):
    """Semantic command type at the decision layer.

    Values map one-to-one to what CoverController will execute in a later step.
    The mapping from CoverCommandType to CoverAction (cover_controller.py) is:
        MOVE_TO_POSITION  → CoverAction.SET_POSITION (or OPEN/CLOSE for Somfy)
        STOP              → CoverAction.STOP
        NO_OP / BLOCKED   → CoverAction.NONE (nothing sent to HA)
    """

    NO_OP = "no_op"
    """Nothing to do — cover already at target or no target exists."""

    MOVE_TO_POSITION = "move_to_position"
    """Drive the cover to target_position_ha (execution allowed)."""

    MOVE_TO_TILT = "move_to_tilt"
    """Adjust tilt only — Phase 2; never set in this version."""

    MOVE_TO_POSITION_AND_TILT = "move_to_position_and_tilt"
    """Drive both position and tilt — Phase 2; never set in this version."""

    STOP = "stop"
    """Stop mid-travel — future use."""

    BLOCKED = "blocked"
    """Command was computed but execution was prevented by CommandFilter."""


# ---------------------------------------------------------------------------
# Helper: derive command type from CommandFilterResult
# ---------------------------------------------------------------------------

_NO_OP_BLOCKED_REASONS: frozenset[str] = frozenset({
    BLOCKED_SAME_POSITION,
    BLOCKED_NO_TARGET_POSITION,
})


def _command_type_from_filter(result: CommandFilterResult) -> CoverCommandType:
    """Derive the semantic CoverCommandType from a CommandFilterResult.

    Decision tree:
      allowed=True + position + tilt → MOVE_TO_POSITION_AND_TILT
      allowed=True + position only   → MOVE_TO_POSITION
      allowed=True + tilt only       → MOVE_TO_TILT
      allowed=True + neither         → NO_OP
      allowed=False + same/no target → NO_OP  (nothing useful to do)
      allowed=False + other reason   → BLOCKED (something computed, prevented)
    """
    if result.allowed:
        has_position = result.target_position_ha is not None
        has_tilt = result.target_tilt_ha is not None
        if has_position and has_tilt:
            return CoverCommandType.MOVE_TO_POSITION_AND_TILT
        if has_position:
            return CoverCommandType.MOVE_TO_POSITION
        if has_tilt:
            return CoverCommandType.MOVE_TO_TILT
        return CoverCommandType.NO_OP
    if result.blocked_reason in _NO_OP_BLOCKED_REASONS:
        return CoverCommandType.NO_OP
    return CoverCommandType.BLOCKED


# ---------------------------------------------------------------------------
# CoverIntent — semantic intent for one cover entity
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CoverIntent:
    """Semantic command for one cover entity in one coordinator cycle.

    CoverIntent captures the *decision* at the highest level of abstraction:
    - What was decided (command_type, target positions)
    - Whether execution is permitted (allowed, blocked_reason)
    - Full context for diagnostics and explainability

    CoverController (cover_controller.py) translates this into the low-level
    CoverCommand (SET_POSITION vs OPEN/CLOSE strategy, TravelTracker update,
    AssumedStateManager update).

    All position fields use the documented convention:
      target_position_internal: 0=open, 100=shaded (SmartShading internal)
      target_position_ha:       0=closed, 100=open (HA cover entity)
    Both are None when the decision carries no concrete position.
    """

    cover_entity_id: str
    """HA entity_id of the cover this intent targets (e.g. 'cover.south_window')."""

    command_type: CoverCommandType
    """Semantic command type derived from the CommandFilterResult."""

    target_position_internal: int | None
    """Target position in SmartShading internal convention (0=open, 100=shaded).
    None when no explicit position was decided."""

    target_position_ha: int | None
    """Target position in HA cover convention (0=closed, 100=open).
    None when target_position_internal is None.
    Already converted via to_ha_position() — do not convert again."""

    target_tilt: int | None
    """Target tilt position — Phase 2 only.  Always None in this version."""

    is_safety: bool
    """True when triggered by STORM_SAFE or WIND_SAFE (Tier 1 Safety).
    Safety intents bypass the StateGuard action interval and position
    tolerance checks in CommandFilter."""

    execution_mode: str
    """ExecutionMode.value at the time this intent was computed.
    'recommendation_only' or 'automatic'."""

    allowed: bool
    """True when CommandFilter permitted execution for this cycle.
    When True, this intent may proceed to CoverController."""

    blocked_reason: str | None
    """BLOCKED_* constant from command_filter.py.  None when allowed=True."""

    decided_by: str
    """The evaluator class or tier that produced the WindowDecision
    (e.g. 'StormEvaluator', 'SolarEvaluator', 'TierOrchestrator:fallback').
    For logging and diagnostics."""

    computed_at: datetime
    """UTC timestamp when this intent was produced."""


# ---------------------------------------------------------------------------
# ExecutionPlan — all intents for one window
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionPlan:
    """Aggregated execution plan for one window in one coordinator cycle.

    Contains one CoverIntent per cover entity in the window's CoverGroup.
    When a window has multiple covers (synchronized group), each cover gets
    the same intent; CoverController may later differentiate by capability.

    ExecutionPlan is the unit passed from the Coordinator to the future
    HAServiceAdapter (Step 9G3).
    """

    window_id: str
    """The window this plan targets."""

    intents: tuple[CoverIntent, ...]
    """One CoverIntent per cover entity in the window's CoverGroup.
    Empty when no cover entities are associated with this window."""

    contains_safety_intent: bool
    """True when at least one intent has is_safety=True.
    Safety intents are flagged for priority dispatch in later steps."""

    any_allowed: bool
    """True when at least one intent has allowed=True.
    When False, no cover commands will be sent for this window this cycle."""

    computed_at: datetime
    """UTC timestamp when this plan was built."""


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_cover_intent(
    *,
    cover_entity_id: str,
    filter_result: CommandFilterResult,
    decided_by: str,
    now: datetime,
) -> CoverIntent:
    """Build a CoverIntent from a CommandFilterResult.

    This is the primary factory for CoverIntent objects.  It derives the
    semantic command type, preserves all diagnostic context from the filter
    result, and attaches the evaluator name and timestamp.

    Parameters
    ----------
    cover_entity_id:
        The HA entity_id of the cover to command.
    filter_result:
        Result from CommandFilter.evaluate() for this window/cycle.
        Carries the allowed flag, blocked_reason, target positions, and
        execution mode.
    decided_by:
        WindowDecision.decided_by — the evaluator that made the decision.
    now:
        UTC timestamp for computed_at.
    """
    return CoverIntent(
        cover_entity_id=cover_entity_id,
        command_type=_command_type_from_filter(filter_result),
        target_position_internal=filter_result.target_position_internal,
        target_position_ha=filter_result.target_position_ha,
        target_tilt=filter_result.target_tilt_ha,
        is_safety=filter_result.is_safety,
        execution_mode=filter_result.execution_mode,
        allowed=filter_result.allowed,
        blocked_reason=filter_result.blocked_reason,
        decided_by=decided_by,
        computed_at=now,
    )


def build_execution_plan(
    *,
    window_id: str,
    cover_entity_ids: Sequence[str],
    filter_result: CommandFilterResult,
    decided_by: str,
    now: datetime | None = None,
) -> ExecutionPlan:
    """Build an ExecutionPlan for all covers in one window.

    Applies the same CommandFilterResult to every cover entity in the window's
    CoverGroup: all covers receive the same intent this cycle.  CoverController
    may later differentiate behavior per cover based on CoverCapability (e.g.
    Somfy vs. standard positioning covers).

    Parameters
    ----------
    window_id:
        The window identifier.
    cover_entity_ids:
        All cover entity_ids in the window's CoverGroup.  An empty sequence
        produces an ExecutionPlan with no intents and any_allowed=False.
    filter_result:
        Result from CommandFilter.evaluate() for this window/cycle.
    decided_by:
        WindowDecision.decided_by — passed through to each CoverIntent.
    now:
        UTC timestamp; defaults to datetime.now(timezone.utc) when None.
    """
    ts = now if now is not None else datetime.now(timezone.utc)
    intents = tuple(
        build_cover_intent(
            cover_entity_id=eid,
            filter_result=filter_result,
            decided_by=decided_by,
            now=ts,
        )
        for eid in cover_entity_ids
    )
    return ExecutionPlan(
        window_id=window_id,
        intents=intents,
        contains_safety_intent=any(i.is_safety for i in intents),
        any_allowed=any(i.allowed for i in intents),
        computed_at=ts,
    )
