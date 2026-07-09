"""Config Flow and Options Flow for SmartShading (ARCHITECTURE.md §7).

Architecture: one config entry per zone.  Each "Eintrag hinzufügen" click
creates a new SmartShading entry for one zone.  Multiple entries are allowed
and appear as separate items under "SmartShading" in the HA integrations UI.

Config Flow steps (per zone setup):
  1. async_step_user              zone name, HA location flag
  2. async_step_weather           optional weather entity + dedicated sensors
  3. async_step_lifecycle         night/morning trigger type selection
  3b. async_step_lifecycle_detail conditional time/elevation fields
  4. async_step_presence          presence entities + absence_delay_min
  5. async_step_window            window name, floor level, azimuth
  6. async_step_cover_group       assign covers to a new CoverGroup for the window
  7. async_step_add_another_window loop or continue to comfort
  8. async_step_comfort           heat/glare/solar-gain toggles + indoor temp sensor
     Shade-position defaults (40/25/10 %) are applied automatically on finish.
  Entry title = zone name.  No single-instance guard.

Options Flow (post-setup editing via a section menu):
  weather               weather entity + sensor entity IDs
  lifecycle             night/morning trigger type selection
  lifecycle_detail      conditional time/elevation fields
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

from .config_entry_data import SmartShadingConfigEntryData, from_storage_dict, to_storage_dict
from .models.comfort import ComfortConfig
from .models.config import GlobalDefaults, ShadePositionDefaults
from .models.cover_group import CoverGroup, CoverHardwareType, CoverSyncMode, cover_hardware_type_from_str
from .models.lifecycle import LifecycleScheduleMode, MorningTrigger, NightDayLifecycleConfig, NightTrigger
from .models.window import WindowBehaviorMode, WindowConfig
from .models.zone import ZoneConfig
from .const import (
    COMPASS_AZIMUTHS,
    CONF_ABSENCE_DELAY_MIN,
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
    DEFAULT_SOLAR_GAIN_MAX_OUTDOOR_TEMP_C,
    CONF_GLARE_MIN_EXPOSURE_WM2,
    DEFAULT_GLARE_MIN_EXPOSURE_WM2,
    GLARE_MIN_EXPOSURE_MAX_WM2,
    DEFAULT_WEEKDAY_MORNING_FIXED_TIME,
    DEFAULT_WEEKDAY_NIGHT_FIXED_TIME,
    DEFAULT_WEEKEND_MORNING_FIXED_TIME,
    DEFAULT_WEEKEND_NIGHT_FIXED_TIME,
    CONF_SCHEDULE_MODE,
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


class SmartShadingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
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
        self._morning_trigger: MorningTrigger = MorningTrigger(DEFAULT_MORNING_TRIGGER)
        self._morning_fixed_time: time = time.fromisoformat(DEFAULT_MORNING_FIXED_TIME)
        self._morning_sun_elevation: float = DEFAULT_MORNING_SUN_ELEVATION
        self._morning_position: int = DEFAULT_MORNING_POSITION
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
        # Stable zone ID for this setup run (UUID4, generated once — all windows
        # in this flow share the single zone, so the ID must not change between
        # async_step_cover_group() calls and _async_finish()).
        self._default_zone_id: str = f"zone_{uuid.uuid4().hex}"
        # Comfort settings (Comfort Engine phase, 2026-06-17)
        self._indoor_temperature_sensor_ids: list[str] = []
        self._heat_protection_enabled: bool = True
        self._glare_protection_enabled: bool = True
        self._solar_gain_enabled: bool = True
        self._glare_min_exposure_wm2: float = DEFAULT_GLARE_MIN_EXPOSURE_WM2
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
            return await self.async_step_lifecycle_detail()

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

    # -- Step 3b: conditional time / elevation fields (branches on schedule_mode) --

    async def async_step_lifecycle_detail(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        is_weekday_weekend = self._schedule_mode is LifecycleScheduleMode.WEEKDAY_WEEKEND

        if user_input is not None:
            if is_weekday_weekend:
                # Parse shared elevation fields (elevation thresholds don't vary by day of week)
                if self._night_trigger in {NightTrigger.SUN_ELEVATION, NightTrigger.BOTH}:
                    if CONF_NIGHT_ELEVATION_PRESET in user_input:
                        self._night_sun_elevation = _resolve_elevation(
                            user_input,
                            CONF_NIGHT_ELEVATION_PRESET,
                            CONF_NIGHT_SUN_ELEVATION,
                            NIGHT_ELEVATION_PRESETS,
                            self._night_sun_elevation,
                        )
                if self._morning_trigger in {MorningTrigger.SUN_ELEVATION, MorningTrigger.BOTH}:
                    if CONF_MORNING_ELEVATION_PRESET in user_input:
                        self._morning_sun_elevation = _resolve_elevation(
                            user_input,
                            CONF_MORNING_ELEVATION_PRESET,
                            CONF_MORNING_SUN_ELEVATION,
                            MORNING_ELEVATION_PRESETS,
                            self._morning_sun_elevation,
                        )
                # Parse weekday profile
                self._weekday_night_fixed_time = _parse_time_input(
                    user_input.get(CONF_WEEKDAY_NIGHT_FIXED_TIME), DEFAULT_WEEKDAY_NIGHT_FIXED_TIME
                )
                self._weekday_night_position = int(
                    user_input.get(CONF_WEEKDAY_NIGHT_POSITION, DEFAULT_NIGHT_POSITION)
                )
                self._weekday_morning_fixed_time = _parse_time_input(
                    user_input.get(CONF_WEEKDAY_MORNING_FIXED_TIME), DEFAULT_WEEKDAY_MORNING_FIXED_TIME
                )
                self._weekday_morning_position = int(
                    user_input.get(CONF_WEEKDAY_MORNING_POSITION, DEFAULT_MORNING_POSITION)
                )
                # Parse weekend profile
                self._weekend_night_fixed_time = _parse_time_input(
                    user_input.get(CONF_WEEKEND_NIGHT_FIXED_TIME), DEFAULT_WEEKEND_NIGHT_FIXED_TIME
                )
                self._weekend_night_position = int(
                    user_input.get(CONF_WEEKEND_NIGHT_POSITION, DEFAULT_NIGHT_POSITION)
                )
                self._weekend_morning_fixed_time = _parse_time_input(
                    user_input.get(CONF_WEEKEND_MORNING_FIXED_TIME), DEFAULT_WEEKEND_MORNING_FIXED_TIME
                )
                self._weekend_morning_position = int(
                    user_input.get(CONF_WEEKEND_MORNING_POSITION, DEFAULT_MORNING_POSITION)
                )
            else:
                if CONF_NIGHT_FIXED_TIME in user_input:
                    self._night_fixed_time = _parse_time_input(
                        user_input[CONF_NIGHT_FIXED_TIME], DEFAULT_NIGHT_FIXED_TIME
                    )
                if CONF_NIGHT_ELEVATION_PRESET in user_input:
                    self._night_sun_elevation = _resolve_elevation(
                        user_input,
                        CONF_NIGHT_ELEVATION_PRESET,
                        CONF_NIGHT_SUN_ELEVATION,
                        NIGHT_ELEVATION_PRESETS,
                        self._night_sun_elevation,
                    )
                if CONF_MORNING_FIXED_TIME in user_input:
                    self._morning_fixed_time = _parse_time_input(
                        user_input[CONF_MORNING_FIXED_TIME], DEFAULT_MORNING_FIXED_TIME
                    )
                if CONF_MORNING_ELEVATION_PRESET in user_input:
                    self._morning_sun_elevation = _resolve_elevation(
                        user_input,
                        CONF_MORNING_ELEVATION_PRESET,
                        CONF_MORNING_SUN_ELEVATION,
                        MORNING_ELEVATION_PRESETS,
                        self._morning_sun_elevation,
                    )
                self._night_position = int(user_input.get(CONF_NIGHT_POSITION, DEFAULT_NIGHT_POSITION))
                self._morning_position = int(user_input.get(CONF_MORNING_POSITION, DEFAULT_MORNING_POSITION))
            return await self.async_step_presence()

        position_selector = NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%")
        )

        night_preset_selector = SelectSelector(
            SelectSelectorConfig(
                options=[*NIGHT_ELEVATION_PRESETS.keys(), ELEVATION_PRESET_CUSTOM],
                mode=SelectSelectorMode.LIST,
                translation_key="night_elevation_preset",
            )
        )
        morning_preset_selector = SelectSelector(
            SelectSelectorConfig(
                options=[*MORNING_ELEVATION_PRESETS.keys(), ELEVATION_PRESET_CUSTOM],
                mode=SelectSelectorMode.LIST,
                translation_key="morning_elevation_preset",
            )
        )
        custom_elevation_selector = NumberSelector(
            NumberSelectorConfig(min=-90, max=90, step=0.5, mode=NumberSelectorMode.BOX)
        )

        if is_weekday_weekend:
            schema_dict: dict[Any, Any] = {}
            # Shared elevation fields: elevation thresholds are the same for all days
            # of the week (they are a physical property of the sky, not a schedule).
            if self._night_trigger in {NightTrigger.SUN_ELEVATION, NightTrigger.BOTH}:
                night_preset_default = _elevation_to_preset(self._night_sun_elevation, NIGHT_ELEVATION_PRESETS)
                schema_dict[vol.Required(CONF_NIGHT_ELEVATION_PRESET, default=night_preset_default)] = night_preset_selector
                schema_dict[vol.Optional(CONF_NIGHT_SUN_ELEVATION, default=self._night_sun_elevation)] = custom_elevation_selector
            if self._morning_trigger in {MorningTrigger.SUN_ELEVATION, MorningTrigger.BOTH}:
                morning_preset_default = _elevation_to_preset(self._morning_sun_elevation, MORNING_ELEVATION_PRESETS)
                schema_dict[vol.Required(CONF_MORNING_ELEVATION_PRESET, default=morning_preset_default)] = morning_preset_selector
                schema_dict[vol.Optional(CONF_MORNING_SUN_ELEVATION, default=self._morning_sun_elevation)] = custom_elevation_selector
            # Weekday section: per-day time + position
            if self._night_trigger in {NightTrigger.FIXED_TIME, NightTrigger.BOTH}:
                schema_dict[
                    vol.Required(CONF_WEEKDAY_NIGHT_FIXED_TIME, default=DEFAULT_WEEKDAY_NIGHT_FIXED_TIME)
                ] = TimeSelector()
            schema_dict[
                vol.Required(CONF_WEEKDAY_NIGHT_POSITION, default=self._weekday_night_position)
            ] = position_selector
            if self._morning_trigger in {MorningTrigger.FIXED_TIME, MorningTrigger.BOTH}:
                schema_dict[
                    vol.Required(CONF_WEEKDAY_MORNING_FIXED_TIME, default=DEFAULT_WEEKDAY_MORNING_FIXED_TIME)
                ] = TimeSelector()
            schema_dict[
                vol.Required(CONF_WEEKDAY_MORNING_POSITION, default=self._weekday_morning_position)
            ] = position_selector
            # Weekend section: per-day time + position (elevation reused from above)
            if self._night_trigger in {NightTrigger.FIXED_TIME, NightTrigger.BOTH}:
                schema_dict[
                    vol.Required(CONF_WEEKEND_NIGHT_FIXED_TIME, default=DEFAULT_WEEKEND_NIGHT_FIXED_TIME)
                ] = TimeSelector()
            schema_dict[
                vol.Required(CONF_WEEKEND_NIGHT_POSITION, default=self._weekend_night_position)
            ] = position_selector
            if self._morning_trigger in {MorningTrigger.FIXED_TIME, MorningTrigger.BOTH}:
                schema_dict[
                    vol.Required(CONF_WEEKEND_MORNING_FIXED_TIME, default=DEFAULT_WEEKEND_MORNING_FIXED_TIME)
                ] = TimeSelector()
            schema_dict[
                vol.Required(CONF_WEEKEND_MORNING_POSITION, default=self._weekend_morning_position)
            ] = position_selector
            return self.async_show_form(step_id="lifecycle_detail", data_schema=vol.Schema(schema_dict))

        # SAME_EVERY_DAY: elevation selectors already built above; reuse them.
        schema_dict = {}
        if self._night_trigger in {NightTrigger.FIXED_TIME, NightTrigger.BOTH}:
            schema_dict[vol.Required(CONF_NIGHT_FIXED_TIME, default=DEFAULT_NIGHT_FIXED_TIME)] = TimeSelector()
        if self._night_trigger in {NightTrigger.SUN_ELEVATION, NightTrigger.BOTH}:
            night_preset_default = _elevation_to_preset(self._night_sun_elevation, NIGHT_ELEVATION_PRESETS)
            schema_dict[vol.Required(CONF_NIGHT_ELEVATION_PRESET, default=night_preset_default)] = night_preset_selector
            schema_dict[vol.Optional(CONF_NIGHT_SUN_ELEVATION, default=self._night_sun_elevation)] = custom_elevation_selector
        schema_dict[vol.Required(CONF_NIGHT_POSITION, default=self._night_position)] = position_selector
        if self._morning_trigger in {MorningTrigger.FIXED_TIME, MorningTrigger.BOTH}:
            schema_dict[vol.Required(CONF_MORNING_FIXED_TIME, default=DEFAULT_MORNING_FIXED_TIME)] = TimeSelector()
        if self._morning_trigger in {MorningTrigger.SUN_ELEVATION, MorningTrigger.BOTH}:
            morning_preset_default = _elevation_to_preset(self._morning_sun_elevation, MORNING_ELEVATION_PRESETS)
            schema_dict[vol.Required(CONF_MORNING_ELEVATION_PRESET, default=morning_preset_default)] = morning_preset_selector
            schema_dict[vol.Optional(CONF_MORNING_SUN_ELEVATION, default=self._morning_sun_elevation)] = custom_elevation_selector
        schema_dict[vol.Required(CONF_MORNING_POSITION, default=self._morning_position)] = position_selector
        return self.async_show_form(step_id="lifecycle_detail", data_schema=vol.Schema(schema_dict))

    # -- Step 4: presence / absence settings --

    async def async_step_presence(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._presence_entity_ids = user_input.get(CONF_PRESENCE_ENTITY_IDS, [])
            self._absence_delay_min = int(user_input[CONF_ABSENCE_DELAY_MIN])
            raw_pos = user_input.get(CONF_ABSENCE_POSITION)
            self._absence_position = int(raw_pos) if raw_pos is not None else None
            return await self.async_step_window()

        _prefill = _first_zone_entry_data(self.hass) or {}
        _prefill_presence = _prefill.get(CONF_PRESENCE_ENTITY_IDS, [])
        _prefill_delay = _prefill.get(CONF_ABSENCE_DELAY_MIN, DEFAULT_ABSENCE_DELAY_MIN)
        _prefill_pos = next(
            (z.get("absence_position") for z in _prefill.get("zones", []) if z.get("absence_position") is not None),
            DEFAULT_ABSENCE_POSITION,
        )

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
            }
        )
        return self.async_show_form(step_id="presence", data_schema=schema)

    # -- Step 3: comfort settings (indoor temperature + protection toggles) --

    async def async_step_comfort(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        # Prefill from first existing zone entry
        _prefill = _first_zone_entry_data(self.hass)
        errors: dict[str, str] = {}

        if user_input is not None:
            _raw_glare = user_input.get(CONF_GLARE_MIN_EXPOSURE_WM2)
            try:
                _glare_min = float(_raw_glare)
                if not (0.0 <= _glare_min <= GLARE_MIN_EXPOSURE_MAX_WM2):
                    raise ValueError
            except (TypeError, ValueError):
                errors["base"] = "invalid_glare_min_exposure"
            if not errors:
                self._indoor_temperature_sensor_ids = user_input.get(CONF_INDOOR_TEMPERATURE_SENSOR_IDS) or []
                self._heat_protection_enabled = bool(user_input[CONF_HEAT_PROTECTION_ENABLED])
                self._glare_protection_enabled = bool(user_input[CONF_GLARE_PROTECTION_ENABLED])
                self._solar_gain_enabled = bool(user_input[CONF_SOLAR_GAIN_ENABLED])
                self._glare_min_exposure_wm2 = _glare_min
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
                vol.Required(
                    CONF_GLARE_MIN_EXPOSURE_WM2,
                    default=_prefill_comfort.get("glare_min_exposure_wm2", DEFAULT_GLARE_MIN_EXPOSURE_WM2),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=GLARE_MIN_EXPOSURE_MAX_WM2, step=5,
                        mode=NumberSelectorMode.BOX, unit_of_measurement="W/m²",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="comfort", data_schema=schema, errors=errors)

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
                glare_min_exposure_wm2=self._glare_min_exposure_wm2,
            ),
        )
        return self.async_create_entry(title=self._zone_name, data=to_storage_dict(entry_data))


# ---------------------------------------------------------------------------
# Options Flow
# ---------------------------------------------------------------------------

class SmartShadingOptionsFlow(config_entries.OptionsFlow):
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
        # Add Window flow state (Options path for structural additions)
        self._add_window_pending: dict[str, Any] | None = None
        self._add_windows: list[WindowConfig] = []
        self._add_cover_groups: list[CoverGroup] = []
        # Edit/Remove Window flow state
        self._edit_window_id: str | None = None

    # -- Init: section menu (zone entries only) --

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if self._config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SYSTEM:
            return self.async_abort(reason="no_options_for_system_entry")
        return self.async_show_menu(
            step_id="init",
            menu_options=["weather", "lifecycle", "presence", "comfort", "behavior", "add_window", "edit_window", "remove_window"],
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
            return await self.async_step_lifecycle_detail()

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

    # -- Schedule (lifecycle) — step 2: conditional time / elevation fields --

    async def async_step_lifecycle_detail(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        stored_lc = (self._config_entry.data.get("lifecycle_config") or {})
        pending_night = self._pending_night_trigger or NightTrigger(
            stored_lc.get("night_trigger", DEFAULT_NIGHT_TRIGGER)
        )
        pending_morning = self._pending_morning_trigger or MorningTrigger(
            stored_lc.get("morning_trigger", DEFAULT_MORNING_TRIGGER)
        )
        try:
            active_schedule_mode = self._pending_schedule_mode or LifecycleScheduleMode(
                stored_lc.get("schedule_mode", LifecycleScheduleMode.SAME_EVERY_DAY.value)
            )
        except ValueError:
            active_schedule_mode = LifecycleScheduleMode.SAME_EVERY_DAY
        is_weekday_weekend = active_schedule_mode is LifecycleScheduleMode.WEEKDAY_WEEKEND

        position_selector = NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%")
        )

        if user_input is not None:
            if is_weekday_weekend:
                # Parse shared elevation fields (elevation thresholds do not vary by day of week)
                if pending_night in {NightTrigger.SUN_ELEVATION, NightTrigger.BOTH}:
                    if CONF_NIGHT_ELEVATION_PRESET in user_input:
                        night_sun_elevation_ww = _resolve_elevation(
                            user_input,
                            CONF_NIGHT_ELEVATION_PRESET,
                            CONF_NIGHT_SUN_ELEVATION,
                            NIGHT_ELEVATION_PRESETS,
                            stored_lc.get("night_sun_elevation_deg", DEFAULT_NIGHT_SUN_ELEVATION),
                        )
                    else:
                        night_sun_elevation_ww = stored_lc.get("night_sun_elevation_deg", DEFAULT_NIGHT_SUN_ELEVATION)
                else:
                    night_sun_elevation_ww = stored_lc.get("night_sun_elevation_deg", DEFAULT_NIGHT_SUN_ELEVATION)
                if pending_morning in {MorningTrigger.SUN_ELEVATION, MorningTrigger.BOTH}:
                    if CONF_MORNING_ELEVATION_PRESET in user_input:
                        morning_sun_elevation_ww = _resolve_elevation(
                            user_input,
                            CONF_MORNING_ELEVATION_PRESET,
                            CONF_MORNING_SUN_ELEVATION,
                            MORNING_ELEVATION_PRESETS,
                            stored_lc.get("morning_sun_elevation_deg", DEFAULT_MORNING_SUN_ELEVATION),
                        )
                    else:
                        morning_sun_elevation_ww = stored_lc.get("morning_sun_elevation_deg", DEFAULT_MORNING_SUN_ELEVATION)
                else:
                    morning_sun_elevation_ww = stored_lc.get("morning_sun_elevation_deg", DEFAULT_MORNING_SUN_ELEVATION)
                # When switching TO weekday_weekend, pre-fill weekday from the shared
                # values so the user has a sensible starting point.
                weekday_night_time = _parse_time_input(
                    user_input.get(CONF_WEEKDAY_NIGHT_FIXED_TIME),
                    stored_lc.get("weekday_night_fixed_time") or stored_lc.get("night_fixed_time", DEFAULT_WEEKDAY_NIGHT_FIXED_TIME),
                )
                weekday_morning_time = _parse_time_input(
                    user_input.get(CONF_WEEKDAY_MORNING_FIXED_TIME),
                    stored_lc.get("weekday_morning_fixed_time") or stored_lc.get("morning_fixed_time", DEFAULT_WEEKDAY_MORNING_FIXED_TIME),
                )
                weekend_night_time = _parse_time_input(
                    user_input.get(CONF_WEEKEND_NIGHT_FIXED_TIME),
                    stored_lc.get("weekend_night_fixed_time", DEFAULT_WEEKEND_NIGHT_FIXED_TIME),
                )
                weekend_morning_time = _parse_time_input(
                    user_input.get(CONF_WEEKEND_MORNING_FIXED_TIME),
                    stored_lc.get("weekend_morning_fixed_time", DEFAULT_WEEKEND_MORNING_FIXED_TIME),
                )
                new_lc = {
                    **stored_lc,
                    "night_trigger": pending_night.value,
                    "morning_trigger": pending_morning.value,
                    "schedule_mode": active_schedule_mode.value,
                    "night_sun_elevation_deg": night_sun_elevation_ww,
                    "morning_sun_elevation_deg": morning_sun_elevation_ww,
                    "weekday_night_fixed_time": weekday_night_time.isoformat(),
                    "weekday_night_position": int(user_input.get(CONF_WEEKDAY_NIGHT_POSITION, stored_lc.get("weekday_night_position", DEFAULT_NIGHT_POSITION))),
                    "weekday_morning_fixed_time": weekday_morning_time.isoformat(),
                    "weekday_morning_position": int(user_input.get(CONF_WEEKDAY_MORNING_POSITION, stored_lc.get("weekday_morning_position", DEFAULT_MORNING_POSITION))),
                    "weekend_night_fixed_time": weekend_night_time.isoformat(),
                    "weekend_night_position": int(user_input.get(CONF_WEEKEND_NIGHT_POSITION, stored_lc.get("weekend_night_position", DEFAULT_NIGHT_POSITION))),
                    "weekend_morning_fixed_time": weekend_morning_time.isoformat(),
                    "weekend_morning_position": int(user_input.get(CONF_WEEKEND_MORNING_POSITION, stored_lc.get("weekend_morning_position", DEFAULT_MORNING_POSITION))),
                }
                return self._save_and_reload({"lifecycle_config": new_lc})

            # SAME_EVERY_DAY save path
            night_fixed_time = _parse_time_input(
                user_input.get(CONF_NIGHT_FIXED_TIME),
                stored_lc.get("night_fixed_time", DEFAULT_NIGHT_FIXED_TIME),
            )
            if CONF_NIGHT_ELEVATION_PRESET in user_input:
                night_sun_elevation = _resolve_elevation(
                    user_input,
                    CONF_NIGHT_ELEVATION_PRESET,
                    CONF_NIGHT_SUN_ELEVATION,
                    NIGHT_ELEVATION_PRESETS,
                    stored_lc.get("night_sun_elevation_deg", DEFAULT_NIGHT_SUN_ELEVATION),
                )
            else:
                night_sun_elevation = stored_lc.get("night_sun_elevation_deg", DEFAULT_NIGHT_SUN_ELEVATION)
            morning_fixed_time = _parse_time_input(
                user_input.get(CONF_MORNING_FIXED_TIME),
                stored_lc.get("morning_fixed_time", DEFAULT_MORNING_FIXED_TIME),
            )
            if CONF_MORNING_ELEVATION_PRESET in user_input:
                morning_sun_elevation = _resolve_elevation(
                    user_input,
                    CONF_MORNING_ELEVATION_PRESET,
                    CONF_MORNING_SUN_ELEVATION,
                    MORNING_ELEVATION_PRESETS,
                    stored_lc.get("morning_sun_elevation_deg", DEFAULT_MORNING_SUN_ELEVATION),
                )
            else:
                morning_sun_elevation = stored_lc.get("morning_sun_elevation_deg", DEFAULT_MORNING_SUN_ELEVATION)
            night_position = int(user_input.get(CONF_NIGHT_POSITION, stored_lc.get("night_position", DEFAULT_NIGHT_POSITION)))
            morning_position = int(user_input.get(CONF_MORNING_POSITION, stored_lc.get("morning_position", DEFAULT_MORNING_POSITION)))
            new_lc = {
                **stored_lc,
                "schedule_mode": active_schedule_mode.value,
                "night_trigger": pending_night.value,
                "night_fixed_time": night_fixed_time.isoformat(),
                "night_sun_elevation_deg": night_sun_elevation,
                "night_position": night_position,
                "morning_trigger": pending_morning.value,
                "morning_fixed_time": morning_fixed_time.isoformat(),
                "morning_sun_elevation_deg": morning_sun_elevation,
                "morning_position": morning_position,
            }
            return self._save_and_reload({"lifecycle_config": new_lc})

        # Build elevation selectors — shared between WEEKDAY_WEEKEND and SAME_EVERY_DAY branches.
        stored_night_elev = stored_lc.get("night_sun_elevation_deg", DEFAULT_NIGHT_SUN_ELEVATION)
        stored_morning_elev = stored_lc.get("morning_sun_elevation_deg", DEFAULT_MORNING_SUN_ELEVATION)
        night_preset_selector = SelectSelector(
            SelectSelectorConfig(
                options=[*NIGHT_ELEVATION_PRESETS.keys(), ELEVATION_PRESET_CUSTOM],
                mode=SelectSelectorMode.LIST,
                translation_key="night_elevation_preset",
            )
        )
        morning_preset_selector = SelectSelector(
            SelectSelectorConfig(
                options=[*MORNING_ELEVATION_PRESETS.keys(), ELEVATION_PRESET_CUSTOM],
                mode=SelectSelectorMode.LIST,
                translation_key="morning_elevation_preset",
            )
        )
        custom_elevation_selector = NumberSelector(
            NumberSelectorConfig(min=-90, max=90, step=0.5, mode=NumberSelectorMode.BOX)
        )

        # Show form
        if is_weekday_weekend:
            # Pre-fill weekday from shared values when stored weekday fields are empty
            # (user switching from SAME_EVERY_DAY → WEEKDAY_WEEKEND for the first time).
            stored_wday_night_time = (
                stored_lc.get("weekday_night_fixed_time")
                or stored_lc.get("night_fixed_time", DEFAULT_WEEKDAY_NIGHT_FIXED_TIME)
            )
            stored_wday_morning_time = (
                stored_lc.get("weekday_morning_fixed_time")
                or stored_lc.get("morning_fixed_time", DEFAULT_WEEKDAY_MORNING_FIXED_TIME)
            )
            stored_wend_night_time = stored_lc.get("weekend_night_fixed_time", DEFAULT_WEEKEND_NIGHT_FIXED_TIME)
            stored_wend_morning_time = stored_lc.get("weekend_morning_fixed_time", DEFAULT_WEEKEND_MORNING_FIXED_TIME)
            schema_dict: dict[Any, Any] = {}
            # Shared elevation fields (thresholds do not vary by day of week)
            if pending_night in {NightTrigger.SUN_ELEVATION, NightTrigger.BOTH}:
                schema_dict[
                    vol.Required(CONF_NIGHT_ELEVATION_PRESET, default=_elevation_to_preset(stored_night_elev, NIGHT_ELEVATION_PRESETS))
                ] = night_preset_selector
                schema_dict[vol.Optional(CONF_NIGHT_SUN_ELEVATION, default=stored_night_elev)] = custom_elevation_selector
            if pending_morning in {MorningTrigger.SUN_ELEVATION, MorningTrigger.BOTH}:
                schema_dict[
                    vol.Required(CONF_MORNING_ELEVATION_PRESET, default=_elevation_to_preset(stored_morning_elev, MORNING_ELEVATION_PRESETS))
                ] = morning_preset_selector
                schema_dict[vol.Optional(CONF_MORNING_SUN_ELEVATION, default=stored_morning_elev)] = custom_elevation_selector
            # Weekday section: per-day time + position
            if pending_night in {NightTrigger.FIXED_TIME, NightTrigger.BOTH}:
                schema_dict[vol.Required(CONF_WEEKDAY_NIGHT_FIXED_TIME, default=stored_wday_night_time)] = TimeSelector()
            schema_dict[
                vol.Required(CONF_WEEKDAY_NIGHT_POSITION, default=stored_lc.get("weekday_night_position", DEFAULT_NIGHT_POSITION))
            ] = position_selector
            if pending_morning in {MorningTrigger.FIXED_TIME, MorningTrigger.BOTH}:
                schema_dict[vol.Required(CONF_WEEKDAY_MORNING_FIXED_TIME, default=stored_wday_morning_time)] = TimeSelector()
            schema_dict[
                vol.Required(CONF_WEEKDAY_MORNING_POSITION, default=stored_lc.get("weekday_morning_position", DEFAULT_MORNING_POSITION))
            ] = position_selector
            # Weekend section: per-day time + position (elevation reused from above)
            if pending_night in {NightTrigger.FIXED_TIME, NightTrigger.BOTH}:
                schema_dict[vol.Required(CONF_WEEKEND_NIGHT_FIXED_TIME, default=stored_wend_night_time)] = TimeSelector()
            schema_dict[
                vol.Required(CONF_WEEKEND_NIGHT_POSITION, default=stored_lc.get("weekend_night_position", DEFAULT_NIGHT_POSITION))
            ] = position_selector
            if pending_morning in {MorningTrigger.FIXED_TIME, MorningTrigger.BOTH}:
                schema_dict[vol.Required(CONF_WEEKEND_MORNING_FIXED_TIME, default=stored_wend_morning_time)] = TimeSelector()
            schema_dict[
                vol.Required(CONF_WEEKEND_MORNING_POSITION, default=stored_lc.get("weekend_morning_position", DEFAULT_MORNING_POSITION))
            ] = position_selector
            return self.async_show_form(step_id="lifecycle_detail", data_schema=vol.Schema(schema_dict))

        # SAME_EVERY_DAY form
        stored_night_time = stored_lc.get("night_fixed_time", DEFAULT_NIGHT_FIXED_TIME)
        stored_night_pos = stored_lc.get("night_position", DEFAULT_NIGHT_POSITION)
        stored_morning_time = stored_lc.get("morning_fixed_time", DEFAULT_MORNING_FIXED_TIME)
        stored_morning_pos = stored_lc.get("morning_position", DEFAULT_MORNING_POSITION)
        schema_dict = {}
        if pending_night in {NightTrigger.FIXED_TIME, NightTrigger.BOTH}:
            schema_dict[vol.Required(CONF_NIGHT_FIXED_TIME, default=stored_night_time)] = TimeSelector()
        if pending_night in {NightTrigger.SUN_ELEVATION, NightTrigger.BOTH}:
            schema_dict[
                vol.Required(CONF_NIGHT_ELEVATION_PRESET, default=_elevation_to_preset(stored_night_elev, NIGHT_ELEVATION_PRESETS))
            ] = night_preset_selector
            schema_dict[vol.Optional(CONF_NIGHT_SUN_ELEVATION, default=stored_night_elev)] = custom_elevation_selector
        schema_dict[vol.Required(CONF_NIGHT_POSITION, default=stored_night_pos)] = position_selector
        if pending_morning in {MorningTrigger.FIXED_TIME, MorningTrigger.BOTH}:
            schema_dict[vol.Required(CONF_MORNING_FIXED_TIME, default=stored_morning_time)] = TimeSelector()
        if pending_morning in {MorningTrigger.SUN_ELEVATION, MorningTrigger.BOTH}:
            schema_dict[
                vol.Required(CONF_MORNING_ELEVATION_PRESET, default=_elevation_to_preset(stored_morning_elev, MORNING_ELEVATION_PRESETS))
            ] = morning_preset_selector
            schema_dict[vol.Optional(CONF_MORNING_SUN_ELEVATION, default=stored_morning_elev)] = custom_elevation_selector
        schema_dict[vol.Required(CONF_MORNING_POSITION, default=stored_morning_pos)] = position_selector
        return self.async_show_form(step_id="lifecycle_detail", data_schema=vol.Schema(schema_dict))

    # -- Presence --

    async def async_step_presence(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._config_entry.data

        if user_input is not None:
            updates: dict[str, Any] = {
                CONF_PRESENCE_ENTITY_IDS: user_input.get(CONF_PRESENCE_ENTITY_IDS, []),
                CONF_ABSENCE_DELAY_MIN: int(user_input[CONF_ABSENCE_DELAY_MIN]),
            }
            if CONF_ABSENCE_POSITION in user_input:
                raw_pos = user_input[CONF_ABSENCE_POSITION]
                absence_position = int(raw_pos) if raw_pos is not None else None
                zones = list(current.get("zones") or [])
                if zones:
                    zones[0] = {**zones[0], "absence_position": absence_position}
                updates["zones"] = zones
            return self._save_and_reload(updates)

        # Prefill absence_position from the first zone's stored value; fall back to default.
        current_absence_position = (
            ((current.get("zones") or [{}])[0]).get("absence_position")
            or DEFAULT_ABSENCE_POSITION
        )

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PRESENCE_ENTITY_IDS,
                    default=current.get(CONF_PRESENCE_ENTITY_IDS, []),
                ): EntitySelector(EntitySelectorConfig(domain="person", multiple=True)),
                vol.Required(
                    CONF_ABSENCE_DELAY_MIN,
                    default=current.get(CONF_ABSENCE_DELAY_MIN, DEFAULT_ABSENCE_DELAY_MIN),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=1440, step=5,
                        mode=NumberSelectorMode.BOX, unit_of_measurement="min",
                    )
                ),
                vol.Optional(
                    CONF_ABSENCE_POSITION,
                    description={"suggested_value": current_absence_position},
                ): NumberSelector(
                    NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%")
                ),
            }
        )
        return self.async_show_form(step_id="presence", data_schema=schema)

    # -- Comfort settings --

    async def async_step_comfort(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._config_entry.data
        stored_comfort = current.get("comfort_config") or {}
        errors: dict[str, str] = {}

        if user_input is not None:
            # Server-side validation: numeric, finite, non-negative and within a
            # sensible glare range.  NumberSelector already constrains input, but
            # we validate again so a malformed/out-of-range value never reaches the
            # stored config (the BehaviorConfig default is the safe fallback).
            _glare_min = stored_comfort.get(
                "glare_min_exposure_wm2", DEFAULT_GLARE_MIN_EXPOSURE_WM2)
            _raw_glare = user_input.get(CONF_GLARE_MIN_EXPOSURE_WM2)
            try:
                _glare_min = float(_raw_glare)
            except (TypeError, ValueError):
                errors["base"] = "invalid_glare_min_exposure"
            else:
                if not (0.0 <= _glare_min <= GLARE_MIN_EXPOSURE_MAX_WM2):
                    errors["base"] = "invalid_glare_min_exposure"
            if not errors:
                new_comfort = {
                    **stored_comfort,
                    "heat_protection_enabled": bool(user_input[CONF_HEAT_PROTECTION_ENABLED]),
                    "glare_protection_enabled": bool(user_input[CONF_GLARE_PROTECTION_ENABLED]),
                    "solar_gain_enabled": bool(user_input[CONF_SOLAR_GAIN_ENABLED]),
                    "glare_min_exposure_wm2": _glare_min,
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
                vol.Required(
                    CONF_GLARE_MIN_EXPOSURE_WM2,
                    default=stored_comfort.get(
                        "glare_min_exposure_wm2", DEFAULT_GLARE_MIN_EXPOSURE_WM2),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=GLARE_MIN_EXPOSURE_MAX_WM2, step=5,
                        mode=NumberSelectorMode.BOX, unit_of_measurement="W/m²",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="comfort", data_schema=schema, errors=errors)

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
        """
        new_data = {**self._config_entry.data, **updates}
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
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
