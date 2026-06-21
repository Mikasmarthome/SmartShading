"""Cover Entity Snapshot — Phase 9G3a.

Immutable, pure-Python state model for one cover entity's current state as
observed in Home Assistant.

No Home Assistant import.  No service calls.  No async.

PURPOSE
-------
Before SmartShading sends any cover command it needs a reliable, convention-
correct picture of the cover's current state.  This module provides exactly
that, with all raw HA attribute parsing and position-convention conversion
handled in one place.

The Coordinator reads hass.states.get(entity_id) and passes the raw state
string and attributes dict to build_cover_entity_snapshot() — the only function
that touches HA-source data.  Everything downstream (CommandFilter, CoverIntent,
CoverController) sees only CoverEntitySnapshot, never raw HA attributes.

POSITION CONVENTIONS IN THIS MODULE
------------------------------------
  current_position_ha       Raw value from `current_position` attribute,
                            clamped to [0, 100].  This is the HA-convention
                            number exactly as the integration reports it:
                            0 = closed, 100 = open for standard covers.
                            For invert=True covers, 0 = open, 100 = closed —
                            the raw attribute is passed as-is; see note below.

  current_position_internal Derived from current_position_ha via
                            to_internal_position(ha, invert=invert).
                            Always in SmartShading internal convention:
                            0 = open / retracted, 100 = shaded / closed.

  assumed_position_internal Supplied externally by the Coordinator from
                            AssumedStateManager.  SmartShading internal
                            convention.  Used in preference to
                            current_position_internal for covers without
                            reliable feedback.

INVERT NOTE
-----------
Some cover integrations (e.g. certain Somfy/Z-Wave bindings) invert the
direction: the integration reports 0 = open, 100 = closed.  For these covers,
CoverCapability.invert_position = True.

`current_position_ha` stores the raw attribute value regardless of inversion
(so it reflects what the integration actually reports).  Only
`current_position_internal` is guaranteed to be in SmartShading convention.

AVAILABILITY
------------
available = False when:
  - entity not found in HA (state is None)
  - state == "unavailable"  (entity present but unreachable)
  - state == "unknown"      (entity present but state not yet reported)

Both "unavailable" and "unknown" make the entity unsafe to command:
  - unavailable: HA cannot communicate with the cover — any service call fails.
  - unknown: HA has not received a state since startup — we don't know whether
    the cover is open, closed, or moving.  Sending a command could cause
    physical damage (e.g. commanding open when a blind is already at max open).

When available = False, current_position_ha and current_position_internal are
None regardless of what the attributes contain (they are not meaningful).

FEEDBACK-POOR COVERS
--------------------
For Somfy RTS / ESP Somfy and other RF-relay covers where the integration
cannot confirm the cover's actual position:
  has_position_feedback = False   (reflects has_reliable_position_feedback)
  current_position_internal may still be populated from the integration's
  optimistic / last-known attribute — but it should not be trusted for
  tolerance comparisons.
  assumed_position_internal (from AssumedStateManager) is the better source.

COMMANDFILTER COMPATIBILITY
---------------------------
CoverEntitySnapshot provides exactly the fields CommandFilter.evaluate() needs:
  snapshot.available           → is_cover_available
  snapshot.current_position_internal → current_position_internal
  (assumed_position_internal for unreliable covers)

No CommandFilter refactoring is required in 9G3a — the Coordinator extracts
these fields when calling CommandFilter.evaluate().
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .position_semantics import clamp_position, to_internal_position

# ---------------------------------------------------------------------------
# HA state string constants
# ---------------------------------------------------------------------------

#: Entity or integration is not reachable.
_STATE_UNAVAILABLE: str = "unavailable"

#: Entity exists but HA has not received a state yet.
_STATE_UNKNOWN: str = "unknown"

#: Cover is actively opening (moving toward open endpoint).
_STATE_OPENING: str = "opening"

#: Cover is actively closing (moving toward closed endpoint).
_STATE_CLOSING: str = "closing"

#: States that make the entity unsafe to command.
_UNAVAILABLE_STATES: frozenset[str] = frozenset({_STATE_UNAVAILABLE, _STATE_UNKNOWN})


# ---------------------------------------------------------------------------
# CoverEntitySnapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CoverEntitySnapshot:
    """Immutable snapshot of one cover entity's state at one point in time.

    Created once per coordinator cycle per cover entity by
    build_cover_entity_snapshot().  All position values use their explicitly
    documented convention — never mix them.

    FIELD CONVENTIONS
    -----------------
    current_position_ha       HA convention: 0=closed, 100=open
                              (raw attribute value, before inversion)
    current_position_internal SmartShading internal: 0=open, 100=shaded
                              (after applying to_internal_position)
    assumed_position_internal SmartShading internal, from AssumedStateManager
    current_tilt              HA tilt convention (not inverted in this version)
    """

    entity_id: str
    """HA entity_id of the cover (e.g. 'cover.south_window')."""

    available: bool
    """True only when the entity exists and is neither 'unknown' nor
    'unavailable'.  Commands may only be sent when available=True."""

    state: str | None
    """Raw HA state string: 'open', 'closed', 'opening', 'closing',
    'unknown', 'unavailable', or None (entity not found in HA)."""

    current_position_ha: int | None
    """Raw position from the `current_position` attribute, in HA convention
    [0, 100]: 0=closed, 100=open.  None when the entity is unavailable,
    the attribute is absent, or the value is non-numeric.

    For invert=True covers this is the raw value the integration reports
    (which may be 0=open, 100=closed) — still useful for logging."""

    current_position_internal: int | None
    """Position in SmartShading internal convention [0, 100]:
    0=open/retracted, 100=shaded/closed.
    Derived from current_position_ha via to_internal_position(invert=invert).
    None when current_position_ha is None."""

    current_tilt: int | None
    """Current tilt position [0, 100] in HA tilt convention.
    None when the entity does not report tilt or the value is non-numeric.
    Not inverted in this version."""

    is_opening: bool
    """True when HA state is 'opening' (cover is moving toward open)."""

    is_closing: bool
    """True when HA state is 'closing' (cover is moving toward closed)."""

    is_moving: bool
    """True when is_opening or is_closing (cover is in motion per HA)."""

    has_position_feedback: bool
    """True when the cover integration provides reliable position feedback.
    False for Somfy RTS, ESP Somfy, and other RF systems.
    Mirrors CoverCapability.has_reliable_position_feedback.

    When False: current_position_internal should NOT be used for tolerance
    comparisons.  Use assumed_position_internal from AssumedStateManager."""

    has_tilt_feedback: bool
    """True when a valid numeric tilt value was successfully read from the
    `current_tilt_position` attribute this cycle."""

    assumed_position_internal: int | None
    """Best-estimate position from AssumedStateManager, in SmartShading
    internal convention.  Supplied by the Coordinator.
    None if no assumed position is available yet for this cover (first cycle,
    or after a restart without a persisted state)."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_ha_numeric(value: Any) -> int | None:
    """Parse and clamp a position or tilt value from an HA attribute.

    Accepts int, float, and numeric strings.  Returns the value rounded and
    clamped to [0, 100], or None for any non-parseable input (None, non-
    numeric strings, collections, etc.).
    """
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return clamp_position(round(parsed))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_cover_entity_snapshot(
    *,
    entity_id: str,
    state: str | None,
    attributes: Mapping[str, Any],
    invert: bool = False,
    has_reliable_position_feedback: bool = True,
    assumed_position_internal: int | None = None,
) -> CoverEntitySnapshot:
    """Build a CoverEntitySnapshot from raw HA entity state data.

    The Coordinator supplies state/attributes from hass.states.get(), keeping
    this function free of any Home Assistant import.

    Parameters
    ----------
    entity_id:
        HA entity_id of the cover (e.g. 'cover.south_window').
    state:
        Entity's HA state string ('open', 'closed', 'opening', 'closing',
        'unknown', 'unavailable'), or None when the entity is not in HA.
    attributes:
        Entity's HA state attributes dict.  Pass an empty dict or empty
        Mapping when the entity was not found.
    invert:
        True for covers where the integration uses 0=open, 100=closed
        (opposite of standard HA convention).  Mirrors
        CoverCapability.invert_position.  Passed to to_internal_position()
        for position conversion.
    has_reliable_position_feedback:
        True when the integration provides reliable position feedback.
        False for Somfy RTS and similar RF systems.  Mirrors
        CoverCapability.has_reliable_position_feedback.  Stored as-is in
        snapshot.has_position_feedback.
    assumed_position_internal:
        Optional position from AssumedStateManager, in SmartShading internal
        convention.  Supplied by the Coordinator after calling
        AssumedStateManager.get_state().  None when no assumed state exists.

    Returns
    -------
    CoverEntitySnapshot
        Fully-populated, immutable snapshot ready for CommandFilter and
        CoverController.
    """
    # ------------------------------------------------------------------
    # Availability: None state, or "unknown"/"unavailable" → not available.
    # ------------------------------------------------------------------
    available = state is not None and state not in _UNAVAILABLE_STATES

    # ------------------------------------------------------------------
    # Motion flags from HA state string.
    # ------------------------------------------------------------------
    is_opening = state == _STATE_OPENING
    is_closing = state == _STATE_CLOSING
    is_moving = is_opening or is_closing

    # ------------------------------------------------------------------
    # Position: only meaningful when the entity is available.
    # Even for available entities the attribute may be absent (open/close
    # only covers that don't report a numeric position).
    # ------------------------------------------------------------------
    if available:
        raw_position = _parse_ha_numeric(attributes.get("current_position"))
    else:
        raw_position = None

    if raw_position is not None:
        current_position_ha = raw_position
        current_position_internal = to_internal_position(raw_position, invert=invert)
    else:
        current_position_ha = None
        current_position_internal = None

    # ------------------------------------------------------------------
    # Tilt: same robustness as position, no inversion in this version.
    # ------------------------------------------------------------------
    if available:
        raw_tilt = _parse_ha_numeric(attributes.get("current_tilt_position"))
    else:
        raw_tilt = None

    return CoverEntitySnapshot(
        entity_id=entity_id,
        available=available,
        state=state,
        current_position_ha=current_position_ha,
        current_position_internal=current_position_internal,
        current_tilt=raw_tilt,
        is_opening=is_opening,
        is_closing=is_closing,
        is_moving=is_moving,
        has_position_feedback=has_reliable_position_feedback,
        has_tilt_feedback=raw_tilt is not None,
        assumed_position_internal=assumed_position_internal,
    )
