"""Release-hysteresis holds for Wind and Storm safety states.

After a safety evaluator fires, the hold keeps the safety state active for
a minimum duration — preventing premature release when wind drops briefly
between scan cycles.

When the underlying sensor becomes unavailable while a hold is active, the
hold timer is extended (reset) each cycle rather than counting down.  This
prevents the safety state from clearing based on absent data rather than a
confirmed below-threshold reading.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..models.cover_group import CoverHardwareType


@dataclass
class SafetyHold:
    """Release-hysteresis tracker for a binary safety signal.

    Once the evaluator triggers (``evaluator_triggered=True``), the hold is
    latched.  It remains latched for at least ``hold_s`` seconds after the
    last trigger, unless the sensor is unavailable — in that case the hold
    timer is reset each cycle so the safety state is NOT released based on
    absent data.

    Usage (once per coordinator cycle, before the window loop)::

        latched = hold.update(evaluator_triggered=True, now=now)
    """

    _hold_s: float
    _last_triggered: datetime | None = field(default=None)

    def update(
        self,
        *,
        evaluator_triggered: bool,
        now: datetime,
        sensor_unavailable: bool = False,
    ) -> bool:
        """Update hold state and return True if the safety latch is active.

        Parameters
        ----------
        evaluator_triggered:
            True when the safety evaluator produced a result this cycle.
        now:
            Current UTC timestamp.
        sensor_unavailable:
            True when the underlying wind/weather sensor has no reading
            (state is unavailable/unknown/None).  If the latch is active and
            the sensor disappears, the hold timer is reset so the safety
            state is NOT released on absent data — the cover stays in the
            safe position until a confirmed below-threshold reading arrives.
        """
        if evaluator_triggered:
            self._last_triggered = now
        elif sensor_unavailable and self._last_triggered is not None:
            # Sensor gone while latched: extend hold rather than counting down.
            self._last_triggered = now
        if self._last_triggered is None:
            return False
        elapsed = (now - self._last_triggered).total_seconds()
        if elapsed >= self._hold_s:
            self._last_triggered = None
            return False
        return True

    @property
    def is_held(self) -> bool:
        """True when the latch is currently active."""
        return self._last_triggered is not None

    def seconds_held(self, now: datetime) -> float | None:
        """Seconds since the hold was last triggered, or None when not latched."""
        if self._last_triggered is None:
            return None
        return (now - self._last_triggered).total_seconds()


# Release-hysteresis durations for wind and storm safety latches.
# After the evaluator last fires, the hold persists for this long even if wind
# drops below the threshold — prevents flutter during gusty conditions.
WIND_HOLD_S: float = 600.0   # 10 min = 2 scan cycles (5-min default interval)
STORM_HOLD_S: float = 600.0  # 10 min — same; storm codes change slowly anyway


# Internal safe position per hardware type for STORM_SAFE / WIND_SAFE states.
# Internal convention: 0=open/retracted, 100=shaded/deployed.
# HA conversion (INV-18): ha_position = 100 - internal_position.
#
# ROLLER_SHUTTER / VENETIAN_BLIND: safe = retracted UP = internal 0 → HA 100.
# AWNING / EXTERIOR_SCREEN: safe = retracted = HA 0 → internal 100.
#   Without this, wind/storm events would send AWNING/EXTERIOR_SCREEN to HA 100%
#   (fully deployed) — the most exposed position possible.
# GENERIC: fail-safe open → internal 0 → HA 100.
HARDWARE_SAFE_POSITIONS: dict[CoverHardwareType, int] = {
    CoverHardwareType.ROLLER_SHUTTER:  0,
    CoverHardwareType.VENETIAN_BLIND:  0,
    CoverHardwareType.EXTERIOR_SCREEN: 100,
    CoverHardwareType.AWNING:          100,
    CoverHardwareType.GENERIC:         0,
}
