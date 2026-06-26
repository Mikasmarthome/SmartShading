"""Execution context: one or more covers driven together for a single
window. See ARCHITECTURE.md §3.1, "CoverGroup - Ausführungskontext".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CoverSyncMode(Enum):
    """How multiple covers within one CoverGroup are driven."""

    SYNCHRONOUS = "synchronous"
    INDIVIDUAL = "individual"


class CoverHardwareType(Enum):
    """Physical / functional cover type — determines execution semantics and
    default behaviour settings (daytime minimum, tilt control, anti-heat-buildup).

    IMPORTANT: do NOT confuse with the existing CoverProfile enum in
    cover_capabilities.py.  CoverProfile describes the cover's command-capability
    shape (how it can be driven: position vs open/close-only).  CoverHardwareType
    describes what the cover physically IS, which drives sensible defaults for the
    shading logic.

    ROLLER_SHUTTER
        Opaque vertical blind in a casing.  Full closure during daytime shading
        creates heat buildup between cover and glass.  No tilt capability.
        Daytime minimum open position applies.

    VENETIAN_BLIND
        Horizontal-slat blind (Raffstore / Jalousie).  Tilt capability is the
        primary shading control.  Full closure less problematic due to air
        circulation between slats.

    EXTERIOR_SCREEN
        Textile exterior screen.  No tilt.  Lower heat-buildup risk than a
        roller shutter.  Wind sensitivity higher than a roller shutter.

    AWNING
        Horizontal projection awning.  Wind protection is the dominant safety
        concern.  No position/tilt mapping comparable to vertical covers.

    GENERIC
        Safe fallback for unknown or uncategorised covers.  Conservative
        defaults: no tilt, no anti-heat-buildup, no daytime-minimum enforced.
    """

    ROLLER_SHUTTER  = "roller_shutter"
    VENETIAN_BLIND  = "venetian_blind"
    EXTERIOR_SCREEN = "exterior_screen"
    AWNING          = "awning"
    GENERIC         = "generic"


def cover_sync_mode_from_str(value: str | None) -> CoverSyncMode:
    """Convert a raw string (e.g. from config storage) to CoverSyncMode.

    Returns CoverSyncMode.SYNCHRONOUS when *value* is None, empty, or does
    not match any known enum value — ensuring safe backward-compatible
    deserialization for a legacy cover group stored without sync_mode, or an
    unknown value written by a newer version.
    """
    if not value:
        return CoverSyncMode.SYNCHRONOUS
    try:
        return CoverSyncMode(value)
    except ValueError:
        return CoverSyncMode.SYNCHRONOUS


def cover_hardware_type_from_str(value: str | None) -> CoverHardwareType:
    """Convert a raw string (e.g. from config storage) to CoverHardwareType.

    Returns CoverHardwareType.GENERIC when *value* is None, empty, or does
    not match any known enum value — ensuring safe backward-compatible
    deserialization.
    """
    if not value:
        return CoverHardwareType.GENERIC
    try:
        return CoverHardwareType(value)
    except ValueError:
        return CoverHardwareType.GENERIC


# ---------------------------------------------------------------------------
# Default settings per hardware type
# ---------------------------------------------------------------------------

def default_hardware_settings(hardware_type: CoverHardwareType) -> dict:
    """Return sensible default settings for a given CoverHardwareType.

    The dict uses the same field names as BehaviorConfig / CoverGroup so
    that ConfigResolver can apply them without translation.  All values are
    safe starting points; individual windows/zones/global config may override.

    Returns a plain dict (not a dataclass) so this function can be used by
    ConfigResolver before BehaviorConfig is fully constructed and without
    importing the full config module.

    Keys returned:
      daytime_min_open_position_ha  int | None   HA convention (0=closed, 100=open)
      anti_heat_buildup_enabled     bool
      anti_heat_buildup_position_ha int
      allow_anti_heat_buildup_during_absence  bool
      tilt_control_enabled          bool         True when tilt should be used
      wind_protection_enabled       bool         Override default from BehaviorConfig
      rain_protection_enabled       bool         On for awnings/exterior screens
    """
    _DEFAULTS: dict[CoverHardwareType, dict] = {
        CoverHardwareType.ROLLER_SHUTTER: {
            "daytime_min_open_position_ha":             10,
            "anti_heat_buildup_enabled":                True,
            "anti_heat_buildup_position_ha":            10,
            "allow_anti_heat_buildup_during_absence":   False,
            "tilt_control_enabled":                     False,
            "wind_protection_enabled":                  False,
            "rain_protection_enabled":                  False,
        },
        CoverHardwareType.VENETIAN_BLIND: {
            "daytime_min_open_position_ha":             None,
            "anti_heat_buildup_enabled":                False,
            "anti_heat_buildup_position_ha":            10,
            "allow_anti_heat_buildup_during_absence":   False,
            "tilt_control_enabled":                     True,
            "wind_protection_enabled":                  False,
            "rain_protection_enabled":                  False,
        },
        CoverHardwareType.EXTERIOR_SCREEN: {
            "daytime_min_open_position_ha":             10,
            "anti_heat_buildup_enabled":                False,
            "anti_heat_buildup_position_ha":            10,
            "allow_anti_heat_buildup_during_absence":   False,
            "tilt_control_enabled":                     False,
            "wind_protection_enabled":                  False,
            "rain_protection_enabled":                  True,
        },
        CoverHardwareType.AWNING: {
            "daytime_min_open_position_ha":             None,
            "anti_heat_buildup_enabled":                False,
            "anti_heat_buildup_position_ha":            10,
            "allow_anti_heat_buildup_during_absence":   False,
            "tilt_control_enabled":                     False,
            "wind_protection_enabled":                  True,
            "rain_protection_enabled":                  True,
        },
        CoverHardwareType.GENERIC: {
            "daytime_min_open_position_ha":             None,
            "anti_heat_buildup_enabled":                False,
            "anti_heat_buildup_position_ha":            10,
            "allow_anti_heat_buildup_during_absence":   False,
            "tilt_control_enabled":                     False,
            "wind_protection_enabled":                  False,
            "rain_protection_enabled":                  False,
        },
    }
    return dict(_DEFAULTS[hardware_type])


@dataclass
class CoverGroup:
    """1:1 to exactly one window (window_id). Knows nothing about comfort
    or exposure - it only executes what the Decision Engine computed for
    "its" window (ARCHITECTURE.md §3.0/§3.1).
    """

    id: str
    window_id: str
    cover_ids: list[str] = field(default_factory=list)
    sync_mode: CoverSyncMode = CoverSyncMode.SYNCHRONOUS
    # Step 9G10f-a: physical cover type — drives execution semantics and defaults.
    # Default GENERIC is backward-compatible with all existing configurations.
    hardware_type: CoverHardwareType = CoverHardwareType.GENERIC
