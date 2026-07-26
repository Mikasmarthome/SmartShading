"""T18 — LearningPersistenceAdapter integration tests (realistic upgrade,
corruption, and multi-entry isolation, exercised through the real adapter).

Prior to T18, no test constructed LearningPersistenceAdapter directly — all
existing coverage (T12/T15/T16) exercises the pure functions
(serialize_learning_store / deserialize_into_learning_store / migrate_payload)
directly, never the actual async adapter that a real restart/reload calls
through. This file closes that gap: a realistic v1 payload (matching the
module's own documented storage format) restored through
LearningPersistenceAdapter.async_restore() must set migration_dirty and
produce a usable store; a corrupted payload must degrade to an empty store
without raising; two entries must never see each other's data.

LearningPersistenceAdapter.__init__ defers its `homeassistant.helpers.storage`
import, so only that one module needs stubbing (no coordinator import chain).
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone

import pytest


class _FakeHAStore:
    """Stand-in for homeassistant.helpers.storage.Store."""

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
    # T18: multiple test files in this session each install their own
    # homeassistant.helpers.storage stub; whichever ran last during pytest's
    # collection phase "wins" the sys.modules entry for the rest of the
    # session. Re-installing this file's own stub immediately before each of
    # its own tests run ensures LearningPersistenceAdapter's lazy `from
    # homeassistant.helpers.storage import Store` resolves to THIS file's
    # _FakeHAStore (and therefore THIS file's _registry) during its tests,
    # regardless of import order across files.
    _install_storage_stub()
    yield


from custom_components.smartshading.engines.learning_persistence import (  # noqa: E402
    LearningPersistenceAdapter,
    LearningPersistenceConfig,
    LearningStore,
    _serialize_transition,
)
from custom_components.smartshading.models.learning import (  # noqa: E402
    StateTransitionRecord,
)
from custom_components.smartshading.state_machine.states import ShadingState  # noqa: E402

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_adapter(entry_id: str) -> LearningPersistenceAdapter:
    return LearningPersistenceAdapter(
        hass=object(), config=LearningPersistenceConfig(), entry_id=entry_id,
    )


def _preload(entry_id: str, payload: object) -> None:
    """Directly seed the fake HA storage backing a given entry_id, simulating
    a pre-existing file from a prior install/version."""
    key = (1, f"smartshading_learning_{entry_id}")
    _FakeHAStore._registry[key] = payload


class TestFreshInstall:
    def test_no_file_returns_fresh_start_and_empty_adapter(self):
        adapter = _make_adapter("fresh-entry")
        store = LearningStore()

        result = asyncio.run(adapter.async_restore(store, _NOW))

        assert adapter.fresh_start is True
        assert adapter.migration_dirty is False
        assert result is not None  # a usable (empty) TargetPositionAdapter


class TestRealisticV1Upgrade:
    def test_v1_payload_restores_and_flags_migration_dirty(self):
        entry_id = "upgrade-entry"
        transition = StateTransitionRecord(
            timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            window_id="w1",
            from_state=ShadingState.NORMAL_SHADE,
            to_state=ShadingState.STRONG_SHADE,
            decided_by="HeatEvaluator",
            lifecycle_state="day",
            absence_active=False,
            is_in_solar_sector=True,
        )
        v1_payload = {
            "version": 1,
            "exported_at": "2025-01-01T00:00:00+00:00",
            "windows": {
                "w1": {
                    "transitions": [_serialize_transition(transition)],
                    "overrides": [],
                    "snapshots": [],
                }
            },
        }
        _preload(entry_id, v1_payload)
        adapter = _make_adapter(entry_id)
        store = LearningStore()

        result = asyncio.run(adapter.async_restore(store, _NOW))

        assert adapter.fresh_start is False
        assert adapter.migration_dirty is True, (
            "a genuine v1 payload (no schema_version key) must be flagged for "
            "the coordinator's single controlled post-restore save"
        )
        assert result is not None
        # The transition actually landed in the live store, not just silently accepted.
        assert len(store.get_transitions("w1")) == 1


class TestCorruptedPayload:
    @pytest.mark.parametrize("bad_payload", [
        "not a dict at all",
        42,
        [1, 2, 3],
        {"version": 999, "windows": "not-a-dict"},
        {},  # empty dict — no version, no windows
        None,
    ])
    def test_corrupted_or_malformed_payload_degrades_to_empty_store(self, bad_payload):
        entry_id = f"corrupt-entry-{id(bad_payload)}"
        if bad_payload is not None:
            _preload(entry_id, bad_payload)
        adapter = _make_adapter(entry_id)
        store = LearningStore()

        result = asyncio.run(adapter.async_restore(store, _NOW))  # must not raise

        assert result is not None
        assert adapter.migration_dirty is False
        assert store.get_transitions("w1") == []

    def test_malformed_individual_window_entries_are_skipped_not_fatal(self):
        """A valid-version payload whose 'windows' dict contains malformed
        entries (non-string key, non-dict value) must skip only those
        entries — a well-formed sibling window must still restore."""
        entry_id = "malformed-window-entries-entry"
        good_transition = StateTransitionRecord(
            timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            window_id="w-good",
            from_state=ShadingState.NORMAL_SHADE,
            to_state=ShadingState.STRONG_SHADE,
            decided_by="HeatEvaluator",
            lifecycle_state="day",
            absence_active=False,
            is_in_solar_sector=True,
        )
        payload = {
            "version": 1,
            "schema_version": 2,
            "windows": {
                # Malformed entries come FIRST so that a missing per-entry
                # guard (rather than a skip-and-continue) crashes the loop
                # before it ever reaches the well-formed window below —
                # this ordering is what makes the assertion below actually
                # detect a removed guard, instead of accidentally passing
                # because the good window happened to be processed first.
                "w-bad-streams": "not-a-dict",
                123: {"transitions": [], "overrides": [], "snapshots": []},
                "w-good": {
                    "transitions": [_serialize_transition(good_transition)],
                    "overrides": [], "snapshots": [],
                },
            },
        }
        _preload(entry_id, payload)
        adapter = _make_adapter(entry_id)
        store = LearningStore()

        asyncio.run(adapter.async_restore(store, _NOW))  # must not raise

        assert len(store.get_transitions("w-good")) == 1, (
            "a malformed sibling window entry must not take down the whole "
            "restore — the well-formed window's data must still land"
        )


class TestMultiEntryIsolation:
    def test_two_entries_restore_independently_no_cross_contamination(self):
        entry_a, entry_b = "zone-a", "zone-b"
        payload_a = {
            "version": 1, "exported_at": "2025-01-01T00:00:00+00:00",
            "windows": {"wa": {"transitions": [], "overrides": [], "snapshots": []}},
        }
        _preload(entry_a, payload_a)
        # entry_b has no persisted file at all.

        adapter_a = _make_adapter(entry_a)
        adapter_b = _make_adapter(entry_b)
        store_a, store_b = LearningStore(), LearningStore()

        asyncio.run(adapter_a.async_restore(store_a, _NOW))
        asyncio.run(adapter_b.async_restore(store_b, _NOW))

        assert adapter_a.fresh_start is False
        assert adapter_b.fresh_start is True, (
            "entry_b must not see entry_a's persisted payload — each entry_id "
            "maps to its own storage key (smartshading_learning_<entry_id>)"
        )

    def test_owner_entry_id_mismatch_in_payload_rejects_authority(self):
        """A payload whose recorded owner_entry_id doesn't match the restoring
        adapter's entry_id must not be silently adopted."""
        entry_id = "real-owner"
        payload = {
            "version": 1, "schema_version": 2,
            "owner_entry_id": "someone-elses-entry",
            "exported_at": "2025-01-01T00:00:00+00:00",
            "windows": {},
        }
        _preload(entry_id, payload)
        adapter = _make_adapter(entry_id)
        store = LearningStore()

        result = asyncio.run(adapter.async_restore(store, _NOW))

        assert result is not None
        assert adapter.migration_dirty is False
