"""Forecast Learning Scheduler — Phase 9F12k-6.

Wires the Forecast Learning collection pipeline to the Home Assistant lifecycle.

  _read_sensor(hass, entity_id) -> float | None
      Pure helper; reads one sensor state and converts it to float.
      Returns None for absent, unavailable, unknown, or non-numeric states.
      Never raises.

  async_setup_forecast_learning(hass, entry, store, adapter, *, ...)
      Register one hourly Forecast timer and one 30-minute Reality timer via
      async_track_time_interval.  Fire an immediate first run for each via
      entry.async_create_background_task so that collection starts without
      waiting for the first interval to elapse, AND so the task is tied to
      the config entry's own lifecycle tracking (T17 — a bare
      hass.async_create_task here was previously untracked by HA's entry
      unload machinery entirely and could keep running indefinitely past
      unload/removal).
      Return (cancel_forecast, cancel_reality) so that async_unload_entry can
      stop both timers AND cancel the corresponding startup task cleanly.
      Return None when forecast_entity_id is falsy — Forecast Learning is then
      silently inactive and no timers or tasks are created.

HA-dependency boundary
-----------------------
async_track_time_interval is imported lazily inside async_setup_forecast_learning
when the _track_interval injection parameter is not supplied (production path).
Tests inject a mock tracker so that no HA package is ever loaded during testing.
No other HA symbols appear anywhere in this module.

Tier safety
-----------
No threshold is read or written.  No runtime state is modified.  This module
only schedules the existing orchestrator functions and adds no new collection
or matching policy.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .forecast_orchestrator import (
    async_collect_forecast_cycle,
    async_collect_reality_cycle,
)
from ..models.forecast_store import ForecastLearningStore

_log = logging.getLogger(__name__)

# Local copies of HA state string constants — avoids any homeassistant import.
_STATE_UNAVAILABLE = "unavailable"
_STATE_UNKNOWN     = "unknown"


# ---------------------------------------------------------------------------
# Sensor helper
# ---------------------------------------------------------------------------

def _read_sensor(hass: Any, entity_id: str | None) -> float | None:
    """Read a sensor entity state from hass and convert it to float.

    Parameters
    ----------
    hass:
        Home Assistant core object (duck-typed).
    entity_id:
        entity_id to look up.  None returns None immediately.

    Returns
    -------
    float | None
        Converted sensor value, or None when the entity is absent,
        its state is "unavailable" or "unknown", or the state string
        cannot be parsed as a float.

    Never raises.
    """
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    if state.state in (_STATE_UNAVAILABLE, _STATE_UNKNOWN):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------

async def async_setup_forecast_learning(
    hass: Any,
    entry: Any,
    store: ForecastLearningStore,
    adapter: Any,
    *,
    forecast_entity_id: str | None,
    temp_entity_id: str | None,
    cloud_entity_id: str | None,
    solar_entity_id: str | None,
    _track_interval: Callable | None = None,
    _create_background_task: Callable | None = None,
) -> tuple[Callable[[], None], Callable[[], None]] | None:
    """Register Forecast and Reality collection timers and fire initial runs.

    Parameters
    ----------
    hass:
        Home Assistant core object (duck-typed).
    entry:
        The owning config entry (duck-typed: needs async_create_background_task).
        T17: the two immediate startup tasks below are tied to it, the same way
        coordinator.py already ties its own event-triggered refresh tasks to
        the entry, so HA's entry-unload machinery tracks and can cancel/await
        them instead of them being invisible bare hass tasks.
    store, adapter:
        Restored ForecastLearningStore and its duck-typed persistence adapter.
    forecast_entity_id:
        entity_id of the configured weather entity used for forecast collection.
        When falsy, Forecast Learning is inactive — None is returned, no timers
        are registered, and no tasks are created.
    temp_entity_id, cloud_entity_id, solar_entity_id:
        entity_ids of the sensors feeding the Reality Collector.  Any may be None.
    _track_interval:
        Injection point for tests.  When None (production), the HA function
        homeassistant.helpers.event.async_track_time_interval is imported lazily
        so that this module never imports HA at load time.
    _create_background_task:
        Injection point for tests.  When None (production),
        entry.async_create_background_task is used.

    Returns
    -------
    tuple[Callable, Callable] | None
        (cancel_forecast, cancel_reality) when Learning is active. Each also
        cancels the corresponding startup task (T17).
        None when forecast_entity_id is falsy (Learning inactive).
    """
    if not forecast_entity_id:
        _log.info(
            "ForecastScheduler: no forecast weather entity configured — "
            "Forecast Learning is inactive"
        )
        return None

    if _track_interval is None:
        from homeassistant.helpers.event import async_track_time_interval  # lazy HA import
        _track_interval = async_track_time_interval

    if _create_background_task is None:
        def _create_background_task(coro, name):
            return entry.async_create_background_task(hass, coro, name)

    # ------------------------------------------------------------------
    # Forecast callback — runs every hour and at startup
    # ------------------------------------------------------------------

    async def _forecast_callback(_now: datetime | None = None) -> None:
        try:
            await async_collect_forecast_cycle(hass, forecast_entity_id, store, adapter)
        except Exception:
            _log.error(
                "ForecastScheduler: forecast collection callback raised unexpectedly"
            )

    # ------------------------------------------------------------------
    # Reality callback — runs every 30 minutes and at startup
    # ------------------------------------------------------------------

    async def _reality_callback(_now: datetime | None = None) -> None:
        try:
            await async_collect_reality_cycle(
                store,
                adapter,
                observed_at_utc=datetime.now(timezone.utc),
                cloud_coverage=_read_sensor(hass, cloud_entity_id),
                temperature=_read_sensor(hass, temp_entity_id),
                solar_irradiance=_read_sensor(hass, solar_entity_id),
            )
        except Exception:
            _log.error(
                "ForecastScheduler: reality collection callback raised unexpectedly"
            )

    # ------------------------------------------------------------------
    # Register recurring timers
    # ------------------------------------------------------------------

    cancel_forecast = _track_interval(hass, _forecast_callback, timedelta(hours=1))
    cancel_reality  = _track_interval(hass, _reality_callback,  timedelta(minutes=30))

    # ------------------------------------------------------------------
    # Immediate first runs — do not wait for the first interval to elapse.
    # T17: entry-tracked (see _create_background_task above) instead of a
    # bare hass.async_create_task, so unload/removal can cancel them instead
    # of leaving them to run indefinitely, untracked by anything.
    # ------------------------------------------------------------------

    startup_forecast_task = _create_background_task(
        _forecast_callback(), "smartshading_forecast_startup")
    startup_reality_task = _create_background_task(
        _reality_callback(), "smartshading_forecast_reality_startup")

    def _cancel_forecast() -> None:
        cancel_forecast()
        startup_forecast_task.cancel()

    def _cancel_reality() -> None:
        cancel_reality()
        startup_reality_task.cancel()

    return _cancel_forecast, _cancel_reality
