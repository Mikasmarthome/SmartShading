"""Effective per-window exposure: geometry x weather x learning x season.
See ARCHITECTURE.md §5.2.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .sun_engine import SunGeometry


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
        )
