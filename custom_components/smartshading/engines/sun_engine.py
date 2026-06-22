"""Pure sun-position geometry relative to a window. No learning factors, no
weather corrections - geometry only. See ARCHITECTURE.md §5.1.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Elevation threshold (deg) below which the sun is assumed blocked by
# neighbouring buildings/terrain for a given floor level.  Each floor
# down raises the threshold by 5° following the pattern: a basement window
# in a light well needs a higher sun to receive direct exposure than a
# ground-floor window, which in turn needs more elevation than upper floors.
# Floors ≥ 3 default to 0° (no elevation restriction).
FLOOR_ELEVATION_THRESHOLD_DEG: dict[int, float] = {
    -1: 20.0,  # basement / lower ground
    0: 15.0,   # ground floor
    1: 10.0,   # 1st floor
    2: 5.0,    # 2nd floor
}
DEFAULT_ELEVATION_THRESHOLD_DEG = 0.0  # 3rd floor and above


@dataclass(frozen=True)
class SunPosition:
    """Raw sun position, e.g. from HA's sun.sun entity (read elsewhere)."""

    azimuth: float  # 0-360 deg
    elevation: float  # deg above horizon, negative = below horizon


@dataclass(frozen=True)
class SunGeometry:
    """Output of SunEngine.calculate() - ARCHITECTURE.md §5.1.

    sun_azimuth/sun_elevation are the raw inputs echoed through, needed
    because WindowExposure (§3.6) requires them alongside the
    geometry-derived fields; ARCHITECTURE.md only defines them on
    WindowExposure, not explicitly on SunGeometry's documented output -
    this is a minor additive clarification, not a deviation.
    """

    sun_azimuth: float
    sun_elevation: float
    is_above_horizon: bool
    is_in_tolerance_window: bool
    azimuth_delta_deg: float
    direct_radiation_factor: float
    elevation_clipped: bool
    solar_window_start_azimuth: float
    solar_window_end_azimuth: float


def normalize_angle_diff(angle: float) -> float:
    """Normalize an angle difference to the range [-180, 180)."""
    return (angle + 180.0) % 360.0 - 180.0


class SunEngine:
    """Reine Geometrie (ARCHITECTURE.md §5.1) - no learning, no weather."""

    def calculate(
        self,
        sun_position: SunPosition,
        window_azimuth: float,
        tolerance_start: float,
        tolerance_end: float,
        floor_level: int,
        overhang_depth_m: float,
    ) -> SunGeometry:
        azimuth_delta = normalize_angle_diff(sun_position.azimuth - window_azimuth)
        is_above_horizon = sun_position.elevation > 0.0
        in_tolerance = -tolerance_start <= azimuth_delta <= tolerance_end
        is_in_window = is_above_horizon and in_tolerance

        direct_radiation_factor = 0.0
        if is_in_window:
            direct_radiation_factor = max(
                0.0,
                math.cos(math.radians(azimuth_delta)) * math.sin(math.radians(sun_position.elevation)),
            )

        elevation_threshold = FLOOR_ELEVATION_THRESHOLD_DEG.get(
            floor_level, DEFAULT_ELEVATION_THRESHOLD_DEG
        )
        elevation_clipped = sun_position.elevation < elevation_threshold
        # NOTE (implementation concern): the overhang correction described
        # in §5.1 ("blocked_elevation = atan(overhang_depth_m / window_height_m)")
        # needs a window height, which WindowConfig (§3.1) does not model.
        # overhang_depth_m is accepted here for interface stability but is
        # not yet factored into elevation_clipped - see final report.
        _ = overhang_depth_m  # intentionally unused until window height exists

        return SunGeometry(
            sun_azimuth=sun_position.azimuth,
            sun_elevation=sun_position.elevation,
            is_above_horizon=is_above_horizon,
            is_in_tolerance_window=is_in_window,
            azimuth_delta_deg=azimuth_delta,
            direct_radiation_factor=direct_radiation_factor,
            elevation_clipped=elevation_clipped,
            solar_window_start_azimuth=(window_azimuth - tolerance_start) % 360.0,
            solar_window_end_azimuth=(window_azimuth + tolerance_end) % 360.0,
        )
