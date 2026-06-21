"""Forecast Collection Orchestrator — Phase 9F12k-5.

Combines the pure-Python and HA-dependent building blocks into three
async functions that drive the complete Forecast Learning collection pipeline.

  async_run_startup_matching   Called once after ForecastLearningStore is
                               restored; matches all unmatched snapshots
                               against all stored realities.

  async_collect_reality_cycle  Runs every 30 minutes.  Accepts pre-read
                               sensor values (no HA interaction here),
                               creates a RealitySnapshot, runs matching
                               against the new reality, and saves.

  async_collect_forecast_cycle Runs every 60 minutes.  Calls the HA
                               weather adapter, stores new ForecastSnapshots.
                               No matching — targets lie in the future.

Dirty-flag contract
-------------------
Each function returns a bool that is True when the store was modified
(at least one new entry added).  Callers may use this for observability;
async_save() is already called internally when dirty.

Error-handling contract
-----------------------
None of the three functions ever propagates an exception.  An outer
try/except Exception wraps each function body; inner error boundaries
isolate the matching step.  The Coordinator (not yet wired) is never
interrupted.

HA-dependency boundary
-----------------------
Only async_collect_forecast_cycle interacts with HA via the duck-typed
hass argument.  async_collect_reality_cycle and async_run_startup_matching
are fully HA-free — sensor values are passed as plain Python floats.
The adapter parameter is also duck-typed (Any); in tests a lightweight
mock is injected.

Tier safety
-----------
No threshold is read or written.  No runtime state is modified.
Purely coordinates existing building blocks; adds no new policy.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .forecast_collector import build_forecast_snapshots
from .forecast_ha_adapter import async_fetch_forecast_entries
from .forecast_matcher import match_all
from .reality_collector import build_reality_snapshot
from ..models.forecast_store import ForecastLearningStore

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# async_run_startup_matching
# ---------------------------------------------------------------------------

async def async_run_startup_matching(
    store: ForecastLearningStore,
    adapter: Any,
) -> bool:
    """Match all unmatched ForecastSnapshots against all stored RealitySnapshots.

    Called once after ForecastPersistenceAdapter.async_restore().  Recovers
    ForecastRecords that could not be produced during a previous session
    because the Reality Collector had not yet run when the app restarted.

    Parameters
    ----------
    store:
        The restored ForecastLearningStore.
    adapter:
        Duck-typed persistence adapter; async_save() is called when dirty.

    Returns
    -------
    bool
        True if at least one new ForecastRecord was added.
    """
    try:
        unmatched = store.get_unmatched_snapshots()
        if not unmatched:
            return False

        realities = list(store.reality_snapshots.values())
        if not realities:
            return False

        try:
            new_records = match_all(unmatched, realities)
        except Exception:
            _log.error(
                "async_run_startup_matching: match_all raised unexpectedly — "
                "no records will be added this startup"
            )
            return False

        dirty = False
        for record in new_records:
            dirty |= store.add_forecast_record(record)

        if dirty:
            await adapter.async_save(store)

        return dirty

    except Exception:
        _log.error("async_run_startup_matching: unexpected error")
        return False


# ---------------------------------------------------------------------------
# async_collect_reality_cycle
# ---------------------------------------------------------------------------

async def async_collect_reality_cycle(
    store: ForecastLearningStore,
    adapter: Any,
    *,
    observed_at_utc: datetime,
    cloud_coverage: float | None,
    temperature: float | None,
    solar_irradiance: float | None,
) -> bool:
    """Run one 30-minute Reality Collection + Matching cycle.

    No HA interaction occurs inside this function.  The caller (Coordinator
    or Scheduler) reads sensor states from hass and passes the plain values.

    Flow:
      1. build_reality_snapshot()      — returns None if all values are None
      2. store.add_reality_snapshot()  — False on duplicate; skip matching
      3. match_all(unmatched, [new])   — only against the new reality
      4. store.add_forecast_record()   — for each matched pair
      5. async_save()                  — because the reality was new (dirty)

    Matching runs only when the reality snapshot is new.  Duplicate realities
    are silently ignored; the Coordinator must not save on a duplicate.

    Parameters
    ----------
    store, adapter:
        Live ForecastLearningStore and duck-typed persistence adapter.
    observed_at_utc:
        UTC timestamp of the sensor reading.  Must be timezone-aware.
    cloud_coverage, temperature, solar_irradiance:
        Pre-read sensor values; None when sensor is unavailable.

    Returns
    -------
    bool
        True when the reality snapshot was new (store was modified).
    """
    try:
        reality = build_reality_snapshot(
            observed_at_utc=observed_at_utc,
            cloud_coverage=cloud_coverage,
            temperature=temperature,
            solar_irradiance=solar_irradiance,
        )

        if reality is None:
            # All sensor values were None — nothing to store.
            return False

        if not store.add_reality_snapshot(reality):
            # Duplicate — already seen this minute; skip matching.
            return False

        # New reality — run matching against it only.
        unmatched = store.get_unmatched_snapshots()
        try:
            new_records = match_all(unmatched, [reality])
            for record in new_records:
                store.add_forecast_record(record)
        except Exception:
            _log.error(
                "async_collect_reality_cycle: matching failed — "
                "records may be incomplete for this cycle"
            )

        # Reality was new → dirty regardless of whether any records were produced.
        await adapter.async_save(store)
        return True

    except Exception:
        _log.error("async_collect_reality_cycle: unexpected error")
        return False


# ---------------------------------------------------------------------------
# async_collect_forecast_cycle
# ---------------------------------------------------------------------------

async def async_collect_forecast_cycle(
    hass: Any,
    entity_id: str,
    store: ForecastLearningStore,
    adapter: Any,
    *,
    forecast_created_utc: datetime | None = None,
) -> bool:
    """Run one hourly Forecast Collection cycle.

    Fetches hourly forecasts via the HA weather adapter, converts them to
    ForecastSnapshots, and stores the new ones.  No matching runs here —
    all forecast target times lie in the future and no RealitySnapshot can
    yet exist for them.

    Parameters
    ----------
    hass:
        HA core object (duck-typed).
    entity_id:
        entity_id of the configured weather entity.
    store, adapter:
        Live ForecastLearningStore and duck-typed persistence adapter.
    forecast_created_utc:
        UTC timestamp for this collection cycle.  Defaults to
        datetime.now(timezone.utc) when None.  Override in tests to
        produce deterministic snapshot IDs.

    Returns
    -------
    bool
        True when at least one new ForecastSnapshot was added.
    """
    try:
        if forecast_created_utc is None:
            forecast_created_utc = datetime.now(timezone.utc)

        entries = await async_fetch_forecast_entries(
            hass, entity_id, forecast_created_utc
        )
        # Empty list is normal (entity unavailable, service error, empty forecast).

        snapshots = build_forecast_snapshots(
            source_id=entity_id,
            forecast_created_utc=forecast_created_utc,
            entries=entries,
        )

        dirty = False
        for snap in snapshots:
            dirty |= store.add_forecast_snapshot(snap)

        if dirty:
            await adapter.async_save(store)

        return dirty

    except Exception:
        _log.error(
            "async_collect_forecast_cycle: unexpected error for entity %r",
            entity_id,
        )
        return False
