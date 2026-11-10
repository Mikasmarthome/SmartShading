"""Config Flow and Options Flow for SmartShading (ARCHITECTURE.md §7).

Architecture: one config entry per zone.  Each "Eintrag hinzufügen" click
creates a new SmartShading entry for one zone.  Multiple entries are allowed
and appear as separate items under "SmartShading" in the HA integrations UI.

Config Flow steps (per zone setup):
  1. async_step_user              zone name, HA location flag
  2. async_step_weather           optional weather entity + dedicated sensors
  3. async_step_comfort           heat/glare/solar-gain toggles + indoor temp sensor
  4. async_step_lifecycle         night/morning trigger type selection
  4b. async_step_lifecycle_night / _morning (or _weekday / _weekend of each), and a
      custom-elevation page only when "Custom" was chosen
  5. async_step_presence          presence entities + absence_delay_min
  6. async_step_window            window name, floor level, azimuth
  7. async_step_cover_group       assign covers to a new CoverGroup for the window
  8. async_step_add_another_window loop or finish
     Shade-position defaults (40/25/10 %) are applied automatically on finish.
  Entry title = zone name.  No single-instance guard.

Options Flow (post-setup editing via a section menu):
  weather               weather entity + sensor entity IDs
  lifecycle             night/morning trigger type selection
  lifecycle_night ...   the same pages as in setup, written once after the last page
  lifecycle_advanced    (under Advanced) sun events, time limits, active months
  presence              presence entities + absence delay
  comfort               heat/glare/solar-gain toggles
  behavior              shade-position defaults
  add_window            add a window to this zone entry (structural change)
  add_window_cover_group  assign covers for the new window
  add_window_loop       add another window or finish
"""
from __future__ import annotations

import uuid
from datetime import time
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TimeSelector,
)

from .config_entry_data import (
    SmartShadingConfigEntryData,
    _override_policy_from_storage,
    _resolve_lifecycle_config_dict,
    from_storage_dict,
    to_storage_dict,
)
from .models.comfort import ComfortConfig
from .models.config import GlobalDefaults, ShadePositionDefaults
from .models.manual_override import (
    DECISION_FILTER_ANY,
    DECISION_FILTER_COMFORT,
    DECISION_FILTER_PROTECTION,
    RELEASE_CHOICE_DURATION,
    RELEASE_CHOICE_FIXED_TIME,
    RELEASE_CHOICE_LIFECYCLE,
    RELEASE_CHOICE_MANUAL,
    RELEASE_CHOICE_NEXT_DECISION,
    RELEASE_CHOICES,
    OverrideReleaseStrategy,
    release_choice_to_strategy,
    release_strategy_to_choice,
)
from .models.cover_group import CoverGroup, CoverHardwareType, CoverSyncMode, cover_hardware_type_from_str
from .models.presence import PresencePolicy
from .models.lifecycle import (
    LifecycleScheduleMode,
    MorningTrigger,
    NightDayLifecycleConfig,
    NightTrigger,
    SunEvent,
)
from .models.window import WindowBehaviorMode, WindowConfig
from .models.zone import ZoneConfig
from .cover_ownership_runtime import current_claims as _ownership_claims
from .engines.cover_ownership import (
    covers_claimed_by_others as _covers_claimed_by_others,
    normalize_cover_id as _normalize_cover_id,
)
from .const import (
    COMPASS_AZIMUTHS,
    CONF_ABSENCE_DELAY_MIN,
    CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT,
    CONF_ENTRY_TYPE,
    ENTRY_TYPE_SYSTEM,
    ENTRY_TYPE_ZONE,
    CONF_ABSENCE_POSITION,
    CONF_ADD_ANOTHER_WINDOW,
    CONF_CLOUD_COVER_SENSOR_ID,
    CONF_COMPASS_DIRECTION,
    CONF_COVER_ENTITIES,
    CONF_COVER_HARDWARE_TYPE,
    CONF_CUSTOM_AZIMUTH,
    CONF_FLOOR_LEVEL,
    CONF_GLARE_PROTECTION_ENABLED,
    CONF_HEAT_PROTECTION_ENABLED,
    CONF_INDOOR_TEMPERATURE_SENSOR_ID,
    CONF_INDOOR_TEMPERATURE_SENSOR_IDS,
    CONF_LIGHT_SHADE_POSITION,
    CONF_MORNING_ELEVATION_PRESET,
    DEFAULT_LIGHT_SHADE_POSITION,
    DEFAULT_NORMAL_SHADE_POSITION,
    DEFAULT_STRONG_SHADE_POSITION,
    CONF_MORNING_FIXED_TIME,
    CONF_MORNING_POSITION,
    CONF_MORNING_SUN_ELEVATION,
    CONF_MORNING_TRIGGER,
    CONF_NIGHT_ELEVATION_PRESET,
    CONF_NIGHT_FIXED_TIME,
    CONF_NIGHT_POSITION,
    CONF_NIGHT_SUN_ELEVATION,
    CONF_NIGHT_TRIGGER,
    CONF_NORMAL_SHADE_POSITION,
    CONF_OUTDOOR_TEMPERATURE_SENSOR_ID,
    CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS,
    CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS,
    CONF_OVERRIDE_SAFETY_TIMEOUT_ENABLED,
    CONF_OVERRIDE_DETECTION_TOLERANCE,
    CONF_OVERRIDE_DURATION_MIN,
    CONF_OVERRIDE_EDIT_TIMEOUTS,
    CONF_OVERRIDE_RELEASE_CHOICE,
    CONF_OVERRIDE_DECISION_FILTER,
    CONF_OVERRIDE_FIXED_UNTIL,
    CONF_OVERRIDE_NIGHT_DURATION_MIN,
    CONF_OVERRIDE_USE_SYSTEM_DEFAULT,
    DEFAULT_OVERRIDE_DETECTION_TOLERANCE,
    DEFAULT_OVERRIDE_DURATION_MIN,
    DEFAULT_OVERRIDE_NIGHT_DURATION_MIN,
    OVERRIDE_DETECTION_TOLERANCE_MAX,
    OVERRIDE_DURATION_MIN_MAX,
    CONF_PRESENCE_ENTITY_IDS,
    CONF_SOLAR_GAIN_ENABLED,
    CONF_SOLAR_RADIATION_SENSOR_ID,
    CONF_STRONG_SHADE_POSITION,
    CONF_USE_HOME_LOCATION,
    CONF_WEATHER_ENTITY_ID,
    CONF_WIND_SPEED_SENSOR_ID,
    CONF_RAIN_SENSOR_ID,
    DEFAULT_RAIN_RELEASE_DELAY_MIN,
    CONF_REMOVE_CONFIRMED,
    CONF_WINDOW_ID,
    CONF_WINDOW_NAME,
    CONF_ZONE_NAME,
    CUSTOM_AZIMUTH_OPTION,
    DEFAULT_ABSENCE_DELAY_MIN,
    DEFAULT_ABSENCE_POSITION,
    DEFAULT_HEAT_PROTECTION_INDOOR_TEMP_C,
    DEFAULT_HEAT_PROTECTION_OUTDOOR_TEMP_C,
    DEFAULT_MORNING_FIXED_TIME,
    DEFAULT_MORNING_POSITION,
    DEFAULT_MORNING_SUN_ELEVATION,
    DEFAULT_MORNING_TRIGGER,
    DEFAULT_NIGHT_FIXED_TIME,
    DEFAULT_NIGHT_POSITION,
    DEFAULT_NIGHT_SUN_ELEVATION,
    DEFAULT_NIGHT_TRIGGER,
    CONF_NIGHT_SUN_EVENT,
    CONF_MORNING_SUN_EVENT,
    SUN_EVENT_OPTIONS,
    CONF_NIGHT_NOT_BEFORE,
    CONF_NIGHT_NOT_AFTER,
    CONF_MORNING_NOT_BEFORE,
    CONF_MORNING_NOT_AFTER,
    CONF_PRESENCE_POLICY,
    DEFAULT_PRESENCE_POLICY,
    PRESENCE_POLICY_OPTIONS,
    DEFAULT_SOLAR_GAIN_MAX_OUTDOOR_TEMP_C,
    DEFAULT_WEEKDAY_MORNING_FIXED_TIME,
    DEFAULT_WEEKDAY_NIGHT_FIXED_TIME,
    DEFAULT_WEEKEND_MORNING_FIXED_TIME,
    DEFAULT_WEEKEND_NIGHT_FIXED_TIME,
    CONF_SCHEDULE_MODE,
    CONF_ACTIVE_MONTHS,
    CONF_WEEKDAY_NIGHT_FIXED_TIME,
    CONF_WEEKDAY_NIGHT_POSITION,
    CONF_WEEKDAY_MORNING_FIXED_TIME,
    CONF_WEEKDAY_MORNING_POSITION,
    CONF_WEEKEND_NIGHT_FIXED_TIME,
    CONF_WEEKEND_NIGHT_POSITION,
    CONF_WEEKEND_MORNING_FIXED_TIME,
    CONF_WEEKEND_MORNING_POSITION,
    CONF_WINDOW_BEHAVIOR_MODE,
    CONF_CONTACT_SENSOR_ENTITY_ID,
    CONF_CONTACT_SENSOR_ENTITY_IDS,
    CONF_NIGHT_BLOCK_ON_WINDOW_OPEN,
    CONF_NIGHT_LIFT_ON_WINDOW_OPEN,
    CONF_WINDOW_OPEN_NIGHT_POSITION,
    DEFAULT_WINDOW_OPEN_NIGHT_POSITION_HA,
    CONF_MANUAL_SUN_SECTOR_ENABLED,
    CONF_MANUAL_SUN_SECTOR_START_DEG,
    CONF_MANUAL_SUN_SECTOR_END_DEG,
    CONF_OBSTRUCTION_1_ENABLED,
    CONF_OBSTRUCTION_1_AZIMUTH_START,
    CONF_OBSTRUCTION_1_AZIMUTH_END,
    CONF_OBSTRUCTION_1_BLOCK_FROM_ELEVATION,
    CONF_OBSTRUCTION_1_BLOCK_UNTIL_ELEVATION,
    CONF_OBSTRUCTION_2_ENABLED,
    CONF_OBSTRUCTION_2_AZIMUTH_START,
    CONF_OBSTRUCTION_2_AZIMUTH_END,
    CONF_OBSTRUCTION_2_BLOCK_FROM_ELEVATION,
    CONF_OBSTRUCTION_2_BLOCK_UNTIL_ELEVATION,
    CONF_OBSTRUCTION_3_ENABLED,
    CONF_OBSTRUCTION_3_AZIMUTH_START,
    CONF_OBSTRUCTION_3_AZIMUTH_END,
    CONF_OBSTRUCTION_3_BLOCK_FROM_ELEVATION,
    CONF_OBSTRUCTION_3_BLOCK_UNTIL_ELEVATION,
    DOMAIN,
    ELEVATION_PRESET_CUSTOM,
    LIFECYCLE_SCHEDULE_MODE_OPTIONS,
    LIFECYCLE_TRIGGER_OPTIONS,
    MORNING_ELEVATION_PRESETS,
    NIGHT_ELEVATION_PRESETS,
    WINDOW_BEHAVIOR_MODE_OPTIONS,
)

_COMPASS_OPTIONS: list[str] = [*COMPASS_AZIMUTHS.keys(), CUSTOM_AZIMUTH_OPTION]


def _first_zone_entry_data(hass: Any) -> dict[str, Any] | None:
    """Return ConfigEntry.data of the first existing zone entry, or None.

    Used to prefill new-zone Config Flow steps so users don't re-enter
    shared sensors (weather, indoor temperature, presence) for every zone.
    Only zone entries are considered; the system entry is excluded.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    for entry in entries:
        if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_SYSTEM:
            return entry.data
    return None


def _safe_positive_int(value: Any, default: int, *, maximum: int) -> int:
    """Never raises: missing/None/wrong-type/non-numeric -> default. A
    numeric value outside [1, maximum] (including zero or negative) is
    clamped into range rather than stored as-is or rejected — matches the
    "safe default, never crash" discipline used throughout this module and
    config_entry_data.py."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


# ---------------------------------------------------------------------------
# Manual Override policy form (Phase 5).  The System entry's default and a
# zone's own policy share the same pages:
#
#   page 1  release choice (5 options) + allow comfort / protection actions
#           + "also adjust the safety timeout"
#   page 2  only the one detail page the choice needs:
#             after a duration            -> daytime / night duration
#             at a fixed clock time       -> the clock time
#             next matching decision      -> which decisions release it
#             schedule transition, manual -> no detail page
#   page 3  optional safety-timeout page (timeout switch + the two durations),
#           only when asked for on page 1 and only for the choices it affects
#
# Every value that a page does not show stays exactly as stored (partial merge).
# The five choices map onto the unchanged seven stored release strategies, see
# models/manual_override.release_choice_to_strategy().
# ---------------------------------------------------------------------------

# Keys of the pre-T10 policy format; superseded by release_strategy and inert
# once it is stored, so a save writes the current format only.
_SUPERSEDED_OVERRIDE_KEYS = frozenset({"duration_mode", "break_on_lifecycle"})

# Complete stored shape: a save fills in what an older / partial entry lacks,
# never replaces what is there.
_OVERRIDE_POLICY_DEFAULTS: dict[str, Any] = {
    "release_strategy": OverrideReleaseStrategy.LIFECYCLE.value,
    "fixed_until": None,
    "allow_comfort_actions": False,
    "allow_protection_actions": False,
    "duration_min": DEFAULT_OVERRIDE_DURATION_MIN,
    "night_duration_min": DEFAULT_OVERRIDE_NIGHT_DURATION_MIN,
    "safety_timeout_enabled": True,
}

# Choices for which the optional safety-timeout page is meaningful: the other
# two end by their own deadline and never use the safety timeout.
_OVERRIDE_TIMEOUT_CHOICES = frozenset({
    RELEASE_CHOICE_LIFECYCLE, RELEASE_CHOICE_NEXT_DECISION, RELEASE_CHOICE_MANUAL,
})

_OVERRIDE_DETAIL_STEP = {
    RELEASE_CHOICE_DURATION: "override_duration",
    RELEASE_CHOICE_FIXED_TIME: "override_fixed_time",
    RELEASE_CHOICE_NEXT_DECISION: "override_decision",
}


def _override_minutes_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=1, max=OVERRIDE_DURATION_MIN_MAX, step=1,
            mode=NumberSelectorMode.BOX, unit_of_measurement="min",
        )
    )


def _override_minutes_default(values: dict[str, Any], key: str, default: int) -> int:
    return _safe_positive_int(values.get(key), default, maximum=OVERRIDE_DURATION_MIN_MAX)


