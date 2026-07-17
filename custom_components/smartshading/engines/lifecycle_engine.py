"""Night/Day Lifecycle Engine. See ARCHITECTURE.md §5.6.

No Home Assistant dependency - takes the current time and sun elevation
as plain inputs (already read by the coordinator every cycle for
exposure purposes), exactly like SunEngine/ExposureEngine.

Also hosts PresenceDebouncer (pure absence-delay logic).

Schedule mode
-------------
When NightDayLifecycleConfig.schedule_mode is WEEKDAY_WEEKEND the engine
selects between weekday and weekend fixed-time and position fields based on
the local weekday of `now`.  Monday–Friday (weekday() 0–4) use the weekday
profile; Saturday–Sunday (weekday() 5–6) use the weekend profile.

The active profile affects:
  - night fixed_time threshold  (weekday vs weekend)
  - morning fixed_time threshold
  - night_position / morning_position used in coordinator shading target

Sun-elevation thresholds are shared across both profiles because elevation
is a physical property of the sky, not a social schedule.

Active months
-------------
NightDayLifecycleConfig.active_months (v1.2.0-beta.1), when set, restricts
night/morning trigger evaluation to the listed calendar months (1-12).
None (default) is unrestricted — every month, unchanged from prior versions.
Outside the active months the schedule behaves as if night_enabled and
morning_enabled were both False for that cycle: no new NIGHT is entered,
and a NIGHT state carried over from the last active month releases to DAY
on the next cycle instead of waiting for a morning trigger that may never
fire while the schedule is inactive.

The evaluation always uses the LOCAL date embedded in `now`.  The coordinator
passes a HA-localised datetime for this reason.  Do NOT convert to UTC here.

Night carryover after restart
-----------------------------
When HA restarts between midnight and the morning trigger time, the previous
lifecycle state context is lost. get_lifecycle_state() is called with
previous=DAY (the default). For a FIXED_TIME night trigger of, say, 21:00,
the check `now.time() >= 21:00` fails at 00:15 (00:15 < 21:00), so the
engine would incorrectly return DAY instead of NIGHT.

_is_night_carryover() detects this "after midnight, before morning" window
by checking that:
  - we are in the AM period (before noon, ruling out afternoon restarts)
  - the morning trigger has not yet fired
  - for FIXED_TIME triggers: sun elevation is below the horizon (< 0°)
  - for SUN_ELEVATION triggers: elevation is still below the night threshold
  - for BOTH triggers: either elevation condition is met

This is used inside get_lifecycle_state() when previous=DAY to bootstrap
the correct NIGHT state after a restart.
"""
from __future__ import annotations

from datetime import datetime, time
from typing import NamedTuple

from ..models.lifecycle import (
    LifecycleScheduleMode,
    LifecycleState,
    MorningTrigger,
    NightDayLifecycleConfig,
    NightTrigger,
)

# Fallback times used when a fixed_time field is None but the trigger expects one.
_DEFAULT_NIGHT_TIME = time(22, 0)
_DEFAULT_WEEKEND_NIGHT_TIME = time(23, 0)
_DEFAULT_MORNING_TIME = time(6, 30)
_DEFAULT_WEEKEND_MORNING_TIME = time(8, 30)

# Sun elevation below which we treat night as carrying over after a restart.
# Using 0° (horizon) rather than the configured threshold: more conservative
# and universal across trigger types.
_CARRYOVER_SUN_ELEVATION_DEG: float = 0.0


def _time_threshold_met(now: datetime, fixed_time: time | None) -> bool:
    """True once the local wall-clock time has reached `fixed_time`.
    `fixed_time` unset (None) -> never met, never raises."""
    if fixed_time is None:
        return False
    return now.time() >= fixed_time


class _ScheduleProfile(NamedTuple):
    """Active fixed-time and position values for the current cycle."""

    night_fixed_time: time | None
    night_position: int
    morning_fixed_time: time | None
    morning_position: int


def _is_weekend(now: datetime) -> bool:
    """Return True for Saturday (5) and Sunday (6) in local time."""
    return now.weekday() >= 5


def _is_month_active(now: datetime, config: NightDayLifecycleConfig) -> bool:
    """True when the schedule is active in `now`'s calendar month.

    config.active_months is None -> unrestricted, always True (default,
    matches pre-v1.2.0-beta.1 behavior for every existing config).
    """
    if config.active_months is None:
        return True
    return now.month in config.active_months


