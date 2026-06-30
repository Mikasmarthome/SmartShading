"""Effective per-window exposure: geometry x weather x learning x season.
See ARCHITECTURE.md §5.2.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from ..const import (
    LOW_ANGLE_GLARE_MAX_ELEVATION_DEG,
    LOW_ANGLE_GLARE_MIN_ELEVATION_DEG,
)
from .sun_engine import SunGeometry


def compute_low_angle_direct_glare_wm2(
    measured_solar_wm2: float,
    sun_elevation_deg: float,
    azimuth_delta_deg: float,
) -> float:
    """Estimate the direct-beam irradiance on a VERTICAL window for low sun.

    The measured solar sensor reads HORIZONTAL global irradiance (GHI), which
    already contains a sin(elevation) factor (GHI ≈ DNI·sin(elev) + diffuse).
    The standard ``direct_radiation_factor`` projects that onto the window with
    cos(azimuth_delta)·sin(elevation) — i.e. it applies sin(elevation) a SECOND
    time.  For a horizontal roof that is correct, but for a vertical window at a
    low sun angle it massively under-represents east/west glare: the real beam
    on the glass is roughly DNI·cos(elev)·cos(azimuth_delta).

    This returns that vertical-incidence estimate, derived from the measured GHI:
        DNI ≈ GHI / sin(elev)
        vertical ≈ DNI · cos(elev) · cos(azimuth_delta)
                 = GHI · cot(elev) · cos(azimuth_delta)

    Only meaningful in the low-to-mid elevation band; returns 0.0 outside it (so
    normal/high-sun cases keep using the standard effective-exposure path) and
    when there is no measured beam.  Cloud handling is NOT re-applied here — the
    measured value passed in is already the authoritative source value.
    """
    if measured_solar_wm2 <= 0.0:
        return 0.0
    if not (
        LOW_ANGLE_GLARE_MIN_ELEVATION_DEG
        <= sun_elevation_deg
        <= LOW_ANGLE_GLARE_MAX_ELEVATION_DEG
    ):
        return 0.0
    elev = math.radians(sun_elevation_deg)
    sin_elev = math.sin(elev)
    if sin_elev <= 0.0:
        return 0.0
    cot_elev = math.cos(elev) / sin_elev
    incidence = max(0.0, math.cos(math.radians(azimuth_delta_deg)))
    return measured_solar_wm2 * cot_elev * incidence


@dataclass(frozen=True)
class WindowExposure:
    """ARCHITECTURE.md §3.6."""

    window_id: str
    timestamp: datetime

    # Raw sun data
    sun_azimuth: float
    sun_elevation: float
    is_above_horizon: bool

    # Window-specific exposure
    is_in_tolerance_window: bool
    azimuth_delta_deg: float
    direct_radiation_factor: float
    elevation_clipped: bool

    # Effective exposure
    theoretical_exposure: float
    learned_solar_impact_factor: float
    seasonal_factor: float
    effective_exposure: float

    # Authoritative measured (or fallback) solar source value fed in, and the
    # low-angle vertical-window direct-glare estimate derived from it.  Defaults
    # keep older direct constructions (e.g. tests) backward compatible — a 0.0
    # measured value can never trigger the low-angle glare path.
    measured_solar_wm2: float = 0.0
    low_angle_direct_glare_wm2: float = 0.0


class ExposureEngine:
    """Combines sun geometry with weather, learning and seasonal correction
    into an effective per-window exposure (ARCHITECTURE.md §5.2).

    learned_solar_impact_factor and seasonal_factor are accepted as plain
    floats (default 1.0 = "no adjustment yet") rather than a full
    LearningRecord/Season type - the Learning Engine is explicitly out of
    scope for this phase (see ARCHITECTURE.md §16.3 future placeholder).
    This matches the documented fallback behavior ("1.0 wenn kein Lernen")
    without anticipating the Learning Engine's eventual data model.
    """

    def calculate(
        self,
        sun_geometry: SunGeometry,
        effective_solar_radiation_wm2: float,
        window_id: str,
        timestamp: datetime,
        learned_solar_impact_factor: float = 1.0,
        seasonal_factor: float = 1.0,
    ) -> WindowExposure:
        theoretical_exposure = sun_geometry.direct_radiation_factor * effective_solar_radiation_wm2
        effective_exposure = theoretical_exposure * learned_solar_impact_factor * seasonal_factor

        # Low-angle vertical-window direct-glare estimate (only non-zero in the
        # low-elevation band when the window is geometrically lit).  Computed from
        # the same authoritative source value — no second cloud reduction.
        low_angle_direct_glare = (
            compute_low_angle_direct_glare_wm2(
                effective_solar_radiation_wm2,
                sun_geometry.sun_elevation,
                sun_geometry.azimuth_delta_deg,
            )
            if sun_geometry.is_in_tolerance_window
            else 0.0
        )

        return WindowExposure(
            window_id=window_id,
            timestamp=timestamp,
            sun_azimuth=sun_geometry.sun_azimuth,
            sun_elevation=sun_geometry.sun_elevation,
            is_above_horizon=sun_geometry.is_above_horizon,
            is_in_tolerance_window=sun_geometry.is_in_tolerance_window,
            azimuth_delta_deg=sun_geometry.azimuth_delta_deg,
            direct_radiation_factor=sun_geometry.direct_radiation_factor,
            elevation_clipped=sun_geometry.elevation_clipped,
            theoretical_exposure=theoretical_exposure,
            learned_solar_impact_factor=learned_solar_impact_factor,
            seasonal_factor=seasonal_factor,
            effective_exposure=effective_exposure,
            measured_solar_wm2=effective_solar_radiation_wm2,
            low_angle_direct_glare_wm2=low_angle_direct_glare,
        )
