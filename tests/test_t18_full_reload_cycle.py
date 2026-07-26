"""T18 — full reload-cycle state-preservation test.

Prior coverage (T16's TestAssumedStatePersistenceRoundtrip) proved the pure
serialize/deserialize functions round-trip a single AssumedPositionState
correctly, and T17's TestUnloadOrdering proved async_unload_entry calls
things in the right order structurally (AST-based). Neither test exercises
an actual second coordinator-owned object materializing from persisted data
through LearningPersistenceAdapter — i.e. what genuinely happens across a
real unload -> setup (reload) or a full HA restart.

This test simulates that: "coordinator A" saves its current_states and
assumed_state via LearningPersistenceAdapter.async_save() exactly the way
coordinator.py's _build_save_kwargs() does (export_for_restore() objects
passed straight through, not pre-serialized dicts); a fresh "coordinator B"
(brand new LearningStore + AssumedStateManager, as constructed at
__init__.py's _async_setup_zone_entry) restores from the SAME entry_id and
must end up with equivalent, USABLE state — reconstructed via the identical
pattern coordinator.py's own restore block uses.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone

import pytest

from custom_components.smartshading.cover_control.assumed_state_manager import (
    AssumedPositionState,
    AssumedStateManager,
)


class _FakeHAStore:
    _registry: dict[tuple[int, str], dict | None] = {}

    def __init__(self, hass, version: int, key: str) -> None:
        self._key = (version, key)
        self._registry.setdefault(self._key, None)

    async def async_load(self) -> dict | None:
        return self._registry[self._key]

    async def async_save(self, data: dict) -> None:
        self._registry[self._key] = data


def _install_storage_stub() -> None:
    mod = types.ModuleType("homeassistant.helpers.storage")
    mod.Store = _FakeHAStore
    sys.modules["homeassistant.helpers.storage"] = mod
    sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))


_install_storage_stub()


@pytest.fixture(autouse=True)
def _reinstall_storage_stub():
    # T18: see test_t18_learning_persistence_adapter.py for why this must be
    # re-installed before each test rather than only once at import time.
    _install_storage_stub()
    yield


from custom_components.smartshading.engines.learning_persistence import (  # noqa: E402
    LearningPersistenceAdapter,
    LearningPersistenceConfig,
    LearningStore,
)
from custom_components.smartshading.state_machine.states import ShadingState  # noqa: E402

_NOW = datetime(2026, 2, 1, 8, 0, 0, tzinfo=timezone.utc)


def _reconstruct_assumed_state_manager(extras) -> AssumedStateManager:
    """Mirrors coordinator.py's restore block exactly (assumed_state
    reconstruction from RestoreExtras), so this test exercises the same
    logic path a real reload runs, not a re-invented shortcut."""
    manager = AssumedStateManager()
    raw_as = getattr(extras, "assumed_state", {}) or {}
    for cid, av in raw_as.items():
        try:
            lkg_raw = av.get("last_known_good_at")
            lca_raw = av.get("last_commanded_at")
            manager.initialize_from_restore(AssumedPositionState(
                cover_id=cid,
                assumed_position=int(av["assumed_position"]),
                assumed_tilt=av.get("assumed_tilt"),
                last_commanded_at=(
                    datetime.fromisoformat(lca_raw) if lca_raw else None),
                last_known_good_at=datetime.fromisoformat(lkg_raw),
                confidence=0.0,
                position_uncertainty_pct=float(av.get("position_uncertainty_pct", 0.0)),
                is_drift_suspected=False,
                interrupted_travel=bool(av.get("interrupted_travel", False)),
                last_commanded_position=av.get("last_commanded_position"),
            ))
        except Exception:
            continue
    return manager


def test_current_states_and_assumed_state_survive_a_full_reload_cycle():
    entry_id = "reload-cycle-entry"

    # ---- "Coordinator A" (pre-unload) ----
    store_a = LearningStore()
    asm_a = AssumedStateManager()
    asm_a.initialize_from_restore(AssumedPositionState(
        cover_id="cover.rts_1",
        assumed_position=42,
        assumed_tilt=None,
        last_commanded_at=_NOW,
        last_known_good_at=_NOW,
        confidence=0.0,
        position_uncertainty_pct=3.5,
        is_drift_suspected=False,
        interrupted_travel=False,
        last_commanded_position=42,
    ))
    current_states_a = {"w1": ShadingState.STRONG_SHADE, "w2": ShadingState.NORMAL_SHADE}

    adapter_a = LearningPersistenceAdapter(
        hass=object(), config=LearningPersistenceConfig(), entry_id=entry_id)

    # Exactly what coordinator.py's _build_save_kwargs() passes: raw
    # AssumedPositionState objects keyed by cover_id, not pre-converted dicts —
    # serialize_learning_store() itself does the .isoformat()/attribute reads.
    assumed_state_objects = {
        cid: asm_a.export_for_restore(cid) for cid in asm_a.known_cover_ids()
    }

    ok = asyncio.run(adapter_a.async_save(
        store_a, active_window_ids={"w1", "w2"}, now=_NOW,
        current_states=current_states_a,
        assumed_state=assumed_state_objects,
    ))
    assert ok is True

    # ---- Unload happens here (no code to run — this IS the reload boundary) ----

    # ---- "Coordinator B" (fresh instances, post-setup after reload) ----
    store_b = LearningStore()
    adapter_b = LearningPersistenceAdapter(
        hass=object(), config=LearningPersistenceConfig(), entry_id=entry_id)

    asyncio.run(adapter_b.async_restore(store_b, _NOW))
    extras = adapter_b.last_restore_extras

    assert extras is not None
    assert adapter_b.fresh_start is False
    assert extras.current_states == {"w1": "strong_shade", "w2": "normal_shade"}

    asm_b = _reconstruct_assumed_state_manager(extras)

    restored_state = asm_b.get_state("cover.rts_1", _NOW)
    assert restored_state is not None
    assert restored_state.assumed_position == 42
    assert restored_state.position_uncertainty_pct == 3.5
    assert restored_state.last_commanded_position == 42
    assert restored_state.interrupted_travel is False


def test_reload_cycle_with_no_prior_assumed_state_yields_empty_manager():
    """A window with no covers using assumed-state tracking (all reliable
    feedback) must reload cleanly into an empty-but-usable manager — not an
    error, and not a manager that claims false confidence for an unknown cover."""
    entry_id = "reload-cycle-empty-entry"
    store_a = LearningStore()
    adapter_a = LearningPersistenceAdapter(
        hass=object(), config=LearningPersistenceConfig(), entry_id=entry_id)

    asyncio.run(adapter_a.async_save(
        store_a, active_window_ids=set(), now=_NOW,
        current_states={}, assumed_state={},
    ))

    store_b = LearningStore()
    adapter_b = LearningPersistenceAdapter(
        hass=object(), config=LearningPersistenceConfig(), entry_id=entry_id)
    asyncio.run(adapter_b.async_restore(store_b, _NOW))
    extras = adapter_b.last_restore_extras

    asm_b = _reconstruct_assumed_state_manager(extras)

    assert asm_b.known_cover_ids() == set()
    assert asm_b.get_state("cover.unknown", _NOW) is None