def _active_profile(now: datetime, config: NightDayLifecycleConfig) -> _ScheduleProfile:
    """Return the night/morning times and positions active for this cycle.

    SAME_EVERY_DAY: use the shared fields every day.
    WEEKDAY_WEEKEND: select weekday or weekend fields based on local day of week.

    The fixed_time fields for weekday/weekend fall back to the shared
    night_fixed_time / morning_fixed_time when not explicitly set (None).
    This ensures a sensible default for zones upgraded from SAME_EVERY_DAY
    without explicitly configuring both weekday and weekend values.
    """
    if config.schedule_mode is LifecycleScheduleMode.WEEKDAY_WEEKEND and _is_weekend(now):
        return _ScheduleProfile(
            night_fixed_time=(
                config.weekend_night_fixed_time
                if config.weekend_night_fixed_time is not None
                else config.night_fixed_time
            ),
            night_position=config.weekend_night_position,
            morning_fixed_time=(
                config.weekend_morning_fixed_time
                if config.weekend_morning_fixed_time is not None
                else config.morning_fixed_time
            ),
            morning_position=config.weekend_morning_position,
        )
    if config.schedule_mode is LifecycleScheduleMode.WEEKDAY_WEEKEND:
        # Weekday profile
        return _ScheduleProfile(
            night_fixed_time=(
                config.weekday_night_fixed_time
                if config.weekday_night_fixed_time is not None
                else config.night_fixed_time
            ),
            night_position=config.weekday_night_position,
            morning_fixed_time=(
                config.weekday_morning_fixed_time
                if config.weekday_morning_fixed_time is not None
                else config.morning_fixed_time
            ),
            morning_position=config.weekday_morning_position,
        )
    # SAME_EVERY_DAY
    return _ScheduleProfile(
        night_fixed_time=config.night_fixed_time,
        night_position=config.night_position,
        morning_fixed_time=config.morning_fixed_time,
        morning_position=config.morning_position,
    )


def _is_night_carryover(
    now: datetime,
    sun_elevation_deg: float | None,
    config: NightDayLifecycleConfig,
    profile: _ScheduleProfile,
) -> bool:
    """True when we appear to be in the continuation of a night that started on the previous day.

    This addresses the HA-restart bootstrap problem: when the coordinator
    starts fresh (previous_lifecycle_state=DAY) after midnight but before
    the morning trigger time, the standard FIXED_TIME check `now.time() >=
    night_fixed_time` fails (e.g. 00:15 < 21:00) and night is never detected.

    Conditions for a carryover:
    - We are in the AM period (before 12:00 noon) — rules out afternoon/evening.
    - The morning trigger has not yet fired.
    - Night is configured (trigger not DISABLED).
    - For SUN_ELEVATION trigger: elevation is still at or below the night threshold.
      None elevation → cannot confirm → returns False (preserve existing state).
    - For FIXED_TIME trigger: evaluated on time alone; no elevation needed when
      None. When elevation is available it is used as a cross-check (< 0°).
    - For BOTH: elevation condition requires data; FIXED_TIME part still valid.
    """
    if now.time() >= time(12, 0):
        return False  # PM — definitely not a carryover from last night

    if _time_threshold_met(now, profile.morning_fixed_time):
        return False  # Morning has already fired this day

    if config.night_trigger is NightTrigger.DISABLED:
        return False

    if config.night_trigger is NightTrigger.SUN_ELEVATION:
        # Cannot confirm SUN_ELEVATION carryover without elevation data.
        if sun_elevation_deg is None:
            return False
        return sun_elevation_deg <= config.night_sun_elevation_deg

    if config.night_trigger is NightTrigger.BOTH:
        # Elevation part requires data; FIXED_TIME part is handled below.
        if sun_elevation_deg is not None and sun_elevation_deg <= config.night_sun_elevation_deg:
            return True

    # FIXED_TIME (or BOTH without elevation match): FIXED_TIME is evaluated on
    # time alone — no elevation cross-check is required when data is absent.
    # When elevation is available, require sun below the horizon (< 0°) as a
    # cross-check to prevent false positives in unusual config combinations.
    if profile.night_fixed_time is None:
        return False
    if sun_elevation_deg is None:
        return True  # FIXED_TIME: before noon, morning not fired → carryover confirmed
    return sun_elevation_deg < _CARRYOVER_SUN_ELEVATION_DEG


def check_night_interval_active(
    now: datetime,
    sun_elevation_deg: float | None,
    config: NightDayLifecycleConfig,
) -> bool:
    """Return True when the configured night interval is currently active.

    Computed independently of any cached lifecycle_state so it cannot be
    defeated by stale post-restart state or window-behavior-mode overrides
    (e.g. ABSENCE_ONLY forces lifecycle_state=DAY in the evaluator WDI).

    Uses the same trigger and carryover logic as LifecycleEngine without
    relying on a cached previous state: previous=DAY triggers the carryover
    path for the after-midnight, before-morning window.

    When sun_elevation_deg is None, absent-evidence semantics apply:
    - FIXED_TIME triggers evaluate on time alone (no elevation needed).
    - SUN_ELEVATION-only triggers return False — no new elevation-based state
      is created from absent data.
    - BOTH triggers use only the FIXED_TIME part when elevation is absent.
    None is passed through; the trigger functions handle it per type.
    """
    if not config.night_enabled or not _is_month_active(now, config):
        return False
    profile = _active_profile(now, config)
    if LifecycleEngine._check_night_trigger(now, sun_elevation_deg, config, profile):
        return True
    return _is_night_carryover(now, sun_elevation_deg, config, profile)


