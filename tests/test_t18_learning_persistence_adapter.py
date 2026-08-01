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
from custom_components.smartshading.models.pending_outcome import PendingOutcome  # noqa: E402
from custom_components.smartshading.models.bounded_experiment import (  # noqa: E402
    BoundedExperiment, STATUS_OBSERVING, STATUS_INTERRUPTED_PARTIAL,
)
from custom_components.smartshading.models.persistent_adoption import (  # noqa: E402
    PersistentTargetAdoption, STATUS_MONITORING,
)
from custom_components.smartshading.engines.experiment_engine import (  # noqa: E402
    reconcile_restored_experiments,
)
from custom_components.smartshading.engines.adoption_engine import (  # noqa: E402
    reconcile_restored_adoptions,
)

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


class TestPendingExperimentAdoptionAdapterRoundtrip:
    """Phase 2B.5 (G3-G5): closes the one remaining gap in the restart-
    reconciliation proof chain -- TC_PO_16 (tests/test_learning_switch_
    pending_outcome_lifecycle.py) proves the real, pure reconcile functions
    in isolation on in-memory model objects; TC_PO_17 proves the coordinator
    assigns their results onto its own real attributes. Neither exercises
    the real adapter serialize/deserialize contract in between. This test
    drives exactly that missing link:

        real async_save() -> real persisted payload -> new adapter instance
        -> real async_restore() -> real Model.from_dict() deserialization
        -> the same real reconcile_restored_experiments/adoptions functions

    It does NOT exercise a full coordinator restart, a config-entry reload,
    or a Home Assistant process restart -- coordinator-level restore wiring
    is TC_PO_17's job; this test is scoped to the adapter contract only.
    """

    def test_pending_outcome_experiment_adoption_survive_real_save_restore_and_reconcile_correctly(self):
        entry_id = "lifecycle-roundtrip-entry"
        window_id = "w-lifecycle"
        decision_ts = datetime(2026, 1, 10, 9, 30, 0, tzinfo=timezone.utc)

        pending = PendingOutcome(
            window_id=window_id,
            decision_timestamp=decision_ts,
            from_state=ShadingState.OPEN,
            to_state=ShadingState.NORMAL_SHADE,
            decided_by="tier2_comfort",
            lifecycle_state="day",
            indoor_temp_outcome_delay_min=30,
            target_position=45,
            indoor_temp_at_decision=23.5,
            solar_exposure_at_decision=310.0,
            decision_id="dec-lifecycle-1",
            experiment_id="exp-lifecycle-1",
            config_fingerprint="fp-lifecycle-abc",
            created_at_utc=decision_ts,
            restart_count=0,
        )

        experiment = BoundedExperiment(
            experiment_id="exp-lifecycle-1", source_shadow_id="shadow-lifecycle-1",
            window_id=window_id, zone_id="zone-lifecycle", intensity_level="normal",
            context_family="day|mid", created_at=decision_ts, updated_at=decision_ts,
            status=STATUS_OBSERVING, confirmation="command_sent",
            source_decision_ids=("dec-lifecycle-1",), experiment_decision_id="dec-lifecycle-1",
            config_generation=3, baseline_parameter_target_ha=60,
            experiment_parameter_target_ha=55, expected_final_candidate_target_ha=55,
            actual_dispatched_target_ha=55, delta_ha=-5,
        )

        adoption = PersistentTargetAdoption(
            adoption_id="adopt-lifecycle-1", window_id=window_id, zone_id="zone-lifecycle",
            intensity_level="normal", context_family="day|mid",
            configured_target_ha=60, adopted_delta_ha=-5, effective_target_ha=55,
            source_experiment_ids=("exp-lifecycle-0",), source_decision_ids=("dec-lifecycle-0",),
            consumed_experiment_ids=("exp-lifecycle-0",),
            created_at=decision_ts, updated_at=decision_ts, activated_at=decision_ts,
            config_generation=3, status=STATUS_MONITORING, suspended=False,
            confidence=0.72, reliability=0.68,
        )

        adapter1 = _make_adapter(entry_id)
        store1 = LearningStore()
        # Populate the restore baseline (fresh_start) before saving, matching
        # the real production call shape (async_save is always preceded by
        # at least one async_restore in the real coordinator lifecycle).
        asyncio.run(adapter1.async_restore(store1, _NOW))

        # Real contract (learning_persistence.py:1296-1321, matches
        # coordinator.py's _build_save_kwargs exactly): pending_outcomes are
        # passed as RAW model objects (the adapter calls po.to_dict()
        # internally, learning_persistence.py:547-550); bounded_experiments/
        # persistent_adoptions are passed PRE-SERIALIZED (coordinator.py's
        # _experiments_storage()/_adoptions_storage() already return
        # [e.to_dict() for e in ...] -- learning_persistence.py:568,570 store
        # them as-is, with no second .to_dict() call).
        saved = asyncio.run(adapter1.async_save(
            store1, {window_id}, _NOW,
            pending_outcomes=[pending],
            bounded_experiments=[experiment.to_dict()],
            persistent_adoptions=[adoption.to_dict()],
        ))
        assert saved is True

        # New adapter instance, same entry_id, new empty LearningStore --
        # simulates a fresh restore from the persisted file, not a mutation
        # of the same in-memory objects.
        adapter2 = _make_adapter(entry_id)
        store2 = LearningStore()
        asyncio.run(adapter2.async_restore(store2, _NOW))
        extras = adapter2.last_restore_extras

        assert extras is not None
        assert len(extras.pending_outcomes) == 1
        assert len(extras.bounded_experiments) == 1
        assert len(extras.persistent_adoptions) == 1

        restored_pending = extras.pending_outcomes[0]
        assert restored_pending.window_id == window_id
        assert restored_pending.decision_timestamp == decision_ts
        assert restored_pending.from_state == pending.from_state
        assert restored_pending.to_state == ShadingState.NORMAL_SHADE
        assert restored_pending.lifecycle_state == "day"
        assert restored_pending.indoor_temp_outcome_delay_min == 30
        assert restored_pending.decision_id == "dec-lifecycle-1"
        assert restored_pending.experiment_id == "exp-lifecycle-1"
        assert restored_pending.config_fingerprint == "fp-lifecycle-abc"
        assert restored_pending.created_at_utc == decision_ts
        assert restored_pending.restart_count == 0

        restored_experiment = extras.bounded_experiments[0]
        assert restored_experiment.experiment_id == "exp-lifecycle-1"
        assert restored_experiment.window_id == window_id
        assert restored_experiment.zone_id == "zone-lifecycle"
        assert restored_experiment.status == STATUS_OBSERVING
        assert restored_experiment.confirmation == "command_sent"
        assert restored_experiment.source_decision_ids == ("dec-lifecycle-1",)
        assert restored_experiment.experiment_decision_id == "dec-lifecycle-1"
        assert restored_experiment.config_generation == 3
        assert restored_experiment.baseline_parameter_target_ha == 60
        assert restored_experiment.expected_final_candidate_target_ha == 55
        assert restored_experiment.actual_dispatched_target_ha == 55
        assert restored_experiment.delta_ha == -5
        assert restored_experiment.created_at == decision_ts
        assert restored_experiment.updated_at == decision_ts

        restored_adoption = extras.persistent_adoptions[0]
        assert restored_adoption.adoption_id == "adopt-lifecycle-1"
        assert restored_adoption.window_id == window_id
        assert restored_adoption.zone_id == "zone-lifecycle"
        assert restored_adoption.status == STATUS_MONITORING
        assert restored_adoption.suspended is False
        assert restored_adoption.adopted_delta_ha == -5
        assert restored_adoption.effective_target_ha == 55
        assert restored_adoption.source_experiment_ids == ("exp-lifecycle-0",)
        assert restored_adoption.source_decision_ids == ("dec-lifecycle-0",)
        assert restored_adoption.consumed_experiment_ids == ("exp-lifecycle-0",)
        assert restored_adoption.config_generation == 3
        assert restored_adoption.confidence == 0.72
        assert restored_adoption.reliability == 0.68
        assert restored_adoption.current_gate_reason is None
        assert restored_adoption.created_at == decision_ts
        assert restored_adoption.updated_at == decision_ts

        # Apply the real, current restart-reconciliation rules to the
        # ACTUALLY-DESERIALIZED objects -- not the original in-memory ones --
        # exactly as the coordinator's real restore path does
        # (coordinator.py ~2984-2992).
        active_experiments, experiment_history = reconcile_restored_experiments(
            extras.bounded_experiments, _NOW
        )
        active_adoptions, adoption_history = reconcile_restored_adoptions(
            extras.persistent_adoptions, _NOW
        )

        # Experiment: an OBSERVING experiment must never silently resume.
        assert active_experiments == {}, "no active experiment must survive restart-reconciliation"
        assert len(experiment_history) == 1
        demoted = experiment_history[0]
        assert demoted.status == STATUS_INTERRUPTED_PARTIAL
        assert demoted.abort_reason == "interrupted_by_restart"

        # Adoption: stays active, not historized, but suspended pending revalidation.
        assert len(active_adoptions) == 1, "the adoption must remain an active adoption, not be dropped"
        assert len(adoption_history) == 0, (
            "a plain non-terminal adoption must not be rolled back/invalidated/"
            "historized by restart-reconciliation alone"
        )
        reconciled_adoption = active_adoptions[adoption.adoption_key]
        assert reconciled_adoption.suspended is True
        assert reconciled_adoption.current_gate_reason == "awaiting_restart_revalidation"
        assert reconciled_adoption.status == STATUS_MONITORING, "status itself is not force-changed"
        assert reconciled_adoption.effective_target_ha == 55, "no target re-application by restore alone"

        # Idempotency: matches the real API contract of both reconcile
        # functions (stateless, deterministic pure functions of their input
        # list) -- re-running with the SAME restored input again must
        # produce the identical single-entry result, not accumulate.
        active_experiments_2, experiment_history_2 = reconcile_restored_experiments(
            extras.bounded_experiments, _NOW
        )
        assert active_experiments_2 == {}
        assert len(experiment_history_2) == 1
        active_adoptions_2, adoption_history_2 = reconcile_restored_adoptions(
            extras.persistent_adoptions, _NOW
        )
        assert len(adoption_history_2) == 0
        assert len(active_adoptions_2) == 1
