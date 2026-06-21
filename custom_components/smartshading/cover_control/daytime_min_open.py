"""Daytime Minimum Open Position — Step 9G10f-b.

Pure Python, no Home Assistant dependency.

Prevents SmartShading from closing a cover fully during normal daytime solar
shading (heat protection, glare protection, solar shading tiers).  The clamp
is defined in HA convention (0=closed, 100=open): when the computed target
would be below the minimum, it is raised to the minimum.

WHAT THIS MODULE DOES
---------------------
  apply_daytime_min_open(target_position_ha, daytime_min_ha, new_state)
      Returns the final HA target, whether the clamp was applied, and the
      pre-clamp original target (for diagnostics).

WHAT IS EXEMPT FROM THE CLAMP
------------------------------
  STORM_SAFE / WIND_SAFE   — safety commands bypass everything
  MANUAL_OVERRIDE          — user explicitly chose a position
  NIGHT_CLOSED             — lifecycle night closing is intentional
  ABSENCE_CLOSED           — explicit absence configuration

  Only LIGHT_SHADE / NORMAL_SHADE / STRONG_SHADE / OPEN go through the
  clamp.  OPEN will never trigger because target_position_ha = 100 ≥ any
  reasonable daytime_min_ha.

HA CONVENTION NOTE
------------------
  target_position_ha  0 = closed (max shade), 100 = open (no shade)
  daytime_min_ha     10 → the cover must be at least 10 % open during
                           daytime solar shading

  Clamp logic: if target_position_ha < daytime_min_ha:
                   final = daytime_min_ha   (raise toward open)
"""
from __future__ import annotations

from ..state_machine.states import ShadingState


# States that are EXEMPT from the daytime minimum clamp.
# All other states (LIGHT_SHADE, NORMAL_SHADE, STRONG_SHADE, OPEN) go
# through the clamp.  OPEN never actually triggers it (target 100 ≥ min).
DAYTIME_CLAMP_EXEMPT_STATES: frozenset[ShadingState] = frozenset({
    ShadingState.STORM_SAFE,
    ShadingState.WIND_SAFE,
    ShadingState.MANUAL_OVERRIDE,
    ShadingState.NIGHT_CLOSED,
    ShadingState.ABSENCE_CLOSED,
})


def apply_daytime_min_open(
    target_position_ha: int,
    daytime_min_ha: int | None,
    new_state: ShadingState,
) -> tuple[int, bool, int | None]:
    """Apply the daytime minimum open position clamp if applicable.

    Parameters
    ----------
    target_position_ha:
        The cover target in HA convention (0=closed, 100=open).
    daytime_min_ha:
        Minimum open percentage in HA convention.  None disables the clamp
        for this window (GENERIC, VENETIAN_BLIND, AWNING defaults).
    new_state:
        The ShadingState decided by the TierOrchestrator this cycle.
        Exempt states bypass the clamp entirely.

    Returns
    -------
    (final_target_ha, was_applied, pre_clamp_target_ha)
        final_target_ha
            The final target after applying (or not applying) the clamp.
        was_applied
            True when the clamp raised target_position_ha to daytime_min_ha.
        pre_clamp_target_ha
            The original target before the clamp.  None when was_applied=False.
    """
    # No minimum configured for this hardware type.
    if daytime_min_ha is None:
        return target_position_ha, False, None

    # Exempt states bypass the clamp unconditionally.
    if new_state in DAYTIME_CLAMP_EXEMPT_STATES:
        return target_position_ha, False, None

    # Target already at or above the minimum — no change needed.
    if target_position_ha >= daytime_min_ha:
        return target_position_ha, False, None

    # Clamp: raise the target to the minimum open percentage.
    return daytime_min_ha, True, target_position_ha
