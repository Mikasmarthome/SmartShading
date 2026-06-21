"""HA Forecast Adapter — Phase 9F12k-4.

Bridges the Home Assistant weather entity API and the pure-Python
Forecast Collector Foundation.

Two functions:

  _parse_forecast_item(raw, cutoff_utc) -> ForecastEntry | None
      Pure Python.  No HA imports.  Converts one item from the
      weather.get_forecasts response dict into a ForecastEntry.
      Returns None when the item is missing required fields, contains
      an unparseable datetime, or falls beyond the 24-hour horizon.

  async_fetch_forecast_entries(hass, entity_id, forecast_created_utc)
      -> list[ForecastEntry]
      HA-dependent.  Checks entity state, calls weather.get_forecasts,
      iterates over the response, and delegates per-item parsing to
      _parse_forecast_item.  Never raises; always returns a list.

HA dependency strategy
-----------------------
hass is duck-typed (Any).  No HA package is imported anywhere in this
module — not even lazily.  HA state constants are defined locally as
plain strings; they have been stable for many years.

Field mapping
-------------
  raw["datetime"]          → target_utc   (required; UTC-aware datetime)
  raw["temperature"]       → temperature  (primary; float | None)
  raw["native_temperature"]→ temperature  (fallback if temperature absent)
  raw["cloud_coverage"]    → cloud_coverage (optional; float | None)
  solar_irradiance         → always None  (not available in standard entities)
  raw["templow"]           → ignored      (different statistical quantity)
  raw["condition"]         → ignored      (string-based; too noisy for trust)

Horizon limit
-------------
Items whose target_utc > forecast_created_utc + 24 h are silently
discarded.  Beyond 24 h the Reality Collector cannot be expected to
produce a matching RealitySnapshot before the 90-day prune window.

Error handling
--------------
Per-item errors (missing / unparseable datetime) skip only that item.
Entity-level errors (unavailable, not found) and service errors return
an empty list.  Nothing is ever propagated to the Coordinator.

Tier safety
-----------
No threshold is read or written.  No runtime state is modified.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .forecast_collector import ForecastEntry

_log = logging.getLogger(__name__)

# Local copies of HA state string constants — avoids any homeassistant import.
_STATE_UNAVAILABLE = "unavailable"
_STATE_UNKNOWN     = "unknown"

# Maximum forecast horizon accepted per collection cycle.
_MAX_HORIZON_HOURS: int = 24


# ---------------------------------------------------------------------------
# Pure-Python item parser
# ---------------------------------------------------------------------------

def _parse_forecast_item(
    raw: dict[str, Any],
    cutoff_utc: datetime,
) -> ForecastEntry | None:
    """Parse one forecast item from weather.get_forecasts into a ForecastEntry.

    Returns None when:
      - "datetime" key is absent or not parseable as an ISO 8601 string
      - target_utc is strictly after cutoff_utc (beyond the 24-hour horizon)

    For temperature, "temperature" takes priority over "native_temperature".
    cloud_coverage and solar_irradiance are always optional.
    solar_irradiance is always None in this implementation.

    No HA imports.  Safe to call in tests without a hass instance.

    Parameters
    ----------
    raw:
        A single forecast dict from the weather.get_forecasts response list.
    cutoff_utc:
        Inclusive upper bound for target_utc
        (normally forecast_created_utc + 24 h).
    """
    # --- datetime (required) ------------------------------------------------
    dt_raw = raw.get("datetime")
    if dt_raw is None:
        return None

    try:
        # Replace "Z" for Python < 3.11 compatibility; fromisoformat handles
        # "+00:00" in all supported Python versions.
        dt_str = str(dt_raw).replace("Z", "+00:00")
        target_utc = datetime.fromisoformat(dt_str)
    except (ValueError, TypeError):
        _log.warning(
            "ForecastHaAdapter: could not parse forecast datetime %r — skipping item",
            dt_raw,
        )
        return None

    # Normalise naive datetimes (should not occur with standard HA providers,
    # but guard defensively) to UTC.
    if target_utc.tzinfo is None:
        target_utc = target_utc.replace(tzinfo=timezone.utc)

    # --- Horizon filter -----------------------------------------------------
    if target_utc > cutoff_utc:
        return None

    # --- temperature: raw["temperature"] > raw["native_temperature"] --------
    temperature: float | None = None
    for key in ("temperature", "native_temperature"):
        val = raw.get(key)
        if val is not None:
            try:
                temperature = float(val)
                break
            except (ValueError, TypeError):
                pass

    # --- cloud_coverage (optional) ------------------------------------------
    cloud_coverage: float | None = None
    cloud_raw = raw.get("cloud_coverage")
    if cloud_raw is not None:
        try:
            cloud_coverage = float(cloud_raw)
        except (ValueError, TypeError):
            pass

    # --- solar_irradiance: always None in this version --------------------------------
    # --- condition, templow: explicitly ignored --------------------------------

    return ForecastEntry(
        target_utc=target_utc,
        cloud_coverage=cloud_coverage,
        temperature=temperature,
        solar_irradiance=None,
    )


# ---------------------------------------------------------------------------
# HA-dependent async wrapper
# ---------------------------------------------------------------------------

async def async_fetch_forecast_entries(
    hass: Any,
    entity_id: str,
    forecast_created_utc: datetime,
) -> list[ForecastEntry]:
    """Fetch hourly forecast data from a HA weather entity.

    Calls weather.get_forecasts with type="hourly", parses each response
    item via _parse_forecast_item, and returns the resulting ForecastEntry
    list.

    Returns an empty list on any failure (entity unavailable, service error,
    empty response).  Never raises.  The Coordinator is never interrupted.

    Parameters
    ----------
    hass:
        Home Assistant core object (duck-typed; no HA import required).
    entity_id:
        entity_id of the configured weather entity
        (e.g. "weather.met_no_hourly").
    forecast_created_utc:
        UTC datetime at which this collection cycle started.
        Used to compute the 24-hour cutoff for horizon filtering.
    """
    # --- Entity availability check -----------------------------------------
    state = hass.states.get(entity_id)
    if state is None:
        # Entity absent: normal at startup (HA loads integrations in parallel).
        # Degraded to DEBUG so that the inevitable transient first-cycle miss
        # after every SmartShading reload does not appear in the warning log.
        _log.debug(
            "ForecastHaAdapter: weather entity %r not found — skipping collection",
            entity_id,
        )
        return []

    if state.state in (_STATE_UNAVAILABLE, _STATE_UNKNOWN):
        _log.warning(
            "ForecastHaAdapter: weather entity %r is %r — skipping collection",
            entity_id,
            state.state,
        )
        return []

    # --- Service call -------------------------------------------------------
    try:
        response = await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": entity_id, "type": "hourly"},
            blocking=True,
            return_response=True,
        )
    except Exception:
        _log.error(
            "ForecastHaAdapter: weather.get_forecasts failed for %r — skipping collection",
            entity_id,
        )
        return []

    # --- Response extraction ------------------------------------------------
    if not response:
        return []

    forecast_list: list[dict] = (
        response.get(entity_id, {}).get("forecast", [])
    )

    cutoff_utc = forecast_created_utc + timedelta(hours=_MAX_HORIZON_HOURS)

    # --- Per-item parsing ---------------------------------------------------
    entries: list[ForecastEntry] = []
    for raw_item in forecast_list:
        entry = _parse_forecast_item(raw_item, cutoff_utc)
        if entry is not None:
            entries.append(entry)

    return entries
