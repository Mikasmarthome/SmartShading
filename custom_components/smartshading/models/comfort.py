"""Comfort data model for SmartShading (ARCHITECTURE.md §3.3 and §5.5).

ComfortProfile holds the full per-zone comfort configuration as documented.
ComfortConfig is the flatter, compact representation that the Config Flow
collects and config_entry_data.py serialises - no zone-level inheritance or
goal-list overhead for this version.
ComfortAssessment is the output of one ComfortEngine.assess() call.

All are pure dataclasses - no Home Assistant imports, no logic here.

Scope: HEAT_PROTECTION, GLARE_PROTECTION, SOLAR_GAIN.
PRIVACY and DAYLIGHT are registered as ComfortGoalType values per §3.3 but
are not evaluated by the Comfort Engine in this version.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ComfortGoalType(Enum):
    """ARCHITECTURE.md §3.3 - all documented comfort goal types."""

    HEAT_PROTECTION = "heat_protection"
    GLARE = "glare"
    PRIVACY = "privacy"
    SOLAR_GAIN = "solar_gain"
    DAYLIGHT = "daylight"


@dataclass
class ComfortGoal:
    """ARCHITECTURE.md §3.3 - one enabled/disabled comfort goal with priority."""

    type: ComfortGoalType
    enabled: bool
    priority: int = 0


@dataclass
class ComfortProfile:
    """ARCHITECTURE.md §3.3 - per-zone comfort configuration (full model).

    Used by the full Comfort Engine path (a later extension). In this version the
    Coordinator reads ComfortConfig directly instead of ComfortProfile.
    """

    id: str
    name: str = "Default"
    goals: list[ComfortGoal] = field(default_factory=list)

    # Indoor temperature (optional per §3.3 and architecture decision)
    indoor_temp_entity_id: str | None = None
    target_indoor_temp_c: float | None = None
    max_indoor_temp_c: float | None = None

    # Thresholds
    max_solar_radiation_wm2: float = 300.0
    min_sun_elevation_shade_deg: float = 10.0

    # Presence (inherited from zone in the full model, future use)
    presence_entity_ids: list[str] = field(default_factory=list)
    absence_delay_min: int = 30

    # Winter / solar gain
    solar_gain_enabled: bool = True
    solar_gain_max_outdoor_temp_c: float = 12.0


@dataclass
class ComfortConfig:
    """Flat comfort configuration stored in ConfigEntry.data (current scope).

    Maps 1:1 to what the Config Flow's "comfort" step collects and what
    config_entry_data.py serialises into ConfigEntry.data. Thresholds and
    feature toggles only - the indoor temperature sensor entity ID lives
    separately in SmartShadingConfigEntryData (same pattern as the other
    optional sensor IDs such as weather_entity_id / solar_radiation_sensor_id).
    """

    heat_protection_enabled: bool = True
    glare_protection_enabled: bool = True
    solar_gain_enabled: bool = True
    heat_protection_indoor_temp_c: float = 24.0
    heat_protection_outdoor_temp_c: float = 26.0
    solar_gain_max_outdoor_temp_c: float = 12.0


@dataclass(frozen=True)
class ComfortAssessment:
    """ARCHITECTURE.md §5.5 - result of one ComfortEngine.assess() call.

    Fields: the three active comfort goals plus the temperature readings
    used to reach the decision. privacy_needed and daylight_comfortable are
    documented in §5.5 but are not implemented in this version.
    """

    heat_protection_needed: bool
    glare_protection_needed: bool
    solar_gain_beneficial: bool
    indoor_temperature: float | None
    indoor_temp_available: bool
    reason: str          # human-readable, surfaced as sensor attribute
    reason_code: str     # ReasonCode.value, machine-readable
