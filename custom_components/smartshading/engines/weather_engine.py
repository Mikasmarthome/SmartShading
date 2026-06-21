"""Weather snapshot model and evaluation. See ARCHITECTURE.md §5.3.

get_snapshot() is an interface stub for this phase: reading a live weather
source (HA weather entity / sensor) requires Home Assistant, which this
package deliberately does not depend on yet (see ARCHITECTURE.md §16.1 -
this is exactly the kind of HA-dependent piece deferred to the integration
phase). calculate_effective_radiation() and is_storm_condition() are pure
math/logic and fully implemented and testable now.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
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
RAIN_CONDITIONS: frozenset[WeatherCondition] = frozenset(
    {WeatherCondition.RAIN, WeatherCondition.HEAVY_RAIN}
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


@dataclass(frozen=True)
class WeatherSnapshot:
    """ARCHITECTURE.md §3.7."""

    timestamp: datetime
    outdoor_temp_c: float
    cloud_cover_pct: float  # 0-100
    solar_radiation_wm2: float
    effective_solar_radiation_wm2: float
    wind_speed_ms: float
    wind_gust_ms: float
    condition: WeatherCondition
    is_rain: bool
    is_storm_safe_required: bool


class WeatherEngine:
    """Evaluates the configured weather source (ARCHITECTURE.md §5.3)."""

    def get_snapshot(self, *args: object, **kwargs: object) -> WeatherSnapshot:
        """Read the configured weather source and build a WeatherSnapshot.

        Not implemented yet - requires a live HA weather entity or sensor.
        Implemented once SmartShading is wired into Home Assistant
        (ARCHITECTURE.md §14, after the core foundation).
        """
        raise NotImplementedError(
            "WeatherEngine.get_snapshot() requires a live weather source and is "
            "implemented when SmartShading is integrated into Home Assistant."
        )

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
    def is_rain_condition(condition: WeatherCondition) -> bool:
        return condition in RAIN_CONDITIONS

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
