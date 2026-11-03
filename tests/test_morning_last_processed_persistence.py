"""B3-010 R12 — real save/restore round trip for the minimal
morning_last_processed marker, through the actual LearningPersistenceAdapter
(the same real async_save()/async_restore() path a genuine restart/reload
calls through), not just the pure serialize/deserialize functions in
isolation.

Coverage:
  MLP-01  A saved marker round-trips through async_save -> async_restore.
  MLP-02  Missing key (payload predates this field) restores to empty --
          backward compatible, never raises.
  MLP-03  Corrupted individual entries (non-string key/value) are skipped
          individually, never fatal to the whole restore.
  MLP-04  A malformed date string is preserved raw in RestoreExtras (date
          parsing itself is the coordinator's job, done on read) -- the
          adapter/deserializer layer only validates shape (str keys, str
          values), consistent with every other additive section's pattern.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone

import pytest


class _FakeHAStore:
    _registry: dict[tuple[int, str], dict | None] = {}

    def __init__(self, hass, version: int, key: str) -> None:
        self._key = (version, key)
        self._registry.setdefault(self._key, None)
        self.save_calls = 0

    async def async_load(self) -> dict | None:
        return self._registry[self._key]

    async def async_save(self, data: dict) -> None:
        self.save_calls += 1
        self._registry[self._key] = data


def _install_storage_stub() -> None:
    mod = types.ModuleType("homeassistant.helpers.storage")
    mod.Store = _FakeHAStore
    sys.modules["homeassistant.helpers.storage"] = mod
    if "homeassistant" not in sys.modules:
        sys.modules["homeassistant"] = types.ModuleType("homeassistant")
    if "homeassistant.helpers" not in sys.modules:
        sys.modules["homeassistant.helpers"] = types.ModuleType("homeassistant.helpers")


_install_storage_stub()


@pytest.fixture(autouse=True)
def _reinstall_storage_stub():
    _install_storage_stub()
    yield


from custom_components.smartshading.engines.learning_persistence import (  # noqa: E402
    LearningPersistenceAdapter,
    LearningPersistenceConfig,
    LearningStore,
)

_NOW = datetime(2026, 8, 10, 7, 0, 0, tzinfo=timezone.utc)


def _make_adapter(entry_id: str) -> LearningPersistenceAdapter:
    return LearningPersistenceAdapter(
        hass=object(), config=LearningPersistenceConfig(), entry_id=entry_id,
    )


def _preload(entry_id: str, payload: object) -> None:
    key = (1, f"smartshading_learning_{entry_id}")
    _FakeHAStore._registry[key] = payload


class TestRoundTrip:
    def test_saved_marker_restores_correctly(self):
        adapter = _make_adapter("mlp-entry-1")
        store = LearningStore()

        ok = asyncio.run(adapter.async_save(
            store, {"w1", "w2"}, _NOW,
            morning_last_processed={"w1": "2026-08-10", "w2": "2026-08-09"},
        ))
        assert ok is True

        adapter2 = _make_adapter("mlp-entry-1")
        asyncio.run(adapter2.async_restore(LearningStore(), _NOW))
        extras = adapter2.last_restore_extras
        assert extras is not None
        assert extras.morning_last_processed == {"w1": "2026-08-10", "w2": "2026-08-09"}


class TestBackwardCompatibility:
    def test_missing_key_restores_to_empty(self):
        """A payload from before this field existed (predates B3-010 R12)
        must restore cleanly with an empty marker dict, never raise."""
        adapter = _make_adapter("mlp-entry-2")
        _preload("mlp-entry-2", {
            "version": 1, "schema_version": 2, "windows": {}, "pending_outcomes": [],
        })
        asyncio.run(adapter.async_restore(LearningStore(), _NOW))
        extras = adapter.last_restore_extras
        assert extras is not None
        assert extras.morning_last_processed == {}


class TestCorruptedEntries:
    def test_non_string_key_or_value_skipped_individually(self):
        adapter = _make_adapter("mlp-entry-3")
        _preload("mlp-entry-3", {
            "version": 1, "schema_version": 2, "windows": {}, "pending_outcomes": [],
            "morning_last_processed": {
                "w1": "2026-08-10",  # valid
                "w2": 12345,  # non-string value -- must be skipped
                123: "2026-08-10",  # non-string key -- must be skipped
            },
        })
        asyncio.run(adapter.async_restore(LearningStore(), _NOW))
        extras = adapter.last_restore_extras
        assert extras is not None
        assert extras.morning_last_processed == {"w1": "2026-08-10"}

    def test_malformed_date_string_preserved_raw_for_coordinator_side_parsing(self):
        """Shape validation (str/str) happens at this layer; actual date
        parsing (and skipping an unparsable date) is the coordinator's
        job on read -- see coordinator.py's restore site."""
        adapter = _make_adapter("mlp-entry-4")
        _preload("mlp-entry-4", {
            "version": 1, "schema_version": 2, "windows": {}, "pending_outcomes": [],
            "morning_last_processed": {"w1": "not-a-real-date"},
        })
        asyncio.run(adapter.async_restore(LearningStore(), _NOW))
        extras = adapter.last_restore_extras
        assert extras is not None
        assert extras.morning_last_processed == {"w1": "not-a-real-date"}


