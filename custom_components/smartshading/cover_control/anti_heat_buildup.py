"""Anti-Heat-Buildup — Step 9G10f-c.

Pure Python, no Home Assistant dependency.

Prevents heat buildup between a roller shutter (Rollladen) and the window
glass during periods of direct solar radiation.  When the shutter is nearly
or fully closed and strong sun hits the glazing, the trapped air heats
significantly.  This module raises the target open position to a minimum
so convection can carry the heat away.

WHAT THIS MODULE DOES
---------------------
  apply_anti_heat_buildup(...)
      Returns the final HA target, whether the clamp was applied, and the
      pre-clamp original target (for diagnostics).

HARDWARE SCOPE (v1.0)
---------------------
  Only CoverHardwareType.ROLLER_SHUTTER.
  VENETIAN_BLIND  — tilt logic handles solar load differentiation.
  EXTERIOR_SCREEN — no typical enclosed air-gap buildup.
  AWNING          — wind/safety mechanics are primary.
  GENERIC         — no hardware assumption can be made.

ACTIVATION CONDITIONS (ALL must be true)
-----------------------------------------
  enabled              — anti_heat_buildup_enabled is True in hardware settings
  hardware_type        — CoverHardwareType.ROLLER_SHUTTER only
  new_state not exempt — safety, override, and night bypass unconditionally
  absence rule         — ABSENCE_CLOSED skipped unless allow_during_absence=True
  in_solar_sector      — sun is within the window's azimuth tolerance window
  strong radiation     — effective_exposure_wm2 >= ANTI_HEAT_BUILDUP_MIN_EXPOSURE_WM2
  target below minimum — target_position_ha < ahb_position_ha

EXEMPT STATES (always bypass — set ANTI_HEAT_BUILDUP_EXEMPT_STATES)
--------------------------------------------------------------------
  STORM_SAFE      — safety commands take unconditional priority
  WIND_SAFE       — safety commands take unconditional priority
  MANUAL_OVERRIDE — user explicitly chose a position; must not be overridden
  NIGHT_CLOSED    — intentional night closure; solar exposure is zero at night

  ABSENCE_CLOSED is conditional:
    allow_during_absence=False (default) → exempt; full closure is respected
    allow_during_absence=True           → active; buildup protection applies

HA CONVENTION NOTE
------------------
  target_position_ha  0 = closed (max shade), 100 = open (no shade)
  ahb_position_ha    10 → the cover must remain at least 10 % open during
                          anti-heat-buildup conditions
  Clamp logic: if target_position_ha < ahb_position_ha → final = ahb_position_ha
"""
from __future__ import annotations

from ..models.cover_group import CoverHardwareType
from ..state_machine.states import ShadingState

# States that unconditionally exempt a window from anti-heat-buildup.
# ABSENCE_CLOSED is handled separately via allow_during_absence.
ANTI_HEAT_BUILDUP_EXEMPT_STATES: frozenset[ShadingState] = frozenset({
    ShadingState.STORM_SAFE,
    ShadingState.WIND_SAFE,
    ShadingState.MANUAL_OVERRIDE,
    ShadingState.NIGHT_CLOSED,
})

# Minimum effective solar exposure (W/m²) on the window surface before
# anti-heat-buildup activates.  Below this threshold the air-gap temperature
# rise is not large enough to warrant forced ventilation.
ANTI_HEAT_BUILDUP_MIN_EXPOSURE_WM2: float = 100.0


def apply_anti_heat_buildup(
    target_position_ha: int | None,
    ahb_position_ha: int | None,
    enabled: bool,
    hardware_type: CoverHardwareType,
    new_state: ShadingState,
    in_solar_sector: bool,
    effective_exposure_wm2: float | None,
    allow_during_absence: bool = False,
) -> tuple[int | None, bool, int | None]:
    """Apply anti-heat-buildup minimum open position if all conditions are met.

    Parameters
    ----------
    target_position_ha:
        Cover target in HA convention (0=closed, 100=open).
        None → function is a no-op; returns (None, False, None).
    ahb_position_ha:
        Minimum open percentage (HA) during anti-heat-buildup protection.
        None disables the clamp regardless of other parameters.
    enabled:
        Whether anti-heat-buildup is enabled in the hardware settings.
    hardware_type:
        CoverHardwareType of the cover.  Only ROLLER_SHUTTER is protected.
    new_state:
        ShadingState decided by TierOrchestrator this cycle.
        Exempt states bypass the clamp unconditionally.
    in_solar_sector:
        True when the sun is within the window's azimuth tolerance window.
    effective_exposure_wm2:
        Effective solar irradiance on the window surface (W/m²).
        None or below ANTI_HEAT_BUILDUP_MIN_EXPOSURE_WM2 → no clamp.
    allow_during_absence:
        When True, anti-heat-buildup activates even during ABSENCE_CLOSED.
        Default False: full closure during absence is always respected.

    Returns
    -------
    (final_target_ha, was_applied, pre_clamp_target_ha)
        final_target_ha
            Target after applying (or not applying) the clamp.  None when
            target_position_ha is None.
        was_applied
            True only when the clamp actually raised target_position_ha.
        pre_clamp_target_ha
            Original target before the clamp.  None when was_applied=False.
    """
    if target_position_ha is None:
        return None, False, None
    if not enabled:
        return target_position_ha, False, None
    if ahb_position_ha is None:
        return target_position_ha, False, None
    if hardware_type is not CoverHardwareType.ROLLER_SHUTTER:
        return target_position_ha, False, None
    if new_state in ANTI_HEAT_BUILDUP_EXEMPT_STATES:
        return target_position_ha, False, None
    if new_state is ShadingState.ABSENCE_CLOSED and not allow_during_absence:
        return target_position_ha, False, None
    if not in_solar_sector:
        return target_position_ha, False, None
    if effective_exposure_wm2 is None or effective_exposure_wm2 < ANTI_HEAT_BUILDUP_MIN_EXPOSURE_WM2:
        return target_position_ha, False, None
    if target_position_ha >= ahb_position_ha:
        return target_position_ha, False, None
    return ahb_position_ha, True, target_position_ha
