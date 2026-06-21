"""Unified capability model for heterogeneous cover hardware. See
ARCHITECTURE.md §3.2, extended with explicit open/close/stop flags so that
open-close-only hardware (Somfy RTS, simple relay covers) is a first-class
scenario rather than an edge case inferred from a single boolean.

Naming note: field names follow ARCHITECTURE.md §3.2 exactly
(`assumed_state`, `invert_position`) rather than the slightly different
wording used when this phase was requested (`assumed_position`,
`inverted_position`), to stay consistent with the already-approved
architecture. `supports_stop`/`supports_open`/`supports_close` are new
fields not yet in §3.2 - a deliberate, documented extension for the Somfy
RTS first-class requirement (see final report).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CoverProfile(Enum):
    """Cover Compatibility Layer (2026-06-16, Capability Detector round) -
    internal identifier for a cover's capability "shape". Only the three
    capability-based GENERIC_* profiles are classified today
    (classify_cover_profile() below) - this enum is designed so brand-
    specific profiles can be added later purely additively, without
    touching the GENERIC_* values or anything that already depends on them:

        ESP_SOMFY_RTS, SHELLY_COVER, ZIGBEE_COVER, SOMFY_IO,
        MATTER_COVER, KNX_COVER

    Adding one of those later means: detect a more specific signal (e.g.
    integration domain, model attribute) *before* falling back to the
    generic capability-based classification, and return the more specific
    member instead. No existing profile member changes meaning.
    """

    GENERIC_POSITION = "generic_position"
    GENERIC_OPEN_CLOSE_STOP = "generic_open_close_stop"
    GENERIC_OPEN_CLOSE = "generic_open_close"
    UNKNOWN = "unknown"


def classify_cover_profile(
    supports_position: bool,
    supports_stop: bool,
    supports_open: bool,
    supports_close: bool,
) -> CoverProfile:
    """Pure classification from raw capability flags into a CoverProfile.

    No Home Assistant dependency - kept here (not in capability_detector.py)
    specifically so it stays unit-testable without a real HA instance, the
    same way as the rest of cover_control/.
    """
    if supports_position:
        return CoverProfile.GENERIC_POSITION
    if supports_stop and supports_open and supports_close:
        return CoverProfile.GENERIC_OPEN_CLOSE_STOP
    if supports_open and supports_close:
        return CoverProfile.GENERIC_OPEN_CLOSE
    return CoverProfile.UNKNOWN


@dataclass
class CoverCapability:
    """ARCHITECTURE.md §3.2, extended with explicit action flags."""

    entity_id: str

    # Basic capabilities
    supports_position: bool  # set_cover_position supported
    supports_tilt: bool  # set_cover_tilt_position supported
    supports_open_close_only: bool  # only open/close, no continuous positioning
    supports_stop: bool = False  # cover.stop_cover supported (often missing on simple relays)
    supports_open: bool = True  # cover.open_cover supported
    supports_close: bool = True  # cover.close_cover supported

    # Feedback
    has_reliable_position_feedback: bool = True  # False for Somfy RTS, most RF systems
    assumed_state: bool = False  # True if the cover reports no real state at all

    # Bounds and safety
    min_position: int = 0
    max_position: int = 100
    safe_position: int = 0  # storm-safe position (default: fully closed)

    # Special behavior
    invert_position: bool = False  # True if 0=open, 100=closed (some integrations)
    travel_time_open_s: float = 30.0
    travel_time_close_s: float = 30.0
    command_debounce_s: float = 3.0

    # Cover Compatibility Layer (2026-06-16) - see CoverProfile above.
    cover_profile: CoverProfile = CoverProfile.UNKNOWN
    device_class: str | None = None  # HA cover device_class, e.g. "shutter"/"garage" - diagnostic only for now

    def supports_continuous_positioning(self) -> bool:
        """True if the cover can be driven to an arbitrary intermediate
        position. False for Somfy RTS / open-close-only hardware, which
        CoverController must drive via full OPEN/CLOSE instead."""
        return self.supports_position and not self.supports_open_close_only
