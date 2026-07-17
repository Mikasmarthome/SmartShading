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
    """ARCHITECTURE.md §3.4.

    Unchanged since before v1.2.0-beta.1 — still exactly these 4 values.
    See NightDayLifecycleConfig.night_sun_event for how a SUN_EVENT resolves
    into the SAME clock-time comparison FIXED_TIME/BOTH already use, without
    needing a 5th trigger value here.
    """

    DISABLED = "disabled"
    SUN_ELEVATION = "sun_elevation"
    FIXED_TIME = "fixed_time"
    BOTH = "both"


class MorningTrigger(Enum):
    """ARCHITECTURE.md §3.4. Unchanged since before v1.2.0-beta.1 — see NightTrigger."""

    DISABLED = "disabled"
    SUN_ELEVATION = "sun_elevation"
    FIXED_TIME = "fixed_time"
    BOTH = "both"


class SunEvent(Enum):
    """Selectable astronomical event for NightDayLifecycleConfig.night_sun_event /
    morning_sun_event (v1.2.0-beta.1).

    Resolved from HA's sun.sun entity attributes (next_rising, next_setting,
    next_dawn, next_dusk) — HA's own sun integration already computes these
    (backed by the astral library internally), so no extra dependency is
    needed. HA defines dawn/dusk as civil twilight (sun at -6° elevation),
    the same depression angle this integration already uses as its default
    night_sun_elevation_deg for the pre-existing SUN_ELEVATION trigger.
    """

    SUNRISE = "sunrise"
    SUNSET = "sunset"
    DAWN = "dawn"
    DUSK = "dusk"


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

    active_months restricts the whole schedule to a subset of calendar
    months (1-12). None (the default) means unrestricted — every month —
    which is byte-for-byte the pre-v1.2.0-beta.1 behavior. When set, the
    night/morning triggers are only evaluated during the listed months;
    outside of them the schedule behaves as if both night_enabled and
    morning_enabled were False for that cycle (see LifecycleEngine).

    night_sun_event / morning_sun_event (v1.2.0-beta.1) are an OVERRIDE on
    top of night_fixed_time / morning_fixed_time, following the same
    Optional-override pattern already used elsewhere in this codebase
    (e.g. per-window night_position, absence_position): None (default)
    means "use night_fixed_time/morning_fixed_time as entered" — the exact
    pre-beta behavior, unconditionally. Set to a SunEvent, it resolves that
    astronomical event into the SAME night_fixed_time/morning_fixed_time
    slot every trigger type already compares against — so it applies
    equally to FIXED_TIME (the astronomical event simply IS the clock time)
    and to BOTH (elevation OR the resolved event, no separate trigger value
    needed to express that combination). Irrelevant when night_trigger/
    morning_trigger is DISABLED or SUN_ELEVATION (no time comparison happens
    there), but harmless if set anyway.

    night_not_before / night_not_after / morning_not_before / morning_not_after
    (v1.2.0-beta.1, T3) clamp the FINAL resolved trigger time — whichever of
    night_fixed_time or a resolved night_sun_event produced it — to an
    optional [earliest, latest] window. None (default, both fields) means no
    restriction: byte-for-byte pre-T3 behavior. Only *_not_before set clamps
    an earlier time up to it; only *_not_after set clamps a later time down
    to it; both set clamps to the [not_before, not_after] window, including
    the not_before == not_after edge case (a fixed effective trigger time).
    Applied by engines.lifecycle_engine.clamp_time() as the last step inside
    _active_profile(), after sun-event resolution — so _evaluate_trigger()
    never needs to know a clamp happened, exactly like the sun-event override
    above needs no dedicated branch there. A not_before > not_after window is
    an invalid configuration: the OptionsFlow rejects and never stores it, but
    if raw storage is ever corrupted to hold one anyway, clamp_time() treats
    it as "no clamp" rather than guessing an interpretation — see its
    docstring for the full contract.
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
    # Sun event override (v1.2.0-beta.1): None = use night_fixed_time as-is
    # (pre-beta behavior). Set = resolve this event into night_fixed_time's
    # slot instead. Shared (not weekday/weekend-specific) — an astronomical
    # event is a physical property of the sky, not a social schedule, same
    # reasoning already applied to night_sun_elevation_deg above.
    night_sun_event: SunEvent | None = None
    # Schedule clamp (v1.2.0-beta.1, T3): optional earliest/latest bounds on
    # the final resolved night trigger time. None = no restriction (default).
    night_not_before: time | None = None
    night_not_after: time | None = None

    # Morning mode — shared
    morning_enabled: bool = True
    morning_trigger: MorningTrigger = MorningTrigger.BOTH
    morning_sun_elevation_deg: float = 5.0
    morning_fixed_time: time | None = None
    morning_position: int = 100
    morning_tilt: int | None = None
    # Sun event override (v1.2.0-beta.1): None = use morning_fixed_time as-is.
    morning_sun_event: SunEvent | None = None
    # Schedule clamp (v1.2.0-beta.1, T3): optional earliest/latest bounds on
    # the final resolved morning trigger time. None = no restriction (default).
    morning_not_before: time | None = None
    morning_not_after: time | None = None

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

    # Active months (v1.2.0-beta.1): restricts the schedule to a subset of
    # calendar months. None = unrestricted (all months), matching prior behavior.
    active_months: list[int] | None = None
