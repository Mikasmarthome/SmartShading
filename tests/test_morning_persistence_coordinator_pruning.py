"""B3-010 R14 item 3 -- orphan window-ID pruning for the two minimal Morning
persistence markers (morning_last_processed, night_last_started), proven
through the REAL coordinator's own _build_save_kwargs() (the single,
authoritative snapshot builder every save path uses -- see that method's
own docstring), not a hand-rolled dict filter.

A window removed from configuration (or renamed) must not have its stale
marker carried forever in the persisted snapshot -- _build_save_kwargs()
filters both markers to `wid in self.windows` at read time, so an orphaned
entry is silently dropped on the very next save, never accumulating.
"""
from __future__ import annotations

import sys
import types
from datetime import date, datetime, timezone
from typing import Any

import pytest


def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


_HA_STUBS = {
    "homeassistant": _stub("homeassistant"),
    "homeassistant.components": _stub("homeassistant.components"),
    "homeassistant.components.cover": _stub(
        "homeassistant.components.cover",
        CoverEntityFeature=type("CEF", (), {"SET_POSITION": 1, "SET_TILT_POSITION": 2, "OPEN": 4, "CLOSE": 8, "STOP": 16}),
    ),
    "homeassistant.config_entries": _stub("homeassistant.config_entries", ConfigEntry=object),
    "homeassistant.core": _stub(
        "homeassistant.core", HomeAssistant=object, Event=object, callback=lambda fn: fn,
    ),
    "homeassistant.helpers": _stub("homeassistant.helpers"),
    "homeassistant.helpers.entity_registry": _stub("homeassistant.helpers.entity_registry", async_get=lambda *a, **k: None),
    "homeassistant.helpers.event": _stub(
        "homeassistant.helpers.event",
        async_track_state_change_event=lambda hass, entity_id, action: (lambda: None),
        async_track_point_in_time=lambda *a, **k: (lambda: None),
        async_call_later=lambda hass, delay, action: (lambda: None),
    ),
    "homeassistant.util": _stub("homeassistant.util"),
}
for _name, _mod in _HA_STUBS.items():
    sys.modules.setdefault(_name, _mod)

sys.modules["homeassistant.util.dt"] = _stub(
    "homeassistant.util.dt",
    utcnow=lambda: datetime.now(timezone.utc),
    now=lambda: datetime.now(timezone.utc),
    as_utc=lambda dt: dt.astimezone(timezone.utc),
    as_local=lambda dt: dt,
    DEFAULT_TIME_ZONE=timezone.utc,
)


class _CoordBase:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass


sys.modules.setdefault("homeassistant.helpers.update_coordinator", _stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=_CoordBase,
    CoordinatorEntity=type("CE", (), {"__class_getitem__": classmethod(lambda cls, x: cls), "__init__": lambda self, c: None}),
))
class _ControllableStore:
    """A real-shaped homeassistant.helpers.storage.Store stand-in: keyed
    storage that genuinely persists across construct/discard (mirroring the
    real on-disk Store's own persistence-beyond-the-Python-object
    semantics), with an injectable failure switch for async_save() -- lets
    a test exercise the REAL LearningPersistenceAdapter.async_save() -> Store
    failure -> caught-and-False path, not a hand-rolled adapter substitute."""

    _registry: dict[tuple[int, str], dict | None] = {}
    fail_next_save: bool = False

    def __init__(self, hass, version, key) -> None:
        self._key = (version, key)
        self._registry.setdefault(self._key, None)

    async def async_load(self):
        return self._registry[self._key]

    async def async_save(self, data: dict) -> None:
        if _ControllableStore.fail_next_save:
            _ControllableStore.fail_next_save = False
            raise OSError("simulated real Store.async_save() failure")
        self._registry[self._key] = data


def _install_storage_stub() -> None:
    # Direct assignment (never setdefault): another test module collected
    # either before or after this one in the same pytest session may
    # register its OWN homeassistant.helpers.storage stub via the same
    # pattern -- LearningPersistenceAdapter.__init__ imports Store lazily
    # (from homeassistant.helpers.storage import Store, executed at
    # _make_coord() CALL time, not at this module's own collection time),
    # so whichever stub is currently installed in sys.modules at THAT
    # moment wins. Re-installing this exact stub immediately before every
    # test in this file (autouse fixture below) is what keeps it stable
    # regardless of collection/run order across the whole suite.
    sys.modules["homeassistant.helpers.storage"] = _stub(
        "homeassistant.helpers.storage",
        Store=_ControllableStore,
    )


_install_storage_stub()


@pytest.fixture(autouse=True)
def _reinstall_storage_stub():
    _install_storage_stub()
    yield


sys.modules.pop("custom_components.smartshading.coordinator", None)
from custom_components.smartshading.coordinator import SmartShadingCoordinator  # noqa: E402
from custom_components.smartshading.models.lifecycle import (  # noqa: E402
    NightDayLifecycleConfig, NightTrigger, MorningTrigger,
)
from custom_components.smartshading.models.window import WindowConfig  # noqa: E402
from datetime import time as _time  # noqa: E402


def _make_coord() -> SmartShadingCoordinator:
    hass = types.SimpleNamespace(data={})
    entry = types.SimpleNamespace(entry_id="e1", options={}, data={}, async_on_unload=lambda cb: None)
    lifecycle_config = NightDayLifecycleConfig(
        id="default",
        night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=_time(22, 0), night_position=0,
        morning_trigger=MorningTrigger.FIXED_TIME, morning_fixed_time=_time(6, 30), morning_position=70,
    )
    coord = SmartShadingCoordinator(hass, entry, lifecycle_config=lifecycle_config)
    coord.windows = {}
    coord.zones = {}
    coord.cover_groups = {}
    coord._startup_cycles_remaining = 0
    return coord


