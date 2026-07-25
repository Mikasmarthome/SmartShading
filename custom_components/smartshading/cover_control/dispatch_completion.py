"""Tiered travel-completion detection for SEQUENTIAL dispatch mode
(v1.2.0-beta.1, T11).

Only ever consulted when DispatchConfig.mode is SEQUENTIAL — PARALLEL and
SPACED never call this module.

Tiered strategy, most-trustworthy signal first:
  1. Reliable position feedback: poll the cover's real HA state; completed
     once idle (not opening/closing) AND within position_tolerance of the
     target. Never assumed — an unreliable-feedback cover's raw state is not
     trustworthy enough to poll (same reasoning as the existing own-command
     guard's unreliable-feedback handling in engines/override_detector.py:
     a stale/misleading reported position must not drive logic).
  2. No reliable feedback: reuse the per-cover travel_time_open_s /
     travel_time_close_s already modeled in cover_capabilities.py (the same
     numbers cover_control/travel_tracker.py's estimator uses), scaled by
     the fraction of the 0-100 range actually being crossed when the start
     position is known, and simply wait that long.
  3. Timeout: both tiers are bounded by max_travel_wait_s — a cover that
     never reports completion (tier 1) or whose estimate somehow exceeds the
     bound (tier 2) is not waited on forever; the queue continues.

No busy-wait: uses asyncio.sleep(poll_interval_s) between checks (tier 1)
or a single bounded sleep (tier 2) — never a tight loop.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .cover_entity_snapshot import build_cover_entity_snapshot

DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_POSITION_TOLERANCE = 5

MonoClock = Callable[[], float]
Sleeper = Callable[[float], "asyncio.Future"]


class CompletionMethod(Enum):
    POSITION = "position"
    MOVEMENT_STATE = "movement_state"
    TIME_ESTIMATE = "time_estimate"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class CompletionResult:
    method: CompletionMethod
    elapsed_s: float
    timed_out: bool


def estimate_travel_duration_s(
    *,
    start_position_ha: int | None,
    target_position_ha: int,
    travel_time_open_s: float,
    travel_time_close_s: float,
) -> float:
    """Seconds needed to travel from start to target, scaled by the fraction
    of the 0-100 range actually crossed. Falls back to the longer of the two
    directional durations when the start position is unknown — never
    under-estimate (and therefore never under-wait) when a real delta can't
    be computed."""
    if start_position_ha is None:
        return max(travel_time_open_s, travel_time_close_s)
    delta = abs(target_position_ha - start_position_ha)
    if delta <= 0:
        return 0.0
    direction_duration = (
        travel_time_open_s if target_position_ha > start_position_ha else travel_time_close_s
    )
    return direction_duration * (delta / 100.0)


def evaluate_travel_status(
    *,
    state: str | None,
    attributes: dict,
    invert: bool,
    has_reliable_position_feedback: bool,
    target_position_ha: int,
    position_tolerance: int = DEFAULT_POSITION_TOLERANCE,
) -> CompletionMethod | None:
    """Pure judgment: has this cover finished the current travel, per the
    most trustworthy signal available? Returns None while still in progress
    (or the entity is unavailable — never treated as "done")."""
    snap = build_cover_entity_snapshot(
        entity_id="_dispatch_completion_check",
        state=state,
        attributes=attributes,
        invert=invert,
        has_reliable_position_feedback=has_reliable_position_feedback,
    )
    if not snap.available or snap.is_moving:
        return None
    if has_reliable_position_feedback and snap.current_position_ha is not None:
        if abs(snap.current_position_ha - target_position_ha) <= position_tolerance:
            return CompletionMethod.POSITION
        return None
    if has_reliable_position_feedback:
        # Reliable feedback but position attribute absent this cycle (some
        # integrations omit it briefly) — idle state alone is still a real
        # signal for these covers.
        return CompletionMethod.MOVEMENT_STATE
    # No reliable feedback at all: real polled state is not trustworthy
    # enough to call "done" from — the time-estimate tier handles this case
    # entirely without ever reaching here (see wait_for_travel_completion).
    return None


async def wait_for_travel_completion(
    hass,
    *,
    entity_id: str,
    target_position_ha: int,
    invert_position: bool,
    has_reliable_position_feedback: bool,
    start_position_ha: int | None,
    travel_time_open_s: float,
    travel_time_close_s: float,
    max_wait_s: float,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    position_tolerance: int = DEFAULT_POSITION_TOLERANCE,
    mono_clock: MonoClock = time.monotonic,
    sleep=asyncio.sleep,
) -> CompletionResult:
    """Wait for one cover's travel to complete, tiered per the module
    docstring. Always returns (never raises) — a timeout is a normal,
    expected outcome the caller (coordinator.py) records in diagnostics and
    continues the queue from, per the T11 spec's "robust: document and
    continue" default."""
    started = mono_clock()
    estimated_s = estimate_travel_duration_s(
        start_position_ha=start_position_ha,
        target_position_ha=target_position_ha,
        travel_time_open_s=travel_time_open_s,
        travel_time_close_s=travel_time_close_s,
    )

    if has_reliable_position_feedback:
        while True:
            elapsed = mono_clock() - started
            if elapsed >= max_wait_s:
                return CompletionResult(CompletionMethod.TIMEOUT, elapsed, True)
            entity_state = hass.states.get(entity_id)
            state = entity_state.state if entity_state is not None else None
            attributes = entity_state.attributes if entity_state is not None else {}
            method = evaluate_travel_status(
                state=state,
                attributes=attributes,
                invert=invert_position,
                has_reliable_position_feedback=True,
                target_position_ha=target_position_ha,
                position_tolerance=position_tolerance,
            )
            if method is not None:
                return CompletionResult(method, mono_clock() - started, False)
            remaining = max_wait_s - (mono_clock() - started)
            await sleep(min(poll_interval_s, max(0.0, remaining)))

    # No reliable feedback: a single bounded wait for the estimated duration
    # — never poll real state for these covers (tier 2/3 of the module
    # docstring).
    wait_s = min(estimated_s, max_wait_s) if estimated_s > 0 else 0.0
    if wait_s > 0:
        await sleep(wait_s)
    timed_out = estimated_s > max_wait_s
    return CompletionResult(
        CompletionMethod.TIMEOUT if timed_out else CompletionMethod.TIME_ESTIMATE,
        mono_clock() - started,
        timed_out,
    )
