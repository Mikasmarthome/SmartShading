"""Azimuth-sector utility.

Shared by:
  - Manual sun sector override (WindowConfig.manual_sun_sector_*)
  - Obstruction zone evaluation (ObstructionZone)

All functions are pure Python, no HA dependency.
"""
from __future__ import annotations


def azimuth_in_sector(azimuth: float, start: float, end: float) -> bool:
    """Return True when *azimuth* falls inside the [start, end] sector.

    Supports wrap-around: if start > end, the range crosses north (0°/360°).

    Examples
    --------
    azimuth_in_sector(100, 70, 180)   → True   (normal range)
    azimuth_in_sector(50,  70, 180)   → False
    azimuth_in_sector(10,  330, 30)   → True   (wrap-around, crosses 0°)
    azimuth_in_sector(180, 330, 30)   → False  (outside wrap-around)
    azimuth_in_sector(355, 330, 30)   → True   (inside wrap-around near 360°)
    """
    az = azimuth % 360.0
    s = start % 360.0
    e = end % 360.0
    if s <= e:
        return s <= az <= e
    # wrap-around: range crosses 0° / 360°
    return az >= s or az <= e
