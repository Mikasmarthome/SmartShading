"""A4 -- cover ownership (duplicate-cover protection), unit level.

Pure classification, the registry's change notification, Repairs issue sync
(through a fake issue registry), the flow helper and the translations.  The
coordinator-level behaviour (no service call, plan invalidation, recovery) is in
tests/test_rain_safe_position_semantics.py next to the shared coordinator
harness.
"""
from __future__ import annotations

import itertools
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.smartshading.engines import cover_ownership as co
from custom_components.smartshading.engines.cover_ownership import (
    ROLE_ACTIVE,
    ROLE_DEDUPLICATED,
    ROLE_QUARANTINED,
    ROLE_SAFETY_ONLY,
    CoverOwnershipRegistry,
    build_claims,
    classify,
    covers_claimed_by_others,
)


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


def _claims(*entries):
    """entries: (entry_id, data)"""
    return build_claims([(eid, f"Entry {eid}", data) for eid, data in entries])


COVER = "cover.terrace"


class TestDetection:
    def test_unique_cover_is_active(self):
        res = classify(_claims(("e1", _data(W("w1", [COVER])))))
        assert res.role("e1", "w1", COVER) == ROLE_ACTIVE
        assert res.covers[COVER].kind == co.KIND_UNIQUE

    def test_same_cover_twice_in_one_window_is_one_party_not_a_conflict(self):
        res = classify(_claims(("e1", _data(W("w1", [COVER, COVER])))))
        assert res.covers[COVER].kind == co.KIND_UNIQUE
        assert res.role("e1", "w1", COVER) == ROLE_ACTIVE

    def test_two_windows_of_one_zone(self):
        res = classify(_claims(("e1", _data(W("w1", [COVER]), W("w2", [COVER], azimuth=90.0)))))
        assert res.covers[COVER].kind == co.KIND_CONFLICT
        assert co.ASPECT_TARGETS in res.covers[COVER].aspects
        assert {res.role("e1", w, COVER) for w in ("w1", "w2")} == {ROLE_SAFETY_ONLY, ROLE_QUARANTINED}

    def test_two_zones_of_one_entry(self):
        zones = [{"id": "z1", "name": "A", "execution": {"active_control_enabled": True}},
                 {"id": "z2", "name": "B", "execution": {"active_control_enabled": True}}]
        data = _data(W("w1", [COVER], zone_id="z1"), W("w2", [COVER], zone_id="z2"), zones=zones)
        res = classify(_claims(("e1", data)))
        assert res.covers[COVER].kind == co.KIND_IDENTICAL          # same settings, different zone only
        assert {res.role("e1", "w1", COVER), res.role("e1", "w2", COVER)} == {ROLE_ACTIVE, ROLE_DEDUPLICATED}

    def test_two_entries(self):
        res = classify(_claims(("e1", _data(W("w1", [COVER]))), ("e2", _data(W("w1", [COVER])))))
        assert res.covers[COVER].kind == co.KIND_IDENTICAL
        assert res.role("e1", "w1", COVER) == ROLE_ACTIVE
        assert res.role("e2", "w1", COVER) == ROLE_DEDUPLICATED

    def test_three_owners(self):
        res = classify(_claims(
            ("e3", _data(W("w1", [COVER]))), ("e1", _data(W("w1", [COVER]))),
            ("e2", _data(W("w1", [COVER], azimuth=45.0)))))
        own = res.covers[COVER]
        assert own.kind == co.KIND_CONFLICT and len(own.parties) == 3
        roles = [res.role(e, "w1", COVER) for e in ("e1", "e2", "e3")]
        assert roles == [ROLE_SAFETY_ONLY, ROLE_QUARANTINED, ROLE_QUARANTINED]

    def test_normalization_follows_ha_entity_id_rules(self):
        res = classify(_claims(("e1", _data(W("w1", ["Cover.Terrace "]))),
                               ("e2", _data(W("w1", ["cover.terrace"])))))
        assert res.covers["cover.terrace"].kind == co.KIND_IDENTICAL

    @pytest.mark.parametrize("other", ["cover.terrace_2", "cover.terrace2", "cover.terrac", "cover_terrace"])
    def test_similar_but_different_ids_do_not_conflict(self, other):
        res = classify(_claims(("e1", _data(W("w1", [COVER]))), ("e2", _data(W("w1", [other])))))
        assert res.role("e1", "w1", COVER) == ROLE_ACTIVE
        assert res.role("e2", "w1", other) == ROLE_ACTIVE
        assert not res.duplicates()

    def test_group_and_single_assignment_in_different_windows(self):
        res = classify(_claims(("e1", _data(W("w1", [COVER, "cover.second"]), W("w2", [COVER], azimuth=10.0)))))
        assert res.covers[COVER].kind == co.KIND_CONFLICT
        assert res.role("e1", "w1", "cover.second") == ROLE_ACTIVE      # the unique cover keeps running