def _window(window_id: str) -> WindowConfig:
    return WindowConfig(
        id=window_id, name=window_id, zone_id="z1", azimuth=180.0, floor_level=0,
        cover_group_id=f"cg_{window_id}",
    )


class TestFailedSaveLeavesMorningMarkerDirty:
    """B3-010 R14 item 3 -- proven through the coordinator's REAL, never-
    replaced self._learning_persistence (a genuine LearningPersistenceAdapter)
    down to a real (fake-backed) homeassistant.helpers.storage.Store whose
    async_save() genuinely raises on the first call -- not a hand-rolled
    stand-in for the adapter itself, so the real
    LearningPersistenceAdapter.async_save() try/except -> False path is
    what's actually exercised, never bypassed.

    A failed save must never advance self._saved_generation, so
    self._learning_dirty (the same generation-counter mechanism every
    _mark_learning_dirty() call site, including the Morning marker write-
    paths, already rides on) stays True. A LATER successful save must then
    persist the SAME current marker value -- proving the failure did not
    silently drop or corrupt the pending write."""

    async def _run(self):
        _ControllableStore._registry.clear()
        _ControllableStore.fail_next_save = False
        coord = _make_coord()
        coord.windows = {"w1": _window("w1")}
        assert coord._learning_dirty is False

        coord._morning_last_processed["w1"] = date(2026, 8, 10)
        coord._mark_learning_dirty()
        assert coord._learning_dirty is True

        _ControllableStore.fail_next_save = True
        now = datetime(2026, 8, 10, 7, 0, 0, tzinfo=timezone.utc)
        ok = await coord._save_learning_snapshot(now)
        assert ok is False
        assert _ControllableStore.fail_next_save is False, (
            "the real Store.async_save() call genuinely happened and raised "
            "-- the one-shot failure switch was consumed"
        )
        assert coord._learning_dirty is True, (
            "a failed save must leave the state dirty -- saved_generation "
            "must never advance past a captured generation that was never "
            "actually persisted"
        )
        assert coord._save_failures == 1

        # A later successful save (the real retry path: the coordinator's
        # own periodic/coalesced save simply tries again on the next
        # opportunity, same method, same still-dirty state) persists the
        # SAME current marker -- proving the failed attempt did not drop it.
        ok2 = await coord._save_learning_snapshot(now)
        assert ok2 is True
        assert coord._learning_dirty is False, (
            "the successful retry must clear dirty for the generation it "
            "actually persisted"
        )

        # Restore via a genuinely new coordinator's own real
        # self._learning_persistence.async_restore() to confirm the marker
        # that survived the failed-then-retried save is really on real
        # (fake-backed) storage, not just in the first coordinator's RAM.
        coord2 = _make_coord()
        coord2.windows = {"w1": _window("w1")}
        await coord2._learning_persistence.async_restore(coord2._learning_store, now)
        extras = coord2._learning_persistence.last_restore_extras
        assert extras is not None
        assert extras.morning_last_processed.get("w1") == "2026-08-10", (
            "the marker that survived the failed save must be the one a "
            "genuinely new coordinator instance's real restore path reads"
        )

    def test_failed_save_then_successful_retry_persists_the_same_marker(self):
        import asyncio
        asyncio.run(self._run())


class TestOrphanWindowMarkerPruning:
    def test_morning_last_processed_prunes_removed_window(self):
        coord = _make_coord()
        coord.windows = {"w1": _window("w1")}
        coord._morning_last_processed = {
            "w1": date(2026, 8, 10),
            "w2_removed": date(2026, 8, 9),  # window no longer in config
        }
        kwargs = coord._build_save_kwargs()
        assert kwargs["morning_last_processed"] == {"w1": "2026-08-10"}, (
            "an orphaned window's marker must be dropped from the "
            "persisted snapshot, not carried forever"
        )

    def test_night_last_started_prunes_removed_window(self):
        coord = _make_coord()
        coord.windows = {"w1": _window("w1")}
        coord._night_last_started = {
            "w1": date(2026, 8, 10),
            "w2_removed": date(2026, 8, 9),
        }
        kwargs = coord._build_save_kwargs()
        assert kwargs["night_last_started"] == {"w1": "2026-08-10"}

    def test_both_markers_pruned_independently_same_cycle(self):
        coord = _make_coord()
        coord.windows = {"w1": _window("w1"), "w2": _window("w2")}
        coord._morning_last_processed = {
            "w1": date(2026, 8, 10), "w_stale_a": date(2026, 8, 1),
        }
        coord._night_last_started = {
            "w2": date(2026, 8, 10), "w_stale_b": date(2026, 8, 1),
        }
        kwargs = coord._build_save_kwargs()
        assert kwargs["morning_last_processed"] == {"w1": "2026-08-10"}
        assert kwargs["night_last_started"] == {"w2": "2026-08-10"}

    def test_renamed_window_id_is_treated_as_orphan(self):
        """A window ID change (e.g. re-created via config flow) has no
        special-case migration -- the old ID's marker is simply pruned,
        exactly like a genuinely removed window; the new ID starts with no
        marker at all, correctly requiring fresh real evidence."""
        coord = _make_coord()
        coord.windows = {"w1_new": _window("w1_new")}
        coord._morning_last_processed = {"w1_old": date(2026, 8, 10)}
        coord._night_last_started = {"w1_old": date(2026, 8, 10)}
        kwargs = coord._build_save_kwargs()
        assert kwargs["morning_last_processed"] == {}
        assert kwargs["night_last_started"] == {}