class LifecycleEngine:
    """ARCHITECTURE.md §5.6. Pure, single-call evaluation - no internal
    clock, no background timers. The caller (Coordinator) is responsible
    for passing the previous LifecycleState so MORNING can be reported as
    the one-cycle transition event it is documented to be (ARCHITECTURE.md
    §4.1: "kein echter Zustand, aber Tracking"), rather than a state with
    duration.

    The engine exposes active_profile() so the coordinator can read the
    current night_position / morning_position without re-deriving the schedule.
    """

    def get_lifecycle_state(
        self,
        now: datetime,
        sun_elevation_deg: float | None,
        config: NightDayLifecycleConfig,
        previous_lifecycle_state: LifecycleState = LifecycleState.DAY,
    ) -> LifecycleState:
        profile = _active_profile(now, config)
        month_active = _is_month_active(now, config)
        is_night = (
            config.night_enabled
            and month_active
            and self._check_night_trigger(now, sun_elevation_deg, config, profile)
        )
        if is_night:
            return LifecycleState.NIGHT

        # Bootstrap carryover: when starting fresh (previous=DAY) after a restart
        # during the night continuation period (after midnight, before morning),
        # the FIXED_TIME trigger doesn't re-fire (00:15 < 21:00). Detect the
        # carryover so night is correctly maintained after a restart.
        if (
            config.night_enabled
            and month_active
            and previous_lifecycle_state is LifecycleState.DAY
            and _is_night_carryover(now, sun_elevation_deg, config, profile)
        ):
            return LifecycleState.NIGHT

        if previous_lifecycle_state is LifecycleState.NIGHT:
            # When morning trigger is disabled, or the schedule just rolled
            # out of its active months, skip the MORNING transition event and
            # go directly to DAY so we don't get stuck in NIGHT indefinitely.
            if (
                not config.morning_enabled
                or config.morning_trigger is MorningTrigger.DISABLED
                or not month_active
            ):
                return LifecycleState.DAY
            is_morning_threshold_met = self._check_morning_trigger(
                now, sun_elevation_deg, config, profile
            )
            return LifecycleState.MORNING if is_morning_threshold_met else LifecycleState.NIGHT

        return LifecycleState.DAY

    def active_profile(self, now: datetime, config: NightDayLifecycleConfig) -> _ScheduleProfile:
        """Return the schedule profile active for the current local datetime."""
        return _active_profile(now, config)

    @staticmethod
    def _check_night_trigger(
        now: datetime,
        sun_elevation_deg: float | None,
        config: NightDayLifecycleConfig,
        profile: _ScheduleProfile,
    ) -> bool:
        elevation_met = (
            False if sun_elevation_deg is None
            else sun_elevation_deg <= config.night_sun_elevation_deg
        )
        time_met = _time_threshold_met(now, profile.night_fixed_time)
        return _evaluate_trigger(config.night_trigger, elevation_met, time_met)

    @staticmethod
    def _check_morning_trigger(
        now: datetime,
        sun_elevation_deg: float | None,
        config: NightDayLifecycleConfig,
        profile: _ScheduleProfile,
    ) -> bool:
        elevation_met = (
            False if sun_elevation_deg is None
            else sun_elevation_deg >= config.morning_sun_elevation_deg
        )
        time_met = _time_threshold_met(now, profile.morning_fixed_time)
        return _evaluate_trigger(config.morning_trigger, elevation_met, time_met)


def _evaluate_trigger(trigger: NightTrigger | MorningTrigger, elevation_met: bool, time_met: bool) -> bool:
    """ARCHITECTURE.md §5.6 trigger semantics:
    DISABLED -> never fires, SUN_ELEVATION -> elevation only,
    FIXED_TIME -> time only, BOTH -> either condition (OR)."""
    if trigger is NightTrigger.DISABLED or trigger is MorningTrigger.DISABLED:
        return False
    if trigger is NightTrigger.SUN_ELEVATION or trigger is MorningTrigger.SUN_ELEVATION:
        return elevation_met
    if trigger is NightTrigger.FIXED_TIME or trigger is MorningTrigger.FIXED_TIME:
        return time_met
    return elevation_met or time_met  # BOTH


class PresenceDebouncer:
    """Aufgabe 3: pure absence-delay logic. The Coordinator reads raw
    presence from `person.*` entities (Home-Assistant-dependent, lives in
    coordinator.py) and feeds the resulting boolean in here each cycle -
    this class itself never touches Home Assistant.

    One instance per integration (presence is house-wide in this version,
    not per-window) - holds the single timestamp needed to debounce
    `absence_delay_min`.
    """

    def __init__(self) -> None:
        self._absent_since: datetime | None = None

    def is_absence_active(self, present: bool, now: datetime, absence_delay_min: int) -> bool:
        if present:
            self._absent_since = None
            return False
        if self._absent_since is None:
            self._absent_since = now
        elapsed_min = (now - self._absent_since).total_seconds() / 60.0
        return elapsed_min >= absence_delay_min
