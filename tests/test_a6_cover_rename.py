"""A6 -- a cover renamed in the entity registry is followed.

Pure rewrite of the stored ids, the handler (config entry update, ownership /
Repairs resync, plan invalidation, reload) and the one-per-instance listener
lifecycle.  Home Assistant itself is replaced by small fakes.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from custom_components.smartshading import cover_ownership_runtime as rt
from custom_components.smartshading.engines.cover_ownership import rewrite_cover_ids

COVER = "cover.terrace"
NEW = "cover.terrace_new"


# --- minimal stored-data builders (same shape as the ownership unit tests) ---

def _data(*windows, zones=None, **entry_level):
    """Stored zone-entry data.  Each window is (id, covers, hardware, window_extras)."""
    zones = zones or [{"id": "z1", "name": "Zone 1", "execution": {"active_control_enabled": True}}]
    wins, groups = [], []
    for wid, covers, hw, extras in windows:
        zone_id = extras.pop("zone_id", zones[0]["id"])
        wins.append({"id": wid, "name": f"Window {wid}", "zone_id": zone_id,
                     "cover_group_id": f"cg_{wid}", "azimuth": 180.0, **extras})
        groups.append({"id": f"cg_{wid}", "window_id": wid, "cover_ids": list(covers),
                       "hardware_type": hw, "sync_mode": "synchronous"})
    base = {"zones": zones, "windows": wins, "cover_groups": groups,
            "shade_position_defaults": {"normal": 60}, "rain_sensor_id": None,
            "wind_speed_sensor_id": None, "weather_entity_id": None,
            "override_policy": {"duration_min": 60}}
    base.update(entry_level)
    return base


def W(wid, covers, hw="awning", **extras):
    return (wid, covers, hw, dict(extras))


def _entry(entry_id, data, *, disabled=False, title=None):
    return SimpleNamespace(
        entry_id=entry_id, title=title or f"Entry {entry_id}", data={**data, "entry_type": "zone"},
        disabled_by="user" if disabled else None)


class _FakeIssueRegistry:
    def __init__(self):
        self.issues = {}
        self.created = 0


@pytest.fixture
def fake_ir(monkeypatch):
    reg = _FakeIssueRegistry()
    mod = types.ModuleType("homeassistant.helpers.issue_registry")
    mod.IssueSeverity = SimpleNamespace(ERROR="error", WARNING="warning")

    def create(hass, domain, issue_id, **kw):
        reg.issues[(domain, issue_id)] = kw
        reg.created += 1

    def delete(hass, domain, issue_id):
        reg.issues.pop((domain, issue_id), None)

    mod.async_create_issue = create
    mod.async_delete_issue = delete
    mod.async_get = lambda hass: reg
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.issue_registry", mod)
    helpers = sys.modules.get("homeassistant.helpers")
    if helpers is not None:
        monkeypatch.setattr(helpers, "issue_registry", mod, raising=False)
    return reg


class _FakeConfigEntries:
    def __init__(self, entries):
        self.entries = list(entries)
        self.updated: list[str] = []
        self.reloads: list[str] = []

    def async_entries(self, domain):
        return list(self.entries)

    def async_get_entry(self, entry_id):
        return next((e for e in self.entries if e.entry_id == entry_id), None)

    def async_update_entry(self, entry, *, data):
        entry.data = data
        self.updated.append(entry.entry_id)

    def async_schedule_reload(self, entry_id):
        self.reloads.append(entry_id)


class ConfigEntryState:
    LOADED = "loaded"
    NOT_LOADED = "not_loaded"


@pytest.fixture(autouse=True)
def _entry_state(monkeypatch):
    mod = sys.modules.get("homeassistant.config_entries")
    if mod is None:
        mod = types.ModuleType("homeassistant.config_entries")
        monkeypatch.setitem(sys.modules, "homeassistant.config_entries", mod)
    monkeypatch.setattr(mod, "ConfigEntryState", ConfigEntryState, raising=False)


def _loaded(entry_id, data, coordinator=None):
    e = _entry(entry_id, data)
    e.state = ConfigEntryState.LOADED
    e.runtime_data = SimpleNamespace(coordinator=coordinator) if coordinator else None
    return e


def _hass(entries):
    return SimpleNamespace(data={}, config_entries=_FakeConfigEntries(entries),
                           bus=_FakeBus())


class _FakeBus:
    def __init__(self):
        self.listeners = []

    def async_listen(self, event_type, cb):
        entry = (event_type, cb)
        self.listeners.append(entry)
        return lambda: self.listeners.remove(entry)


class _Coord:
    def __init__(self):
        self.calls = []

    def async_invalidate_for_cover_identity_change(self, old, new):
        self.calls.append((old, new))
        return 1


class TestRewrite:
    def test_unique_cover_is_replaced(self):
        data = _data(("w1", [COVER, "cover.other"], "awning", {}))
        new, n = rewrite_cover_ids(data, {COVER: NEW})
        assert n == 1 and new["cover_groups"][0]["cover_ids"] == [NEW, "cover.other"]
        assert data["cover_groups"][0]["cover_ids"] == [COVER, "cover.other"]  # input untouched

    def test_case_and_whitespace_are_normalised_when_matching(self):
        new, n = rewrite_cover_ids(_data(("w1", [" Cover.Terrace "], "awning", {})), {COVER: NEW})
        assert n == 1 and new["cover_groups"][0]["cover_ids"] == [NEW]

    def test_rename_onto_an_id_already_in_the_group_keeps_it_once(self):
        new, n = rewrite_cover_ids(_data(("w1", [COVER, NEW], "awning", {})), {COVER: NEW})
        assert n == 1 and new["cover_groups"][0]["cover_ids"] == [NEW]

    def test_unrelated_data_and_garbage_are_left_alone(self):
        data = {"cover_groups": [{"cover_ids": "cover.x"}, "junk", {"id": "g"}], "zones": [1]}
        new, n = rewrite_cover_ids(data, {COVER: NEW})
        assert n == 0 and new["cover_groups"] == data["cover_groups"] and new["zones"] == [1]
        assert rewrite_cover_ids({}, {COVER: NEW}) == ({}, 0)


class TestHandler:
    def test_unique_cover_is_rewritten_reloaded_and_the_plan_invalidated(self, fake_ir):
        coord = _Coord()
        e1 = _loaded("e1", _data(("w1", [COVER], "awning", {})), coord)
        e2 = _loaded("e2", _data(("w1", ["cover.elsewhere"], "awning", {})), _Coord())
        hass = _hass([e1, e2])
        assert rt.async_handle_cover_rename(hass, "cover.Terrace", NEW) == ["e1"]
        assert e1.data["cover_groups"][0]["cover_ids"] == [NEW]
        assert e2.data["cover_groups"][0]["cover_ids"] == ["cover.elsewhere"]   # other zone untouched
        assert coord.calls == [(COVER, NEW)]
        assert hass.config_entries.reloads == ["e1"]
        assert rt.get_rename_stats(hass)["entries_updated"] == 1

    def test_plans_are_invalidated_before_the_stored_ids_change(self, fake_ir):
        order = []

        class _C(_Coord):
            def async_invalidate_for_cover_identity_change(self, old, new):
                order.append("invalidate")
                return 1

        hass = _hass([_loaded("e1", _data(("w1", [COVER], "awning", {})), _C())])
        orig = hass.config_entries.async_update_entry
        hass.config_entries.async_update_entry = lambda e, *, data: (order.append("update"), orig(e, data=data))
        rt.async_handle_cover_rename(hass, COVER, NEW)
        assert order == ["invalidate", "update"]

    def test_identical_duplicate_in_two_zones_follows_in_both(self, fake_ir):
        e1 = _loaded("e1", _data(("w1", [COVER], "awning", {})), _Coord())
        e2 = _loaded("e2", _data(("w1", [COVER], "awning", {})), _Coord())
        hass = _hass([e1, e2])
        assert rt.async_handle_cover_rename(hass, COVER, NEW) == ["e1", "e2"]
        assert hass.config_entries.reloads == ["e1", "e2"]
        # both now claim the new id identically: one warning, no error, no old issue
        (key, issue), = fake_ir.issues.items()
        assert NEW.replace(".", "_") in key[1] and issue["severity"] == "warning"

    def test_a_conflict_that_the_rename_removes_is_closed(self, fake_ir):
        e1 = _loaded("e1", _data(("w1", [COVER], "awning", {})), _Coord())
        e2 = _loaded("e2", _data(("w1", [COVER], "awning", {"azimuth": 3.0})), _Coord())
        hass = _hass([e1, e2])
        rt.async_sync_cover_ownership(hass)
        assert any(i["severity"] == "error" for i in fake_ir.issues.values())
        # only the registry id of e1's cover moved: e2 listed the same entity, so both follow
        # and the conflict is still a conflict on the NEW id -- but never on the old one
        rt.async_handle_cover_rename(hass, COVER, NEW)
        assert all("terrace_new" in k[1] for k in fake_ir.issues)

    def test_unknown_cover_changes_nothing(self, fake_ir):
        e1 = _loaded("e1", _data(("w1", [COVER], "awning", {})), _Coord())
        hass = _hass([e1])
        assert rt.async_handle_cover_rename(hass, "cover.nobody", NEW) == []
        assert hass.config_entries.updated == [] and hass.config_entries.reloads == []

    def test_an_entry_that_is_not_loaded_is_updated_but_not_reloaded(self, fake_ir):
        e1 = _loaded("e1", _data(("w1", [COVER], "awning", {})))
        e1.state = ConfigEntryState.NOT_LOADED
        hass = _hass([e1])
        assert rt.async_handle_cover_rename(hass, COVER, NEW) == ["e1"]
        assert e1.data["cover_groups"][0]["cover_ids"] == [NEW]
        assert hass.config_entries.reloads == []

    def test_repeated_event_is_harmless(self, fake_ir):
        hass = _hass([_loaded("e1", _data(("w1", [COVER], "awning", {})), _Coord())])
        rt.async_handle_cover_rename(hass, COVER, NEW)
        assert rt.async_handle_cover_rename(hass, COVER, NEW) == []
        assert hass.config_entries.reloads == ["e1"]


class _Event:
    def __init__(self, **data):
        self.data = data


@pytest.fixture
def er_stub(monkeypatch):
    mod = types.ModuleType("homeassistant.helpers.entity_registry")
    mod.EVENT_ENTITY_REGISTRY_UPDATED = "entity_registry_updated"
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", mod)
    helpers = sys.modules.get("homeassistant.helpers")
    if helpers is not None:
        monkeypatch.setattr(helpers, "entity_registry", mod, raising=False)
    return mod


class TestListener:
    def test_registered_once_and_removed_with_the_last_zone(self, er_stub, fake_ir):
        e1 = _loaded("e1", _data(("w1", [COVER], "awning", {})), _Coord())
        e2 = _loaded("e2", _data(("w1", ["cover.b"], "awning", {})), _Coord())
        hass = _hass([e1, e2])
        for _ in range(3):
            rt.async_setup_cover_rename_listener(hass)
        assert len(hass.bus.listeners) == 1
        rt.async_teardown_cover_rename_listener(hass, "e1")          # e2 still loaded
        assert len(hass.bus.listeners) == 1
        e2.state = ConfigEntryState.NOT_LOADED
        rt.async_teardown_cover_rename_listener(hass, "e1")
        assert hass.bus.listeners == []
        rt.async_teardown_cover_rename_listener(hass, "e1")          # idempotent
        assert hass.bus.listeners == []

    def test_only_cover_to_cover_updates_with_a_new_id_are_followed(self, er_stub, fake_ir):
        hass = _hass([_loaded("e1", _data(("w1", [COVER], "awning", {})), _Coord())])
        rt.async_setup_cover_rename_listener(hass)
        (_, cb), = hass.bus.listeners
        cb(_Event(action="update", entity_id="light.x", old_entity_id="cover.terrace"))
        cb(_Event(action="update", entity_id=NEW))                                  # no old id
        cb(_Event(action="create", entity_id=NEW, old_entity_id=COVER))
        cb(_Event(action="update", entity_id=COVER, old_entity_id=COVER))
        assert hass.config_entries.updated == []
        cb(_Event(action="update", entity_id=NEW, old_entity_id=COVER))
        assert hass.config_entries.updated == ["e1"]

    def test_a_failing_handler_does_not_raise_into_the_event_loop(self, er_stub, fake_ir, monkeypatch):
        hass = _hass([_loaded("e1", _data(("w1", [COVER], "awning", {})), _Coord())])
        rt.async_setup_cover_rename_listener(hass)
        monkeypatch.setattr(rt, "async_handle_cover_rename", lambda *a: 1 / 0)
        (_, cb), = hass.bus.listeners
        cb(_Event(action="update", entity_id=NEW, old_entity_id=COVER))
