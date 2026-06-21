"""Stability guards: minimum state duration, minimum action interval,
hysteresis, and position lock. See ARCHITECTURE.md §4.3, with the
minimum_state_duration vs. minimum_action_interval clarification added
during the second audit round.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .states import ShadingState

# States during which the shading position is locked (ARCHITECTURE.md §4.3,
# "Position Lock - Kernregel").
_POSITION_LOCKED_STATES: frozenset[ShadingState] = frozenset(
    {ShadingState.LIGHT_SHADE, ShadingState.NORMAL_SHADE, ShadingState.STRONG_SHADE}
)


@dataclass(frozen=True)
class HysteresisConfig:
    """Entry/exit thresholds for a shade state (ARCHITECTURE.md §4.3,
    "Hysterese-Beispiele"). Exit is always less aggressive than entry."""

    entry_threshold_wm2: float
    exit_threshold_wm2: float


@dataclass
class StateGuardConfig:
    """Mirrors ARCHITECTURE.md §4.3 StateGuardConfig exactly."""

    minimum_state_duration: dict[ShadingState, timedelta] = field(default_factory=dict)
    minimum_action_interval: timedelta = timedelta(minutes=3)
    hysteresis: dict[ShadingState, HysteresisConfig] = field(default_factory=dict)
    open_delay_after_sun_lost: timedelta = timedelta(minutes=10)
    position_lock_during_shading: bool = True


class StateGuard:
    """Per-window stability guard.

    Two independent, separately tracked mechanisms (ARCHITECTURE.md §4.3
    clarification):

    - is_locked(): protects *state transitions* (minimum_state_duration).
      Only ever consulted for de-escalations - state_machine.transitions
      .bypasses_guard() decides when that is the case (escalations,
      lifecycle-direct exits, MANUAL_OVERRIDE/STORM_SAFE/WIND_SAFE exits
      all skip this check entirely, per the §5.7 P0-1 fix).
    - can_send_action(): protects *cover commands* (minimum_action_interval).
      STORM_SAFE and WIND_SAFE (both Tier-1 Safety) always bypass this.
    """

    def __init__(self, config: StateGuardConfig | None = None) -> None:
        self._config = config or StateGuardConfig()
        self._entered_at: dict[str, datetime] = {}
        self._last_action_at: dict[str, datetime] = {}

    def record_state_entered(self, window_id: str, now: datetime) -> None:
        """Call whenever a window's ShadingState actually changes."""
        self._entered_at[window_id] = now

    def record_action_sent(self, window_id: str, now: datetime) -> None:
        """Call whenever a cover command is actually sent for this window."""
        self._last_action_at[window_id] = now

    def is_locked(self, window_id: str, current_state: ShadingState, now: datetime) -> bool:
        """True if `current_state` has not been held for
        `minimum_state_duration[current_state]` yet.

        Callers must only invoke this for transitions where
        state_machine.transitions.bypasses_guard() returned False - see
        ARCHITECTURE.md §5.7 step 9.
        """
        min_duration = self._config.minimum_state_duration.get(current_state)
        if min_duration is None:
            return False
        entered_at = self._entered_at.get(window_id)
        if entered_at is None:
            return False
        return (now - entered_at) < min_duration

    def can_send_action(self, window_id: str, proposed_state: ShadingState, now: datetime) -> bool:
        """True if a cover command may be sent now.

        STORM_SAFE and WIND_SAFE (both Tier-1 Safety, ARCHITECTURE.md §4.3)
        always bypass minimum_action_interval - immediate cover retraction
        must never be throttled by a prior command's cooldown.
        """
        if proposed_state in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE):
            return True
        last_action_at = self._last_action_at.get(window_id)
        if last_action_at is None:
            return True
        return (now - last_action_at) >= self._config.minimum_action_interval

    def is_position_locked(self, current_state: ShadingState) -> bool:
        """True if the cover position is locked during the current shading
        phase (ARCHITECTURE.md §4.3, "Position Lock - Kernregel")."""
        if not self._config.position_lock_during_shading:
            return False
        return current_state in _POSITION_LOCKED_STATES