class TestSixConflictCases:
    """Checklist: different normal targets / hardware types / rain-wind-storm /
    Safety in only one / contact-override in only one => conflict; an exactly
    identical duplicate is deduplicated."""

    def _pair(self, a, b):
        return classify(_claims(("e1", _data(a)), ("e2", _data(b)))).covers[COVER]

    def test_exactly_identical_is_deduplicated(self):
        own = self._pair(W("w1", [COVER]), W("w1", [COVER]))
        assert own.kind == co.KIND_IDENTICAL

    def test_different_normal_targets(self):
        own = self._pair(W("w1", [COVER], azimuth=90.0), W("w1", [COVER], azimuth=180.0))
        assert own.kind == co.KIND_CONFLICT and co.ASPECT_TARGETS in own.aspects

    def test_different_hardware_types(self):
        own = self._pair(W("w1", [COVER], "awning"), W("w1", [COVER], "roller_shutter"))
        assert co.ASPECT_HARDWARE in own.aspects and not own.safety_ok

    def test_different_rain_settings(self):
        own = self._pair(W("w1", [COVER], rain_protection_enabled=True),
                         W("w1", [COVER], rain_protection_enabled=False))
        assert co.ASPECT_SAFETY in own.aspects and own.safety_ok and own.rain_union

    def test_safety_only_in_one_assignment(self):
        res = classify(_claims(
            ("e1", _data(W("w1", [COVER]), rain_sensor_id="binary_sensor.rain")),
            ("e2", _data(W("w1", [COVER])))))
        assert res.covers[COVER].kind == co.KIND_CONFLICT
        assert co.ASPECT_SAFETY in res.covers[COVER].aspects

    def test_contact_override_only_in_one_assignment(self):
        res = classify(_claims(
            ("e1", _data(W("w1", [COVER], contact_sensor_ids=["binary_sensor.door"]))),
            ("e2", _data(W("w1", [COVER])))))
        assert co.ASPECT_CONTACT_OVERRIDE in res.covers[COVER].aspects

    def test_different_rain_safe_positions_make_safety_ambiguous(self):
        own = self._pair(W("w1", [COVER], rain_safe_position_ha=0),
                         W("w1", [COVER], rain_safe_position_ha=30))
        assert own.kind == co.KIND_CONFLICT and not own.safety_ok


class TestDeterminism:
    ENTRIES = [
        ("e1", _data(W("w1", [COVER]), W("w9", ["cover.only_e1"]))),
        ("e2", _data(W("w1", [COVER], azimuth=45.0))),
        ("e3", _data(W("w2", [COVER]))),
    ]

    def test_result_does_not_depend_on_setup_or_listing_order(self):
        outcomes = set()
        for perm in itertools.permutations(self.ENTRIES):
            res = classify(_claims(*perm))
            outcomes.add(tuple(sorted(
                (e, w, c, res.role(e, w, c))
                for c, own in res.covers.items() for e, w in own.parties)))
        assert len(outcomes) == 1

    def test_owner_is_the_smallest_stable_key(self):
        res = classify(_claims(*reversed(self.ENTRIES)))
        assert res.covers[COVER].owner == ("e1", "w1")
        assert res.role("e1", "w1", COVER) == ROLE_SAFETY_ONLY

    def test_hardware_mismatch_means_nobody_sends(self):
        res = classify(_claims(("e1", _data(W("w1", [COVER], "awning"))),
                               ("e2", _data(W("w1", [COVER], "exterior_screen")))))
        assert {res.role("e1", "w1", COVER), res.role("e2", "w1", COVER)} == {ROLE_QUARANTINED}

    def test_a_broken_entry_never_breaks_the_others(self):
        claims = build_claims([("bad", "x", {"windows": "nonsense"}), ("e1", "ok", _data(W("w1", [COVER])))])
        assert [c.cover_id for c in claims] == [COVER]