class TestSaveOmitsNothing:
    def test_default_none_saves_as_empty_dict(self):
        """Every other save call site (coordinator._build_save_kwargs())
        always passes a real dict; a direct async_save() call without the
        kwarg must still produce a valid, empty section, never omit the
        key or raise."""
        adapter = _make_adapter("mlp-entry-5")
        store = LearningStore()
        ok = asyncio.run(adapter.async_save(store, set(), _NOW))
        assert ok is True

        adapter2 = _make_adapter("mlp-entry-5")
        asyncio.run(adapter2.async_restore(LearningStore(), _NOW))
        extras = adapter2.last_restore_extras
        assert extras is not None
        assert extras.morning_last_processed == {}


class TestNightLastStartedRoundTrip:
    """B3-010 R14 item 2 -- night_last_started mirrors morning_last_processed's
    own real save/restore path exactly (same adapter, same additive-payload
    pattern) -- see this module's own docstring for why this is the real
    async_save()/async_restore() path, not just pure serialize/deserialize
    functions in isolation."""

    def test_saved_marker_restores_correctly(self):
        adapter = _make_adapter("nls-entry-1")
        store = LearningStore()

        ok = asyncio.run(adapter.async_save(
            store, {"w1", "w2"}, _NOW,
            night_last_started={"w1": "2026-08-09", "w2": "2026-08-10"},
        ))
        assert ok is True

        adapter2 = _make_adapter("nls-entry-1")
        asyncio.run(adapter2.async_restore(LearningStore(), _NOW))
        extras = adapter2.last_restore_extras
        assert extras is not None
        assert extras.night_last_started == {"w1": "2026-08-09", "w2": "2026-08-10"}

    def test_missing_key_restores_to_empty(self):
        adapter = _make_adapter("nls-entry-2")
        _preload("nls-entry-2", {
            "version": 1, "schema_version": 2, "windows": {}, "pending_outcomes": [],
        })
        asyncio.run(adapter.async_restore(LearningStore(), _NOW))
        extras = adapter.last_restore_extras
        assert extras is not None
        assert extras.night_last_started == {}

    def test_non_string_key_or_value_skipped_individually(self):
        adapter = _make_adapter("nls-entry-3")
        _preload("nls-entry-3", {
            "version": 1, "schema_version": 2, "windows": {}, "pending_outcomes": [],
            "night_last_started": {
                "w1": "2026-08-09",  # valid
                "w2": 12345,  # non-string value -- must be skipped
                123: "2026-08-09",  # non-string key -- must be skipped
            },
        })
        asyncio.run(adapter.async_restore(LearningStore(), _NOW))
        extras = adapter.last_restore_extras
        assert extras is not None
        assert extras.night_last_started == {"w1": "2026-08-09"}

    def test_both_markers_saved_and_restored_together_independently(self):
        """The two markers are independent sections in the same payload --
        saving/restoring one must never clobber or conflate the other."""
        adapter = _make_adapter("nls-entry-4")
        store = LearningStore()
        ok = asyncio.run(adapter.async_save(
            store, {"w1"}, _NOW,
            morning_last_processed={"w1": "2026-08-10"},
            night_last_started={"w1": "2026-08-09"},
        ))
        assert ok is True

        adapter2 = _make_adapter("nls-entry-4")
        asyncio.run(adapter2.async_restore(LearningStore(), _NOW))
        extras = adapter2.last_restore_extras
        assert extras is not None
        assert extras.morning_last_processed == {"w1": "2026-08-10"}
        assert extras.night_last_started == {"w1": "2026-08-09"}

    def test_default_none_saves_as_empty_dict(self):
        adapter = _make_adapter("nls-entry-5")
        store = LearningStore()
        ok = asyncio.run(adapter.async_save(store, set(), _NOW))
        assert ok is True

        adapter2 = _make_adapter("nls-entry-5")
        asyncio.run(adapter2.async_restore(LearningStore(), _NOW))
        extras = adapter2.last_restore_extras
        assert extras is not None
        assert extras.night_last_started == {}
