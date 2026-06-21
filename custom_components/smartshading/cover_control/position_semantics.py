"""Central position semantics for SmartShading — Phase 9G1.

This module is the single, authoritative source of truth for position
convention conversion between SmartShading's internal model and the
Home Assistant cover entity model.

THE FUNDAMENTAL RULE
--------------------
All SmartShading code (evaluators, learning, BehaviorConfig, WindowDecision,
AdaptationTrace, StateGuard) works exclusively in the INTERNAL convention.

Conversion to/from HA convention happens ONLY at the execution boundary,
using to_ha_position() and to_internal_position() defined here.

No other code may perform inline arithmetic (100 - x) for position
conversion — always call the named function.

INTERNAL CONVENTION (SmartShading)
-----------------------------------
    0   = fully open / no shading / retracted
    100 = fully shaded / closed / extended

EXAMPLES:
    OPEN          → 0
    LIGHT_SHADE   → 60
    NORMAL_SHADE  → 75
    STRONG_SHADE  → 90
    NIGHT_CLOSED  → 100
    STORM_SAFE    → 0  (retracted/open — safest for awnings and screens)
    WIND_SAFE     → 0  (retracted/open)

HOME ASSISTANT COVER CONVENTION
---------------------------------
    0   = closed
    100 = open

CONVERSION FORMULA
-------------------
    ha_position = 100 - internal_position     (standard)
    ha_position = internal_position           (invert_position=True covers)

INVERT POSITION
---------------
Some cover integrations (e.g. certain Somfy/Z-Wave covers) already invert
the direction relative to HA standard.  For these covers, invert_position=True
in CoverCapability means "the numeric value is identical in both conventions"
— no arithmetic conversion is needed.

SAFETY POSITION SEMANTICS
--------------------------
For Storm and Wind safety (STORM_SAFE / WIND_SAFE):
    Internal position 0 = retracted/open = no wind load.
    This is correct for awnings, screens, and marquises.

    For covers that should CLOSE during storm (e.g. roller shutters protecting
    a window frame), set ExecutionCapability.safe_position_internal = 100.
    The CommandFilter will use this value instead of the evaluator default.

POSITION VALUES
---------------
All positions are integers in [0, 100], inclusive.
Out-of-range inputs are clamped to [0, 100] before conversion.
Negative values and values > 100 are never produced by this module.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Named constants for pole positions — use these instead of magic numbers
# ---------------------------------------------------------------------------

#: Internal convention: fully open / no shading / retracted
INTERNAL_OPEN: int = 0

#: Internal convention: fully shaded / closed / extended
INTERNAL_CLOSED: int = 100

#: HA convention: cover at the open endpoint
HA_OPEN: int = 100

#: HA convention: cover at the closed endpoint
HA_CLOSED: int = 0

# ---------------------------------------------------------------------------
# Internal safety positions for ShadingState outcomes
# These mirror the constants in the corresponding evaluators and are
# reproduced here so position_semantics.py is the single reference point
# for what each state means positionally.
# ---------------------------------------------------------------------------

#: STORM_SAFE — retracted/open in internal convention (0 = no wind load).
#: For roller shutters that should close during storm, override via
#: ExecutionCapability.safe_position_internal.
STORM_SAFE_POSITION_INTERNAL: int = 0

#: WIND_SAFE — same semantics as STORM_SAFE.
WIND_SAFE_POSITION_INTERNAL: int = 0

#: OPEN state — fully open, no shading.
OPEN_POSITION_INTERNAL: int = 0


# ---------------------------------------------------------------------------
# Core conversion functions
# ---------------------------------------------------------------------------

def clamp_position(value: int) -> int:
    """Clamp *value* to the valid position range [0, 100].

    All position functions pass inputs through this clamp, so callers
    never need to validate range before calling to_ha_position() or
    to_internal_position().
    """
    return max(0, min(100, value))


def to_ha_position(internal: int, *, invert: bool = False) -> int:
    """Convert a SmartShading internal position to a HA cover position.

    This is the ONLY place in SmartShading that may cross the internal→HA
    convention boundary.  Do NOT inline ``100 - x`` anywhere else.

    Parameters
    ----------
    internal:
        SmartShading internal position (0=open, 100=shaded).
        Clamped to [0, 100] before conversion.
    invert:
        True for covers where the HA integration already inverts the
        direction (0=open, 100=closed in the integration's own convention).
        In this case, the numeric value is used as-is.

    Returns
    -------
    int
        HA cover position in [0, 100].

    Examples
    --------
    >>> to_ha_position(0)    # internal open → HA open
    100
    >>> to_ha_position(100)  # internal closed → HA closed
    0
    >>> to_ha_position(75)   # NORMAL_SHADE → HA 25
    25
    >>> to_ha_position(75, invert=True)  # inverted cover: pass through
    75
    """
    clamped = clamp_position(internal)
    if invert:
        return clamped
    return 100 - clamped


def to_internal_position(ha: int, *, invert: bool = False) -> int:
    """Convert a HA cover position to a SmartShading internal position.

    This is the ONLY place in SmartShading that may cross the HA→internal
    convention boundary.  Do NOT inline ``100 - x`` anywhere else.

    Parameters
    ----------
    ha:
        HA cover position (0=closed, 100=open).
        Clamped to [0, 100] before conversion.
    invert:
        True for covers where the HA integration already inverts the
        direction.  In this case, the numeric value is used as-is.

    Returns
    -------
    int
        SmartShading internal position in [0, 100].

    Examples
    --------
    >>> to_internal_position(100)  # HA open → internal open
    0
    >>> to_internal_position(0)    # HA closed → internal closed
    100
    >>> to_internal_position(25)   # HA 25 → internal 75 (NORMAL_SHADE)
    75
    """
    clamped = clamp_position(ha)
    if invert:
        return clamped
    return 100 - clamped


# ---------------------------------------------------------------------------
# Position comparison helpers
# ---------------------------------------------------------------------------

def positions_within_tolerance(
    a_internal: int,
    b_internal: int,
    tolerance: int,
) -> bool:
    """True when two internal positions differ by at most *tolerance* units.

    Used by CommandFilter to suppress commands when the cover is already
    close enough to the target.

    Both positions are in internal convention (0=open, 100=shaded).
    *tolerance* is in the same units (position points, not percent).

    A tolerance of 3 means: if the cover is already within ±3 units of
    the target, no new command is sent.
    """
    return abs(a_internal - b_internal) <= tolerance
