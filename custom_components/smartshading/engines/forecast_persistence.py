"""Forecast Learning HA Persistence Wrapper — Phase 9F12j.

Thin async layer between ForecastLearningStore (pure Python, no HA) and
homeassistant.helpers.storage.Store (HA file-based persistence).

Responsibilities
----------------
  async_restore()  Load raw dict from HA storage → ForecastLearningStore.
                   Returns an empty store on any failure; never raises.
  async_save()     Prune expired records, serialize to dict, write to HA
                   storage.  Logs errors; never raises.

Pruning
-------
  async_save() calls store.prune(cutoff_utc) before serializing.
  cutoff_utc = now_utc − timedelta(days=FORECAST_RETENTION_DAYS)
  The ForecastLearningStore.prune() contract removes all entries whose
  reference timestamp is strictly before cutoff_utc.

What is NOT stored
------------------
  ForecastTrustResult and ForecastTrustSummary are computed on demand and
  are never written to storage.

Dependency injection
--------------------
  ForecastPersistenceAdapter.__init__ accepts any object that implements
  the _StoreProtocol duck type (async_load / async_save).  This avoids a
  module-level homeassistant import and keeps tests free of HA fixtures.

  Production usage:
      adapter = ForecastPersistenceAdapter.create(hass, entry_id)

  Test usage (inject a mock):
      adapter = ForecastPersistenceAdapter(mock_store)

  Legacy storage note:
      The file "smartshading_forecast_learning" (without an entry_id suffix)
      may exist in /config/.storage/ from an earlier implementation that used
      a single global store.  Current code only reads and writes the per-zone
      key "smartshading_forecast_learning_{entry_id}".  The legacy file is
      inert and can be deleted manually; it is NOT cleaned up automatically
      to avoid accidental data loss.

Tier safety
-----------
  No threshold is read or written.  No runtime state is modified.
  Purely I/O — load raw bytes in, write raw bytes out.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from ..models.forecast_store import ForecastLearningStore

_log = logging.getLogger(__name__)

STORAGE_KEY: str = "smartshading_forecast_learning"
STORAGE_VERSION: int = 1
FORECAST_RETENTION_DAYS: int = 90

# Hard count caps applied after age-based pruning.
# At the default 90-day retention window and 5-min coordinator interval:
#   ForecastSnapshot: ~1/h → max ~2 160 per zone per 90 days
#   RealitySnapshot:  ~2/h → max ~4 320 per zone per 90 days
#   ForecastRecord:   1 per matched snapshot → same order as forecast snapshots
# Caps are set 15-20 % above expected maximum to absorb edge cases (missed
# cycles, catch-up bursts) without ever allowing unbounded growth.
FORECAST_MAX_FORECAST_SNAPSHOTS: int = 2500
FORECAST_MAX_REALITY_SNAPSHOTS: int = 5000
FORECAST_MAX_FORECAST_RECORDS: int = 2500


# ---------------------------------------------------------------------------
# Duck-typed store interface — keeps the module free of HA imports at load time
# ---------------------------------------------------------------------------

class _StoreProtocol(Protocol):
    """Minimal interface required by ForecastPersistenceAdapter.

    homeassistant.helpers.storage.Store satisfies this protocol.
    Any test-double that implements these two methods also qualifies.
    """

    async def async_load(self) -> dict | None: ...
    async def async_save(self, data: dict) -> None: ...


# ---------------------------------------------------------------------------
# Public adapter
# ---------------------------------------------------------------------------

class ForecastPersistenceAdapter:
    """HA persistence wrapper for ForecastLearningStore.

    Translates between the pure-Python ForecastLearningStore and HA's
    file-based storage (homeassistant.helpers.storage.Store).

    All I/O errors are caught, logged, and swallowed so that the
    Coordinator is never interrupted by a storage failure.
    """

    def __init__(self, store: _StoreProtocol) -> None:
        self._store = store
        self._fresh_start: bool = False

    @property
    def fresh_start(self) -> bool:
        """True when async_restore found no persisted file (first run after setup).

        Used by async_setup_entry to write an initial schema-valid storage file
        immediately so that smartshading_forecast_learning_<id> is visible in
        /config/.storage/ from the moment SmartShading is configured.
        """
        return self._fresh_start

    @classmethod
    def create(cls, hass: Any, entry_id: str) -> ForecastPersistenceAdapter:
        """Create an adapter backed by a real HA Store.

        The homeassistant import is intentionally deferred to this factory
        method so that the module can be imported (and tested) without HA
        being installed.

        *entry_id* scopes the storage key to this config entry so that
        multiple SmartShading zones never share a file and cannot overwrite
        each other's learning data.
        """
        from homeassistant.helpers.storage import Store  # lazy HA import

        return cls(Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry_id}"))

    # ------------------------------------------------------------------
    # async_restore
    # ------------------------------------------------------------------

    async def async_restore(self) -> ForecastLearningStore:
        """Load and deserialize ForecastLearningStore from HA storage.

        Returns ForecastLearningStore.empty() in all failure scenarios:
          - File not present yet (async_load returns None) — normal on first start
          - async_load raises an exception
          - Data is corrupt (from_dict handles this internally)

        Never propagates exceptions to the Coordinator.
        """
        try:
            raw = await self._store.async_load()
        except Exception:
            _log.error(
                "ForecastPersistenceAdapter: failed to load from storage — "
                "starting with empty store"
            )
            return ForecastLearningStore.empty()

        if raw is None:
            self._fresh_start = True
            _log.debug("ForecastPersistenceAdapter: no persisted data found, starting fresh")
            return ForecastLearningStore.empty()

        # from_dict() never raises; handles corrupt data internally.
        return ForecastLearningStore.from_dict(raw)

    # ------------------------------------------------------------------
    # async_save
    # ------------------------------------------------------------------

    async def async_save(self, store: ForecastLearningStore) -> None:
        """Prune expired entries and persist ForecastLearningStore to HA storage.

        Steps:
          1. Compute cutoff = now_utc − FORECAST_RETENTION_DAYS days.
          2. Call store.prune(cutoff) — removes stale entries in place.
          3. Serialize the pruned store via store.to_dict().
          4. Write the dict to HA storage.

        Errors are logged at ERROR level.  The method never raises so that a
        storage failure does not block the Coordinator event loop.
        """
        cutoff_utc = datetime.now(timezone.utc) - timedelta(days=FORECAST_RETENTION_DAYS)
        store.prune(cutoff_utc)
        store.prune_to_count(
            FORECAST_MAX_FORECAST_SNAPSHOTS,
            FORECAST_MAX_REALITY_SNAPSHOTS,
            FORECAST_MAX_FORECAST_RECORDS,
        )

        try:
            await self._store.async_save(store.to_dict())
        except Exception:
            _log.error(
                "ForecastPersistenceAdapter: failed to save to storage"
            )
