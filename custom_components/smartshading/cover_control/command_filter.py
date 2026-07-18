"""Command Filter Foundation — Phase 9G1.

Pure Python decision layer that gates whether a cover command should proceed
to actual execution.  No Home Assistant dependency.  No service calls.

This module answers a single question per evaluation cycle:
    "Should we send a cover command, and if not, why not?"

It does NOT send any commands.  The actual HA service call lives in a later
phase (Step 9G2).

DESIGN INVARIANTS
-----------------
  No HA dependency — pure Python, testable without a HA installation.
  Deterministic — same inputs always produce the same result.
  No state — CommandFilter is stateless; all relevant state is passed in.
  Single responsibility — gating only, no position computation.

BLOCKING ORDER (evaluated top-to-bottom; first match wins)
-----------------------------------------------------------
1. MANUAL_OVERRIDE active          → blocked, always
2. Cover entity unavailable        → blocked, always
3. RECOMMENDATION_ONLY mode        → blocked (even for safety — user opt-out)
4. StateGuard action interval      → blocked, unless is_safety=True
5. Target position within tolerance → blocked, unless is_safety=True
6. Comfort Movement Stability Hold  → blocked, unless is_safety=True (v1.1.1)
7. Fallback/Open release pending   → blocked, unless is_safety=True (F29)
8. No target position              → blocked ("no_target_position")
9. Otherwise                       → allowed

SAFETY BYPASS SEMANTICS
-----------------------
STORM_SAFE and WIND_SAFE bypass the StateGuard action interval check and the
position-tolerance check.  They do NOT bypass recommendation_only or
cover_unavailable — if a cover is unavailable, it cannot receive any command.

EXECUTION MODE
--------------
RECOMMENDATION_ONLY:
    The TierOrchestrator computes a decision, the StateGuard checks are run,
    and a CommandFilterResult is produced — but execution is never permitted.
    The result documents what would have happened, for diagnostic use.

AUTOMATIC:
    All checks apply; execution is permitted when no check blocks it.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .position_semantics import clamp_position, positions_within_tolerance, to_ha_position

# ---------------------------------------------------------------------------
# Blocked-reason string constants
# (string constants rather than an enum: extensible, log-friendly)
# ---------------------------------------------------------------------------

#: MANUAL_OVERRIDE is active — SmartShading must never command the cover.
BLOCKED_MANUAL_OVERRIDE: str = "manual_override"

#: The cover entity is unavailable or unknown in Home Assistant.
BLOCKED_COVER_UNAVAILABLE: str = "cover_unavailable"

#: Execution mode is RECOMMENDATION_ONLY — automatic commands are disabled.
BLOCKED_RECOMMENDATION_ONLY: str = "recommendation_only"

#: StateGuard minimum_action_interval has not elapsed since the last command.
BLOCKED_GUARD_ACTION_INTERVAL: str = "guard_action_interval"

#: The target position is within tolerance of the current position.
BLOCKED_SAME_POSITION: str = "same_position"

#: A comfort-tier re-target (Solar/Heat/Glare) within the Comfort Movement
#: Stability Hold window of a previous comfort dispatch — suppressed to avoid
#: rapid back-and-forth movement (v1.1.1 field fix). See engines/comfort_movement_hold.py.
BLOCKED_COMFORT_POSITION_HOLD: str = "comfort_position_hold"

#: A "TierOrchestrator:fallback" OPEN proposal that has not yet been
#: confirmed for enough consecutive cycles — suppressed to avoid a visible
#: open-then-close flap when heat/glare/solar protection clears for only a
#: single outlier cycle (F29 field fix). See
#: engines/comfort_movement_hold.py should_delay_fallback_open().
BLOCKED_FALLBACK_RELEASE_PENDING: str = "fallback_release_pending"

#: No concrete target position is available for this decision.
BLOCKED_NO_TARGET_POSITION: str = "no_target_position"


# ---------------------------------------------------------------------------
# ExecutionMode
# ---------------------------------------------------------------------------

class ExecutionMode(Enum):
    """Whether SmartShading should actually send cover commands.

    RECOMMENDATION_ONLY:
        Decisions are computed and displayed as sensor attributes; the
        CommandFilter always blocks execution.  Default / opt-in safety net
        before automatic control is enabled.

    AUTOMATIC:
        The CommandFilter may permit execution when all gating checks pass.
        StateGuard action-interval and position-tolerance checks apply.
    """

    RECOMMENDATION_ONLY = "recommendation_only"
    AUTOMATIC = "automatic"


# ---------------------------------------------------------------------------
# ExecutionCapability
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionCapability:
    """Execution-level capability model for one cover group.

    This is distinct from CoverCapability (which describes hardware).
    ExecutionCapability describes HOW SmartShading should behave when
    commanding this cover.

    All positions in this dataclass use the SmartShading INTERNAL convention
    (0=open, 100=shaded).  to_ha_position() converts them at the execution
    boundary.

    safe_position_internal
        The position SmartShading drives the cover to during STORM_SAFE or
        WIND_SAFE states.  Default 0 = retracted/open, which is correct for
        awnings, screens, and marquises (no wind load when retracted).
        For roller shutters that should CLOSE during storm, set this to 100.

    position_tolerance
        If the cover's current position is within this many internal units
        of the target, the command is suppressed.  Prevents micro-commands
        caused by sensor noise or rounding.  Default 3 (= ±3%).

    tilt_tolerance
        Same concept for tilt (Phase 2).  Always applied but effectively
        unused in this version since target_tilt is always None.  Default 3.
    """

    safe_position_internal: int = 0   # internal: 0=retracted/open, 100=closed
    position_tolerance: int = 3        # internal position units
    tilt_tolerance: int = 3            # internal tilt units (Phase 2)


# ---------------------------------------------------------------------------
# CommandFilterResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandFilterResult:
    """Outcome of one CommandFilter.evaluate() call.

    This is a value object — immutable, complete, suitable for diagnostic
    logging and for passing to the future HAServiceAdapter (Step 9G2).

    allowed
        True when the command may proceed to HA service dispatch.
        False when any blocking condition was triggered.

    blocked_reason
        A BLOCKED_* string constant explaining why execution was prevented.
        None when allowed=True.

    target_position_internal
        The target position in SmartShading internal convention [0, 100].
        May be None if the decision carried no explicit position.

    target_position_ha
        target_position_internal converted to HA convention [0, 100].
        May be None when target_position_internal is None.

    execution_mode
        The ExecutionMode.value string ("recommendation_only" or "automatic")
        for the cycle that produced this result.

    is_safety
        True when the triggering decision is a Tier 1 Safety state
        (STORM_SAFE, WIND_SAFE, or RAIN_SAFE — v1.2.0-beta.1, T8).  Safety
        commands bypass some filters (not all — see module docstring).
    """

    allowed: bool
    blocked_reason: str | None          # None when allowed=True
    target_position_internal: int | None
    target_position_ha: int | None      # None when target_position_internal is None
    execution_mode: str                  # ExecutionMode.value
    is_safety: bool
    target_tilt_ha: int | None = None   # HA tilt convention [0, 100]; None = no tilt target


# ---------------------------------------------------------------------------
# CommandFilter
# ---------------------------------------------------------------------------

class CommandFilter:
    """Stateless gate for cover command execution.

    Usage (one call per window per evaluation cycle):

        result = CommandFilter().evaluate(
            target_position_internal=wdi_decision.target_position,
            current_position_internal=assumed_state.assumed_position,
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=is_storm_or_wind,
            is_manual_override=active_override is not None,
            is_cover_available=cap.entity_available,
            state_guard_allowed=guard.can_send_action(window_id, state, now),
            execution_capability=exec_cap,
            invert_position=cover_cap.invert_position,
        )

        if result.allowed:
            # proceed to HAServiceAdapter (Step 9G2)
            ...
        else:
            log.debug("Command blocked: %s", result.blocked_reason)
    """

    def evaluate(
        self,
        *,
        target_position_internal: int | None,
        current_position_internal: int | None,
        execution_mode: ExecutionMode,
        is_safety: bool,
        is_manual_override: bool,
        is_cover_available: bool,
        state_guard_allowed: bool,
        execution_capability: ExecutionCapability,
        invert_position: bool = False,
        target_tilt_ha: int | None = None,
        comfort_hold_allowed: bool = True,
        fallback_release_allowed: bool = True,
    ) -> CommandFilterResult:
        """Evaluate all blocking conditions and return a CommandFilterResult.

        Parameters
        ----------
        target_position_internal:
            The position the evaluator pipeline decided on (internal
            convention, 0=open, 100=shaded).  None means "no explicit
            position decision" (e.g. TierOrchestrator produced a state
            without a concrete position).
        current_position_internal:
            The cover's current assumed position (internal convention).
            None means the position is completely unknown.
        execution_mode:
            RECOMMENDATION_ONLY or AUTOMATIC.
        is_safety:
            True when shading_state is STORM_SAFE or WIND_SAFE.
            Safety commands bypass action-interval and position-tolerance
            checks, but not recommendation_only or cover_unavailable.
        is_manual_override:
            True when a MANUAL_OVERRIDE is active for this window.
            Always blocks; SmartShading never commands a user-held cover.
        is_cover_available:
            False when the HA cover entity is unavailable/unknown.
            Always blocks; service calls to unavailable entities are errors.
        state_guard_allowed:
            Result of StateGuard.can_send_action().  STORM/WIND bypass
            (is_safety=True) overrides a False value here.
        execution_capability:
            Per-cover execution parameters (tolerance, safe_position).
        invert_position:
            Mirror of CoverCapability.invert_position.  Passed to
            to_ha_position() for the HA-convention conversion.
        comfort_hold_allowed:
            False when the coordinator's ComfortMovementHold determined this
            comfort-tier (Solar/Heat/Glare) re-target is within the stability
            hold window of a previous comfort dispatch (v1.1.1 field fix).
            Default True preserves prior behavior for every non-comfort
            decision path. Does not bypass is_safety.
        fallback_release_allowed:
            False when the coordinator's ComfortMovementHold determined this
            "TierOrchestrator:fallback" OPEN proposal has not yet been
            confirmed for enough consecutive cycles (F29 field fix). Default
            True preserves prior behavior for every non-fallback decision
            path. Does not bypass is_safety.

        Returns
        -------
        CommandFilterResult
            Complete gate decision with reason and converted target position.
        """
        # Pre-compute the HA position for the result object.
        ha_pos: int | None = (
            to_ha_position(target_position_internal, invert=invert_position)
            if target_position_internal is not None
            else None
        )
        # Clamp tilt to [0, 100] to guard against out-of-range values from callers.
        _tilt_ha: int | None = (
            clamp_position(target_tilt_ha) if target_tilt_ha is not None else None
        )

        def _blocked(reason: str) -> CommandFilterResult:
            return CommandFilterResult(
                allowed=False,
                blocked_reason=reason,
                target_position_internal=target_position_internal,
                target_position_ha=ha_pos,
                execution_mode=execution_mode.value,
                is_safety=is_safety,
                target_tilt_ha=_tilt_ha,
            )

        def _allowed() -> CommandFilterResult:
            return CommandFilterResult(
                allowed=True,
                blocked_reason=None,
                target_position_internal=target_position_internal,
                target_position_ha=ha_pos,
                execution_mode=execution_mode.value,
                is_safety=is_safety,
                target_tilt_ha=_tilt_ha,
            )

        # ------------------------------------------------------------------
        # Blocking check 1: Manual Override — unconditional, no bypasses.
        # ------------------------------------------------------------------
        if is_manual_override:
            return _blocked(BLOCKED_MANUAL_OVERRIDE)

        # ------------------------------------------------------------------
        # Blocking check 2: Cover unavailable — unconditional, no bypasses.
        # Sending a service call to an unavailable entity raises in HA.
        # ------------------------------------------------------------------
        if not is_cover_available:
            return _blocked(BLOCKED_COVER_UNAVAILABLE)

        # ------------------------------------------------------------------
        # Blocking check 3: Recommendation-only mode.
        # Even safety commands do not override the user's explicit choice to
        # disable automatic control.  The sensor will still show the decision.
        # ------------------------------------------------------------------
        if execution_mode is ExecutionMode.RECOMMENDATION_ONLY:
            return _blocked(BLOCKED_RECOMMENDATION_ONLY)

        # ------------------------------------------------------------------
        # Blocking check 4: StateGuard action interval.
        # Safety (STORM_SAFE / WIND_SAFE) bypasses this check: immediate
        # retraction must never be delayed by a prior command's cooldown.
        # ------------------------------------------------------------------
        if not state_guard_allowed and not is_safety:
            return _blocked(BLOCKED_GUARD_ACTION_INTERVAL)

        # ------------------------------------------------------------------
        # Blocking check 5: Position within tolerance.
        # If the cover is already close enough to the target, skip the
        # command to avoid motor wear and repeated service calls.
        # Safety bypasses: a retraction command must always execute even if
        # the assumed position is already near the target (the assumed
        # position may be wrong after HA restart or drift).
        # ------------------------------------------------------------------
        if (
            target_position_internal is not None
            and current_position_internal is not None
            and not is_safety
            and positions_within_tolerance(
                target_position_internal,
                current_position_internal,
                execution_capability.position_tolerance,
            )
        ):
            return _blocked(BLOCKED_SAME_POSITION)

        # ------------------------------------------------------------------
        # Blocking check 6: Comfort Movement Stability Hold (v1.1.1).
        # A comfort-tier re-target (Solar/Heat/Glare) within the hold window
        # of a previous comfort dispatch is suppressed. comfort_hold_allowed
        # is computed by the coordinator's ComfortMovementHold and is already
        # True for every non-comfort decision (Safety, Night, Night Contact,
        # Absence, Manual Override, fallback/open) — `not is_safety` here is
        # defense in depth only.
        # ------------------------------------------------------------------
        if not comfort_hold_allowed and not is_safety:
            return _blocked(BLOCKED_COMFORT_POSITION_HOLD)

        # ------------------------------------------------------------------
        # Blocking check 7: Fallback/Open release pending (F29 field fix).
        # A "TierOrchestrator:fallback" OPEN proposal is held back until it
        # has been confirmed for enough consecutive cycles, avoiding a
        # visible open-then-close flap when protection clears for only a
        # single outlier cycle. Safety bypasses, same as comfort hold above.
        # ------------------------------------------------------------------
        if not fallback_release_allowed and not is_safety:
            return _blocked(BLOCKED_FALLBACK_RELEASE_PENDING)

        # ------------------------------------------------------------------
        # Blocking check 8: No target position available.
        # ------------------------------------------------------------------
        if target_position_internal is None:
            return _blocked(BLOCKED_NO_TARGET_POSITION)

        return _allowed()
