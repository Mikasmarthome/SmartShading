"""Travel-time tracking: estimates a cover's position while it is moving,
using elapsed-time / total-travel-time interpolation. See ARCHITECTURE.md
§15 Blocker A ("eigene, schlanke Implementierung") and §6.2.

This module only models *active travel* (a command currently in flight).
The resting position once travel completes is AssumedStateManager's
responsibility, not this module's - see assumed_state_manager.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def _clamp_position(position: float) -> int:
    return int(round(max(0.0, min(100.0, position))))


@dataclass
class TravelState:
    """One cover's in-flight travel."""

    cover_id: str
    start_position: int
    target_position: int
    started_at: datetime
    travel_time_open_s: float
    travel_time_close_s: float

    def direction_travel_time_s(self) -> float:
        """Travel time for this specific move's direction. 0=closed,
        100=open, so a higher target means opening."""
        if self.target_position == self.start_position:
            return 0.0
        if self.target_position > self.start_position:
            return self.travel_time_open_s
        return self.travel_time_close_s


class TravelTracker:
    """Per-cover travel-time model. No Home Assistant dependencies.

    Direction reversal: if start_travel() is called for a cover that is
    already moving, the caller is expected to pass the current estimate
    (via estimate_position()) as the new start_position - this naturally
    handles "changed its mind mid-flight" without any special-casing here.
    """

    def __init__(self) -> None:
        self._states: dict[str, TravelState] = {}

    def start_travel(
        self,
        cover_id: str,
        start_position: int,
        target_position: int,
        started_at: datetime,
        travel_time_open_s: float,
        travel_time_close_s: float,
    ) -> None:
        self._states[cover_id] = TravelState(
            cover_id=cover_id,
            start_position=_clamp_position(start_position),
            target_position=_clamp_position(target_position),
            started_at=started_at,
            travel_time_open_s=travel_time_open_s,
            travel_time_close_s=travel_time_close_s,
        )

    def is_moving(self, cover_id: str, now: datetime) -> bool:
        return self.remaining_travel_s(cover_id, now) > 0.0

    def get_target_position(self, cover_id: str) -> int | None:
        state = self._states.get(cover_id)
        return state.target_position if state is not None else None

    def remaining_travel_s(self, cover_id: str, now: datetime) -> float:
        state = self._states.get(cover_id)
        if state is None:
            return 0.0
        total = state.direction_travel_time_s()
        if total <= 0.0:
            return 0.0
        elapsed = (now - state.started_at).total_seconds()
        return max(0.0, total - elapsed)

    def estimate_position(self, cover_id: str, now: datetime) -> int | None:
        """Interpolated position estimate. Returns None if this cover has
        no active travel (already arrived, or never started)."""
        state = self._states.get(cover_id)
        if state is None:
            return None
        return self._estimate_from_state(state, now)

    def stop_travel(self, cover_id: str, now: datetime) -> int | None:
        """Handle an explicit stop command: freezes travel at the current
        estimate and clears it. Returns the resulting resting position
        (the caller should hand this to AssumedStateManager), or None if
        this cover had no active travel."""
        state = self._states.pop(cover_id, None)
        if state is None:
            return None
        return self._estimate_from_state(state, now)

    def clear(self, cover_id: str) -> None:
        """Call once travel has naturally completed (estimate reached the
        target) to stop tracking it as in-flight."""
        self._states.pop(cover_id, None)

    @staticmethod
    def _estimate_from_state(state: TravelState, now: datetime) -> int:
        total = state.direction_travel_time_s()
        if total <= 0.0:
            return state.target_position
        elapsed = (now - state.started_at).total_seconds()
        fraction = max(0.0, min(1.0, elapsed / total))
        raw = state.start_position + (state.target_position - state.start_position) * fraction
        return _clamp_position(raw)
