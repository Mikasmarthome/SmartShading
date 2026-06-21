"""Cover control orchestration: target position in, travel strategy +
TravelTracker + AssumedStateManager out. No real Home Assistant
service calls yet - CoverCommand is the intent that a later Coordinator/
integration phase will actually dispatch to HA.

First-class Somfy RTS / open-close-only handling (see final report):
covers without continuous positioning are driven via full OPEN/CLOSE
instead of set_cover_position, and covers without reliable feedback always
go through AssumedStateManager rather than trusting a reported position.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .assumed_state_manager import AssumedStateManager
from .cover_capabilities import CoverCapability
from .travel_tracker import TravelTracker


class CoverAction(Enum):
    SET_POSITION = "set_position"
    OPEN = "open"
    CLOSE = "close"
    STOP = "stop"
    NONE = "none"  # already at target, nothing to send


@dataclass(frozen=True)
class CoverCommand:
    """The command CoverController decided on. Sending it to Home
    Assistant is the responsibility of a later integration phase."""

    cover_id: str
    action: CoverAction
    target_position: int | None
    issued_at: datetime


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


class CoverController:
    """Decides *how* to drive a cover towards a target position, given its
    capability, and keeps TravelTracker/AssumedStateManager in sync.
    """

    def __init__(self, travel_tracker: TravelTracker, assumed_state_manager: AssumedStateManager) -> None:
        self._travel_tracker = travel_tracker
        self._assumed_state_manager = assumed_state_manager

    def send_target_position(
        self,
        cover_id: str,
        capability: CoverCapability,
        target_position: int,
        now: datetime,
    ) -> CoverCommand:
        """Resolve a travel strategy for `target_position` and start
        tracking it. Returns the resulting CoverCommand (not yet sent to HA).
        """
        clamped_target = _clamp(target_position, capability.min_position, capability.max_position)
        current_estimate = self._estimate_current_position(cover_id, capability, now)

        if clamped_target == current_estimate:
            return CoverCommand(cover_id, CoverAction.NONE, clamped_target, now)

        if capability.supports_continuous_positioning():
            action = CoverAction.SET_POSITION
            effective_target = clamped_target
        else:
            # Somfy RTS / open-close-only strategy: no intermediate
            # position is achievable, so approximate via a full OPEN or
            # CLOSE towards whichever bound is closer to the requested
            # target's direction.
            if clamped_target > current_estimate:
                action = CoverAction.OPEN
                effective_target = capability.max_position
            else:
                action = CoverAction.CLOSE
                effective_target = capability.min_position

        self._travel_tracker.start_travel(
            cover_id=cover_id,
            start_position=current_estimate,
            target_position=effective_target,
            started_at=now,
            travel_time_open_s=capability.travel_time_open_s,
            travel_time_close_s=capability.travel_time_close_s,
        )
        self._assumed_state_manager.update(
            cover_id=cover_id,
            position=effective_target,
            commanded_at=now,
            has_reliable_position_feedback=capability.has_reliable_position_feedback,
        )

        return CoverCommand(cover_id, action, effective_target, now)

    def send_stop(self, cover_id: str, capability: CoverCapability, now: datetime) -> CoverCommand | None:
        """Returns None if this cover does not support stopping
        (ARCHITECTURE.md Somfy RTS requirement: not every relay cover has
        a stop command)."""
        if not capability.supports_stop:
            return None

        resting_position = self._travel_tracker.stop_travel(cover_id, now)
        if resting_position is None:
            # Nothing was in flight - still a valid stop, just a no-op for tracking.
            return CoverCommand(cover_id, CoverAction.STOP, None, now)

        self._assumed_state_manager.update(
            cover_id=cover_id,
            position=resting_position,
            commanded_at=now,
            has_reliable_position_feedback=capability.has_reliable_position_feedback,
        )
        return CoverCommand(cover_id, CoverAction.STOP, resting_position, now)

    def poll_travel_progress(self, cover_id: str, capability: CoverCapability, now: datetime) -> int | None:
        """Call periodically while a cover is in flight to keep the
        assumed position fresh without waiting for travel to complete.
        Returns the current estimate, or None if this cover isn't moving.

        Does not increment position_uncertainty_pct (see
        AssumedStateManager.record_progress) - uncertainty grows once per
        commanded travel, not once per poll tick.
        """
        if not self._travel_tracker.is_moving(cover_id, now):
            return None
        estimate = self._travel_tracker.estimate_position(cover_id, now)
        if estimate is None:
            return None
        self._assumed_state_manager.record_progress(cover_id, estimate, now)
        if not self._travel_tracker.is_moving(cover_id, now):
            self._travel_tracker.clear(cover_id)
        del capability  # not needed yet, kept for a consistent call signature with the other methods
        return estimate

    def _estimate_current_position(self, cover_id: str, capability: CoverCapability, now: datetime) -> int:
        if self._travel_tracker.is_moving(cover_id, now):
            estimate = self._travel_tracker.estimate_position(cover_id, now)
            if estimate is not None:
                return estimate
        state = self._assumed_state_manager.get_state(cover_id, now)
        if state is not None:
            return state.assumed_position
        return capability.min_position
