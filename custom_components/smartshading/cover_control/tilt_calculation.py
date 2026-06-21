"""Simple tilt target calculation for tilt-capable covers (Step 9G10f-e).

Pure Python, no Home Assistant dependency.

DESIGN INTENT
-------------
This module implements a straightforward v1.0 tilt strategy for VENETIAN_BLIND
covers based on sun elevation only.  Complex lamellar geometry, Raffstore
physics, and learning-based optimisation are explicitly out of scope for this
step and will be added in later steps.

The result feeds directly into the existing tilt-execution infrastructure
(Step 9G10f-d) via coordinator.py.  The calculated `target_tilt_ha` value is
recorded in WindowExecutionDiagnostics for future learning signal evaluation.

TILT CONVENTION
---------------
HA tilt convention is used throughout (0 = slats fully closed, 100 = slats
fully open).  There are no "internal" tilt positions — unlike cover position,
tilt has only one convention across the entire system.

ELEVATION BANDS (v1.0 defaults)
--------------------------------
  sun_elevation < LOW_ELEVATION_DEG (15°)   → TILT_LOW  (25)
  sun_elevation 15°–HIGH_ELEVATION_DEG (35°) → TILT_MID  (45)
  sun_elevation > 35°                         → TILT_HIGH (65)

Rationale:
  - A low sun angle causes stronger glare / lower incidence angle, so slats
    are closed more (lower tilt value).
  - A high sun angle is more overhead; opening slats slightly lets in diffuse
    light while the cover position handles the shading depth.
  - Values are intentionally coarse for v1.0 — a three-band model is
    explainable and tunable without learning data.

GATE CONDITIONS (all must be True for a tilt target to be returned)
--------------------------------------------------------------------
  1. hardware_type is CoverHardwareType.VENETIAN_BLIND
  2. supports_tilt is True
  3. tilt_control_enabled is True
  4. in_solar_sector is True
  5. sun_elevation_deg is not None
  6. sun_elevation_deg > 0  (sun must be above horizon)
  7. new_state in TILT_ACTIVE_STATES

If any gate fails, the function returns None → no tilt service call is issued.

SAFETY / OVERRIDE INVARIANTS
------------------------------
STORM_SAFE and WIND_SAFE are not in TILT_ACTIVE_STATES → None returned.
MANUAL_OVERRIDE is not in TILT_ACTIVE_STATES → None returned.
NIGHT_CLOSED and ABSENCE_CLOSED are not in TILT_ACTIVE_STATES → None returned.
These are hard invariants, not soft checks; they cannot be configured away.
"""
from __future__ import annotations

from ..models.cover_group import CoverHardwareType
from ..state_machine.states import ShadingState

# ---------------------------------------------------------------------------
# State gate: tilt is only calculated during active solar shading states.
# All safety, lifecycle, and manual states are explicitly excluded.
# ---------------------------------------------------------------------------

TILT_ACTIVE_STATES: frozenset[ShadingState] = frozenset({
    ShadingState.STRONG_SHADE,
    ShadingState.NORMAL_SHADE,
    ShadingState.LIGHT_SHADE,
})
"""States during which tilt calculation produces a target.

Deliberately excludes OPEN (no shading wanted), STORM_SAFE / WIND_SAFE
(safety tier — never touch additional controls), MANUAL_OVERRIDE, NIGHT_CLOSED,
and ABSENCE_CLOSED."""

# ---------------------------------------------------------------------------
# Elevation bands (v1.0 defaults — intentionally coarse / explainable)
# ---------------------------------------------------------------------------

LOW_ELEVATION_DEG: float = 15.0
"""Sun elevation below which the low tilt value is used.

Low elevation → oblique incidence → stronger glare → close slats more."""

HIGH_ELEVATION_DEG: float = 35.0
"""Sun elevation above which the high tilt value is used.

High elevation → more overhead → open slats slightly while cover handles depth."""

TILT_LOW: int = 25
"""Tilt target (HA convention) when sun elevation is below LOW_ELEVATION_DEG."""

TILT_MID: int = 45
"""Tilt target (HA convention) when sun elevation is between the two thresholds."""

TILT_HIGH: int = 65
"""Tilt target (HA convention) when sun elevation is above HIGH_ELEVATION_DEG."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_simple_tilt_target(
    *,
    hardware_type: CoverHardwareType,
    supports_tilt: bool,
    tilt_control_enabled: bool,
    in_solar_sector: bool,
    sun_elevation_deg: float | None,
    new_state: ShadingState,
) -> int | None:
    """Return a tilt target in HA convention [0, 100], or None.

    Returns None when any gate condition is not satisfied, which instructs the
    coordinator not to include a tilt target in the ExecutionPlan.  This
    guarantees that no tilt service call is ever issued for non-tilt covers,
    non-shading states, or when sun geometry data is unavailable.

    Parameters
    ----------
    hardware_type:
        Physical cover type.  Only VENETIAN_BLIND produces a tilt target.
    supports_tilt:
        True when the HA cover entity reports CoverEntityFeature.SET_TILT_POSITION.
    tilt_control_enabled:
        True when the hardware settings for this CoverGroup have tilt control
        activated (default_hardware_settings() → "tilt_control_enabled").
    in_solar_sector:
        True when the window is within the sun's tolerance window this cycle
        (SunGeometry.is_in_tolerance_window).
    sun_elevation_deg:
        Sun elevation in degrees.  Negative values mean the sun is below the
        horizon.  None means no sun position data is available this cycle.
    new_state:
        The ShadingState decided by TierOrchestrator for this cycle.
    """
    # Gate 1: only VENETIAN_BLIND supports automatic tilt targets.
    if hardware_type is not CoverHardwareType.VENETIAN_BLIND:
        return None

    # Gate 2: physical capability and user configuration.
    if not supports_tilt or not tilt_control_enabled:
        return None

    # Gate 3: sun must be geometrically relevant for this window.
    if not in_solar_sector:
        return None

    # Gate 4–5: sun position must be available and above the horizon.
    if sun_elevation_deg is None or sun_elevation_deg <= 0.0:
        return None

    # Gate 6: only compute tilt during active shading states.
    if new_state not in TILT_ACTIVE_STATES:
        return None

    # All gates passed — derive tilt target from sun elevation band.
    if sun_elevation_deg < LOW_ELEVATION_DEG:
        raw = TILT_LOW
    elif sun_elevation_deg <= HIGH_ELEVATION_DEG:
        raw = TILT_MID
    else:
        raw = TILT_HIGH

    return max(0, min(100, raw))
