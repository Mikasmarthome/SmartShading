"""Weather evaluation helpers. See ARCHITECTURE.md §5.3.

Pure math/logic (radiation estimate, storm classification, HA weather-state
parsing) — reading a live weather source is done directly in coordinator.py,
which owns the actual HA weather-entity/sensor access.
"""
from __future__ import annotations

import math
from enum import Enum


class WeatherCondition(Enum):
    """ARCHITECTURE.md §3.7."""

    CLEAR = "clear"
    PARTLY_CLOUDY = "partly_cloudy"
    CLOUDY = "cloudy"
    OVERCAST = "overcast"
    RAIN = "rain"
    HEAVY_RAIN = "heavy_rain"
    THUNDERSTORM = "thunderstorm"  # -> STORM_SAFE
    HAIL = "hail"  # -> STORM_SAFE
    SNOW = "snow"
    FOG = "fog"
    WINDY = "windy"
    STORM = "storm"  # -> STORM_SAFE (highest priority)


STORM_CONDITIONS: frozenset[WeatherCondition] = frozenset(
    {WeatherCondition.STORM, WeatherCondition.THUNDERSTORM, WeatherCondition.HAIL}
)

# ---------------------------------------------------------------------------
# HA weather entity state alias map
# ---------------------------------------------------------------------------
# HA weather integrations (DWD, OpenWeatherMap, yr.no, Met.no, …) often use
# state strings that do not match WeatherCondition enum values exactly.
# This map translates those aliases so parse_weather_condition() can
# classify them correctly for storm detection and condition-based logic.
#
# Unmapped states (e.g. "exceptional") fall through to None, which is
# the safe default (no condition-based action taken).
_HA_CONDITION_ALIASES: dict[str, WeatherCondition] = {
    # Clear / sun
    "sunny":          WeatherCondition.CLEAR,
    "clear-night":    WeatherCondition.CLEAR,
    # Partial cloud
    "partlycloudy":   WeatherCondition.PARTLY_CLOUDY,
    # Rain
    "rainy":          WeatherCondition.RAIN,
    "snowy-rainy":    WeatherCondition.RAIN,
    "pouring":        WeatherCondition.HEAVY_RAIN,
    # Thunderstorm (DWD uses "lightning" / "lightning-rainy")
    "lightning":      WeatherCondition.THUNDERSTORM,
    "lightning-rainy": WeatherCondition.THUNDERSTORM,
    # Snow
    "snowy":          WeatherCondition.SNOW,
    # Wind variant
    "windy-variant":  WeatherCondition.WINDY,
    # "exceptional" has no safe classification → not listed here → returns None
}

DEFAULT_SOLAR_CONSTANT_WM2 = 1000.0
DEFAULT_STORM_WIND_THRESHOLD_MS = 20.0


class WeatherEngine:
    """Evaluates the configured weather source (ARCHITECTURE.md §5.3)."""

    @staticmethod
    def calculate_effective_radiation(
        sun_elevation_deg: float,
        cloud_cover_pct: float,
        solar_constant_wm2: float = DEFAULT_SOLAR_CONSTANT_WM2,
    ) -> float:
        """Fallback radiation estimate when no dedicated sensor exists
        (ARCHITECTURE.md §5.3, "Solarstrahlung-Berechnung")."""
        if sun_elevation_deg <= 0.0:
            return 0.0
        cloud_factor = max(0.0, min(1.0, cloud_cover_pct / 100.0)) * 0.85
        return solar_constant_wm2 * math.sin(math.radians(sun_elevation_deg)) * (1.0 - cloud_factor)

    @staticmethod
    def is_storm_condition(
        condition: WeatherCondition,
        wind_gust_ms: float,
        storm_wind_threshold_ms: float = DEFAULT_STORM_WIND_THRESHOLD_MS,
    ) -> bool:
        """ARCHITECTURE.md §4.6 STORM_SAFE entry conditions (weather part)."""
        return condition in STORM_CONDITIONS or wind_gust_ms >= storm_wind_threshold_ms

    @staticmethod
    def parse_weather_condition(raw: str | None) -> WeatherCondition | None:
        """Parse a raw HA weather entity state string into a WeatherCondition.

        Tries the canonical WeatherCondition enum value first, then falls back
        to _HA_CONDITION_ALIASES for provider-specific strings (DWD, OWM, …).
        Never raises: unrecognised or None values become None (fail-safe).
        """
        if raw is None:
            return None
        normalized = raw.strip().lower()
        try:
            return WeatherCondition(normalized)
        except ValueError:
            return _HA_CONDITION_ALIASES.get(normalized)

    @staticmethod
    def parse_numeric_state(raw_value: object) -> float | None:
        """Best-effort parse of a HA entity state/attribute into a float.

        Returns None for `None`, "unknown", "unavailable", empty strings,
        or anything else that cannot be converted - never raises. Used to
        read optional weather/solar sensors (ARCHITECTURE.md §5.3
        "Multi-tier sensor fallback") without crashing the update cycle
        when a sensor is misconfigured or temporarily unavailable.
        """
        if raw_value is None:
            return None
        if isinstance(raw_value, (int, float)):
            return float(raw_value)
        text = str(raw_value).strip().lower()
        if text in ("", "unknown", "unavailable", "none"):
            return None
        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def normalize_temperature_c(value: float, unit: object) -> float:
        """Normalize an already-parsed numeric temperature reading to °C.

        F20: a dedicated sensor's `unit_of_measurement` reflects whatever unit
        it is CURRENTLY reporting in (e.g. °F on a US-locale Home Assistant
        instance) — Home Assistant does not silently normalize this for a
        custom integration reading `state.state` directly. Only Fahrenheit is
        converted; a missing/unrecognized unit is trusted as already °C,
        matching the behavior every existing (mostly °C) installation already
        relies on — this must never regress a working setup that has no
        `unit_of_measurement` set at all.
        """
        if isinstance(unit, str) and unit.strip().lower() in ("°f", "f", "fahrenheit"):
            return (value - 32.0) * 5.0 / 9.0
        return value

    @staticmethod
    def normalize_wind_speed_ms(value: float, unit: object) -> float:
        """Normalize an already-parsed numeric wind-speed reading to m/s.

        F20: km/h and mph are converted; a missing/unrecognized unit
        (including m/s itself) is trusted as already m/s, matching every
        existing installation's current behavior.
        """
        if isinstance(unit, str):
            normalized_unit = unit.strip().lower()
            if normalized_unit in ("km/h", "kmh", "kph"):
                return value / 3.6
            if normalized_unit in ("mph", "mi/h"):
                return value * 0.44704
        return value

    #: Illuminance units (lux family) that must never be silently treated as
    #: solar irradiance (W/m²) — visually similar "brightness" sensors are a
    #: realistic misconfiguration, and there is no safe automatic lux→W/m²
    #: estimate (see engines/solar_source.py FB_UNIT_MISMATCH).
    _NON_SOLAR_IRRADIANCE_UNITS = frozenset({"lx", "lux", "klx", "klux"})

    @staticmethod
    def is_plausible_solar_unit(unit: object) -> bool:
        """False only for a unit clearly NOT solar irradiance (lux family).

        True for "w/m²", "w/m2", or a missing/unrecognized unit — deliberately
        permissive by default so an existing sensor with no explicit
        `unit_of_measurement` (the common case today) keeps working exactly
        as before. This is a unit-family sanity check, not a conversion.
        """
        if isinstance(unit, str) and unit.strip().lower() in WeatherEngine._NON_SOLAR_IRRADIANCE_UNITS:
            return False
        return True
