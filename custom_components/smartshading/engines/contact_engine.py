"""Contact sensor engine — normalizes window open/closed state.

Reads a binary_sensor entity that represents the physical window contact
(reed switch or similar).  Maps the raw HA state string to the ContactStatus
enum so that the rest of the integration never needs to reason about raw
HA state strings.

Absent-evidence semantics: an unavailable or unknown sensor → UNKNOWN.
The NightContactHold treats UNKNOWN conservatively:
  - Option A (block): UNKNOWN does NOT block the night move.
  - Catch-up: UNKNOWN does NOT trigger catch-up.  The hold stays blocked
    until a definitive CLOSED reading arrives (sensor fault never moves cover).
  - Option B (lift): UNKNOWN does NOT trigger HOLD_NIGHT_VENT.
  - Option B (return): UNKNOWN does NOT trigger RETURN_TO_NIGHT while venting.

Position convention note:
  All contact_engine output is ContactStatus (an enum). The coordinator
  translates OPEN/CLOSED/UNKNOWN to specific cover decisions. This module
  has no knowledge of cover positions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ContactStatus(Enum):
    """Normalized window contact state.

    OPEN    — window is physically open (contact broken).
    CLOSED  — window is physically closed (contact made).
    UNKNOWN — sensor unavailable, stale, or absent.
    """

    OPEN    = "open"
    CLOSED  = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ContactReading:
    """A single normalized contact sensor reading.

    Attributes
    ----------
    status:
        Normalized ContactStatus (OPEN / CLOSED / UNKNOWN).
    sensor_entity_id:
        The HA entity_id that was read.  None when no sensor is configured.
    raw_value:
        The raw HA state string ("on", "off", "unavailable", …) or None.
    read_at_utc:
        UTC timestamp of the HA state, or None if unknown.
    is_stale:
        True when the reading was not refreshed within ``staleness_s`` seconds.
        A stale reading keeps its normalized status but is flagged for diagnostics.
    """

    status: ContactStatus
    sensor_entity_id: str | None
    raw_value: str | None
    read_at_utc: datetime | None
    is_stale: bool = False


def normalize_contact_state(raw: str | None) -> ContactStatus:
    """Map a raw HA binary_sensor state to ContactStatus.

    HA convention for binary_sensor with device_class=window or door:
      "on"  = window/door is OPEN  (contact broken)
      "off" = window/door is CLOSED (contact made)

    Any unavailable/unknown/None state → UNKNOWN (fail-safe).
    Case-insensitive to tolerate non-standard integrations.
    """
    if raw is None:
        return ContactStatus.UNKNOWN
    lower = raw.lower()
    if lower == "on":
        return ContactStatus.OPEN
    if lower == "off":
        return ContactStatus.CLOSED
    return ContactStatus.UNKNOWN


def build_contact_reading(
    *,
    entity_id: str | None,
    hass_state: str | None,
    read_at_utc: datetime | None,
    now_utc: datetime,
    staleness_s: float = 600.0,
) -> ContactReading:
    """Build a normalized ContactReading from raw HA state data.

    Parameters
    ----------
    entity_id:
        HA entity_id of the contact sensor, or None if unconfigured.
    hass_state:
        Raw state string from hass.states.get(entity_id).state,
        or None if the entity is absent / not configured.
    read_at_utc:
        ``state.last_updated`` from HA (UTC), or None if unknown.
    now_utc:
        Current UTC clock time for staleness evaluation.
    staleness_s:
        Maximum age (seconds) before a reading is flagged stale.
        Default 600 s (10 min).  Stale readings keep their normalized
        status but is_stale=True surfaces in diagnostics.

    Returns
    -------
    ContactReading
        Always returns a valid ContactReading.  When entity_id is None,
        returns status=UNKNOWN, entity_id=None.
    """
    if entity_id is None:
        return ContactReading(
            status=ContactStatus.UNKNOWN,
            sensor_entity_id=None,
            raw_value=None,
            read_at_utc=None,
            is_stale=False,
        )

    status = normalize_contact_state(hass_state)

    is_stale = False
    if read_at_utc is not None:
        age_s = (now_utc - read_at_utc).total_seconds()
        if age_s > staleness_s:
            is_stale = True

    return ContactReading(
        status=status,
        sensor_entity_id=entity_id,
        raw_value=hass_state,
        read_at_utc=read_at_utc,
        is_stale=is_stale,
    )