class TestRegistry:
    def test_notifies_only_entries_whose_role_changed(self):
        reg = CoverOwnershipRegistry()
        seen = []
        reg.add_listener("e1", lambda: seen.append("e1"))
        reg.add_listener("e2", lambda: seen.append("e2"))
        reg.add_listener("e3", lambda: seen.append("e3"))
        reg.update(_claims(("e1", _data(W("w1", [COVER]))), ("e2", _data(W("w1", [COVER]))),
                           ("e3", _data(W("w1", ["cover.other"])))))
        assert seen == ["e2"]                                              # only e2 became deduplicated
        before = len(seen)
        reg.update(_claims(("e1", _data(W("w1", [COVER]))), ("e2", _data(W("w1", [COVER]))),
                           ("e3", _data(W("w1", ["cover.other"])))))
        assert len(seen) == before                                         # idempotent

    def test_resolution_is_notified_and_counted(self):
        reg = CoverOwnershipRegistry()
        reg.update(_claims(("e1", _data(W("w1", [COVER]))), ("e2", _data(W("w1", [COVER], azimuth=5.0)))))
        assert reg.summary()["active_conflicts"] == 1
        seen = []
        reg.add_listener("e2", lambda: seen.append("e2"))
        reg.update(_claims(("e1", _data(W("w1", [COVER])))))
        assert seen == ["e2"] and reg.summary()["active_conflicts"] == 0 and reg.resolutions == 1

    def test_two_parties_become_three_and_one_again(self):
        reg = CoverOwnershipRegistry()
        a, b, c = (("e%d" % i, _data(W("w1", [COVER], azimuth=float(i)))) for i in (1, 2, 3))
        reg.update(_claims(a, b))
        assert len(reg.result.covers[COVER].parties) == 2
        reg.update(_claims(a, b, c))
        assert len(reg.result.covers[COVER].parties) == 3
        reg.update(_claims(a))
        assert reg.role("e1", "w1", COVER) == ROLE_ACTIVE and not reg.result.duplicates()

    def test_removed_listener_is_not_called_and_leaves_nothing(self):
        reg = CoverOwnershipRegistry()
        calls = []
        unsub = reg.add_listener("e1", lambda: calls.append(1))
        unsub()
        unsub()
        reg.update(_claims(("e1", _data(W("w1", [COVER]))), ("e2", _data(W("w1", [COVER], azimuth=1.0)))))
        assert calls == [] and reg.listener_count() == 0

    def test_flow_helper_rejects_new_duplicates_only(self):
        claims = _claims(("e1", _data(W("w1", [COVER]))))
        assert covers_claimed_by_others(claims, [COVER], own_key=None) == [COVER]
        assert covers_claimed_by_others(claims, ["Cover.Terrace"], own_key=None) == [COVER]
        assert covers_claimed_by_others(claims, [COVER], own_key=("e1", "w1")) == []      # editing itself
        assert covers_claimed_by_others(claims, ["cover.free"], own_key=None) == []


# ---------------------------------------------------------------------------
# Repairs issues through a fake issue registry
# ---------------------------------------------------------------------------


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


def _hass(entries):
    return SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(async_entries=lambda domain: list(entries)),
    )


def _entry(entry_id, data, *, disabled=False, title=None):
    return SimpleNamespace(
        entry_id=entry_id, title=title or f"Entry {entry_id}", data={**data, "entry_type": "zone"},
        disabled_by="user" if disabled else None)


