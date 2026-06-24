"""HA Service Adapter — Phase 9G4.

Single, authoritative boundary between SmartShading's pure execution model
and Home Assistant cover service calls.

RESPONSIBILITY
--------------
This is the ONLY module in SmartShading that may call
    hass.services.async_call("cover", ...)

No other code in cover_control/ or elsewhere may make HA cover service calls
directly.  All such calls must go through dispatch_cover_intent().

WHAT THIS MODULE DOES
---------------------
  CoverIntent (MOVE_TO_POSITION)
      → hass.services.async_call("cover", "set_cover_position", {position: ha})
      → ExecutionResult (SENT or FAILED)

  CoverIntent (MOVE_TO_TILT)
      → hass.services.async_call("cover", "set_cover_tilt_position", {tilt_position: ha})
      → ExecutionResult (SENT or FAILED, tilt_sent=True/False)

  CoverIntent (MOVE_TO_POSITION_AND_TILT)
      → cover.set_cover_position first, then cover.set_cover_tilt_position
      → ExecutionResult:
            both succeed → SENT,   tilt_sent=True
            position ok, tilt fails → FAILED, tilt_sent=False, tilt_error set
            position fails → FAILED (no tilt attempt)

WHAT THIS MODULE DOES NOT DO
-----------------------------
  - No position conversion.  target_position_ha / target_tilt come pre-converted.
  - No Stop dispatch (future use).
  - No state reading.  CoverEntitySnapshot is produced upstream.
  - No StateGuard update.  The Coordinator calls StateGuard.record_action_sent()
    ONLY when ExecutionPlanResult.any_sent=True AND any_failed=False.
  - No retry logic.  One attempt per cycle; the next cycle retries naturally.

POSITION INVARIANT (CRITICAL)
------------------------------
    ALWAYS:   service_data["position"] = intent.target_position_ha
    NEVER:    service_data["position"] = intent.target_position_internal

HA cover.set_cover_position expects HA convention: 0=closed, 100=open.
SmartShading internal convention is the opposite: 0=open, 100=shaded.
Sending an internal position to HA would move the cover to the WRONG position.

TILT INVARIANT
--------------
    ALWAYS:   service_data["tilt_position"] = intent.target_tilt
    target_tilt is already in HA tilt convention [0, 100] as stored in
    CoverIntent.target_tilt (populated from CommandFilterResult.target_tilt_ha).

DISPATCH DECISION TREE
-----------------------
Each gate is evaluated in order; the first matching gate produces the result.

1. now_utc is naive datetime            → ValueError
2. intent.allowed = False               → BLOCKED
3. execution_mode = recommendation_only → BLOCKED (defensive)
4. command_type = NO_OP / BLOCKED / STOP → NOT_ATTEMPTED
5. command_type = MOVE_TO_POSITION      → send position → SENT or FAILED
6. command_type = MOVE_TO_TILT         → send tilt → SENT(tilt_sent=True) or FAILED
7. command_type = MOVE_TO_POSITION_AND_TILT
       → send position first
       → if position fails → FAILED (no tilt attempt)
       → send tilt
       → if tilt succeeds → SENT (tilt_sent=True)
       → if tilt fails → FAILED (tilt_sent=False, tilt_error set, sent_at_utc from position)

SENT_AT_UTC ON EXCEPTION
-------------------------
When async_call() raises, sent_at_utc is set to now_utc — the call was started,
which begins the global 1.0 s dispatch interval regardless of outcome.
For MOVE_TO_POSITION_AND_TILT where position succeeded but tilt raised:
sent_at_utc reflects the position dispatch time, status=FAILED signals the
partial failure.  Only NOT_ATTEMPTED and BLOCKED leave sent_at_utc as None
because no async_call was ever invoked.

HOMEASSISTANT IMPORT
--------------------
HomeAssistant is only imported under TYPE_CHECKING so this module remains
importable in the pure-Python test environment without a real HA installation.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from .command_filter import BLOCKED_RECOMMENDATION_ONLY, ExecutionMode
from .execution_plan import CoverCommandType, CoverIntent
from .execution_result import (
    ExecutionResult,
    ExecutionStatus,
    build_blocked_result,
    build_failed_result,
    build_not_attempted_result,
    build_sent_result,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


# ---------------------------------------------------------------------------
# HA service constants
# ---------------------------------------------------------------------------

_COVER_DOMAIN: str = "cover"
_SERVICE_SET_POSITION: str = "set_cover_position"
_SERVICE_SET_TILT: str = "set_cover_tilt_position"

_FIELD_ENTITY_ID: str = "entity_id"
_FIELD_POSITION: str = "position"          # HA convention: 0=closed, 100=open
_FIELD_TILT_POSITION: str = "tilt_position"  # HA tilt convention: [0, 100]


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _require_utc(dt: datetime) -> None:
    """Raise ValueError when *dt* has no timezone information."""
    if dt.tzinfo is None:
        raise ValueError(
            f"now_utc must be a timezone-aware datetime (UTC). "
            f"Got naive datetime: {dt!r}. "
            f"Use datetime.now(timezone.utc) or dt_util.utcnow()."
        )


async def _dispatch_position(
    hass: HomeAssistant,
    intent: CoverIntent,
    *,
    now_utc: datetime,
) -> tuple[bool, str | None]:
    """Send cover.set_cover_position.  Returns (sent_ok, error_str)."""
    service_data = {
        _FIELD_ENTITY_ID: intent.cover_entity_id,
        _FIELD_POSITION: intent.target_position_ha,
    }
    try:
        await hass.services.async_call(_COVER_DOMAIN, _SERVICE_SET_POSITION, service_data)
        return True, None
    except Exception as exc:
        return False, f"cover.set_cover_position raised {type(exc).__name__}: {exc!s}"


async def _dispatch_tilt(
    hass: HomeAssistant,
    intent: CoverIntent,
) -> tuple[bool, str | None]:
    """Send cover.set_cover_tilt_position.  Returns (sent_ok, error_str)."""
    service_data = {
        _FIELD_ENTITY_ID: intent.cover_entity_id,
        _FIELD_TILT_POSITION: intent.target_tilt,
    }
    try:
        await hass.services.async_call(_COVER_DOMAIN, _SERVICE_SET_TILT, service_data)
        return True, None
    except Exception as exc:
        return False, f"cover.set_cover_tilt_position raised {type(exc).__name__}: {exc!s}"


# ---------------------------------------------------------------------------
# Main dispatch function
# ---------------------------------------------------------------------------

async def dispatch_cover_intent(
    hass: HomeAssistant,
    intent: CoverIntent,
    *,
    now_utc: datetime,
) -> ExecutionResult:
    """Dispatch one CoverIntent to the appropriate HA cover service(s).

    This is the ONLY function that may issue cover service calls in SmartShading.

    Supports:
      MOVE_TO_POSITION              → cover.set_cover_position
      MOVE_TO_TILT                  → cover.set_cover_tilt_position
      MOVE_TO_POSITION_AND_TILT     → position first, then tilt (two calls)

    Returns one of: BLOCKED, NOT_ATTEMPTED, SENT, or FAILED.
    For MOVE_TO_POSITION_AND_TILT partial failure (position ok, tilt fails):
      status=FAILED, tilt_error set, sent_at_utc reflects position dispatch time.
    """
    _require_utc(now_utc)

    # Gate 1: CommandFilter already blocked this intent.
    if not intent.allowed:
        return build_blocked_result(
            intent,
            reason=f"intent not allowed by CommandFilter: {intent.blocked_reason}",
        )

    # Gate 2: Recommendation-only mode (defensive — normally caught by gate 1).
    if intent.execution_mode == ExecutionMode.RECOMMENDATION_ONLY.value:
        return build_blocked_result(
            intent,
            reason="execution_mode is recommendation_only — no service call sent",
            blocked_reason=BLOCKED_RECOMMENDATION_ONLY,
        )

    # Gate 3: Dispatch by command type.
    if intent.command_type is CoverCommandType.MOVE_TO_POSITION:
        if intent.target_position_ha is None:
            return build_not_attempted_result(
                intent,
                reason="target_position_ha is None — cannot dispatch cover.set_cover_position",
            )
        sent_ok, err = await _dispatch_position(hass, intent, now_utc=now_utc)
        if not sent_ok:
            return build_failed_result(
                intent, error=err or "unknown", sent_at_utc=now_utc,
                reason=f"{err} for {intent.cover_entity_id!r}",
            )
        return build_sent_result(
            intent, sent_at_utc=now_utc,
            reason=(
                f"cover.set_cover_position dispatched: "
                f"entity={intent.cover_entity_id!r} "
                f"ha_position={intent.target_position_ha} "
                f"internal_position={intent.target_position_internal}"
            ),
        )

    if intent.command_type is CoverCommandType.MOVE_TO_TILT:
        if intent.target_tilt is None:
            return build_not_attempted_result(
                intent,
                reason="target_tilt is None — cannot dispatch cover.set_cover_tilt_position",
            )
        tilt_ok, tilt_err = await _dispatch_tilt(hass, intent)
        if not tilt_ok:
            return build_failed_result(
                intent, error=tilt_err or "unknown", sent_at_utc=now_utc,
                reason=f"{tilt_err} for {intent.cover_entity_id!r}",
            )
        return replace(
            build_sent_result(
                intent, sent_at_utc=now_utc,
                reason=(
                    f"cover.set_cover_tilt_position dispatched: "
                    f"entity={intent.cover_entity_id!r} tilt={intent.target_tilt}"
                ),
            ),
            tilt_sent=True,
        )

    if intent.command_type is CoverCommandType.MOVE_TO_POSITION_AND_TILT:
        if intent.target_position_ha is None and intent.target_tilt is None:
            return build_not_attempted_result(
                intent, reason="both target_position_ha and target_tilt are None",
            )
        pos_sent_at = now_utc
        # Step 1: position dispatch (if a position target exists).
        if intent.target_position_ha is not None:
            sent_ok, pos_err = await _dispatch_position(hass, intent, now_utc=now_utc)
            if not sent_ok:
                return build_failed_result(
                    intent, error=pos_err or "unknown", sent_at_utc=now_utc,
                    reason=f"position dispatch failed for {intent.cover_entity_id!r}: {pos_err}",
                )
        # Step 2: tilt dispatch.
        if intent.target_tilt is None:
            return build_sent_result(
                intent, sent_at_utc=pos_sent_at,
                reason=(
                    f"cover.set_cover_position dispatched (no tilt target): "
                    f"entity={intent.cover_entity_id!r} ha_position={intent.target_position_ha}"
                ),
            )
        tilt_ok, tilt_err = await _dispatch_tilt(hass, intent)
        if not tilt_ok:
            # Position succeeded, tilt failed: partial failure.
            # sent_at_utc reflects when position was dispatched.
            return replace(
                build_failed_result(
                    intent,
                    error=f"tilt dispatch failed: {tilt_err}",
                    sent_at_utc=pos_sent_at,
                    reason=(
                        f"position sent but tilt dispatch failed "
                        f"for {intent.cover_entity_id!r}: {tilt_err}"
                    ),
                ),
                tilt_error=tilt_err,
            )
        return replace(
            build_sent_result(
                intent, sent_at_utc=pos_sent_at,
                reason=(
                    f"cover.set_cover_position and cover.set_cover_tilt_position dispatched: "
                    f"entity={intent.cover_entity_id!r} "
                    f"ha_position={intent.target_position_ha} tilt={intent.target_tilt}"
                ),
            ),
            tilt_sent=True,
        )

    # All other types (NO_OP, BLOCKED, STOP): no dispatch.
    return build_not_attempted_result(
        intent,
        reason=(
            f"command_type '{intent.command_type.value}' does not produce a service call "
            f"(NO_OP, BLOCKED, or STOP)"
        ),
    )