def _override_choice_schema(values: dict[str, Any]) -> vol.Schema:
    choice, _ = release_strategy_to_choice(_override_policy_from_storage(values).release_strategy)
    return vol.Schema(
        {
            vol.Required(CONF_OVERRIDE_RELEASE_CHOICE, default=choice): SelectSelector(
                SelectSelectorConfig(
                    options=list(RELEASE_CHOICES),
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="override_release_choice",
                )
            ),
            vol.Required(
                CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS,
                default=bool(values.get("allow_comfort_actions", False)),
            ): BooleanSelector(),
            vol.Required(
                CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS,
                default=bool(values.get("allow_protection_actions", False)),
            ): BooleanSelector(),
            vol.Required(CONF_OVERRIDE_EDIT_TIMEOUTS, default=False): BooleanSelector(),
        }
    )


def _override_duration_schema(values: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_OVERRIDE_DURATION_MIN,
                default=_override_minutes_default(values, "duration_min", DEFAULT_OVERRIDE_DURATION_MIN),
            ): _override_minutes_selector(),
            vol.Required(
                CONF_OVERRIDE_NIGHT_DURATION_MIN,
                default=_override_minutes_default(
                    values, "night_duration_min", DEFAULT_OVERRIDE_NIGHT_DURATION_MIN),
            ): _override_minutes_selector(),
        }
    )


def _override_fixed_time_schema(suggested: Any) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_OVERRIDE_FIXED_UNTIL, description={"suggested_value": suggested},
            ): TimeSelector(),
        }
    )


def _override_decision_schema(values: dict[str, Any]) -> vol.Schema:
    _, decision_filter = release_strategy_to_choice(_override_policy_from_storage(values).release_strategy)
    return vol.Schema(
        {
            vol.Required(
                CONF_OVERRIDE_DECISION_FILTER, default=decision_filter or DECISION_FILTER_ANY,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[DECISION_FILTER_ANY, DECISION_FILTER_COMFORT, DECISION_FILTER_PROTECTION],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="override_decision_filter",
                )
            ),
        }
    )


def _override_timeout_schema(values: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_OVERRIDE_SAFETY_TIMEOUT_ENABLED,
                default=bool(values.get("safety_timeout_enabled", True)),
            ): BooleanSelector(),
        }
        | _override_duration_schema(values).schema
    )


def _parse_optional_time_input(value: Any) -> time | None:
    """TimeSelector submits a "HH:MM:SS" string, or is simply absent from
    user_input when left empty (it's an Optional field). Used for the T3
    schedule-clamp not_before/not_after fields, where "unset" is a valid,
    meaningful value (no restriction) - unlike _parse_time_input's Required
    fields, there is no fallback default to fall back to. Never raises."""
    if not isinstance(value, str):
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


def _optional_time_to_storage(value: time | None) -> str | None:
    """Inverse of _parse_optional_time_input: None stays None (no clamp
    bound), a time serializes to its ISO string for JSON storage."""
    return value.isoformat() if value is not None else None



def _parse_time_input(value: Any, fallback: str) -> time:
    """TimeSelector submits a "HH:MM:SS" string. Never raises - an
    unparsable value falls back to the field's own documented default,
    never to an exception."""
    text = value if isinstance(value, str) else fallback
    try:
        return time.fromisoformat(text)
    except ValueError:
        return time.fromisoformat(fallback)


def _elevation_to_preset(elevation: float, presets: dict[str, float]) -> str:
    """Map a stored elevation float back to its preset key, or 'custom' if it
    doesn't match any preset exactly. Used to pre-populate the SelectSelector
    when the lifecycle step is shown (e.g. after a validation error)."""
    for key, value in presets.items():
        if value == elevation:
            return key
    return ELEVATION_PRESET_CUSTOM


def _resolve_elevation(
    user_input: dict[str, Any],
    preset_key: str,
    custom_key: str,
    presets: dict[str, float],
    fallback: float,
) -> float:
    """Resolve preset selector + optional custom number → final elevation float.
    When the preset is a named value, the custom number field is ignored.
    When the preset is 'custom', the custom number field is used (falling back
    to `fallback` if the optional field was not submitted)."""
    preset = user_input[preset_key]
    if preset != ELEVATION_PRESET_CUSTOM:
        return presets[preset]
    return float(user_input.get(custom_key, fallback))


def _resolve_azimuth(user_input: dict[str, Any]) -> float | None:
    """Resolve the chosen compass direction (or custom value) to a plain
    azimuth float. Returns None if a custom azimuth was required but not
    supplied/invalid - WindowConfig.azimuth (§3.1) is always a float
    internally, regardless of which UI path was used to get there.
    """
    direction = user_input[CONF_COMPASS_DIRECTION]
    if direction != CUSTOM_AZIMUTH_OPTION:
        return COMPASS_AZIMUTHS[direction]

    custom_azimuth = user_input.get(CONF_CUSTOM_AZIMUTH)
    if custom_azimuth is None:
        return None
    if not (0.0 <= float(custom_azimuth) <= 359.0):
        return None
    return float(custom_azimuth)


def _compass_from_azimuth(azimuth: float) -> tuple[str, float | None]:
    """Convert a stored azimuth float to (compass_direction, custom_azimuth_or_None).

    Used to pre-fill the Edit Window form from the stored value.
    Returns ("custom", azimuth) when the value doesn't match a preset exactly.
    """
    for direction, preset_az in COMPASS_AZIMUTHS.items():
        if abs(azimuth - preset_az) < 0.5:
            return direction, None
    return CUSTOM_AZIMUTH_OPTION, azimuth



def _covers_already_assigned(
    hass, cover_ids, *, own_key=None, extra_taken=(),
) -> bool:
    """True when any selected cover is already assigned to another SmartShading
    window (any entry), assigned earlier in the running flow, or selected twice.
    New duplicates are rejected; existing ones keep loading (see ownership)."""
    taken_here = {_normalize_cover_id(c) for c in extra_taken}
    if any(_normalize_cover_id(c) in taken_here for c in cover_ids):
        return True
    return bool(_covers_claimed_by_others(_ownership_claims(hass), cover_ids, own_key=own_key))


# ---------------------------------------------------------------------------
# Schedule pages (night / morning), shared by the setup and the options flow
# ---------------------------------------------------------------------------

_TIME_TRIGGER_VALUES = frozenset({"fixed_time", "both"})
_SUN_TRIGGER_VALUES = frozenset({"sun_elevation", "both"})

# step id -> (phase, day part). "all" = one schedule for every day.
_SCHEDULE_PAGES: dict[str, tuple[str, str]] = {
    "lifecycle_night": ("night", "all"),
    "lifecycle_night_weekday": ("night", "weekday"),
    "lifecycle_night_weekend": ("night", "weekend"),
    "lifecycle_morning": ("morning", "all"),
    "lifecycle_morning_weekday": ("morning", "weekday"),
    "lifecycle_morning_weekend": ("morning", "weekend"),
}

_PHASE_FIELDS: dict[str, dict[str, Any]] = {
    "night": {
        "time_field": {
            "all": CONF_NIGHT_FIXED_TIME,
            "weekday": CONF_WEEKDAY_NIGHT_FIXED_TIME,
            "weekend": CONF_WEEKEND_NIGHT_FIXED_TIME,
        },
        "time_default": {
            "all": DEFAULT_NIGHT_FIXED_TIME,
            "weekday": DEFAULT_WEEKDAY_NIGHT_FIXED_TIME,
            "weekend": DEFAULT_WEEKEND_NIGHT_FIXED_TIME,
        },
        "position_field": {
            "all": CONF_NIGHT_POSITION,
            "weekday": CONF_WEEKDAY_NIGHT_POSITION,
            "weekend": CONF_WEEKEND_NIGHT_POSITION,
        },
        "position_default": DEFAULT_NIGHT_POSITION,
        "preset_field": CONF_NIGHT_ELEVATION_PRESET,
        "presets": NIGHT_ELEVATION_PRESETS,
        "custom_field": CONF_NIGHT_SUN_ELEVATION,
        "elevation_key": "night_sun_elevation_deg",
        "elevation_default": DEFAULT_NIGHT_SUN_ELEVATION,
        "event_field": CONF_NIGHT_SUN_EVENT,
        "preset_translation_key": "night_elevation_preset",
        "event_translation_key": "night_sun_event",
    },
    "morning": {
        "time_field": {
            "all": CONF_MORNING_FIXED_TIME,
            "weekday": CONF_WEEKDAY_MORNING_FIXED_TIME,
            "weekend": CONF_WEEKEND_MORNING_FIXED_TIME,
        },
        "time_default": {
            "all": DEFAULT_MORNING_FIXED_TIME,
            "weekday": DEFAULT_WEEKDAY_MORNING_FIXED_TIME,
            "weekend": DEFAULT_WEEKEND_MORNING_FIXED_TIME,
        },
        "position_field": {
            "all": CONF_MORNING_POSITION,
            "weekday": CONF_WEEKDAY_MORNING_POSITION,
            "weekend": CONF_WEEKEND_MORNING_POSITION,
        },
        "position_default": DEFAULT_MORNING_POSITION,
        "preset_field": CONF_MORNING_ELEVATION_PRESET,
        "presets": MORNING_ELEVATION_PRESETS,
        "custom_field": CONF_MORNING_SUN_ELEVATION,
        "elevation_key": "morning_sun_elevation_deg",
        "elevation_default": DEFAULT_MORNING_SUN_ELEVATION,
        "event_field": CONF_MORNING_SUN_EVENT,
        "preset_translation_key": "morning_elevation_preset",
        "event_translation_key": "morning_sun_event",
    },
}


def _schedule_pages(
    night: NightTrigger, morning: MorningTrigger, mode: LifecycleScheduleMode
) -> list[str]:
    """Ordered step ids of the schedule pages that apply: a phase whose trigger
    is disabled has no page, and a weekday/weekend schedule has one page per
    day part."""
    pages: list[str] = []
    for phase, trigger in (("night", night), ("morning", morning)):
        if trigger.value == "disabled":
            continue
        if mode is LifecycleScheduleMode.WEEKDAY_WEEKEND:
            pages += [f"lifecycle_{phase}_weekday", f"lifecycle_{phase}_weekend"]
        else:
            pages.append(f"lifecycle_{phase}")
    return pages


def _stored_storage_key(phase: str, day: str, what: str) -> str:
    """Storage key of a page value: 'fixed_time' or 'position' of a day part."""
    prefix = "" if day == "all" else f"{day}_"
    return f"{prefix}{phase}_{what}"


def _assemble_lifecycle_config(
    stored_lc: dict[str, Any],
    pending: dict[str, Any],
    night: NightTrigger,
    morning: MorningTrigger,
    mode: LifecycleScheduleMode,
) -> dict[str, Any]:
    """The lifecycle_config an options save writes after the schedule pages.

    ``pending`` holds only what the pages collected (parsed times, positions,
    elevations); everything else comes from the stored schedule, so a partial
    edit never overwrites an unrelated value. A sun event or a time limit only
    exists next to a time: it is cleared for a phase that has no time any more.
    The advanced values are kept for the phases that still have one - the
    Advanced schedule page is where they are edited.
    """
    new_lc: dict[str, Any] = {
        **stored_lc,
        "night_trigger": night.value,
        "morning_trigger": morning.value,
        "schedule_mode": mode.value,
    }
    if night is NightTrigger.DISABLED and morning is MorningTrigger.DISABLED:
        return new_lc

    def _time(key: str, fallback: str) -> str:
        value = pending.get(key)
        if isinstance(value, time):
            return value.isoformat()
        return _parse_time_input(None, fallback).isoformat()

    for phase in ("night", "morning"):
        fields = _PHASE_FIELDS[phase]
        elevation_key = fields["elevation_key"]
        new_lc[elevation_key] = pending.get(elevation_key, stored_lc.get(elevation_key, fields["elevation_default"]))
    if mode is LifecycleScheduleMode.WEEKDAY_WEEKEND:
        for phase in ("night", "morning"):
            fields = _PHASE_FIELDS[phase]
            weekday_key = f"weekday_{phase}_fixed_time"
            weekend_key = f"weekend_{phase}_fixed_time"
            new_lc[weekday_key] = _time(
                weekday_key,
                stored_lc.get(weekday_key) or stored_lc.get(f"{phase}_fixed_time", fields["time_default"]["weekday"]),
            )
            new_lc[weekend_key] = _time(
                weekend_key, stored_lc.get(weekend_key, fields["time_default"]["weekend"])
            )
            for day in ("weekday", "weekend"):
                position_key = f"{day}_{phase}_position"
                new_lc[position_key] = int(
                    pending.get(position_key, stored_lc.get(position_key, fields["position_default"]))
                )
    else:
        for phase in ("night", "morning"):
            fields = _PHASE_FIELDS[phase]
            time_key = f"{phase}_fixed_time"
            position_key = f"{phase}_position"
            new_lc[time_key] = _time(time_key, stored_lc.get(time_key, fields["time_default"]["all"]))
            new_lc[position_key] = int(
                pending.get(position_key, stored_lc.get(position_key, fields["position_default"]))
            )
    raw_months = stored_lc.get("active_months") or []
    new_lc["active_months"] = sorted(int(m) for m in raw_months) if raw_months else None
    for phase, trigger in (("night", night), ("morning", morning)):
        has_time = trigger.value in _TIME_TRIGGER_VALUES
        new_lc[f"{phase}_sun_event"] = (stored_lc.get(f"{phase}_sun_event") or None) if has_time else None
        for edge in ("not_before", "not_after"):
            key = f"{phase}_{edge}"
            new_lc[key] = (
                _optional_time_to_storage(_parse_optional_time_input(stored_lc.get(key))) if has_time else None
            )
    return new_lc


