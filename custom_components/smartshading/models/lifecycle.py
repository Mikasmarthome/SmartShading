"""Night/Day Lifecycle data model. See ARCHITECTURE.md §3.4 (NightDayLifecycleConfig)
and §5.6 (Night/Day Lifecycle Engine). Pure dataclasses/enums - no Home
Assistant dependency, consistent with the rest of models/.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from enum import Enum


class LifecycleScheduleMode(Enum):
    """Controls whether the zone uses one shared schedule or separate weekday/weekend schedules.

    SAME_EVERY_DAY:    A single night/morning time + position applies every day.
    WEEKDAY_WEEKEND:   Separate night/morning times and positions for Mon-Fri vs Sat-Sun.

    Legacy configs without this field are treated as SAME_EVERY_DAY.
    """

    SAME_EVERY_DAY = "same_every_day"
    WEEKDAY_WEEKEND = "weekday_weekend"


class NightTrigger(Enum):
    """ARCHITECTURE.md §3.4."""

    DISABLED = "disabled"
    SUN_ELEVATION = "sun_elevation"
    FIXED_TIME = "fixed_time"
    BOTH = "both"


class MorningTrigger(Enum):
    """ARCHITECTURE.md §3.4."""

    DISABLED = "disabled"
    SUN_ELEVATION = "sun_elevation"
    FIXED_TIME = "fixed_time"
    BOTH = "both"


class LifecycleState(Enum):
    """ARCHITECTURE.md §5.6.

    NIGHT and DAY/MORNING are fully implemented by LifecycleEngine
    (engines/lifecycle_engine.py). EVENING ("Übergang zu NIGHT") has no
    documented trigger condition in §5.6 and is therefore not produced by
    the engine yet - see the implementation's final report for why this is
    a deliberate placeholder, not an oversight.
    """

    NIGHT = "night"
    MORNING = "morning"
    DAY = "day"
    EVENING = "evening"


@dataclass
class NightDayLifecycleConfig:
    """Night/Day lifecycle configuration for a SmartShading zone.

    schedule_mode controls whether the zone applies one shared schedule every
    day (SAME_EVERY_DAY) or separate schedules for weekdays and weekends
    (WEEKDAY_WEEKEND).  Legacy configs without this field default to SAME_EVERY_DAY.

    Shared fields (night_* / morning_*) are used when schedule_mode is
    SAME_EVERY_DAY and also serve as the primary trigger configuration
    (night_trigger, night_sun_elevation_deg, morning_trigger,
    morning_sun_elevation_deg) in WEEKDAY_WEEKEND mode — only the fixed_time
    and position fields differ between weekday and weekend profiles.
    """

    id: str

    # Schedule mode (v1.0): controls per-day vs weekday/weekend differentiation.
    # Defaults to SAME_EVERY_DAY for backward compatibility.
    schedule_mode: LifecycleScheduleMode = LifecycleScheduleMode.SAME_EVERY_DAY

    # Night mode — shared
    night_enabled: bool = True
    night_trigger: NightTrigger = NightTrigger.BOTH
    night_sun_elevation_deg: float = -6.0
    night_fixed_time: time | None = None
    night_position: int = 0
    night_tilt: int | None = None

    # Morning mode — shared
    morning_enabled: bool = True
    morning_trigger: MorningTrigger = MorningTrigger.BOTH
    morning_sun_elevation_deg: float = 5.0
    morning_fixed_time: time | None = None
    morning_position: int = 100
    morning_tilt: int | None = None

    # Weekday schedule (used when schedule_mode is WEEKDAY_WEEKEND)
    weekday_night_fixed_time: time | None = None   # default same as night_fixed_time
    weekday_night_position: int = 0
    weekday_morning_fixed_time: time | None = None  # default same as morning_fixed_time
    weekday_morning_position: int = 100

    # Weekend schedule (used when schedule_mode is WEEKDAY_WEEKEND)
    weekend_night_fixed_time: time | None = None   # default: 23:00
    weekend_night_position: int = 0
    weekend_morning_fixed_time: time | None = None  # default: 08:30
    weekend_morning_position: int = 100

    # Legacy field — retained for storage round-trip compatibility.
    weekday_enabled: bool = False
    weekend_morning_delay_min: int = 60
