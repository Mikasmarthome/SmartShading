"""Global defaults and the cross-level config resolver. See ARCHITECTURE.md §3.1."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .window import WindowConfig
    from .zone import ZoneConfig


@dataclass
class GlobalDefaults:
    """Mandatory fallback values - the lowest level of the
    Global -> Zone -> Window inheritance chain (ARCHITECTURE.md §3.1).

    Every field here always has a concrete value. ZoneConfig and the
    override portion of WindowConfig carry the same field names as
    Optional and fall back to these defaults via ConfigResolver.
    """

    tolerance_start: float = 60.0
    tolerance_end: float = 60.0
    night_shading_enabled: bool = True
    absence_shading_enabled: bool = True
    absence_position: int = 30  # ARCHITECTURE.md §3.1 P0-2: mostly closed, not fully closed
    learning_enabled: bool = True
    comfort_profile_id: str = "default"
    lifecycle_config_id: str = "default"
    position_tolerance: int = 5  # minimum delta to trigger a cover command
    command_debounce_s: float = 3.0
    settle_window_s: float | None = None  # None = 2 x update_interval, computed by the coordinator
    # Rain Protection global defaults (per-window overrides via WindowConfig).
    # Hardware-type defaults from default_hardware_settings() take precedence when
    # the window has no explicit rain_protection_enabled set.
    rain_release_delay_min: int = 30


@dataclass
class ShadePositionDefaults:
    """Simple starting values for cover target position per shading
    intensity (architecture review, 2026-06-16 - starting values).

    Deliberately NOT the full PositionCalculator (ARCHITECTURE.md §5.7,
    step 11) - that engine computes a target position continuously from
    exposure/comfort and does not exist yet. These three values are a
    placeholder lookup a future simple PositionCalculator can start from,
    and what the Config Flow's "Grundverhalten" step collects for now.
    """

    light_shade_position: int = 40
    normal_shade_position: int = 25
    strong_shade_position: int = 10


class ConfigResolver:
    """Resolves a single field through Window override > Zone default >
    Global default (ARCHITECTURE.md §3.1).

    Engines must always read resolved values through this resolver (or a
    future pre-resolved view such as ResolvedWindowConfig) - never read
    raw override fields directly off WindowConfig/ZoneConfig.
    """

    @staticmethod
    def resolve(
        window: "WindowConfig",
        zone: "ZoneConfig",
        global_defaults: GlobalDefaults,
        field: str,
    ) -> Any:
        for source in (window, zone, global_defaults):
            value = getattr(source, field, None)
            if value is not None:
                return value
        raise ValueError(f"No value found for field '{field}' at any inheritance level")
