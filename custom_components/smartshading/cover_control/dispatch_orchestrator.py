"""Dispatch orchestration — pure wait-computation for the Pass-2 dispatch
loop (v1.2.0-beta.1, T11).

Generalizes the previously hardcoded, always-on
DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS (global_dispatch_throttle.py) into
a configurable DispatchMode (see models/dispatch_config.py) while reusing
the SAME GlobalSerialDispatch lock + GlobalDispatchThrottle instance the
coordinator already shares across every zone entry — no parallel dispatch
architecture, no second lock.

Two things stay deliberately OUT of this module and are NOT reinvented here,
because existing mechanisms already cover them (see coordinator.py's Pass-2
loop and engines/override_release.py's architecture note):
  - Cross-cycle staleness/preemption: the existing self._dispatch_generation
    stale-intent guard (coordinator.py) already lets a new coordinator cycle
    (fired mid-dispatch by a presence/safety event) cancel not-yet-dispatched
    non-safety intents while safety intents remain exempt. T15 note: this
    guard runs exactly once per intent, immediately before that intent's own
    dispatch_cover_intent() call — it is NOT re-checked after the SEQUENTIAL
    completion-wait (dispatch_completion.py) or the post-dispatch state
    mutation (assumed-state update, StateGuard, ComfortMovementHold). Those
    steps only run for intents that already passed the guard once, and by
    design a real command that was already sent is not un-sent by a later
    generation bump — but a generation bump during the completion-wait does
    not gate anything downstream of it either.
  - Duplicate/no-op suppression: CommandFilter's pre-dispatch position-
    tolerance check (BLOCKED_SAME_POSITION) already prevents redispatching an
    unchanged target.

This module has no Home Assistant dependency. Pure Python, directly
unit-testable without HA or asyncio.
"""
from __future__ import annotations

from datetime import timedelta

from ..models.dispatch_config import DispatchConfig, DispatchMode


def effective_interval_s(
    config: DispatchConfig, *, is_first_in_zone_group: bool
) -> float:
    """The minimum delay (seconds) before this command may START, relative
    to the previous command's start — NOT counting any SEQUENTIAL
    completion-wait (that is computed separately, after dispatch, by
    cover_control/dispatch_completion.py).

    zone_batching forces at least start_interval_s at a zone boundary
    regardless of mode — this is the only place PARALLEL/SEQUENTIAL can
    still produce a non-zero pre-dispatch wait.
    """
    if config.zone_batching and is_first_in_zone_group:
        return max(0.0, config.start_interval_s)
    if config.mode is DispatchMode.SPACED:
        return max(0.0, config.start_interval_s)
    # PARALLEL and SEQUENTIAL both start the next command immediately once
    # their own gating (none, or the completion-wait) is satisfied.
    return 0.0


def resolve_pre_dispatch_wait(
    config: DispatchConfig,
    *,
    is_first_in_zone_group: bool,
    time_since_last_dispatch_s: float | None,
) -> timedelta:
    """How long to sleep before dispatching this intent.

    time_since_last_dispatch_s is None for the very first dispatch of the
    coordinator's lifetime (or after a fresh GlobalDispatchThrottle) — always
    an immediate dispatch, matching GlobalDispatchThrottle's own "first
    dispatch is always immediate" contract.
    """
    interval = effective_interval_s(config, is_first_in_zone_group=is_first_in_zone_group)
    if interval <= 0 or time_since_last_dispatch_s is None:
        return timedelta(0)
    remaining = interval - time_since_last_dispatch_s
    return timedelta(seconds=remaining) if remaining > 0 else timedelta(0)


def requires_completion_wait(config: DispatchConfig) -> bool:
    """True only for SEQUENTIAL — the only mode where the NEXT command must
    wait for the PREVIOUS one's travel to finish rather than just a fixed
    interval since it started."""
    return config.mode is DispatchMode.SEQUENTIAL
