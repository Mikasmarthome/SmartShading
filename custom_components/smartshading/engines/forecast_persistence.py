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

# P10: forecast payload schema — same hardening standard as the learning store.
FORECAST_PAYLOAD_SCHEMA: int = 1

# Forecast record sections validated per-record on restore.
_FORECAST_SECTIONS: tuple[str, ...] = (
    "forecast_snapshots", "reality_snapshots", "forecast_records",
)


def compute_provider_fingerprint(
    *, forecast_entity: str | None, solar_entity: str | None, owner: str | None,
) -> str | None:
    """Privacy-safe stable hash of the forecast source identity (no raw entity ids
    leave the store).  None when nothing is configured."""
    import hashlib
    if not any((forecast_entity, solar_entity, owner)):
        return None
    raw = f"{forecast_entity or ''}|{solar_entity or ''}|{owner or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

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

    def __init__(
        self, store: _StoreProtocol, entry_id: str | None = None,
        *, owner_zone_id: str | None = None, provider_fingerprint: str | None = None,
    ) -> None:
        self._store = store
        self._entry_id = entry_id
        self._owner_zone_id = owner_zone_id
        self._provider_fingerprint = provider_fingerprint
        self._fresh_start: bool = False
        self._restore_rejected: bool = False
        self._provider_changed: bool = False
        self._restore_diagnostics: dict = {}

    @property
    def provider_changed(self) -> bool:
        """True when the stored provider fingerprint differs from the current one —
        the coordinator suspends forecast-dependent strategy authority."""
        return self._provider_changed

    @property
    def restore_diagnostics(self) -> dict:
        """Structured, privacy-safe forecast_restore diagnostics from the last load."""
        return self._restore_diagnostics

    @property
    def fresh_start(self) -> bool:
        """True when async_restore found no persisted file (first run after setup).

        Used by async_setup_entry to write an initial schema-valid storage file
        immediately so that smartshading_forecast_learning_<id> is visible in
        /config/.storage/ from the moment SmartShading is configured.
        """
        return self._fresh_start

    @classmethod
    def create(
        cls, hass: Any, entry_id: str, *, owner_zone_id: str | None = None,
        provider_fingerprint: str | None = None,
    ) -> ForecastPersistenceAdapter:
        """Create an adapter backed by a real HA Store.

        The homeassistant import is intentionally deferred to this factory
        method so that the module can be imported (and tested) without HA
        being installed.

        *entry_id* scopes the storage key to this config entry so that
        multiple SmartShading zones never share a file and cannot overwrite
        each other's learning data.  *provider_fingerprint* lets restore reject
        stale trust after a forecast-source change.
        """
        from homeassistant.helpers.storage import Store  # lazy HA import

        return cls(
            Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry_id}"), entry_id=entry_id,
            owner_zone_id=owner_zone_id, provider_fingerprint=provider_fingerprint)

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

        # P10: the single authoritative forecast restore pipeline —
        # root validation → schema gate → owner → provider fingerprint →
        # per-record validation → from_dict.
        from .storage_validation import payload_has_nan_or_inf
        # Root gate is TYPE-only: a single NaN/Inf inside one record must NOT reject
        # the whole payload (per-record validation below drops that record while
        # valid neighbours survive).  A non-mapping root IS a whole-payload error.
        if not isinstance(raw, dict):
            self._restore_rejected = True
            _log.warning("Forecast: invalid root payload — no forecast trust authority")
            return ForecastLearningStore.empty()
        schema = int(raw.get("payload_schema_version", FORECAST_PAYLOAD_SCHEMA))
        self._restore_diagnostics = {
            "schema_version": schema, "owner_valid": True,
            "provider_fingerprint_match": True,
            "invalid_records_by_section": {}, "invalid_records_by_reason": {},
            "trust_restored": False,
        }
        # Unknown newer schema → no forecast trust authority (baseline control stays).
        if schema > FORECAST_PAYLOAD_SCHEMA:
            self._restore_rejected = True
            _log.warning("Forecast: payload schema newer than supported — no trust authority")
            return ForecastLearningStore.empty()
        # Owner mismatch → reject the WHOLE forecast payload (no cross-zone trust).
        _owner = raw.get("owner_entry_id")
        if _owner is not None and self._entry_id is not None and _owner != self._entry_id:
            self._restore_rejected = True
            self._restore_diagnostics["owner_valid"] = False
            _log.warning("Forecast: stored payload owner mismatch — rejecting whole payload")
            return ForecastLearningStore.empty()
        # Provider/source fingerprint change → do NOT restore stale trust authority.
        _fp = raw.get("provider_fingerprint")
        if (_fp is not None and self._provider_fingerprint is not None
                and _fp != self._provider_fingerprint):
            self._provider_changed = True
            self._restore_diagnostics["provider_fingerprint_match"] = False
            _log.warning("Forecast: provider fingerprint changed — old trust not restored")
            return ForecastLearningStore.empty()

        # Per-record validation: drop NaN/Infinity records per section before from_dict
        # so a corrupt record never poisons a model; valid neighbours survive.
        inval_section: dict = {}
        for section in _FORECAST_SECTIONS:
            recs = raw.get(section)
            if not isinstance(recs, dict):
                continue
            bad = [rid for rid, rec in recs.items() if payload_has_nan_or_inf(rec)]
            for rid in bad:
                recs.pop(rid, None)
            if bad:
                inval_section[section] = len(bad)
        self._restore_diagnostics["invalid_records_by_section"] = inval_section
        if inval_section:
            self._restore_diagnostics["invalid_records_by_reason"] = {
                "nan_or_inf": sum(inval_section.values())}

        # from_dict() never raises; handles corrupt data internally.
        store = ForecastLearningStore.from_dict(raw)
        self._restore_diagnostics["trust_restored"] = True
        return store

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

        payload = store.to_dict()
        # P10: stamp owner + schema + provider envelope (additive; from_dict ignores).
        payload["payload_schema_version"] = FORECAST_PAYLOAD_SCHEMA
        payload["created_by_domain"] = "smartshading"
        if self._entry_id is not None:
            payload["owner_entry_id"] = self._entry_id
        if self._owner_zone_id is not None:
            payload["owner_zone_id"] = self._owner_zone_id
        if self._provider_fingerprint is not None:
            payload["provider_fingerprint"] = self._provider_fingerprint

        try:
            await self._store.async_save(payload)
        except Exception:
            _log.error(
                "ForecastPersistenceAdapter: failed to save to storage"
            )