class _ScheduleWizard:
    """The night / morning schedule pages of both flows.

    The lifecycle page (triggers + schedule mode) is unchanged. After it come
    only the pages the selection calls for - Night and Morning, or each split
    into Weekdays and Weekend - each showing only the fields that apply, and a
    small follow-up page for a custom sun elevation that is shown only when
    "Custom" was chosen. The setup flow and the options flow differ only in
    where a value is read from and kept (``_sched_get`` / ``_sched_put``) and in
    what happens after the last page (``_sched_finish``).
    """

    _sched_pages: list[str]
    _sched_cursor: int
    # The setup flow has always offered the optional sun event next to the time;
    # in the options flow it lives on the Advanced schedule page.
    _sched_sun_events_on_pages: bool = False

    # -- storage hooks -------------------------------------------------------

    def _sched_triggers(
        self,
    ) -> tuple[NightTrigger, MorningTrigger, LifecycleScheduleMode]:
        raise NotImplementedError

    def _sched_get(self, key: str, default: Any) -> Any:
        raise NotImplementedError

    def _sched_put(self, key: str, value: Any) -> None:
        raise NotImplementedError

    async def _sched_finish(self) -> config_entries.ConfigFlowResult:
        raise NotImplementedError

    # -- navigation ----------------------------------------------------------

    async def _sched_begin(self) -> config_entries.ConfigFlowResult:
        self._sched_pages = _schedule_pages(*self._sched_triggers())
        self._sched_cursor = 0
        if not self._sched_pages:
            return await self._sched_finish()
        return await getattr(self, f"async_step_{self._sched_pages[0]}")()

    async def _sched_advance(self) -> config_entries.ConfigFlowResult:
        self._sched_cursor += 1
        if self._sched_cursor >= len(self._sched_pages):
            return await self._sched_finish()
        return await getattr(self, f"async_step_{self._sched_pages[self._sched_cursor]}")()

    def _sched_locate(self, step_id: str) -> None:
        pages = getattr(self, "_sched_pages", None)
        if not pages or step_id not in pages:
            pages = self._sched_pages = _schedule_pages(*self._sched_triggers())
        self._sched_cursor = pages.index(step_id)

    # -- one page ------------------------------------------------------------

    async def _sched_page(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> config_entries.ConfigFlowResult:
        phase, day = _SCHEDULE_PAGES[step_id]
        self._sched_locate(step_id)
        night, morning, _mode = self._sched_triggers()
        trigger = night if phase == "night" else morning
        fields = _PHASE_FIELDS[phase]
        has_time = trigger.value in _TIME_TRIGGER_VALUES
        has_sun = trigger.value in _SUN_TRIGGER_VALUES
        shared = day in ("all", "weekday")        # elevation and sun event do not vary by day
        time_key = _stored_storage_key(phase, day, "fixed_time")
        position_key = _stored_storage_key(phase, day, "position")
        time_field = fields["time_field"][day]
        position_field = fields["position_field"][day]
        preset_field = fields["preset_field"]
        event_field = fields["event_field"]
        elevation_key = fields["elevation_key"]
        with_event = self._sched_sun_events_on_pages and has_time and shared

        if user_input is not None:
            if has_time:
                self._sched_put(
                    time_key,
                    _parse_time_input(
                        user_input.get(time_field), self._sched_get(time_key, fields["time_default"][day])
                    ),
                )
            self._sched_put(
                position_key,
                int(user_input.get(position_field, self._sched_get(position_key, fields["position_default"]))),
            )
            if with_event:
                value = user_input.get(event_field)
                self._sched_put(f"{phase}_sun_event", SunEvent(value) if value else None)
            if has_sun and shared and preset_field in user_input:
                preset = user_input[preset_field]
                if preset == ELEVATION_PRESET_CUSTOM:
                    return await getattr(self, f"async_step_lifecycle_{phase}_elevation")()
                self._sched_put(elevation_key, fields["presets"][preset])
            return await self._sched_advance()

        position_selector = NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%")
        )
        schema: dict[Any, Any] = {}
        if has_time:
            schema[
                vol.Required(time_field, default=self._sched_get(time_key, fields["time_default"][day]))
            ] = TimeSelector()
        if has_sun and shared:
            stored_elevation = self._sched_get(elevation_key, fields["elevation_default"])
            schema[
                vol.Required(preset_field, default=_elevation_to_preset(stored_elevation, fields["presets"]))
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=[*fields["presets"].keys(), ELEVATION_PRESET_CUSTOM],
                    mode=SelectSelectorMode.LIST,
                    translation_key=fields["preset_translation_key"],
                )
            )
        if with_event:
            stored_event = self._sched_get(f"{phase}_sun_event", None)
            schema[
                vol.Optional(event_field, description={"suggested_value": stored_event})
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=SUN_EVENT_OPTIONS,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key=fields["event_translation_key"],
                )
            )
        schema[
            vol.Required(position_field, default=self._sched_get(position_key, fields["position_default"]))
        ] = position_selector
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema(schema))

    async def _sched_elevation(
        self, phase: str, user_input: dict[str, Any] | None
    ) -> config_entries.ConfigFlowResult:
        """Follow-up page of a page whose sun position preset is "Custom"."""
        fields = _PHASE_FIELDS[phase]
        step_id = f"lifecycle_{phase}_elevation"
        if user_input is not None:
            current = self._sched_get(fields["elevation_key"], fields["elevation_default"])
            self._sched_put(fields["elevation_key"], float(user_input.get(fields["custom_field"], current)))
            return await self._sched_advance()
        schema = vol.Schema(
            {
                vol.Required(
                    fields["custom_field"],
                    default=self._sched_get(fields["elevation_key"], fields["elevation_default"]),
                ): NumberSelector(NumberSelectorConfig(min=-90, max=90, step=0.5, mode=NumberSelectorMode.BOX))
            }
        )
        return self.async_show_form(step_id=step_id, data_schema=schema)

    async def async_step_lifecycle_night(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        return await self._sched_page("lifecycle_night", user_input)

    async def async_step_lifecycle_night_weekday(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        return await self._sched_page("lifecycle_night_weekday", user_input)

    async def async_step_lifecycle_night_weekend(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        return await self._sched_page("lifecycle_night_weekend", user_input)

    async def async_step_lifecycle_morning(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        return await self._sched_page("lifecycle_morning", user_input)

    async def async_step_lifecycle_morning_weekday(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        return await self._sched_page("lifecycle_morning_weekday", user_input)

    async def async_step_lifecycle_morning_weekend(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        return await self._sched_page("lifecycle_morning_weekend", user_input)

    async def async_step_lifecycle_night_elevation(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        return await self._sched_elevation("night", user_input)

    async def async_step_lifecycle_morning_elevation(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        return await self._sched_elevation("morning", user_input)


class SmartShadingConfigFlow(_ScheduleWizard, config_entries.ConfigFlow, domain=DOMAIN):
    """ARCHITECTURE.md §7 Config Flow, simplified scope."""

    VERSION = 1

    def __init__(self) -> None:
        self._name: str = "SmartShading"
        self._zone_name: str = ""
        self._use_home_location: bool = True
        self._weather_entity_id: str | None = None
        self._solar_radiation_sensor_id: str | None = None
        self._outdoor_temperature_sensor_id: str | None = None
        self._cloud_cover_sensor_id: str | None = None
        self._wind_speed_sensor_id: str | None = None
        self._rain_sensor_id: str | None = None
        self._night_trigger: NightTrigger = NightTrigger(DEFAULT_NIGHT_TRIGGER)
        self._night_fixed_time: time = time.fromisoformat(DEFAULT_NIGHT_FIXED_TIME)
        self._night_sun_elevation: float = DEFAULT_NIGHT_SUN_ELEVATION
        self._night_position: int = DEFAULT_NIGHT_POSITION
        self._night_sun_event: SunEvent | None = None  # None = use night_fixed_time as-is
        self._morning_trigger: MorningTrigger = MorningTrigger(DEFAULT_MORNING_TRIGGER)
        self._morning_fixed_time: time = time.fromisoformat(DEFAULT_MORNING_FIXED_TIME)
        self._morning_sun_elevation: float = DEFAULT_MORNING_SUN_ELEVATION
        self._morning_position: int = DEFAULT_MORNING_POSITION
        self._morning_sun_event: SunEvent | None = None  # None = use morning_fixed_time as-is
        # Weekday/Weekend schedule mode
        self._schedule_mode: LifecycleScheduleMode = LifecycleScheduleMode.SAME_EVERY_DAY
        self._weekday_night_fixed_time: time = time.fromisoformat(DEFAULT_WEEKDAY_NIGHT_FIXED_TIME)
        self._weekday_night_position: int = DEFAULT_NIGHT_POSITION
        self._weekday_morning_fixed_time: time = time.fromisoformat(DEFAULT_WEEKDAY_MORNING_FIXED_TIME)
        self._weekday_morning_position: int = DEFAULT_MORNING_POSITION
        self._weekend_night_fixed_time: time = time.fromisoformat(DEFAULT_WEEKEND_NIGHT_FIXED_TIME)
        self._weekend_night_position: int = DEFAULT_NIGHT_POSITION
        self._weekend_morning_fixed_time: time = time.fromisoformat(DEFAULT_WEEKEND_MORNING_FIXED_TIME)
        self._weekend_morning_position: int = DEFAULT_MORNING_POSITION
        self._presence_entity_ids: list[str] = []
        self._absence_delay_min: int = DEFAULT_ABSENCE_DELAY_MIN
        self._absence_position: int | None = None
        self._presence_policy: PresencePolicy = PresencePolicy(DEFAULT_PRESENCE_POLICY)
        # Stable zone ID for this setup run (UUID4, generated once — all windows
        # in this flow share the single zone, so the ID must not change between
        # async_step_cover_group() calls and _async_finish()).
        self._default_zone_id: str = f"zone_{uuid.uuid4().hex}"
        # Comfort settings (Comfort Engine phase, 2026-06-17)
        self._indoor_temperature_sensor_ids: list[str] = []
        self._heat_protection_enabled: bool = True
        self._glare_protection_enabled: bool = True
        self._solar_gain_enabled: bool = True
        self._heat_protection_indoor_temp_c: float = DEFAULT_HEAT_PROTECTION_INDOOR_TEMP_C
        self._heat_protection_outdoor_temp_c: float = DEFAULT_HEAT_PROTECTION_OUTDOOR_TEMP_C
        self._solar_gain_max_outdoor_temp_c: float = DEFAULT_SOLAR_GAIN_MAX_OUTDOOR_TEMP_C
        self._windows: list[WindowConfig] = []
        self._cover_groups: list[CoverGroup] = []
        self._current_window: dict[str, Any] | None = None  # pending until cover group is chosen

    # -- Step 1: general configuration --

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            zone_name = (user_input.get(CONF_ZONE_NAME) or "").strip()
            if not zone_name:
                errors["base"] = "empty_zone_name"
            else:
                self._zone_name = zone_name
                return await self.async_step_weather()

        schema = vol.Schema(
            {
                vol.Required(CONF_ZONE_NAME): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    # -- Step 2: optional weather/solar inputs --

    async def async_step_weather(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._weather_entity_id = user_input.get(CONF_WEATHER_ENTITY_ID)
            self._solar_radiation_sensor_id = user_input.get(CONF_SOLAR_RADIATION_SENSOR_ID)
            self._outdoor_temperature_sensor_id = user_input.get(CONF_OUTDOOR_TEMPERATURE_SENSOR_ID)
            self._cloud_cover_sensor_id = user_input.get(CONF_CLOUD_COVER_SENSOR_ID)
            self._wind_speed_sensor_id = user_input.get(CONF_WIND_SPEED_SENSOR_ID)
            self._rain_sensor_id = user_input.get(CONF_RAIN_SENSOR_ID)
            return await self.async_step_comfort()

        # Prefill weather fields from first existing zone entry so the user
        # does not have to re-enter the same house-wide sensors for each new zone.
        prefill: dict[str, str | None] = {
            CONF_WEATHER_ENTITY_ID: None,
            CONF_SOLAR_RADIATION_SENSOR_ID: None,
            CONF_OUTDOOR_TEMPERATURE_SENSOR_ID: None,
            CONF_CLOUD_COVER_SENSOR_ID: None,
            CONF_WIND_SPEED_SENSOR_ID: None,
            CONF_RAIN_SENSOR_ID: None,
        }
        src = _first_zone_entry_data(self.hass)
        if src:
            for key in prefill:
                prefill[key] = src.get(key)

        schema = vol.Schema(
            {
                vol.Optional(CONF_WEATHER_ENTITY_ID, description={"suggested_value": prefill[CONF_WEATHER_ENTITY_ID]}): EntitySelector(EntitySelectorConfig(domain="weather")),
                vol.Optional(CONF_SOLAR_RADIATION_SENSOR_ID, description={"suggested_value": prefill[CONF_SOLAR_RADIATION_SENSOR_ID]}): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_OUTDOOR_TEMPERATURE_SENSOR_ID, description={"suggested_value": prefill[CONF_OUTDOOR_TEMPERATURE_SENSOR_ID]}): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_CLOUD_COVER_SENSOR_ID, description={"suggested_value": prefill[CONF_CLOUD_COVER_SENSOR_ID]}): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Optional(CONF_WIND_SPEED_SENSOR_ID, description={"suggested_value": prefill[CONF_WIND_SPEED_SENSOR_ID]}): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Optional(CONF_RAIN_SENSOR_ID, description={"suggested_value": prefill[CONF_RAIN_SENSOR_ID]}): EntitySelector(
                    EntitySelectorConfig(domain=["sensor", "binary_sensor"])
                ),
            }
        )
        return self.async_show_form(step_id="weather", data_schema=schema)

    # -- Step 3: night/morning trigger selection + schedule mode --

    async def async_step_lifecycle(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._night_trigger = NightTrigger(user_input[CONF_NIGHT_TRIGGER])
            self._morning_trigger = MorningTrigger(user_input[CONF_MORNING_TRIGGER])
            try:
                self._schedule_mode = LifecycleScheduleMode(
                    user_input.get(CONF_SCHEDULE_MODE, LifecycleScheduleMode.SAME_EVERY_DAY.value)
                )
            except ValueError:
                self._schedule_mode = LifecycleScheduleMode.SAME_EVERY_DAY
            # Skip the detail step entirely when both triggers are disabled —
            # an empty form confuses users and HA validates empty schemas inconsistently.
            if self._night_trigger is NightTrigger.DISABLED and self._morning_trigger is MorningTrigger.DISABLED:
                return await self.async_step_presence()
            return await self._sched_begin()

        trigger_selector = SelectSelector(
            SelectSelectorConfig(
                options=LIFECYCLE_TRIGGER_OPTIONS,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="lifecycle_trigger",
            )
        )
        schedule_mode_selector = SelectSelector(
            SelectSelectorConfig(
                options=LIFECYCLE_SCHEDULE_MODE_OPTIONS,
                mode=SelectSelectorMode.LIST,
                translation_key="lifecycle_schedule_mode",
            )
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_NIGHT_TRIGGER, default=DEFAULT_NIGHT_TRIGGER): trigger_selector,
                vol.Required(CONF_MORNING_TRIGGER, default=DEFAULT_MORNING_TRIGGER): trigger_selector,
                vol.Required(
                    CONF_SCHEDULE_MODE, default=LifecycleScheduleMode.SAME_EVERY_DAY.value
                ): schedule_mode_selector,
            }
        )
        return self.async_show_form(step_id="lifecycle", data_schema=schema)

    # -- Step 3b: the Night / Morning pages (_ScheduleWizard) --

    _sched_sun_events_on_pages = True

    # storage key of a schedule value -> the attribute this flow keeps it in
    _SCHED_ATTRS: dict[str, str] = {
        "night_fixed_time": "_night_fixed_time",
        "night_sun_elevation_deg": "_night_sun_elevation",
        "night_position": "_night_position",
        "night_sun_event": "_night_sun_event",
        "morning_fixed_time": "_morning_fixed_time",
        "morning_sun_elevation_deg": "_morning_sun_elevation",
        "morning_position": "_morning_position",
        "morning_sun_event": "_morning_sun_event",
        "weekday_night_fixed_time": "_weekday_night_fixed_time",
        "weekday_night_position": "_weekday_night_position",
        "weekday_morning_fixed_time": "_weekday_morning_fixed_time",
        "weekday_morning_position": "_weekday_morning_position",
        "weekend_night_fixed_time": "_weekend_night_fixed_time",
        "weekend_night_position": "_weekend_night_position",
        "weekend_morning_fixed_time": "_weekend_morning_fixed_time",
        "weekend_morning_position": "_weekend_morning_position",
    }

    def _sched_triggers(
        self,
    ) -> tuple[NightTrigger, MorningTrigger, LifecycleScheduleMode]:
        return self._night_trigger, self._morning_trigger, self._schedule_mode

    def _sched_get(self, key: str, default: Any) -> Any:  # noqa: ARG002 - the attribute holds the default
        value = getattr(self, self._SCHED_ATTRS[key])
        if isinstance(value, time):
            return value.isoformat()
        if isinstance(value, SunEvent):
            return value.value
        return value

    def _sched_put(self, key: str, value: Any) -> None:
        setattr(self, self._SCHED_ATTRS[key], value)

    async def _sched_finish(self) -> config_entries.ConfigFlowResult:
        return await self.async_step_presence()

    # -- Step 4: presence / absence settings --

    async def async_step_presence(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._presence_entity_ids = user_input.get(CONF_PRESENCE_ENTITY_IDS, [])
            self._absence_delay_min = int(user_input[CONF_ABSENCE_DELAY_MIN])
            raw_pos = user_input.get(CONF_ABSENCE_POSITION)
            self._absence_position = int(raw_pos) if raw_pos is not None else None
            try:
                self._presence_policy = PresencePolicy(
                    user_input.get(CONF_PRESENCE_POLICY, DEFAULT_PRESENCE_POLICY)
                )
            except ValueError:
                self._presence_policy = PresencePolicy(DEFAULT_PRESENCE_POLICY)
            return await self.async_step_window()

        _prefill = _first_zone_entry_data(self.hass) or {}
        _prefill_presence = _prefill.get(CONF_PRESENCE_ENTITY_IDS, [])
        _prefill_delay = _prefill.get(CONF_ABSENCE_DELAY_MIN, DEFAULT_ABSENCE_DELAY_MIN)
        _prefill_pos = next(
            (z.get("absence_position") for z in _prefill.get("zones", []) if z.get("absence_position") is not None),
            DEFAULT_ABSENCE_POSITION,
        )
        _prefill_policy = _prefill.get(CONF_PRESENCE_POLICY, DEFAULT_PRESENCE_POLICY)

        schema = vol.Schema(
            {
                vol.Optional(CONF_PRESENCE_ENTITY_IDS, default=_prefill_presence): EntitySelector(
                    EntitySelectorConfig(domain="person", multiple=True)
                ),
                vol.Required(CONF_ABSENCE_DELAY_MIN, default=_prefill_delay): NumberSelector(
                    NumberSelectorConfig(min=0, max=1440, step=5, mode=NumberSelectorMode.BOX, unit_of_measurement="min")
                ),
                vol.Optional(CONF_ABSENCE_POSITION, default=_prefill_pos): NumberSelector(
                    NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%")
                ),
                vol.Required(CONF_PRESENCE_POLICY, default=_prefill_policy): SelectSelector(
                    SelectSelectorConfig(
                        options=PRESENCE_POLICY_OPTIONS,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="presence_policy",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="presence", data_schema=schema)

    # -- Step 3: comfort settings (indoor temperature + protection toggles) --

    async def async_step_comfort(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        # Prefill from first existing zone entry
        _prefill = _first_zone_entry_data(self.hass)

        if user_input is not None:
            self._indoor_temperature_sensor_ids = user_input.get(CONF_INDOOR_TEMPERATURE_SENSOR_IDS) or []
            self._heat_protection_enabled = bool(user_input[CONF_HEAT_PROTECTION_ENABLED])
            self._glare_protection_enabled = bool(user_input[CONF_GLARE_PROTECTION_ENABLED])
            self._solar_gain_enabled = bool(user_input[CONF_SOLAR_GAIN_ENABLED])
            return await self.async_step_lifecycle()

        # Indoor temperature sensors are per-zone and must never be carried
        # over from another zone. The sensor list always starts empty so the
        # user explicitly selects sensors for the new zone.
        _prefill_comfort = (_prefill or {}).get("comfort_config") or {}

        schema = vol.Schema(
            {
                vol.Optional(CONF_INDOOR_TEMPERATURE_SENSOR_IDS, default=[]): EntitySelector(
                    EntitySelectorConfig(domain="sensor", multiple=True)
                ),
                vol.Required(CONF_HEAT_PROTECTION_ENABLED, default=_prefill_comfort.get("heat_protection_enabled", True)): BooleanSelector(),
                vol.Required(CONF_GLARE_PROTECTION_ENABLED, default=_prefill_comfort.get("glare_protection_enabled", True)): BooleanSelector(),
                vol.Required(CONF_SOLAR_GAIN_ENABLED, default=_prefill_comfort.get("solar_gain_enabled", True)): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="comfort", data_schema=schema)

    # -- Step 6: add a window --

    async def async_step_window(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            azimuth = _resolve_azimuth(user_input)
            if azimuth is None:
                errors["base"] = "invalid_custom_azimuth"
            else:
                raw_abs = user_input.get(CONF_ABSENCE_POSITION)
                self._current_window = {
                    "id": f"window_{uuid.uuid4().hex}",
                    "name": user_input[CONF_WINDOW_NAME],
                    "floor_level": int(user_input[CONF_FLOOR_LEVEL]),
                    "azimuth": azimuth,
                    "absence_position": int(raw_abs) if raw_abs is not None else None,
                    "behavior_mode": user_input.get(CONF_WINDOW_BEHAVIOR_MODE, WindowBehaviorMode.FULLY_AUTOMATIC.value),
                }
                return await self.async_step_cover_group()

        schema = vol.Schema(
            {
                vol.Required(CONF_WINDOW_NAME): str,
                vol.Required(CONF_FLOOR_LEVEL, default=0): NumberSelector(
                    NumberSelectorConfig(min=-1, max=20, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_COMPASS_DIRECTION, default="south"): SelectSelector(
                    SelectSelectorConfig(
                        options=_COMPASS_OPTIONS,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="compass_direction",
                    )
                ),
                vol.Optional(CONF_CUSTOM_AZIMUTH): NumberSelector(
                    NumberSelectorConfig(min=0, max=359, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_ABSENCE_POSITION): NumberSelector(
                    NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%")
                ),
                vol.Required(CONF_WINDOW_BEHAVIOR_MODE, default=WindowBehaviorMode.FULLY_AUTOMATIC.value): SelectSelector(
                    SelectSelectorConfig(
                        options=WINDOW_BEHAVIOR_MODE_OPTIONS,
                        mode=SelectSelectorMode.LIST,
                        translation_key="window_behavior_mode",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="window", data_schema=schema, errors=errors)

    # -- Step 7: assign covers via a new CoverGroup (Window -> CoverGroup, §3.0) --

    async def async_step_cover_group(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        assert self._current_window is not None  # only reachable via async_step_window

        if user_input is not None:
            cover_entities: list[str] = user_input[CONF_COVER_ENTITIES]
            if not cover_entities:
                errors["base"] = "no_covers_selected"
            elif _covers_already_assigned(
                self.hass, cover_entities,
                extra_taken=[c for g in self._cover_groups for c in g.cover_ids],
            ):
                errors["base"] = "cover_already_assigned"
            else:
                hw_type = cover_hardware_type_from_str(
                    user_input.get(CONF_COVER_HARDWARE_TYPE)
                )
                cover_group = CoverGroup(
                    id=f"cg_{uuid.uuid4().hex}",
                    window_id=self._current_window["id"],
                    cover_ids=cover_entities,
                    sync_mode=CoverSyncMode.SYNCHRONOUS,
                    hardware_type=hw_type,
                )
                _behavior_mode_raw = self._current_window.get("behavior_mode", WindowBehaviorMode.FULLY_AUTOMATIC.value)
                try:
                    _behavior_mode = WindowBehaviorMode(_behavior_mode_raw)
                except ValueError:
                    _behavior_mode = WindowBehaviorMode.FULLY_AUTOMATIC
                window = WindowConfig(
                    id=self._current_window["id"],
                    name=self._current_window["name"],
                    zone_id=self._default_zone_id,
                    azimuth=self._current_window["azimuth"],
                    floor_level=self._current_window["floor_level"],
                    cover_group_id=cover_group.id,
                    absence_position=self._current_window.get("absence_position"),
                    behavior_mode=_behavior_mode,
                )
                self._cover_groups.append(cover_group)
                self._windows.append(window)
                self._current_window = None
                return await self.async_step_add_another_window()

        _hw_options = [t.value for t in CoverHardwareType]
        schema = vol.Schema(
            {
                vol.Required(CONF_COVER_ENTITIES): EntitySelector(
                    EntitySelectorConfig(domain="cover", multiple=True)
                ),
                vol.Required(
                    CONF_COVER_HARDWARE_TYPE,
                    default=CoverHardwareType.ROLLER_SHUTTER.value,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=_hw_options,
                        translation_key="cover_hardware_type",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="cover_group",
            data_schema=schema,
            errors=errors,
            description_placeholders={"window_name": self._current_window["name"]},
        )

    # -- Step 8: loop --

    async def async_step_add_another_window(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            if user_input[CONF_ADD_ANOTHER_WINDOW]:
                return await self.async_step_window()
            return self._async_finish(
                light=DEFAULT_LIGHT_SHADE_POSITION,
                normal=DEFAULT_NORMAL_SHADE_POSITION,
                strong=DEFAULT_STRONG_SHADE_POSITION,
            )

        schema = vol.Schema({vol.Required(CONF_ADD_ANOTHER_WINDOW, default=False): BooleanSelector()})
        return self.async_show_form(
            step_id="add_another_window",
            data_schema=schema,
            description_placeholders={"window_count": str(len(self._windows))},
        )

    # -- System entry auto-creation --

    async def async_step_system(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Auto-create the SmartShading System entry (no user interaction).

        Triggered by _ensure_system_entry() in __init__.py when a zone entry
        is set up and no system entry exists yet.  Aborts immediately if a
        system entry already exists (race-condition guard for multi-zone restarts).
        """
        existing = [
            e
            for e in self.hass.config_entries.async_entries(DOMAIN)
            if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SYSTEM
        ]
        if existing:
            return self.async_abort(reason="system_already_exists")
        return self.async_create_entry(
            title="SmartShading System",
            data={CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM},
        )

    @classmethod
    def async_get_options_flow(
        cls, config_entry: config_entries.ConfigEntry
    ) -> config_entries.OptionsFlow:
        return SmartShadingOptionsFlow(config_entry)

    # -- Finish --

    def _async_finish(self, light: int, normal: int, strong: int) -> config_entries.ConfigFlowResult:
        default_zone = ZoneConfig(id=self._default_zone_id, name=self._zone_name, absence_position=self._absence_position)
        lifecycle_config = NightDayLifecycleConfig(
            id="default",
            schedule_mode=self._schedule_mode,
            night_trigger=self._night_trigger,
            night_fixed_time=self._night_fixed_time,
            night_sun_elevation_deg=self._night_sun_elevation,
            night_position=self._night_position,
            morning_trigger=self._morning_trigger,
            morning_fixed_time=self._morning_fixed_time,
            morning_sun_elevation_deg=self._morning_sun_elevation,
            morning_position=self._morning_position,
            weekday_night_fixed_time=self._weekday_night_fixed_time,
            weekday_night_position=self._weekday_night_position,
            weekday_morning_fixed_time=self._weekday_morning_fixed_time,
            weekday_morning_position=self._weekday_morning_position,
            weekend_night_fixed_time=self._weekend_night_fixed_time,
            weekend_night_position=self._weekend_night_position,
            weekend_morning_fixed_time=self._weekend_morning_fixed_time,
            weekend_morning_position=self._weekend_morning_position,
            night_sun_event=self._night_sun_event,
            morning_sun_event=self._morning_sun_event,
        )
        entry_data = SmartShadingConfigEntryData(
            name=self._zone_name,
            use_home_location=self._use_home_location,
            zones=[default_zone],
            windows=self._windows,
            cover_groups=self._cover_groups,
            shade_position_defaults=ShadePositionDefaults(
                light_shade_position=light,
                normal_shade_position=normal,
                strong_shade_position=strong,
            ),
            lifecycle_config=lifecycle_config,
            presence_entity_ids=self._presence_entity_ids,
            absence_delay_min=self._absence_delay_min,
            presence_policy=self._presence_policy,
            weather_entity_id=self._weather_entity_id,
            solar_radiation_sensor_id=self._solar_radiation_sensor_id,
            outdoor_temperature_sensor_id=self._outdoor_temperature_sensor_id,
            cloud_cover_sensor_id=self._cloud_cover_sensor_id,
            wind_speed_sensor_id=self._wind_speed_sensor_id,
            rain_sensor_id=self._rain_sensor_id,
            indoor_temperature_sensor_ids=self._indoor_temperature_sensor_ids,
            comfort_config=ComfortConfig(
                heat_protection_enabled=self._heat_protection_enabled,
                glare_protection_enabled=self._glare_protection_enabled,
                solar_gain_enabled=self._solar_gain_enabled,
                heat_protection_indoor_temp_c=self._heat_protection_indoor_temp_c,
                heat_protection_outdoor_temp_c=self._heat_protection_outdoor_temp_c,
                solar_gain_max_outdoor_temp_c=self._solar_gain_max_outdoor_temp_c,
            ),
        )
        return self.async_create_entry(title=self._zone_name, data=to_storage_dict(entry_data))


# ---------------------------------------------------------------------------
# Options Flow
# ---------------------------------------------------------------------------

# comfort_config keys that were user settings in earlier versions and are now
# fixed internal policy (const.GLARE_MIN_EXPOSURE_WM2 / HEAT_RELEASE_HYSTERESIS_C).
# Old entries may still carry them; the Comfort options save removes them.
_OBSOLETE_COMFORT_KEYS = frozenset({"glare_min_exposure_wm2", "heat_protection_hysteresis_c"})

class SmartShadingOptionsFlow(_ScheduleWizard, config_entries.OptionsFlow):
    """Options Flow for post-setup editing.

    Settings sections (weather, lifecycle, presence, comfort, behavior) are
    independent — saving one section schedules a reload without affecting other
    sections or the zone_controls stored in options.

    Structural changes (add/edit/remove_window) update the entry's window and
    cover list in-place and trigger a reload so entities reflect the new state.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._pending_night_trigger: NightTrigger | None = None
        self._pending_morning_trigger: MorningTrigger | None = None
        self._pending_schedule_mode: LifecycleScheduleMode | None = None
        # What the schedule pages collected, written once after the last page.
        self._sched_pending: dict[str, Any] = {}
        # Add Window flow state (Options path for structural additions)
        self._add_window_pending: dict[str, Any] | None = None
        self._add_windows: list[WindowConfig] = []
        self._add_cover_groups: list[CoverGroup] = []
        # Edit/Remove Window flow state
        self._edit_window_id: str | None = None
        # Manual Override gate->custom flow state (T21 Phase C2)
        self._pending_override_detection_tolerance: int | None = None
        # Manual Override pages (Phase 5): which policy is being edited and what the pages collected.
        self._override_scope: str | None = None
        self._override_draft: dict[str, Any] = {}
        self._override_queue: list[str] = []
        # Presence gate->custom flow state (T21 Phase C3)
        self._pending_presence_updates: dict[str, Any] = {}

    # -- Init: section menu — branches into the System entry's own menu
    # (T21 Phase C2) or the zone menu, depending on entry type. --

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if self._config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SYSTEM:
            return await self.async_step_system_init()
        return await self._async_step_zone_init(user_input)

    async def async_step_system_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """The System entry owns the global defaults every zone follows unless
        it opts into its own settings: the Manual Override default policy and
        the Presence default."""
        return self.async_show_menu(
            step_id="system_init",
            menu_options=["system_manual_override", "system_presence"],
        )

    async def _async_step_zone_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["weather", "lifecycle", "presence", "comfort", "behavior", "windows", "advanced"],
        )

    # -- Windows: grouping submenu for add/edit/remove (T21 Phase C: reduces
    # the top-level menu from 11 to 7 items; each sub-step below is otherwise
    # completely unchanged). --

    async def async_step_windows(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self.async_show_menu(
            step_id="windows",
            menu_options=["add_window", "edit_window", "remove_window"],
        )

    # -- Advanced: grouping submenu for the less-frequently-used sections
    # (T21 Phase C). Lifecycle profiles (T6) were removed in T21 Phase C3 —
    # see the ownership analysis; this menu now only ever holds genuine
    # expert-only functionality. --

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self.async_show_menu(
            step_id="advanced",
            menu_options=["manual_override", "lifecycle_advanced"],
        )

    # -- Weather / sensor entities --

    async def async_step_weather(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self._save_and_reload({
                CONF_WEATHER_ENTITY_ID: user_input.get(CONF_WEATHER_ENTITY_ID),
                CONF_SOLAR_RADIATION_SENSOR_ID: user_input.get(CONF_SOLAR_RADIATION_SENSOR_ID),
                CONF_OUTDOOR_TEMPERATURE_SENSOR_ID: user_input.get(CONF_OUTDOOR_TEMPERATURE_SENSOR_ID),
                CONF_CLOUD_COVER_SENSOR_ID: user_input.get(CONF_CLOUD_COVER_SENSOR_ID),
                CONF_WIND_SPEED_SENSOR_ID: user_input.get(CONF_WIND_SPEED_SENSOR_ID),
                CONF_RAIN_SENSOR_ID: user_input.get(CONF_RAIN_SENSOR_ID),
            })

        current = self._config_entry.data
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_WEATHER_ENTITY_ID,
                    description={"suggested_value": current.get(CONF_WEATHER_ENTITY_ID)},
                ): EntitySelector(EntitySelectorConfig(domain="weather")),
                vol.Optional(
                    CONF_SOLAR_RADIATION_SENSOR_ID,
                    description={"suggested_value": current.get(CONF_SOLAR_RADIATION_SENSOR_ID)},
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Optional(
                    CONF_OUTDOOR_TEMPERATURE_SENSOR_ID,
                    description={"suggested_value": current.get(CONF_OUTDOOR_TEMPERATURE_SENSOR_ID)},
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Optional(
                    CONF_CLOUD_COVER_SENSOR_ID,
                    description={"suggested_value": current.get(CONF_CLOUD_COVER_SENSOR_ID)},
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Optional(
                    CONF_WIND_SPEED_SENSOR_ID,
                    description={"suggested_value": current.get(CONF_WIND_SPEED_SENSOR_ID)},
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Optional(
                    CONF_RAIN_SENSOR_ID,
                    description={"suggested_value": current.get(CONF_RAIN_SENSOR_ID)},
                ): EntitySelector(EntitySelectorConfig(domain=["sensor", "binary_sensor"])),
            }
        )
        return self.async_show_form(step_id="weather", data_schema=schema)

    # -- Schedule (lifecycle) — step 1: trigger selection + schedule mode --

    async def async_step_lifecycle(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        stored_lc = (self._config_entry.data.get("lifecycle_config") or {})

        if user_input is not None:
            self._pending_night_trigger = NightTrigger(user_input[CONF_NIGHT_TRIGGER])
            self._pending_morning_trigger = MorningTrigger(user_input[CONF_MORNING_TRIGGER])
            try:
                self._pending_schedule_mode = LifecycleScheduleMode(
                    user_input.get(CONF_SCHEDULE_MODE, LifecycleScheduleMode.SAME_EVERY_DAY.value)
                )
            except ValueError:
                self._pending_schedule_mode = LifecycleScheduleMode.SAME_EVERY_DAY
            # Both disabled: save immediately, no detail step needed.
            if self._pending_night_trigger is NightTrigger.DISABLED and self._pending_morning_trigger is MorningTrigger.DISABLED:
                new_lc = {
                    **stored_lc,
                    "night_trigger": NightTrigger.DISABLED.value,
                    "morning_trigger": MorningTrigger.DISABLED.value,
                    "schedule_mode": self._pending_schedule_mode.value,
                }
                return self._save_and_reload({"lifecycle_config": new_lc})
            self._sched_pending = {}
            return await self._sched_begin()

        stored_schedule_mode = stored_lc.get("schedule_mode", LifecycleScheduleMode.SAME_EVERY_DAY.value)
        trigger_selector = SelectSelector(
            SelectSelectorConfig(
                options=LIFECYCLE_TRIGGER_OPTIONS,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="lifecycle_trigger",
            )
        )
        schedule_mode_selector = SelectSelector(
            SelectSelectorConfig(
                options=LIFECYCLE_SCHEDULE_MODE_OPTIONS,
                mode=SelectSelectorMode.LIST,
                translation_key="lifecycle_schedule_mode",
            )
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NIGHT_TRIGGER,
                    default=stored_lc.get("night_trigger", DEFAULT_NIGHT_TRIGGER),
                ): trigger_selector,
                vol.Required(
                    CONF_MORNING_TRIGGER,
                    default=stored_lc.get("morning_trigger", DEFAULT_MORNING_TRIGGER),
                ): trigger_selector,
                vol.Required(CONF_SCHEDULE_MODE, default=stored_schedule_mode): schedule_mode_selector,
            }
        )
        return self.async_show_form(step_id="lifecycle", data_schema=schema)

    # -- Schedule (lifecycle) — step 2: the Night / Morning pages (_ScheduleWizard) --
    #
    # The options flow keeps what the pages collect in ``_sched_pending`` and
    # writes the schedule once, after the last page. Sun events, time limits
    # and active months are not asked here: they have their own step under
    # Advanced (async_step_lifecycle_advanced).

    def _sched_triggers(
        self,
    ) -> tuple[NightTrigger, MorningTrigger, LifecycleScheduleMode]:
        stored_lc = (self._config_entry.data.get("lifecycle_config") or {})
        night = self._pending_night_trigger or NightTrigger(
            stored_lc.get("night_trigger", DEFAULT_NIGHT_TRIGGER)
        )
        morning = self._pending_morning_trigger or MorningTrigger(
            stored_lc.get("morning_trigger", DEFAULT_MORNING_TRIGGER)
        )
        try:
            mode = self._pending_schedule_mode or LifecycleScheduleMode(
                stored_lc.get("schedule_mode", LifecycleScheduleMode.SAME_EVERY_DAY.value)
            )
        except ValueError:
            mode = LifecycleScheduleMode.SAME_EVERY_DAY
        return night, morning, mode

    def _sched_get(self, key: str, default: Any) -> Any:
        if key in self._sched_pending:
            value = self._sched_pending[key]
            return value.isoformat() if isinstance(value, time) else value
        stored_lc = (self._config_entry.data.get("lifecycle_config") or {})
        if key in ("weekday_night_fixed_time", "weekday_morning_fixed_time"):
            # a first switch to weekday/weekend starts from the shared time
            shared_key = "night_fixed_time" if "night" in key else "morning_fixed_time"
            return stored_lc.get(key) or stored_lc.get(shared_key, default)
        return stored_lc.get(key, default)

    def _sched_put(self, key: str, value: Any) -> None:
        self._sched_pending[key] = value

    async def _sched_finish(self) -> config_entries.ConfigFlowResult:
        stored_lc = (self._config_entry.data.get("lifecycle_config") or {})
        return self._save_and_reload({
            "lifecycle_config": _assemble_lifecycle_config(
                stored_lc, self._sched_pending, *self._sched_triggers()
            )
        })

    # -- Schedule — Advanced: sun events, time limits, active months --

    async def async_step_lifecycle_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Rarely needed schedule settings, edited on their own.

        Only the settings that apply to the stored triggers are offered (a sun
        event and a time limit need a time); everything else in the stored
        schedule - including values of a phase without a time - is left exactly
        as it is, so saving this page can neither lose nor reactivate anything.
        """
        stored_lc = (self._config_entry.data.get("lifecycle_config") or {})
        try:
            night = NightTrigger(stored_lc.get("night_trigger", DEFAULT_NIGHT_TRIGGER))
        except ValueError:
            night = NightTrigger(DEFAULT_NIGHT_TRIGGER)
        try:
            morning = MorningTrigger(stored_lc.get("morning_trigger", DEFAULT_MORNING_TRIGGER))
        except ValueError:
            morning = MorningTrigger(DEFAULT_MORNING_TRIGGER)
        phases = [
            phase
            for phase, trigger in (("night", night), ("morning", morning))
            if trigger.value in _TIME_TRIGGER_VALUES
        ]
        event_fields = {"night": CONF_NIGHT_SUN_EVENT, "morning": CONF_MORNING_SUN_EVENT}
        limit_fields = {
            "night": (CONF_NIGHT_NOT_BEFORE, CONF_NIGHT_NOT_AFTER),
            "morning": (CONF_MORNING_NOT_BEFORE, CONF_MORNING_NOT_AFTER),
        }
        errors: dict[str, str] = {}

        if user_input is not None:
            raw_months = user_input.get(CONF_ACTIVE_MONTHS, stored_lc.get("active_months") or [])
            updates: dict[str, Any] = {
                # empty selection = unrestricted (None)
                "active_months": sorted(int(m) for m in raw_months) if raw_months else None,
            }
            limits: dict[str, tuple[time | None, time | None]] = {}
            for phase in phases:
                before_field, after_field = limit_fields[phase]
                limits[phase] = (
                    _parse_optional_time_input(user_input.get(before_field)),
                    _parse_optional_time_input(user_input.get(after_field)),
                )
            for phase in phases:
                before, after = limits[phase]
                if before is not None and after is not None and before > after:
                    errors["base"] = f"{phase}_clamp_window_invalid"
                    break
            if not errors:
                for phase in phases:
                    updates[f"{phase}_sun_event"] = user_input.get(event_fields[phase]) or None
                    updates[f"{phase}_not_before"] = _optional_time_to_storage(limits[phase][0])
                    updates[f"{phase}_not_after"] = _optional_time_to_storage(limits[phase][1])
                return self._save_and_reload({"lifecycle_config": {**stored_lc, **updates}})

        # On a validation-error redisplay keep what was just typed (even if it
        # was cleared) so a rejected window stays visible for the user to fix.
        def _current(field: str, stored_key: str) -> Any:
            return user_input.get(field) if user_input is not None else stored_lc.get(stored_key)

        schema: dict[Any, Any] = {}
        for phase in phases:
            before_field, after_field = limit_fields[phase]
            schema[
                vol.Optional(event_fields[phase], description={"suggested_value": _current(event_fields[phase], f"{phase}_sun_event")})
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=SUN_EVENT_OPTIONS,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key=_PHASE_FIELDS[phase]["event_translation_key"],
                )
            )
            schema[
                vol.Optional(before_field, description={"suggested_value": _current(before_field, f"{phase}_not_before")})
            ] = TimeSelector()
            schema[
                vol.Optional(after_field, description={"suggested_value": _current(after_field, f"{phase}_not_after")})
            ] = TimeSelector()
        schema[
            vol.Optional(
                CONF_ACTIVE_MONTHS,
                default=[str(m) for m in (stored_lc.get("active_months") or [])],
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=[str(m) for m in range(1, 13)],
                multiple=True,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="active_months",
            )
        )
        return self.async_show_form(
            step_id="lifecycle_advanced", data_schema=vol.Schema(schema), errors=errors
        )

    # -- Presence --

    async def async_step_presence(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Gate step (T21 Phase C3): entity selection, aggregation policy,
        and absence_position are always shown here (unaffected by the
        System default); absence_delay_min is the one timing field that may
        defer to the System entry's global default — see
        resolve_zone_absence_delay_min() — and is only ever shown on
        async_step_presence_custom, reached when this gate is answered
        "use my own settings".

        Migration: every zone already has a concrete absence_delay_min
        (a required field, always written by to_storage_dict()) — unlike
        Manual Override/Dispatch there is no "never configured" signal, so
        the toggle itself defaults to False (use my own stored value)
        unless a zone explicitly opts into the System default — this is the
        only choice that guarantees zero behavior change for every existing
        install.
        """
        current = self._config_entry.data

        if user_input is not None:
            try:
                _policy = PresencePolicy(user_input.get(CONF_PRESENCE_POLICY, DEFAULT_PRESENCE_POLICY))
            except ValueError:
                _policy = PresencePolicy(DEFAULT_PRESENCE_POLICY)
            self._pending_presence_updates = {
                CONF_PRESENCE_ENTITY_IDS: user_input.get(CONF_PRESENCE_ENTITY_IDS, []),
                CONF_PRESENCE_POLICY: _policy.value,
            }
            if CONF_ABSENCE_POSITION in user_input:
                raw_pos = user_input[CONF_ABSENCE_POSITION]
                absence_position = int(raw_pos) if raw_pos is not None else None
                zones = list(current.get("zones") or [])
                if zones:
                    zones[0] = {**zones[0], "absence_position": absence_position}
                self._pending_presence_updates["zones"] = zones

            use_system_default = bool(user_input.get(CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT, False))
            if use_system_default:
                self._pending_presence_updates[CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT] = True
                return self._save_and_reload(self._pending_presence_updates)
            return await self.async_step_presence_custom()

        # Prefill absence_position from the first zone's stored value; fall back to
        # the default only when nothing is stored (0 is a valid stored position).
        stored_absence_position = ((current.get("zones") or [{}])[0]).get("absence_position")
        current_absence_position = (
            DEFAULT_ABSENCE_POSITION if stored_absence_position is None else stored_absence_position
        )

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PRESENCE_ENTITY_IDS,
                    default=current.get(CONF_PRESENCE_ENTITY_IDS, []),
                ): EntitySelector(EntitySelectorConfig(domain="person", multiple=True)),
                vol.Required(
                    CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT,
                    default=bool(current.get(CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT, False)),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_ABSENCE_POSITION,
                    description={"suggested_value": current_absence_position},
                ): NumberSelector(
                    NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%")
                ),
                vol.Required(
                    CONF_PRESENCE_POLICY,
                    default=current.get(CONF_PRESENCE_POLICY, DEFAULT_PRESENCE_POLICY),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=PRESENCE_POLICY_OPTIONS,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="presence_policy",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="presence", data_schema=schema)

    async def async_step_presence_custom(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Reached only when the gate step (above) is answered "use my own
        settings" — the one remaining field, absence_delay_min."""
        current = self._config_entry.data

        if user_input is not None:
            updates = dict(getattr(self, "_pending_presence_updates", {}))
            updates[CONF_ABSENCE_DELAY_MIN] = int(
                user_input.get(CONF_ABSENCE_DELAY_MIN, DEFAULT_ABSENCE_DELAY_MIN)
            )
            updates[CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT] = False
            return self._save_and_reload(updates)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ABSENCE_DELAY_MIN,
                    default=current.get(CONF_ABSENCE_DELAY_MIN, DEFAULT_ABSENCE_DELAY_MIN),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=1440, step=5,
                        mode=NumberSelectorMode.BOX, unit_of_measurement="min",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="presence_custom", data_schema=schema)

    # -- Presence — System entry default (T21 Phase C3) --

    async def async_step_system_presence(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._config_entry.data

        if user_input is not None:
            new_value = int(user_input.get(CONF_ABSENCE_DELAY_MIN, DEFAULT_ABSENCE_DELAY_MIN))
            return self._save_system_default_and_reload({"system_absence_delay_min": new_value})

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ABSENCE_DELAY_MIN,
                    default=current.get("system_absence_delay_min", DEFAULT_ABSENCE_DELAY_MIN),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=1440, step=5,
                        mode=NumberSelectorMode.BOX, unit_of_measurement="min",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="system_presence", data_schema=schema)

    # -- Comfort settings --

    async def async_step_comfort(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._config_entry.data
        stored_comfort = current.get("comfort_config") or {}

        if user_input is not None:
            # glare_min_exposure_wm2 / heat_protection_hysteresis_c are internal
            # policy now: drop them from an older stored dict on this save, keep
            # everything else (toggles, thresholds, ...) exactly as stored.
            new_comfort = {
                **{k: v for k, v in stored_comfort.items() if k not in _OBSOLETE_COMFORT_KEYS},
                "heat_protection_enabled": bool(user_input[CONF_HEAT_PROTECTION_ENABLED]),
                "glare_protection_enabled": bool(user_input[CONF_GLARE_PROTECTION_ENABLED]),
                "solar_gain_enabled": bool(user_input[CONF_SOLAR_GAIN_ENABLED]),
            }
            return self._save_and_reload(
                {
                    CONF_INDOOR_TEMPERATURE_SENSOR_IDS: user_input.get(CONF_INDOOR_TEMPERATURE_SENSOR_IDS) or [],
                    "comfort_config": new_comfort,
                }
            )

        # Backward compat: read stored IDs — new key preferred, legacy single-ID fallback.
        _stored_ids: list[str] = (
            current.get(CONF_INDOOR_TEMPERATURE_SENSOR_IDS)
            or ([current[CONF_INDOOR_TEMPERATURE_SENSOR_ID]] if current.get(CONF_INDOOR_TEMPERATURE_SENSOR_ID) else [])
        )
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_INDOOR_TEMPERATURE_SENSOR_IDS,
                    default=_stored_ids,
                ): EntitySelector(EntitySelectorConfig(domain="sensor", multiple=True)),
                vol.Required(
                    CONF_HEAT_PROTECTION_ENABLED,
                    default=stored_comfort.get("heat_protection_enabled", True),
                ): BooleanSelector(),
                vol.Required(
                    CONF_GLARE_PROTECTION_ENABLED,
                    default=stored_comfort.get("glare_protection_enabled", True),
                ): BooleanSelector(),
                vol.Required(
                    CONF_SOLAR_GAIN_ENABLED,
                    default=stored_comfort.get("solar_gain_enabled", True),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="comfort", data_schema=schema)

    # -- Manual Override policy (v1.2.0-beta.1, T7) --

    async def async_step_manual_override(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Gate step (T21 Phase C2): a zone either defers its whole Manual
        Override policy to the System entry's default, or opts into its own
        (async_step_manual_override_custom, the unchanged T21 Phase B form).
        detection_tolerance stays zone-only regardless (hardware-noise
        property of this zone's own covers — see ownership analysis) and is
        always collected here.

        Migration: a zone with an explicit pre-C2 "release_strategy" key
        already stored starts out with its own settings preserved
        (use_system_default=False) so existing behavior is byte-for-byte
        unchanged; a zone that never configured this step starts out
        deferring to the System default (matching its previous behavior of
        implicitly using the hardcoded OverridePolicyConfig() defaults,
        which is also what an unconfigured System entry resolves to).
        """
        current = self._config_entry.data
        stored = current.get("override_policy") or {}
        stored_use_system_default = bool(
            stored.get("use_system_default", "release_strategy" not in stored)
        )

        if user_input is not None:
            use_system_default = bool(
                user_input.get(CONF_OVERRIDE_USE_SYSTEM_DEFAULT, stored_use_system_default)
            )
            detection_tolerance = _safe_positive_int(
                user_input.get(CONF_OVERRIDE_DETECTION_TOLERANCE), DEFAULT_OVERRIDE_DETECTION_TOLERANCE,
                maximum=OVERRIDE_DETECTION_TOLERANCE_MAX,
            )
            if use_system_default:
                new_policy = {**stored, "use_system_default": True, "detection_tolerance": detection_tolerance}
                return self._save_and_reload({"override_policy": new_policy})
            self._pending_override_detection_tolerance = detection_tolerance
            return await self.async_step_manual_override_custom()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_OVERRIDE_USE_SYSTEM_DEFAULT, default=stored_use_system_default,
                ): BooleanSelector(),
                vol.Required(
                    CONF_OVERRIDE_DETECTION_TOLERANCE,
                    default=stored.get("detection_tolerance", DEFAULT_OVERRIDE_DETECTION_TOLERANCE),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=OVERRIDE_DETECTION_TOLERANCE_MAX, step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="manual_override", data_schema=schema)

    # -- Manual Override policy form (Phase 5): shared by the zone's own policy
    # and the System entry's default.  See the module-level comment above
    # _OVERRIDE_POLICY_DEFAULTS for the page structure. --

    def _override_stored(self) -> dict[str, Any]:
        key = "system_override_policy" if self._override_scope == "system" else "override_policy"
        stored = self._config_entry.data.get(key)
        return dict(stored) if isinstance(stored, dict) else {}

    def _override_values(self) -> dict[str, Any]:
        """Stored policy overlaid with what this run of the pages collected so
        far; the pre-T10 keys are left out (release_strategy supersedes them)."""
        merged = {**self._override_stored(), **self._override_draft}
        if "release_strategy" not in merged:
            # Pre-T10 entry: what the loader makes of its old keys is what runs.
            merged["release_strategy"] = _override_policy_from_storage(merged).release_strategy.value
        return {k: v for k, v in merged.items() if k not in _SUPERSEDED_OVERRIDE_KEYS}

    def _override_begin(self, scope: str) -> None:
        self._override_scope = scope
        self._override_draft = {}
        self._override_queue = []

    async def _override_advance(self) -> config_entries.ConfigFlowResult:
        if self._override_queue:
            return await getattr(self, f"async_step_{self._override_queue.pop(0)}")()
        return self._override_finish()

    def _override_finish(self) -> config_entries.ConfigFlowResult:
        policy = {**_OVERRIDE_POLICY_DEFAULTS, **self._override_values()}
        if self._override_scope == "system":
            return self._save_system_default_and_reload({"system_override_policy": policy})
        tolerance = self._pending_override_detection_tolerance
        if tolerance is None:
            tolerance = _safe_positive_int(
                self._override_stored().get("detection_tolerance"), DEFAULT_OVERRIDE_DETECTION_TOLERANCE,
                maximum=OVERRIDE_DETECTION_TOLERANCE_MAX,
            )
        policy["use_system_default"] = False
        policy["detection_tolerance"] = tolerance
        return self._save_and_reload({"override_policy": policy})

    async def _override_choice_step(
        self, step_id: str, scope: str, user_input: dict[str, Any] | None
    ) -> config_entries.ConfigFlowResult:
        self._override_begin(scope)
        if user_input is None:
            return self.async_show_form(
                step_id=step_id, data_schema=_override_choice_schema(self._override_values()))
        choice = user_input.get(CONF_OVERRIDE_RELEASE_CHOICE)
        if choice not in RELEASE_CHOICES:
            choice = RELEASE_CHOICE_LIFECYCLE
        _, stored_filter = release_strategy_to_choice(
            _override_policy_from_storage(self._override_stored()).release_strategy)
        self._override_draft = {
            "release_strategy": release_choice_to_strategy(choice, stored_filter).value,
            "allow_comfort_actions": bool(user_input.get(CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS, False)),
            "allow_protection_actions": bool(user_input.get(CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS, False)),
        }
        detail = _OVERRIDE_DETAIL_STEP.get(choice)
        self._override_queue = [detail] if detail is not None else []
        if choice in _OVERRIDE_TIMEOUT_CHOICES and bool(user_input.get(CONF_OVERRIDE_EDIT_TIMEOUTS, False)):
            self._override_queue.append("override_timeout")
        return await self._override_advance()

    async def async_step_manual_override_custom(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Reached only when the gate step (above) is answered with "use my
        own settings"."""
        return await self._override_choice_step("manual_override_custom", "zone", user_input)

    # -- Manual Override policy — System entry default (T21 Phase C2) --

    async def async_step_system_manual_override(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return await self._override_choice_step("system_manual_override", "system", user_input)

    async def async_step_override_duration(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if self._override_scope is None:
            return await self.async_step_init()
        if user_input is not None:
            values = self._override_values()
            self._override_draft["duration_min"] = _override_minutes_default(
                user_input, CONF_OVERRIDE_DURATION_MIN,
                _override_minutes_default(values, "duration_min", DEFAULT_OVERRIDE_DURATION_MIN))
            self._override_draft["night_duration_min"] = _override_minutes_default(
                user_input, CONF_OVERRIDE_NIGHT_DURATION_MIN,
                _override_minutes_default(values, "night_duration_min", DEFAULT_OVERRIDE_NIGHT_DURATION_MIN))
            return await self._override_advance()
        return self.async_show_form(
            step_id="override_duration", data_schema=_override_duration_schema(self._override_values()))

    async def async_step_override_fixed_time(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if self._override_scope is None:
            return await self.async_step_init()
        errors: dict[str, str] = {}
        suggested = self._override_values().get("fixed_until")
        if user_input is not None:
            fixed_until = _parse_optional_time_input(user_input.get(CONF_OVERRIDE_FIXED_UNTIL))
            if fixed_until is None:
                errors["base"] = "override_fixed_until_required"
            else:
                self._override_draft["fixed_until"] = fixed_until.isoformat()
                return await self._override_advance()
        return self.async_show_form(
            step_id="override_fixed_time", data_schema=_override_fixed_time_schema(suggested), errors=errors)

    async def async_step_override_decision(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if self._override_scope is None:
            return await self.async_step_init()
        if user_input is not None:
            self._override_draft["release_strategy"] = release_choice_to_strategy(
                RELEASE_CHOICE_NEXT_DECISION, user_input.get(CONF_OVERRIDE_DECISION_FILTER)).value
            return await self._override_advance()
        return self.async_show_form(
            step_id="override_decision", data_schema=_override_decision_schema(self._override_values()))

    async def async_step_override_timeout(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if self._override_scope is None:
            return await self.async_step_init()
        if user_input is not None:
            values = self._override_values()
            self._override_draft["safety_timeout_enabled"] = bool(
                user_input.get(CONF_OVERRIDE_SAFETY_TIMEOUT_ENABLED, values.get("safety_timeout_enabled", True)))
            self._override_draft["duration_min"] = _override_minutes_default(
                user_input, CONF_OVERRIDE_DURATION_MIN,
                _override_minutes_default(values, "duration_min", DEFAULT_OVERRIDE_DURATION_MIN))
            self._override_draft["night_duration_min"] = _override_minutes_default(
                user_input, CONF_OVERRIDE_NIGHT_DURATION_MIN,
                _override_minutes_default(values, "night_duration_min", DEFAULT_OVERRIDE_NIGHT_DURATION_MIN))
            return await self._override_advance()
        return self.async_show_form(
            step_id="override_timeout", data_schema=_override_timeout_schema(self._override_values()))

    # -- Behavior / shade-position defaults --

    async def async_step_behavior(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._config_entry.data
        stored_shade = current.get("shade_position_defaults") or {}

        if user_input is not None:
            return self._save_and_reload(
                {
                    "shade_position_defaults": {
                        "light_shade_position": int(user_input[CONF_LIGHT_SHADE_POSITION]),
                        "normal_shade_position": int(user_input[CONF_NORMAL_SHADE_POSITION]),
                        "strong_shade_position": int(user_input[CONF_STRONG_SHADE_POSITION]),
                    }
                }
            )

        percent_selector = NumberSelector(
            NumberSelectorConfig(
                min=0, max=100, step=1,
                mode=NumberSelectorMode.SLIDER, unit_of_measurement="%",
            )
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_LIGHT_SHADE_POSITION,
                    default=stored_shade.get(
                        "light_shade_position", DEFAULT_LIGHT_SHADE_POSITION),
                ): percent_selector,
                vol.Required(
                    CONF_NORMAL_SHADE_POSITION,
                    default=stored_shade.get(
                        "normal_shade_position", DEFAULT_NORMAL_SHADE_POSITION),
                ): percent_selector,
                vol.Required(
                    CONF_STRONG_SHADE_POSITION,
                    default=stored_shade.get(
                        "strong_shade_position", DEFAULT_STRONG_SHADE_POSITION),
                ): percent_selector,
            }
        )
        return self.async_show_form(step_id="behavior", data_schema=schema)

    # -- Add Window (structural addition to this zone entry) --

    async def async_step_add_window(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            azimuth = _resolve_azimuth(user_input)
            if azimuth is None:
                errors["base"] = "invalid_custom_azimuth"
            else:
                raw_abs = user_input.get(CONF_ABSENCE_POSITION)
                self._add_window_pending = {
                    "id": f"window_{uuid.uuid4().hex}",
                    "name": user_input[CONF_WINDOW_NAME],
                    "floor_level": int(user_input[CONF_FLOOR_LEVEL]),
                    "azimuth": azimuth,
                    "absence_position": int(raw_abs) if raw_abs is not None else None,
                    "behavior_mode": user_input.get(CONF_WINDOW_BEHAVIOR_MODE, WindowBehaviorMode.FULLY_AUTOMATIC.value),
                }
                return await self.async_step_add_window_cover_group()

        schema = vol.Schema(
            {
                vol.Required(CONF_WINDOW_NAME): str,
                vol.Required(CONF_FLOOR_LEVEL, default=0): NumberSelector(
                    NumberSelectorConfig(min=-1, max=20, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_COMPASS_DIRECTION, default="south"): SelectSelector(
                    SelectSelectorConfig(
                        options=_COMPASS_OPTIONS,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="compass_direction",
                    )
                ),
                vol.Optional(CONF_CUSTOM_AZIMUTH): NumberSelector(
                    NumberSelectorConfig(min=0, max=359, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_ABSENCE_POSITION): NumberSelector(
                    NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%")
                ),
                vol.Required(CONF_WINDOW_BEHAVIOR_MODE, default=WindowBehaviorMode.FULLY_AUTOMATIC.value): SelectSelector(
                    SelectSelectorConfig(
                        options=WINDOW_BEHAVIOR_MODE_OPTIONS,
                        mode=SelectSelectorMode.LIST,
                        translation_key="window_behavior_mode",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="add_window", data_schema=schema, errors=errors)

    async def async_step_add_window_cover_group(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        assert self._add_window_pending is not None

        if user_input is not None:
            cover_entities: list[str] = user_input[CONF_COVER_ENTITIES]
            if not cover_entities:
                errors["base"] = "no_covers_selected"
            elif _covers_already_assigned(self.hass, cover_entities):
                errors["base"] = "cover_already_assigned"
            else:
                zone_id: str = self._config_entry.data["zones"][0]["id"]
                hw_type = cover_hardware_type_from_str(user_input.get(CONF_COVER_HARDWARE_TYPE))
                cover_group = CoverGroup(
                    id=f"cg_{uuid.uuid4().hex}",
                    window_id=self._add_window_pending["id"],
                    cover_ids=cover_entities,
                    sync_mode=CoverSyncMode.SYNCHRONOUS,
                    hardware_type=hw_type,
                )
                _bm_raw = self._add_window_pending.get("behavior_mode", WindowBehaviorMode.FULLY_AUTOMATIC.value)
                try:
                    _bm = WindowBehaviorMode(_bm_raw)
                except ValueError:
                    _bm = WindowBehaviorMode.FULLY_AUTOMATIC
                window = WindowConfig(
                    id=self._add_window_pending["id"],
                    name=self._add_window_pending["name"],
                    zone_id=zone_id,
                    azimuth=self._add_window_pending["azimuth"],
                    floor_level=self._add_window_pending["floor_level"],
                    cover_group_id=cover_group.id,
                    absence_position=self._add_window_pending.get("absence_position"),
                    behavior_mode=_bm,
                )
                self._add_cover_groups.append(cover_group)
                self._add_windows.append(window)
                self._add_window_pending = None
                return await self.async_step_add_window_loop()

        _hw_options = [t.value for t in CoverHardwareType]
        schema = vol.Schema(
            {
                vol.Required(CONF_COVER_ENTITIES): EntitySelector(
                    EntitySelectorConfig(domain="cover", multiple=True)
                ),
                vol.Required(
                    CONF_COVER_HARDWARE_TYPE,
                    default=CoverHardwareType.ROLLER_SHUTTER.value,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=_hw_options,
                        translation_key="cover_hardware_type",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="add_window_cover_group",
            data_schema=schema,
            errors=errors,
            description_placeholders={"window_name": self._add_window_pending["name"]},
        )

    async def async_step_add_window_loop(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            if user_input[CONF_ADD_ANOTHER_WINDOW]:
                return await self.async_step_add_window()
            return self._save_structure_and_reload()

        schema = vol.Schema({vol.Required(CONF_ADD_ANOTHER_WINDOW, default=False): BooleanSelector()})
        return self.async_show_form(
            step_id="add_window_loop",
            data_schema=schema,
            description_placeholders={"window_count": str(len(self._add_windows))},
        )

    # -- Edit Window --

    async def async_step_edit_window(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._config_entry.data
        windows = current.get("windows", [])

        if user_input is not None:
            self._edit_window_id = user_input[CONF_WINDOW_ID]
            return await self.async_step_edit_window_menu()

        window_options = [{"value": w["id"], "label": w["name"]} for w in windows]
        schema = vol.Schema({
            vol.Required(CONF_WINDOW_ID): SelectSelector(
                SelectSelectorConfig(options=window_options, mode=SelectSelectorMode.DROPDOWN)
            ),
        })
        return self.async_show_form(step_id="edit_window", data_schema=schema)

    # -- Edit Window: shared helpers ----------------------------------------
    def _get_edit_window(self) -> dict[str, Any] | None:
        return next(
            (w for w in self._config_entry.data.get("windows", [])
             if w["id"] == self._edit_window_id),
            None,
        )

    def _edit_window_cover_group(self) -> dict[str, Any] | None:
        return next(
            (cg for cg in self._config_entry.data.get("cover_groups", [])
             if cg.get("window_id") == self._edit_window_id),
            None,
        )

    def _merge_save_window(
        self, updates: dict[str, Any], cover_updates: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Merge a per-page slice into the stored window (and optionally its cover
        group) WITHOUT touching any other field, then persist + reload.

        Reading the current stored window fresh each time means a partial edit of
        one page never resets the values owned by the other pages.
        """
        windows = list(self._config_entry.data.get("windows", []))
        cover_groups = list(self._config_entry.data.get("cover_groups", []))
        for i, w in enumerate(windows):
            if w["id"] == self._edit_window_id:
                windows[i] = {**w, **updates}
                break
        if cover_updates is not None:
            for j, cg in enumerate(cover_groups):
                if cg.get("window_id") == self._edit_window_id:
                    cover_groups[j] = {**cg, **cover_updates}
                    break
        return self._save_and_reload({"windows": windows, "cover_groups": cover_groups})

    def _edit_window_effective_tol(self, window: dict, field: str) -> float:
        """Effective azimuth tolerance: Window → Zone → GlobalDefaults."""
        v = window.get(field)
        if v is not None:
            return float(v)
        zone_id = window.get("zone_id")
        if zone_id:
            zone = next((z for z in self._config_entry.data.get("zones", [])
                         if z["id"] == zone_id), None)
            if zone and zone.get(field) is not None:
                return float(zone[field])
        return float(getattr(GlobalDefaults(), field))

    # -- Edit Window: 4-page sub-menu ---------------------------------------
    async def async_step_edit_window_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        window = self._get_edit_window()
        if window is None:
            return self.async_abort(reason="window_not_found")
        return self.async_show_menu(
            step_id="edit_window_menu",
            menu_options=[
                "edit_window_basics",
                "edit_window_shading",
                "edit_window_solar",
                "edit_window_contact",
            ],
            description_placeholders={"window_name": window["name"]},
        )

    # -- Page 1: basics & cover ---------------------------------------------
    async def async_step_edit_window_basics(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        window = self._get_edit_window()
        if window is None:
            return self.async_abort(reason="window_not_found")
        errors: dict[str, str] = {}
        if user_input is not None:
            new_cover_ids = user_input.get(CONF_COVER_ENTITIES, [])
            new_azimuth = _resolve_azimuth(user_input)
            if not new_cover_ids:
                errors["base"] = "no_covers_selected"
            elif _covers_already_assigned(
                self.hass, new_cover_ids,
                own_key=(self._config_entry.entry_id, self._edit_window_id),
            ):
                errors["base"] = "cover_already_assigned"
            elif new_azimuth is None:
                errors["base"] = "invalid_custom_azimuth"
            else:
                hw_type = cover_hardware_type_from_str(user_input.get(CONF_COVER_HARDWARE_TYPE))
                return self._merge_save_window(
                    {
                        "name": user_input[CONF_WINDOW_NAME],
                        "floor_level": int(user_input.get(CONF_FLOOR_LEVEL, 0)),
                        "azimuth": new_azimuth,
                    },
                    {"cover_ids": new_cover_ids, "hardware_type": hw_type.value},
                )
        compass, custom_az = _compass_from_azimuth(window.get("azimuth", 180.0))
        cover_group = self._edit_window_cover_group()
        current_cover_ids = cover_group["cover_ids"] if cover_group else []
        current_hw_type = (cover_group.get("hardware_type", CoverHardwareType.ROLLER_SHUTTER.value)
                           if cover_group else CoverHardwareType.ROLLER_SHUTTER.value)
        schema = vol.Schema({
            vol.Required(CONF_WINDOW_NAME, default=window["name"]): str,
            vol.Required(CONF_FLOOR_LEVEL, default=window.get("floor_level", 0)): NumberSelector(
                NumberSelectorConfig(min=-1, max=20, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_COMPASS_DIRECTION, default=compass): SelectSelector(
                SelectSelectorConfig(options=_COMPASS_OPTIONS, mode=SelectSelectorMode.DROPDOWN, translation_key="compass_direction")
            ),
            vol.Optional(CONF_CUSTOM_AZIMUTH, description={"suggested_value": custom_az}): NumberSelector(
                NumberSelectorConfig(min=0, max=359, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_COVER_ENTITIES, default=current_cover_ids): EntitySelector(
                EntitySelectorConfig(domain="cover", multiple=True)
            ),
            vol.Required(CONF_COVER_HARDWARE_TYPE, default=current_hw_type): SelectSelector(
                SelectSelectorConfig(options=[t.value for t in CoverHardwareType], translation_key="cover_hardware_type", mode=SelectSelectorMode.LIST)
            ),
        })
        return self.async_show_form(
            step_id="edit_window_basics", data_schema=schema, errors=errors,
            description_placeholders={"window_name": window["name"]},
        )

    # -- Page 2: shading behaviour & positions ------------------------------
    async def async_step_edit_window_shading(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        window = self._get_edit_window()
        if window is None:
            return self.async_abort(reason="window_not_found")
        if user_input is not None:
            raw_abs = user_input.get(CONF_ABSENCE_POSITION)
            raw_night = user_input.get(CONF_NIGHT_POSITION)
            raw_light = user_input.get(CONF_LIGHT_SHADE_POSITION)
            raw_normal = user_input.get(CONF_NORMAL_SHADE_POSITION)
            raw_strong = user_input.get(CONF_STRONG_SHADE_POSITION)
            return self._merge_save_window({
                "behavior_mode": user_input.get(CONF_WINDOW_BEHAVIOR_MODE, WindowBehaviorMode.FULLY_AUTOMATIC.value),
                "absence_position": int(raw_abs) if raw_abs is not None else None,
                "night_position": int(raw_night) if raw_night is not None else None,
                "light_shade_position": int(raw_light) if raw_light is not None else None,
                "normal_shade_position": int(raw_normal) if raw_normal is not None else None,
                "strong_shade_position": int(raw_strong) if raw_strong is not None else None,
            })
        _pos_selector = NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%"))
        schema = vol.Schema({
            vol.Required(CONF_WINDOW_BEHAVIOR_MODE, default=window.get("behavior_mode", WindowBehaviorMode.FULLY_AUTOMATIC.value)): SelectSelector(
                SelectSelectorConfig(options=WINDOW_BEHAVIOR_MODE_OPTIONS, mode=SelectSelectorMode.LIST, translation_key="window_behavior_mode")
            ),
            vol.Optional(CONF_ABSENCE_POSITION, description={"suggested_value": window.get("absence_position")}): _pos_selector,
            vol.Optional(CONF_NIGHT_POSITION, description={"suggested_value": window.get("night_position")}): _pos_selector,
            vol.Optional(CONF_LIGHT_SHADE_POSITION, description={"suggested_value": window.get("light_shade_position")}): _pos_selector,
            vol.Optional(CONF_NORMAL_SHADE_POSITION, description={"suggested_value": window.get("normal_shade_position")}): _pos_selector,
            vol.Optional(CONF_STRONG_SHADE_POSITION, description={"suggested_value": window.get("strong_shade_position")}): _pos_selector,
        })
        return self.async_show_form(
            step_id="edit_window_shading", data_schema=schema,
            description_placeholders={"window_name": window["name"]},
        )

    # -- Page 3: manual sun sector & obstruction zones ----------------------
    async def async_step_edit_window_solar(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        window = self._get_edit_window()
        if window is None:
            return self.async_abort(reason="window_not_found")
        errors: dict[str, str] = {}
        _display_azimuth = window.get("azimuth", 180.0)
        if user_input is not None:
            _sector_enabled = user_input.get(CONF_MANUAL_SUN_SECTOR_ENABLED, False)
            raw_start = user_input.get(CONF_MANUAL_SUN_SECTOR_START_DEG)
            raw_end = user_input.get(CONF_MANUAL_SUN_SECTOR_END_DEG)
            _ex_start = window.get("manual_sun_sector_start_deg")
            _ex_end = window.get("manual_sun_sector_end_deg")
            if _sector_enabled:
                _c_start = (_display_azimuth - self._edit_window_effective_tol(window, "tolerance_start")) % 360.0
                _c_end = (_display_azimuth + self._edit_window_effective_tol(window, "tolerance_end")) % 360.0
                _sector_start = float(raw_start) if raw_start is not None else (_ex_start if _ex_start is not None else _c_start)
                _sector_end = float(raw_end) if raw_end is not None else (_ex_end if _ex_end is not None else _c_end)
            else:
                _sector_start, _sector_end = _ex_start, _ex_end

            def _oz_dict(en, sk, ek, fk, uk) -> dict | None:
                start = user_input.get(sk); end = user_input.get(ek)
                blk_from = user_input.get(fk); blk_until = user_input.get(uk)
                if start is None and end is None and blk_from is None and blk_until is None:
                    return None
                return {
                    "azimuth_start_deg": float(start) if start is not None else 0.0,
                    "azimuth_end_deg": float(end) if end is not None else 0.0,
                    "block_from_elevation_deg": float(blk_from) if blk_from is not None else None,
                    "block_until_elevation_deg": float(blk_until) if blk_until is not None else None,
                    "enabled": bool(user_input.get(en, False)),
                }
            _new_oz = [d for d in [
                _oz_dict(CONF_OBSTRUCTION_1_ENABLED, CONF_OBSTRUCTION_1_AZIMUTH_START, CONF_OBSTRUCTION_1_AZIMUTH_END, CONF_OBSTRUCTION_1_BLOCK_FROM_ELEVATION, CONF_OBSTRUCTION_1_BLOCK_UNTIL_ELEVATION),
                _oz_dict(CONF_OBSTRUCTION_2_ENABLED, CONF_OBSTRUCTION_2_AZIMUTH_START, CONF_OBSTRUCTION_2_AZIMUTH_END, CONF_OBSTRUCTION_2_BLOCK_FROM_ELEVATION, CONF_OBSTRUCTION_2_BLOCK_UNTIL_ELEVATION),
                _oz_dict(CONF_OBSTRUCTION_3_ENABLED, CONF_OBSTRUCTION_3_AZIMUTH_START, CONF_OBSTRUCTION_3_AZIMUTH_END, CONF_OBSTRUCTION_3_BLOCK_FROM_ELEVATION, CONF_OBSTRUCTION_3_BLOCK_UNTIL_ELEVATION),
            ] if d is not None]
            for _d in _new_oz:
                _f, _u = _d.get("block_from_elevation_deg"), _d.get("block_until_elevation_deg")
                if _f is not None and _u is not None and _f > _u:
                    errors["base"] = "obstruction_elevation_range_invalid"
                    break
            if not errors:
                return self._merge_save_window({
                    "manual_sun_sector_start_deg": _sector_start,
                    "manual_sun_sector_end_deg": _sector_end,
                    "obstruction_zones": _new_oz,
                })
        _az_selector = NumberSelector(NumberSelectorConfig(min=0, max=359, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="°"))
        _elev_selector = NumberSelector(NumberSelectorConfig(min=0, max=90, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="°"))
        _st_start = window.get("manual_sun_sector_start_deg")
        _st_end = window.get("manual_sun_sector_end_deg")
        _sector_on = _st_start is not None and _st_end is not None
        _sug_start = _st_start if _st_start is not None else (_display_azimuth - self._edit_window_effective_tol(window, "tolerance_start")) % 360.0
        _sug_end = _st_end if _st_end is not None else (_display_azimuth + self._edit_window_effective_tol(window, "tolerance_end")) % 360.0
        _stored_oz = (window.get("obstruction_zones") or [])[:3]

        def _oz_stored(idx, key, default=None):
            if idx >= len(_stored_oz):
                return default
            raw_oz = _stored_oz[idx]
            if key == "block_until_elevation_deg" and key not in raw_oz:
                return raw_oz.get("min_elevation_deg", default)
            return raw_oz.get(key, default)
        schema = vol.Schema({
            vol.Required(CONF_MANUAL_SUN_SECTOR_ENABLED, default=_sector_on): BooleanSelector(),
            vol.Optional(CONF_MANUAL_SUN_SECTOR_START_DEG, default=_sug_start): _az_selector,
            vol.Optional(CONF_MANUAL_SUN_SECTOR_END_DEG, default=_sug_end): _az_selector,
            vol.Required(CONF_OBSTRUCTION_1_ENABLED, default=_oz_stored(0, "enabled", False)): BooleanSelector(),
            vol.Optional(CONF_OBSTRUCTION_1_AZIMUTH_START, description={"suggested_value": _oz_stored(0, "azimuth_start_deg")}): _az_selector,
            vol.Optional(CONF_OBSTRUCTION_1_AZIMUTH_END, description={"suggested_value": _oz_stored(0, "azimuth_end_deg")}): _az_selector,
            vol.Optional(CONF_OBSTRUCTION_1_BLOCK_FROM_ELEVATION, description={"suggested_value": _oz_stored(0, "block_from_elevation_deg")}): _elev_selector,
            vol.Optional(CONF_OBSTRUCTION_1_BLOCK_UNTIL_ELEVATION, description={"suggested_value": _oz_stored(0, "block_until_elevation_deg")}): _elev_selector,
            vol.Required(CONF_OBSTRUCTION_2_ENABLED, default=_oz_stored(1, "enabled", False)): BooleanSelector(),
            vol.Optional(CONF_OBSTRUCTION_2_AZIMUTH_START, description={"suggested_value": _oz_stored(1, "azimuth_start_deg")}): _az_selector,
            vol.Optional(CONF_OBSTRUCTION_2_AZIMUTH_END, description={"suggested_value": _oz_stored(1, "azimuth_end_deg")}): _az_selector,
            vol.Optional(CONF_OBSTRUCTION_2_BLOCK_FROM_ELEVATION, description={"suggested_value": _oz_stored(1, "block_from_elevation_deg")}): _elev_selector,
            vol.Optional(CONF_OBSTRUCTION_2_BLOCK_UNTIL_ELEVATION, description={"suggested_value": _oz_stored(1, "block_until_elevation_deg")}): _elev_selector,
            vol.Required(CONF_OBSTRUCTION_3_ENABLED, default=_oz_stored(2, "enabled", False)): BooleanSelector(),
            vol.Optional(CONF_OBSTRUCTION_3_AZIMUTH_START, description={"suggested_value": _oz_stored(2, "azimuth_start_deg")}): _az_selector,
            vol.Optional(CONF_OBSTRUCTION_3_AZIMUTH_END, description={"suggested_value": _oz_stored(2, "azimuth_end_deg")}): _az_selector,
            vol.Optional(CONF_OBSTRUCTION_3_BLOCK_FROM_ELEVATION, description={"suggested_value": _oz_stored(2, "block_from_elevation_deg")}): _elev_selector,
            vol.Optional(CONF_OBSTRUCTION_3_BLOCK_UNTIL_ELEVATION, description={"suggested_value": _oz_stored(2, "block_until_elevation_deg")}): _elev_selector,
        })
        return self.async_show_form(
            step_id="edit_window_solar", data_schema=schema, errors=errors,
            description_placeholders={"window_name": window["name"]},
        )

    # -- Page 4: window contact(s) & night ventilation ----------------------
    async def async_step_edit_window_contact(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        window = self._get_edit_window()
        if window is None:
            return self.async_abort(reason="window_not_found")
        errors: dict[str, str] = {}
        if user_input is not None:
            _raw = user_input.get(CONF_CONTACT_SENSOR_ENTITY_IDS)
            if _raw is None:  # tolerate the legacy single-contact key
                _raw = user_input.get(CONF_CONTACT_SENSOR_ENTITY_ID)
            if isinstance(_raw, str):
                _contacts = [_raw] if _raw else []
            else:
                _contacts = [e for e in (_raw or []) if e]
            _contacts = list(dict.fromkeys(_contacts))
            _night_block = bool(user_input.get(CONF_NIGHT_BLOCK_ON_WINDOW_OPEN, False))
            _night_lift = bool(user_input.get(CONF_NIGHT_LIFT_ON_WINDOW_OPEN, False))
            raw_vent = user_input.get(CONF_WINDOW_OPEN_NIGHT_POSITION)
            # Option A (block) and Option B (ventilation) are independent: each
            # may be enabled on its own.  Both require a configured contact.
            if _night_block and not _contacts:
                errors[CONF_CONTACT_SENSOR_ENTITY_IDS] = "contact_sensor_required_for_block"
            if _night_lift and not _contacts:
                errors[CONF_CONTACT_SENSOR_ENTITY_IDS] = "contact_sensor_required_for_lift"
            if not errors:
                return self._merge_save_window({
                    "contact_sensor_entity_ids": _contacts,
                    "contact_sensor_entity_id": _contacts[0] if _contacts else None,
                    "night_block_on_window_open": _night_block,
                    "night_lift_on_window_open": _night_lift,
                    "window_open_night_position_ha": int(raw_vent) if raw_vent is not None else DEFAULT_WINDOW_OPEN_NIGHT_POSITION_HA,
                })
        _raw_stored = window.get("contact_sensor_entity_ids")
        if _raw_stored is None:
            _raw_stored = window.get("contact_sensor_entity_id")
        if isinstance(_raw_stored, str):
            _stored_contacts = [_raw_stored] if _raw_stored else []
        else:
            _stored_contacts = [e for e in (_raw_stored or []) if e]
        schema = vol.Schema({
            vol.Optional(CONF_CONTACT_SENSOR_ENTITY_IDS, description={"suggested_value": _stored_contacts}): EntitySelector(
                EntitySelectorConfig(domain="binary_sensor", device_class=["window", "door", "opening", "garage_door"], multiple=True)
            ),
            vol.Required(CONF_NIGHT_BLOCK_ON_WINDOW_OPEN, default=window.get("night_block_on_window_open", False)): BooleanSelector(),
            vol.Required(CONF_NIGHT_LIFT_ON_WINDOW_OPEN, default=window.get("night_lift_on_window_open", False)): BooleanSelector(),
            vol.Optional(CONF_WINDOW_OPEN_NIGHT_POSITION, description={"suggested_value": window.get("window_open_night_position_ha", DEFAULT_WINDOW_OPEN_NIGHT_POSITION_HA)}): NumberSelector(
                NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%")
            ),
        })
        return self.async_show_form(
            step_id="edit_window_contact", data_schema=schema, errors=errors,
            description_placeholders={"window_name": window["name"]},
        )

    # -- Remove Window --

    async def async_step_remove_window(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._config_entry.data
        windows = current.get("windows", [])
        errors: dict[str, str] = {}

        if user_input is not None:
            self._edit_window_id = user_input[CONF_WINDOW_ID]
            if len(windows) <= 1:
                errors["base"] = "cannot_remove_last_window"
            else:
                return await self.async_step_remove_window_confirm()

        if not errors and len(windows) <= 1:
            errors["base"] = "cannot_remove_last_window"

        window_options = [{"value": w["id"], "label": w["name"]} for w in windows]
        schema = vol.Schema({
            vol.Required(CONF_WINDOW_ID): SelectSelector(
                SelectSelectorConfig(options=window_options, mode=SelectSelectorMode.DROPDOWN)
            ),
        })
        return self.async_show_form(step_id="remove_window", data_schema=schema, errors=errors)

    async def async_step_remove_window_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._config_entry.data
        window_id = self._edit_window_id
        window = next((w for w in current.get("windows", []) if w["id"] == window_id), None)
        if window is None:
            return self.async_abort(reason="window_not_found")

        if user_input is not None:
            if user_input.get(CONF_REMOVE_CONFIRMED):
                windows = [w for w in current.get("windows", []) if w["id"] != window_id]
                cover_groups = [cg for cg in current.get("cover_groups", []) if cg.get("window_id") != window_id]
                return self._save_and_reload({"windows": windows, "cover_groups": cover_groups})
            # User declined: close flow without changes, preserve options.
            return self.async_create_entry(data={**self._config_entry.options})

        schema = vol.Schema({
            vol.Required(CONF_REMOVE_CONFIRMED, default=False): BooleanSelector(),
        })
        return self.async_show_form(
            step_id="remove_window_confirm",
            data_schema=schema,
            description_placeholders={"window_name": window["name"]},
        )

    # -- Common save helpers --

    def _save_and_reload(
        self, updates: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Merge updates into config_entry.data, schedule a reload, close flow.

        Passing the current options dict preserves zone_controls and any other
        options that OptionsFlow does not own (e.g. debug_logging).
        async_create_entry(data={}) would wipe them entirely.

        T21 Phase C3: also migrates away the removed Lifecycle Profile
        storage keys ("lifecycle_profiles"/"active_lifecycle_profile_id") if
        a pre-C3 install still carries them — the config_entry_data.py's
        _resolve_lifecycle_config_dict() rule (a set active_lifecycle_profile_id
        wins over the flat lifecycle_config) is applied ONE LAST TIME here to
        materialize whichever config was actually in effect into the
        "lifecycle_config" key that survives, THEN the two profile keys are
        dropped — the next time any step saves. This must happen together
        (materialize-then-drop), not just drop: dropping the keys alone
        while a non-default profile was active would silently revert
        behavior to the (possibly different) flat legacy config.
        """
        merged = {**self._config_entry.data, **updates}
        if "lifecycle_config" not in updates and (
            "active_lifecycle_profile_id" in merged or "lifecycle_profiles" in merged
        ):
            resolved = _resolve_lifecycle_config_dict(merged)
            if resolved is not None:
                merged["lifecycle_config"] = resolved
        new_data = {
            k: v for k, v in merged.items()
            if k not in (
                "lifecycle_profiles", "active_lifecycle_profile_id",
                "ema_enabled", "ema_alpha",
            )
        }
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self._config_entry.entry_id)
        )
        return self.async_create_entry(data={**self._config_entry.options})

    def _save_system_default_and_reload(
        self, updates: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Like _save_and_reload, but also reloads every zone entry so a
        changed System-entry global default (T21 Phase C2) takes effect
        immediately for every zone still deferring to it, instead of only on
        that zone's own next reload."""
        new_data = {**self._config_entry.data, **updates}
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id == self._config_entry.entry_id:
                continue
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SYSTEM:
                continue
            self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self._config_entry.entry_id)
        )
        return self.async_create_entry(data={**self._config_entry.options})

    def _save_structure_and_reload(self) -> config_entries.ConfigFlowResult:
        """Merge newly collected windows/cover_groups into this entry's data and reload."""
        current_data = from_storage_dict(self._config_entry.data)
        current_data.windows.extend(self._add_windows)
        current_data.cover_groups.extend(self._add_cover_groups)
        self.hass.config_entries.async_update_entry(
            self._config_entry,
            data=to_storage_dict(current_data),
        )
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self._config_entry.entry_id)
        )
        return self.async_create_entry(data={**self._config_entry.options})
