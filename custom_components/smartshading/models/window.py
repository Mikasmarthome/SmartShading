"""Per-window configuration. See ARCHITECTURE.md §3.0 / §3.1.

SmartShading evaluates concrete windows, not facades - this is the central
configuration unit the Decision Engine and State Machine operate on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .obstruction import ObstructionZone


class WindowBehaviorMode(Enum):
    """Controls how much automatic logic applies to a window.

    FULLY_AUTOMATIC       — all tiers active (default).
    ABSENCE_AND_SCHEDULE  — absence shading + night/morning lifecycle + safety guards;
                            solar/heat/glare/daytime-fallback tiers skip.
    ABSENCE_ONLY          — only absence shading + safety guards; normal solar/heat/glare/lifecycle tiers skip.
    DISABLED_AUTOMATIC    — only safety guards (storm/wind); all automatic logic including absence skips.

    Safety (Tier 1) always takes precedence regardless of mode.
    """

    FULLY_AUTOMATIC = "fully_automatic"
    ABSENCE_AND_SCHEDULE = "absence_and_schedule"
    ABSENCE_ONLY = "absence_only"
    DISABLED_AUTOMATIC = "disabled_automatic"


@dataclass
class WindowConfig:
    """A single physical window.

    Fields are grouped into:
    - Identity & geometry: always window-specific, never inherited.
    - Execution context: points at the CoverGroup that physically drives
      this window (ARCHITECTURE.md §3.0).
    - Behavior (inherited, overridable): the window-level layer of the
      Global -> Zone -> Window inheritance chain. These are None unless
      this specific window deviates from its zone/global default - read
      them only through ConfigResolver (models/config.py), never directly.
    """

    id: str
    name: str
    zone_id: str
    azimuth: float  # 0-360 deg, 0=N, 90=E, 180=S, 270=W
    floor_level: int  # 0=ground floor, 1=first floor, ...
    cover_group_id: str

    overhang_depth_m: float = 0.0  # 0 = no overhang/balcony
    area_m2: float | None = None

    # ShadingGroup — optional harmonization group within the zone (Step 9G10e).
    # None = no harmonization. String value = group key (zone-scoped).
    # zone_id + shading_group_id forms the logical group key.
    # Examples: "south", "west", "terrace"
    shading_group_id: str | None = None

    # Behavior (inherited, overridable) - None = inherit, see ConfigResolver
    tolerance_start: float | None = None
    tolerance_end: float | None = None
    night_shading_enabled: bool | None = None
    absence_shading_enabled: bool | None = None
    absence_position: int | None = None
    learning_enabled: bool | None = None
    comfort_profile_id: str | None = None
    lifecycle_config_id: str | None = None

    # Participation mode: controls which automatic tiers run for this window.
    # FULLY_AUTOMATIC = all tiers; ABSENCE_ONLY = safety + absence only;
    # DISABLED_AUTOMATIC = safety only. Defaults to FULLY_AUTOMATIC.
    behavior_mode: WindowBehaviorMode = WindowBehaviorMode.FULLY_AUTOMATIC

    # Per-window shading target position overrides (HA convention: 0=closed, 100=open).
    # None = inherit zone default (shade_position_defaults).
    # Set to override zone defaults for this specific window only.
    light_shade_position: int | None = None
    normal_shade_position: int | None = None
    strong_shade_position: int | None = None

    # Rain Protection — per-window overrides (None = inherit from hardware default via GlobalDefaults).
    # rain_protection_enabled=True → RainEvaluator fires for this window when rain is detected.
    # rain_safe_position_ha: HA position (0=closed, 100=open) to move to when RAIN_SAFE.
    #   None = use hardware-type default (AWNING/EXTERIOR_SCREEN → HA 0; others → HA 100).
    # rain_release_delay_min: minutes to wait after rain stops before resuming normal control.
    rain_protection_enabled: bool | None = None
    rain_safe_position_ha: int | None = None
    rain_release_delay_min: int | None = None

    # Manual sun sector override.
    # When both fields are not None, the automatic sector (window_azimuth ± tolerance)
    # is replaced by the manually defined [start, end] azimuth range.
    # Supports wrap-around: start > end crosses north (0°).
    # None on either field = automatic sector (default).
    manual_sun_sector_start_deg: float | None = None
    manual_sun_sector_end_deg: float | None = None

    # Per-window obstruction zones (mountains, trees, roof overhangs …).
    # Empty list = no obstruction (default, behavior unchanged).
    # Evaluated OR-style: any active blocking zone suppresses direct solar exposure.
    obstruction_zones: list[ObstructionZone] = field(default_factory=list)

    # Window contact sensor — physical open/close sensor for this window.
    # None = no contact sensor configured (contact night logic is disabled).
    # Stored as entity_id string; coordinator reads the HA state each cycle.
    contact_sensor_entity_id: str | None = None

    # Night behavior when contact sensor is configured.
    # Option A: block the automatic night move while the window is open.
    #   When the window closes during the same night, SmartShading performs
    #   exactly one catch-up move to the night position.
    # Option B (requires A): when the window is opened after the night move
    #   was done, drive to window_open_night_position_ha; drive back to night
    #   position when the window closes again.
    # Both default to False (opt-in); None = inherit (not used for booleans here,
    # since these are per-window-only — no zone/global inheritance path).
    night_block_on_window_open: bool = False
    night_lift_on_window_open: bool = False  # Option B; only effective when A=True

    # HA-convention position (0=closed, 100=open) for Option B (NIGHT_VENT state).
    # None → coordinator uses DEFAULT_WINDOW_OPEN_NIGHT_POSITION_HA (100 = fully open).
    window_open_night_position_ha: int | None = None