class TestRepairsAndSync:
    def _sync(self, hass, **kw):
        from custom_components.smartshading import cover_ownership_runtime as rt
        return rt.async_sync_cover_ownership(hass, **kw)

    def test_one_stable_issue_per_conflict_independent_of_order(self, fake_ir):
        e1 = _entry("e1", _data(W("w1", [COVER])))
        e2 = _entry("e2", _data(W("w1", [COVER], azimuth=3.0)))
        self._sync(_hass([e1, e2]))
        ids_a = set(fake_ir.issues)
        fake_ir.issues.clear()
        self._sync(_hass([e2, e1]))
        assert ids_a == set(fake_ir.issues) == {("smartshading", "duplicate_cover_cover_terrace")}

    def test_conflict_issue_contents_and_privacy(self, fake_ir):
        self._sync(_hass([_entry("e1", _data(W("w1", [COVER]), rain_sensor_id="binary_sensor.secret")),
                          _entry("e2", _data(W("w1", [COVER])))]))
        issue = fake_ir.issues[("smartshading", "duplicate_cover_cover_terrace")]
        assert issue["translation_key"] == "duplicate_cover_conflict"
        assert issue["is_fixable"] is False and issue["severity"] == "error"
        ph = issue["translation_placeholders"]
        assert set(ph) == {"cover", "locations", "count"} and ph["cover"] == COVER and ph["count"] == "2"
        assert "binary_sensor.secret" not in json.dumps(ph) and "Window w1" in ph["locations"]

    def test_hardware_mismatch_uses_the_no_safety_text(self, fake_ir):
        self._sync(_hass([_entry("e1", _data(W("w1", [COVER], "awning"))),
                          _entry("e2", _data(W("w1", [COVER], "roller_shutter")))]))
        assert fake_ir.issues[("smartshading", "duplicate_cover_cover_terrace")][
            "translation_key"] == "duplicate_cover_conflict_no_safety"

    def test_identical_duplicate_gets_a_warning_not_an_error(self, fake_ir):
        self._sync(_hass([_entry("e1", _data(W("w1", [COVER]))), _entry("e2", _data(W("w1", [COVER])))]))
        (key, issue), = fake_ir.issues.items()
        assert key[1].startswith("duplicate_cover_identical_") and issue["severity"] == "warning"
        assert issue["translation_key"] == "duplicate_cover_identical"

    def test_repeated_sync_does_not_duplicate_and_resolution_closes_the_issue(self, fake_ir):
        e1 = _entry("e1", _data(W("w1", [COVER])))
        e2 = _entry("e2", _data(W("w1", [COVER], azimuth=3.0)))
        hass = _hass([e1, e2])
        for _ in range(3):
            self._sync(hass)
        assert len(fake_ir.issues) == 1
        e2.data = {**_data(W("w1", ["cover.elsewhere"])), "entry_type": "zone"}      # user fixed it
        self._sync(hass)
        assert fake_ir.issues == {}

    def test_removing_an_entry_closes_the_issue_and_disabled_entries_do_not_count(self, fake_ir):
        e1 = _entry("e1", _data(W("w1", [COVER])))
        e2 = _entry("e2", _data(W("w1", [COVER], azimuth=3.0)))
        hass = _hass([e1, e2])
        self._sync(hass)
        assert fake_ir.issues
        self._sync(hass, exclude_entry_id="e2")           # e2 is being removed
        assert fake_ir.issues == {}
        self._sync(hass)
        assert fake_ir.issues
        e2.disabled_by = "user"
        self._sync(hass)
        assert fake_ir.issues == {}

    def test_new_conflict_after_reload_reappears(self, fake_ir):
        e1 = _entry("e1", _data(W("w1", [COVER])))
        e2 = _entry("e2", _data(W("w1", ["cover.elsewhere"])))
        hass = _hass([e1, e2])
        self._sync(hass)
        assert fake_ir.issues == {}
        e2.data = {**_data(W("w1", [COVER], azimuth=9.0)), "entry_type": "zone"}
        self._sync(hass)
        assert len(fake_ir.issues) == 1

    def test_system_entry_is_ignored(self, fake_ir):
        sys_entry = SimpleNamespace(entry_id="sys", title="System", data={"entry_type": "system"},
                                    disabled_by=None)
        self._sync(_hass([sys_entry, _entry("e1", _data(W("w1", [COVER])))]))
        assert fake_ir.issues == {}

    def test_issue_sync_failure_never_breaks_setup(self, fake_ir, monkeypatch):
        mod = sys.modules["homeassistant.helpers.issue_registry"]
        monkeypatch.setattr(mod, "async_create_issue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        self._sync(_hass([_entry("e1", _data(W("w1", [COVER]))), _entry("e2", _data(W("w1", [COVER], azimuth=2.0)))]))


# ---------------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------------

_BASE = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"


class TestTranslations:
    def _all(self):
        files = {"strings": _BASE / "strings.json"}
        files.update({p.stem: p for p in (_BASE / "translations").glob("*.json")})
        return {k: json.loads(p.read_text(encoding="utf-8")) for k, p in files.items()}

    def test_issue_and_error_keys_exist_in_every_language(self):
        data = self._all()
        assert len(data) == 25
        for name, d in data.items():
            assert set(d["issues"]) == {
                "duplicate_cover_conflict", "duplicate_cover_conflict_no_safety", "duplicate_cover_identical"}, name
            for issue in d["issues"].values():
                assert issue["title"].strip() and issue["description"].strip()
            assert d["config"]["error"]["cover_already_assigned"].strip(), name
            assert d["options"]["error"]["cover_already_assigned"].strip(), name

    def test_placeholders_are_preserved_in_every_language(self):
        for name, d in self._all().items():
            for key, issue in d["issues"].items():
                assert "{cover}" in issue["description"] and "{locations}" in issue["description"], (name, key)
                assert "{" not in issue["title"], (name, key)
