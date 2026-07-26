"""T18 — ForecastPersistenceAdapter integration tests.

Prior to T18 there was ZERO test coverage for this entire subsystem: no test
referenced ForecastPersistenceAdapter or ForecastLearningStore at all. These
tests close that gap for the beta release: fresh install, save/restore round
trip, corrupted payload, schema mismatch, owner mismatch, and provider
fingerprint change must all degrade gracefully (never raise, always fall back
to an empty store) since real users may carry storage files across upgrades.

ForecastPersistenceAdapter is duck-typed (_StoreProtocol: async_load/async_save)
specifically so it can be tested without any HA dependency — no stubs needed.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.smartshading.engines.forecast_persistence import (
    FORECAST_PAYLOAD_SCHEMA,
    ForecastPersistenceAdapter,
)
from custom_components.smartshading.models.forecast_store import ForecastLearningStore


class _FakeStore:
    """In-memory stand-in for homeassistant.helpers.storage.Store."""

    def __init__(self, initial: dict | None = None, *, raise_on_load: bool = False,
                 raise_on_save: bool = False):
        self._data = initial
        self._raise_on_load = raise_on_load
        self._raise_on_save = raise_on_save
        self.saved_payloads: list[dict] = []

    async def async_load(self) -> dict | None:
        if self._raise_on_load:
            raise OSError("simulated disk failure")
        return self._data

    async def async_save(self, data: dict) -> None:
        if self._raise_on_save:
            raise OSError("simulated disk failure")
        self.saved_payloads.append(data)
        self._data = data


class TestFreshInstall:
    def test_no_persisted_file_returns_empty_store_and_sets_fresh_start(self):
        store = _FakeStore(initial=None)
        adapter = ForecastPersistenceAdapter(store, entry_id="e1")

        result = asyncio.run(adapter.async_restore())

        assert isinstance(result, ForecastLearningStore)
        assert result.to_dict()["forecast_snapshots"] == {}
        assert adapter.fresh_start is True

    def test_load_exception_returns_empty_store_never_raises(self):
        store = _FakeStore(raise_on_load=True)
        adapter = ForecastPersistenceAdapter(store, entry_id="e1")

        result = asyncio.run(adapter.async_restore())

        assert isinstance(result, ForecastLearningStore)
        assert result.to_dict()["forecast_snapshots"] == {}


class TestSaveRestoreRoundTrip:
    def test_save_then_restore_preserves_owner_and_schema_envelope(self):
        store = _FakeStore(initial=None)
        adapter = ForecastPersistenceAdapter(
            store, entry_id="e1", owner_zone_id="z1", provider_fingerprint="fp123")

        asyncio.run(adapter.async_save(ForecastLearningStore.empty()))

        assert len(store.saved_payloads) == 1
        payload = store.saved_payloads[0]
        assert payload["payload_schema_version"] == FORECAST_PAYLOAD_SCHEMA
        assert payload["owner_entry_id"] == "e1"
        assert payload["owner_zone_id"] == "z1"
        assert payload["provider_fingerprint"] == "fp123"
        assert payload["created_by_domain"] == "smartshading"

        # A second adapter instance (simulating a reload) restores it cleanly.
        adapter2 = ForecastPersistenceAdapter(
            store, entry_id="e1", owner_zone_id="z1", provider_fingerprint="fp123")
        restored = asyncio.run(adapter2.async_restore())

        assert isinstance(restored, ForecastLearningStore)
        assert adapter2.fresh_start is False
        assert adapter2.restore_diagnostics["trust_restored"] is True

    def test_save_failure_is_counted_and_swallowed(self):
        store = _FakeStore(raise_on_save=True)
        adapter = ForecastPersistenceAdapter(store, entry_id="e1")

        asyncio.run(adapter.async_save(ForecastLearningStore.empty()))  # must not raise

        assert adapter.save_failures == 1


class TestCorruptedPayload:
    def test_non_dict_root_returns_empty_store(self):
        for bad_root in ["a string", 42, [1, 2, 3], True]:
            store = _FakeStore(initial=bad_root)
            adapter = ForecastPersistenceAdapter(store, entry_id="e1")

            result = asyncio.run(adapter.async_restore())

            assert isinstance(result, ForecastLearningStore)
            assert result.to_dict()["forecast_snapshots"] == {}

    def test_nan_infinity_record_dropped_but_valid_neighbours_survive(self):
        payload = {
            "payload_schema_version": FORECAST_PAYLOAD_SCHEMA,
            "forecast_snapshots": {
                "bad1": {"value": float("nan")},
                "good1": {
                    "captured_at": "2026-01-01T00:00:00+00:00",
                    "forecast_entity_id": "weather.home",
                    "condition": "sunny",
                    "cloud_coverage_pct": 10.0,
                    "temperature_c": 20.0,
                    "precipitation_probability_pct": 0.0,
                    "wind_speed_kmh": 5.0,
                },
            },
        }
        store = _FakeStore(initial=payload)
        adapter = ForecastPersistenceAdapter(store, entry_id="e1")

        result = asyncio.run(adapter.async_restore())

        assert isinstance(result, ForecastLearningStore)
        assert adapter.restore_diagnostics["invalid_records_by_section"].get(
            "forecast_snapshots") == 1


class TestSchemaAndOwnershipGates:
    def test_unknown_newer_schema_rejected(self):
        payload = {"payload_schema_version": FORECAST_PAYLOAD_SCHEMA + 1}
        store = _FakeStore(initial=payload)
        adapter = ForecastPersistenceAdapter(store, entry_id="e1")

        result = asyncio.run(adapter.async_restore())

        assert isinstance(result, ForecastLearningStore)
        assert result.to_dict()["forecast_snapshots"] == {}

    def test_owner_entry_id_mismatch_rejects_whole_payload(self):
        payload = {
            "payload_schema_version": FORECAST_PAYLOAD_SCHEMA,
            "owner_entry_id": "other-entry",
        }
        store = _FakeStore(initial=payload)
        adapter = ForecastPersistenceAdapter(store, entry_id="e1")

        result = asyncio.run(adapter.async_restore())

        assert isinstance(result, ForecastLearningStore)
        assert adapter.restore_diagnostics["owner_valid"] is False

    def test_provider_fingerprint_change_does_not_restore_stale_trust(self):
        payload = {
            "payload_schema_version": FORECAST_PAYLOAD_SCHEMA,
            "provider_fingerprint": "old-fp",
        }
        store = _FakeStore(initial=payload)
        adapter = ForecastPersistenceAdapter(
            store, entry_id="e1", provider_fingerprint="new-fp")

        result = asyncio.run(adapter.async_restore())

        assert isinstance(result, ForecastLearningStore)
        assert adapter.provider_changed is True
        assert result.to_dict()["forecast_snapshots"] == {}


class TestMultiEntryIsolation:
    def test_two_entries_each_restore_only_their_own_payload(self):
        store_a = _FakeStore(initial=None)
        store_b = _FakeStore(initial=None)
        adapter_a = ForecastPersistenceAdapter(store_a, entry_id="entry-a")
        adapter_b = ForecastPersistenceAdapter(store_b, entry_id="entry-b")

        asyncio.run(adapter_a.async_save(ForecastLearningStore.empty()))
        asyncio.run(adapter_b.async_save(ForecastLearningStore.empty()))

        assert store_a.saved_payloads[0]["owner_entry_id"] == "entry-a"
        assert store_b.saved_payloads[0]["owner_entry_id"] == "entry-b"

        # entry-a's store data must never leak into entry-b's adapter.
        adapter_b2 = ForecastPersistenceAdapter(store_b, entry_id="entry-a")
        result = asyncio.run(adapter_b2.async_restore())
        # store_b's payload was saved with owner_entry_id="entry-b", but this
        # adapter claims to be entry-a — mismatch must reject it.
        assert adapter_b2.restore_diagnostics.get("owner_valid") is False
        assert result.to_dict()["forecast_snapshots"] == {}
