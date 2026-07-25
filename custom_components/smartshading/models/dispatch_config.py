"""Dispatch strategy configuration — v1.2.0-beta.1, T11.

Replaces the previously hardcoded, non-configurable
DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS (cover_control/global_dispatch_throttle.py)
with a small, user-facing strategy the coordinator's existing dispatch loop
consults. Deliberately two orthogonal knobs (mode + zone_batching) rather than
one enum enumerating every mode/batching combination — that keeps the config
flow to one selector and one checkbox instead of a confusing matrix, per the
T11 ticket's explicit "keine unübersichtliche Matrix" requirement.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DispatchMode(Enum):
    """How successive cover commands within one coordinator cycle are paced.

    PARALLEL   — no artificial delay between starting successive commands.
                 Only sensible when the hardware/RF path reliably tolerates
                 near-simultaneous commands (e.g. Zigbee/Z-Wave/cloud covers,
                 not shared single-threaded RF gateways like Somfy RTS).
    SPACED     — a fixed delay (start_interval_s) between the START of one
                 command and the START of the next. This is the historical,
                 always-on behavior (DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS)
                 generalized into a configurable strategy — the default
                 reproduces it exactly (start_interval_s=2.0).
    SEQUENTIAL — the next command does not start until the previous cover's
                 travel is detected complete (see cover_control/
                 dispatch_completion.py), plus an optional short
                 post_travel_pause_s settle time.
    """

    PARALLEL = "parallel"
    SPACED = "spaced"
    SEQUENTIAL = "sequential"


DEFAULT_START_INTERVAL_S = 2.0
"""Reproduces the pre-T11 DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS exactly —
existing installations see byte-for-byte identical timing after upgrading
with no config changes (default mode is SPACED at this same interval)."""

DEFAULT_MAX_TRAVEL_WAIT_S = 40.0
"""Upper bound on how long SEQUENTIAL mode waits for one cover's travel to
complete before giving up and continuing the queue. Comfortably above the
default per-cover travel_time_open_s/close_s (30.0s, cover_capabilities.py)
so a normal full-range travel is never mistaken for a timeout."""

DEFAULT_POST_TRAVEL_PAUSE_S = 0.5
"""Short settle time after a SEQUENTIAL-mode completion is detected, before
starting the next command — lets a cover's motor/RF module fully quiesce."""

START_INTERVAL_S_MIN = 0.0
START_INTERVAL_S_MAX = 30.0
MAX_TRAVEL_WAIT_S_MIN = 5.0
MAX_TRAVEL_WAIT_S_MAX = 300.0
POST_TRAVEL_PAUSE_S_MIN = 0.0
POST_TRAVEL_PAUSE_S_MAX = 10.0


@dataclass(frozen=True)
class DispatchConfig:
    """User-facing dispatch strategy, resolved once per Coordinator instance
    (config-flow-driven, not runtime-mutable) and consulted by the existing
    Pass-2 dispatch loop (coordinator.py) via cover_control/
    dispatch_orchestrator.py's pure wait-computation helpers.
    """

    mode: DispatchMode = DispatchMode.SPACED
    start_interval_s: float = DEFAULT_START_INTERVAL_S
    """SPACED: delay between successive command starts. Also the forced
    inter-zone gap when zone_batching=True, regardless of mode."""
    max_travel_wait_s: float = DEFAULT_MAX_TRAVEL_WAIT_S
    """SEQUENTIAL only: timeout waiting for one cover's travel to complete."""
    post_travel_pause_s: float = DEFAULT_POST_TRAVEL_PAUSE_S
    """SEQUENTIAL only: extra settle pause after a detected completion."""
    zone_batching: bool = False
    """When True, the configured start_interval_s is always enforced at a
    zone boundary (the first command of a new zone), even under PARALLEL —
    so zones stay visually/RF-wise separated while covers within the same
    zone follow the configured mode. Does not change dispatch ORDER (already
    zone-grouped by _dispatch_order_key/F18) — only adds a minimum pause at
    the boundary."""
