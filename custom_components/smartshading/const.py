"""Central constants for the SmartShading integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "smartshading"

# Entry type discrimination: distinguishes the system entry from zone entries.
# Stored in ConfigEntry.data[CONF_ENTRY_TYPE].  Absent = zone (legacy compat).
CONF_ENTRY_TYPE = "entry_type"
ENTRY_TYPE_SYSTEM = "system"
ENTRY_TYPE_ZONE = "zone"

# Platform lists per entry type.
# Zone entries own sensors, binary sensors, and zone-level switches.
# The system entry owns the export button and the system-level debug switch.
ZONE_PLATFORMS: list[str] = ["sensor", "binary_sensor", "switch"]
SYSTEM_PLATFORMS: list[str] = ["button", "switch"]

# Union for reference and backward-compatible test assertions.
PLATFORMS: list[str] = [*ZONE_PLATFORMS, *SYSTEM_PLATFORMS]

# Identifier for the SmartShading System device in the HA device registry.
# Fixed string so the device remains stable regardless of which entry owns it.
SYSTEM_DEVICE_IDENTIFIER = "smartshading_system"

# hass.data[DOMAIN] key — legacy; no longer used functionally.  Kept so that
# any external code referencing the constant does not break at import time.
DATA_SYSTEM_ENTRY_ID = "system_entry_id"

# hass.data[DOMAIN] keys for integration-wide runtime state.
DATA_GLOBAL_DISPATCH = "global_dispatch"   # GlobalSerialDispatch instance
DATA_DEBUG_LOGGING = "debug_logging"       # bool: debug logging enabled

# config_entry.options key for the debug logging switch (system entry only).
CONF_DEBUG_LOGGING = "debug_logging"

# Storage key for the privacy-safe learning export written by the export button.
LEARNING_EXPORT_STORAGE_KEY = "smartshading_learning_export"
LEARNING_EXPORT_STORAGE_VERSION = 1

# Zone control persistence key in config_entry.options (Step 9G11).
# Stores per-zone learning_enabled / active_control_enabled so switch
# state survives HA restart without modifying config_entry.data.
CONF_ZONE_CONTROLS = "zone_controls"

# ARCHITECTURE.md §9 Coordinator.
DEFAULT_UPDATE_INTERVAL = timedelta(minutes=5)

# --- Config Flow (ARCHITECTURE.md §7) ---

CONF_USE_HOME_LOCATION = "use_home_location"
CONF_ZONE_NAME = "zone_name"
CONF_WINDOW_NAME = "window_name"
CONF_FLOOR_LEVEL = "floor_level"
CONF_COMPASS_DIRECTION = "compass_direction"
CONF_CUSTOM_AZIMUTH = "custom_azimuth"
CONF_COVER_ENTITIES = "cover_entities"
CONF_COVER_HARDWARE_TYPE = "cover_hardware_type"
CONF_ADD_ANOTHER_WINDOW = "add_another_window"
CONF_WINDOW_ID = "window_id"
CONF_REMOVE_CONFIRMED = "remove_confirmed"
CONF_LIGHT_SHADE_POSITION = "light_shade_position"
CONF_NORMAL_SHADE_POSITION = "normal_shade_position"
CONF_STRONG_SHADE_POSITION = "strong_shade_position"

# Per-window manual sun sector override (v1.0).
CONF_MANUAL_SUN_SECTOR_ENABLED = "manual_sun_sector_enabled"
CONF_MANUAL_SUN_SECTOR_START_DEG = "manual_sun_sector_start_deg"
CONF_MANUAL_SUN_SECTOR_END_DEG = "manual_sun_sector_end_deg"

# Per-window obstruction zones: 3 zones, each with 5 fields.
# block_from and block_until replace the old min_elevation field.
# Old stored data with min_elevation_deg is migrated on read (config_entry_data.py).
CONF_OBSTRUCTION_1_ENABLED = "obstruction_1_enabled"
CONF_OBSTRUCTION_1_AZIMUTH_START = "obstruction_1_azimuth_start_deg"
CONF_OBSTRUCTION_1_AZIMUTH_END = "obstruction_1_azimuth_end_deg"
CONF_OBSTRUCTION_1_BLOCK_FROM_ELEVATION = "obstruction_1_block_from_elevation_deg"
CONF_OBSTRUCTION_1_BLOCK_UNTIL_ELEVATION = "obstruction_1_block_until_elevation_deg"
CONF_OBSTRUCTION_2_ENABLED = "obstruction_2_enabled"
CONF_OBSTRUCTION_2_AZIMUTH_START = "obstruction_2_azimuth_start_deg"
CONF_OBSTRUCTION_2_AZIMUTH_END = "obstruction_2_azimuth_end_deg"
CONF_OBSTRUCTION_2_BLOCK_FROM_ELEVATION = "obstruction_2_block_from_elevation_deg"
CONF_OBSTRUCTION_2_BLOCK_UNTIL_ELEVATION = "obstruction_2_block_until_elevation_deg"
CONF_OBSTRUCTION_3_ENABLED = "obstruction_3_enabled"
CONF_OBSTRUCTION_3_AZIMUTH_START = "obstruction_3_azimuth_start_deg"
CONF_OBSTRUCTION_3_AZIMUTH_END = "obstruction_3_azimuth_end_deg"
CONF_OBSTRUCTION_3_BLOCK_FROM_ELEVATION = "obstruction_3_block_from_elevation_deg"
CONF_OBSTRUCTION_3_BLOCK_UNTIL_ELEVATION = "obstruction_3_block_until_elevation_deg"

# Weather/solar inputs (2026-06-16): all optional, single shared source for
# the whole house (not per-window/per-zone). See coordinator.py for the
# dedicated-sensor > weather-entity-attribute > fallback read order.
CONF_WEATHER_ENTITY_ID = "weather_entity_id"
CONF_SOLAR_RADIATION_SENSOR_ID = "solar_radiation_sensor_id"
CONF_OUTDOOR_TEMPERATURE_SENSOR_ID = "outdoor_temperature_sensor_id"
CONF_CLOUD_COVER_SENSOR_ID = "cloud_cover_sensor_id"
CONF_WIND_SPEED_SENSOR_ID = "wind_speed_sensor_id"
CONF_RAIN_SENSOR_ID = "rain_sensor_id"

# Rain protection per-window config keys (stored in ConfigEntry.data per window).
CONF_RAIN_PROTECTION_ENABLED = "rain_protection_enabled"
CONF_RAIN_SAFE_POSITION = "rain_safe_position"
CONF_RAIN_RELEASE_DELAY_MIN = "rain_release_delay_min"

# Defaults for rain protection settings.
DEFAULT_RAIN_RELEASE_DELAY_MIN = 30   # minutes dry cooldown before RAIN_SAFE releases

# Lifecycle/Presence inputs (2026-06-16): wires the already-implemented
# Lifecycle Engine (models/lifecycle.py, engines/lifecycle_engine.py) to
# real Config Flow input instead of hardcoded defaults.
CONF_NIGHT_TRIGGER = "night_trigger"
CONF_NIGHT_FIXED_TIME = "night_fixed_time"
CONF_NIGHT_SUN_ELEVATION = "night_sun_elevation"
CONF_MORNING_TRIGGER = "morning_trigger"
CONF_MORNING_FIXED_TIME = "morning_fixed_time"
CONF_MORNING_SUN_ELEVATION = "morning_sun_elevation"
CONF_PRESENCE_ENTITY_IDS = "presence_entity_ids"
CONF_ABSENCE_DELAY_MIN = "absence_delay_min"
CONF_ABSENCE_POSITION = "absence_position"

# Sun-elevation presets (2026-06-17 UX round): user-friendly labels that map to
# the same elevation floats the Lifecycle Engine already uses internally. The
# preset string is a Config Flow UI construct only - it is never stored in
# ConfigEntry.data (only the resolved float night_sun_elevation_deg is stored).
CONF_NIGHT_ELEVATION_PRESET = "night_elevation_preset"
CONF_MORNING_ELEVATION_PRESET = "morning_elevation_preset"
ELEVATION_PRESET_CUSTOM = "custom"

# Keys match the translation_key used in the SelectSelectorConfig, which in
# turn maps to selector.<key>.options.<option> in strings/en/de.json.
NIGHT_ELEVATION_PRESETS: dict[str, float] = {
    "sunset": 0.0,   # Sonnenuntergang / At sunset
    "dusk": -6.0,    # Dämmerung / Dusk (civil twilight)
    "dark": -12.0,   # Dunkel / Dark (nautical twilight)
}
MORNING_ELEVATION_PRESETS: dict[str, float] = {
    "sunrise": 0.0,  # Sonnenaufgang / At sunrise
    "dawn": -6.0,    # Morgendämmerung / Dawn (civil twilight)
    "bright": -12.0, # Hell werden / Getting bright (nautical twilight)
}

# NightTrigger/MorningTrigger selector options - values match
# models.lifecycle.NightTrigger/MorningTrigger.value exactly, so the
# Config Flow can convert directly via NightTrigger(value).
LIFECYCLE_TRIGGER_OPTIONS: list[str] = ["disabled", "fixed_time", "sun_elevation", "both"]

DEFAULT_NIGHT_TRIGGER = "fixed_time"
DEFAULT_NIGHT_FIXED_TIME = "22:00:00"
DEFAULT_NIGHT_SUN_ELEVATION = -6.0
DEFAULT_MORNING_TRIGGER = "fixed_time"
DEFAULT_MORNING_FIXED_TIME = "06:30:00"
DEFAULT_MORNING_SUN_ELEVATION = 0.0
DEFAULT_ABSENCE_DELAY_MIN = 30
DEFAULT_ABSENCE_POSITION = 10

# Sentinel value for the "I'll enter my own azimuth" compass-direction option.
CUSTOM_AZIMUTH_OPTION = "custom"

# User-friendly compass directions (ARCHITECTURE.md architecture review,
# 2026-06-16): internally SmartShading always works with a plain azimuth
# float (WindowConfig.azimuth, §3.1) - these are only Config Flow UX sugar.
COMPASS_AZIMUTHS: dict[str, float] = {
    "north": 0.0,
    "northeast": 45.0,
    "east": 90.0,
    "southeast": 135.0,
    "south": 180.0,
    "southwest": 225.0,
    "west": 270.0,
    "northwest": 315.0,
}

# Default Zone identifiers kept for migration awareness and potential
# backwards-compatibility guards.  The config flow now uses the user-provided
# integration name as the zone name (not DEFAULT_ZONE_NAME).
DEFAULT_ZONE_ID = "default"
DEFAULT_ZONE_NAME = "Default Zone"

# Comfort settings (Comfort Engine phase, 2026-06-17).
# All sensor entity IDs follow the same optional-sensor pattern as the
# weather inputs above.
CONF_INDOOR_TEMPERATURE_SENSOR_ID = "indoor_temperature_sensor_id"   # legacy single-sensor key
CONF_INDOOR_TEMPERATURE_SENSOR_IDS = "indoor_temperature_sensor_ids"  # v1.0 multi-sensor key
CONF_HEAT_PROTECTION_ENABLED = "heat_protection_enabled"
CONF_GLARE_PROTECTION_ENABLED = "glare_protection_enabled"
CONF_SOLAR_GAIN_ENABLED = "solar_gain_enabled"
CONF_HEAT_PROTECTION_INDOOR_TEMP_C = "heat_protection_indoor_temp_c"
CONF_HEAT_PROTECTION_OUTDOOR_TEMP_C = "heat_protection_outdoor_temp_c"
CONF_SOLAR_GAIN_MAX_OUTDOOR_TEMP_C = "solar_gain_max_outdoor_temp_c"

DEFAULT_HEAT_PROTECTION_INDOOR_TEMP_C = 24.0
DEFAULT_HEAT_PROTECTION_OUTDOOR_TEMP_C = 26.0
DEFAULT_SOLAR_GAIN_MAX_OUTDOOR_TEMP_C = 12.0

# Schedule position defaults: night closes the cover (0), morning opens it (100).
# HA position convention: 0=closed, 100=open.
CONF_NIGHT_POSITION = "night_position"
CONF_MORNING_POSITION = "morning_position"
DEFAULT_NIGHT_POSITION = 0
DEFAULT_MORNING_POSITION = 100

# Weekday/Weekend schedule mode (v1.0)
CONF_SCHEDULE_MODE = "schedule_mode"

# Weekday schedule (Mon–Fri)
CONF_WEEKDAY_NIGHT_FIXED_TIME = "weekday_night_fixed_time"
CONF_WEEKDAY_NIGHT_POSITION = "weekday_night_position"
CONF_WEEKDAY_MORNING_FIXED_TIME = "weekday_morning_fixed_time"
CONF_WEEKDAY_MORNING_POSITION = "weekday_morning_position"

# Weekend schedule (Sat–Sun)
CONF_WEEKEND_NIGHT_FIXED_TIME = "weekend_night_fixed_time"
CONF_WEEKEND_NIGHT_POSITION = "weekend_night_position"
CONF_WEEKEND_MORNING_FIXED_TIME = "weekend_morning_fixed_time"
CONF_WEEKEND_MORNING_POSITION = "weekend_morning_position"

# Default times for weekday/weekend profiles
DEFAULT_WEEKDAY_NIGHT_FIXED_TIME = "22:00:00"    # same as DEFAULT_NIGHT_FIXED_TIME
DEFAULT_WEEKDAY_MORNING_FIXED_TIME = "06:30:00"  # same as DEFAULT_MORNING_FIXED_TIME
DEFAULT_WEEKEND_NIGHT_FIXED_TIME = "23:00:00"    # one hour later than weekday
DEFAULT_WEEKEND_MORNING_FIXED_TIME = "08:30:00"  # two hours later than weekday

# Options for the schedule_mode selector
LIFECYCLE_SCHEDULE_MODE_OPTIONS = ["same_every_day", "weekday_weekend"]

# Per-window behavior/participation mode (v1.0 UX fix batch).
CONF_WINDOW_BEHAVIOR_MODE = "window_behavior_mode"
WINDOW_BEHAVIOR_MODE_OPTIONS = ["fully_automatic", "absence_and_schedule", "absence_only", "disabled_automatic"]

# Window contact sensor and night-contact behavior (v1.1.0).
CONF_CONTACT_SENSOR_ENTITY_ID = "contact_sensor_entity_id"
CONF_NIGHT_BLOCK_ON_WINDOW_OPEN = "night_block_on_window_open"   # Option A
CONF_NIGHT_LIFT_ON_WINDOW_OPEN = "night_lift_on_window_open"     # Option B
CONF_WINDOW_OPEN_NIGHT_POSITION = "window_open_night_position"   # position for Option B

# Default HA-convention position for NIGHT_VENT (Option B) when not explicitly set.
# 100 = fully open — ventilation-friendly default.
DEFAULT_WINDOW_OPEN_NIGHT_POSITION_HA = 100
