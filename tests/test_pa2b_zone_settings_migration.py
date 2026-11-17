"""PA-2b (stub level): materialization of the System-derived zone settings.

Pure logic plus the write protocol against a minimal ConfigEntries double.  Persistence and
lifecycle against real Home Assistant are in tests/real_ha/test_real_ha_zone_settings_migration.py.
"""
from __future__ import annotations

import ast
import copy
import itertools
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.smartshading import zone_settings_migration as zm
from custom_components.smartshading.config_entry_data import (
    resolve_zone_absence_delay_min,
    resolve_zone_override_policy,
)
from custom_components.smartshading.zone_settings_migration import (
    ZoneMigrationDeferred,
    ZoneMigrationError,
    materialize_zone_settings,
    migrate_zone_entry,
    validate_legacy_zone_settings,
    verify_materialization,
)

_SYSTEM_POLICY = {
    "release_strategy": "duration", "duration_min": 45, "night_duration_min": 300,
    "detection_tolerance": 99, "allow_comfort_actions": True,
    "allow_protection_actions": True, "safety_timeout_enabled": False,
}
_SYSTEM = {"entry_type": "system", "system_override_policy": _SYSTEM_POLICY, "system_absence_delay_min": 90}
_OWN_POLICY = {
    "release_strategy": "manual", "fixed_until": None, "allow_comfort_actions": False,
    "allow_protection_actions": True, "duration_min": 10, "night_duration_min": 20,
    "detection_tolerance": 7, "safety_timeout_enabled": True,
}


def _zone(**over: Any) -> dict[str, Any]:
    base = {"name": "Z", "zones": [{"id": "z"}], "unrelated": {"keep": [1, 2]},
            "override_policy": copy.deepcopy(_OWN_POLICY), "absence_delay_min": 20,
            "absence_delay_use_system_default": False}
    base.update(over)
    return {k: v for k, v in base.items() if v != "DROP"}


def _effective(zone, system):
    return resolve_zone_override_policy(dict(zone), system), resolve_zone_absence_delay_min(dict(zone), system)


class _Config:
    def __init__(self, fail=None, partial=False):
        self.calls: list[tuple[Any, dict]] = []
        self.fail, self.partial = fail, partial

    def async_update_entry(self, entry, **kw):
        self.calls.append((entry, kw))
        if self.fail and not (self.partial and kw.get("minor_version") == zm.TARGET_MINOR_VERSION is False):
            if self.partial:
                entry.data, entry.minor_version = kw["data"], kw["minor_version"]   # applied, then raises
            if not self.partial or kw["minor_version"] == zm.TARGET_MINOR_VERSION:
                raise self.fail
        entry.data, entry.minor_version = kw.get("data", entry.data), kw.get("minor_version", entry.minor_version)
        return True


def _entry(data, minor=1, config=None):
    return SimpleNamespace(entry_id="e", data=data, minor_version=minor, version=1), \
        SimpleNamespace(config_entries=config or _Config())


# --- effective-value preservation over a full combination matrix ---------------------------

_POLICIES = ["DROP", None, {}, {"use_system_default": True, "release_strategy": "manual"},
             {"use_system_default": False, "release_strategy": "duration", "duration_min": 5},
             copy.deepcopy(_OWN_POLICY), {"release_strategy": "fixed_time", "fixed_until": "07:30:00"},
             {"release_strategy": "bogus", "break_on_lifecycle": False, "duration_mode": "fixed_time"},
             {"break_on_lifecycle": False, "duration_mode": "legacy", "detection_tolerance": 3}]
_DELAYS = ["DROP", None, 0, 20, "35", 12.0]
_FLAGS = ["DROP", True, False]
_SYSTEMS = [_SYSTEM, {"entry_type": "system"}, {"system_override_policy": {}, "system_absence_delay_min": 5}]


@pytest.mark.parametrize("policy,delay,flag", list(itertools.product(_POLICIES, _DELAYS, _FLAGS)))
@pytest.mark.parametrize("system", _SYSTEMS, ids=["custom", "empty", "partial"])
def test_effective_values_are_identical_before_and_after_materialization(policy, delay, flag, system) -> None:
    zone = _zone(override_policy=policy, absence_delay_min=delay, absence_delay_use_system_default=flag)
    validate_legacy_zone_settings(zone, system)
    migrated = materialize_zone_settings(zone, system)
    verify_materialization(zone, system, migrated)
    assert _effective(migrated, system) == _effective(zone, system)
    assert _effective(migrated, None) == _effective(zone, system)       # no System needed any more
    assert migrated["override_policy"]["use_system_default"] is False
    assert migrated["absence_delay_use_system_default"] is False
    assert {k: v for k, v in migrated.items() if k not in zm._OWN_KEYS} == \
        {k: v for k, v in zone.items() if k not in zm._OWN_KEYS}
    assert materialize_zone_settings(migrated, system) == migrated        # idempotent


def test_a_deferring_zone_keeps_its_own_detection_tolerance_and_takes_the_system_values() -> None:
    zone = _zone(override_policy={"use_system_default": True, "release_strategy": "x", "detection_tolerance": 7},
                 absence_delay_min=20, absence_delay_use_system_default=True)
    migrated = materialize_zone_settings(zone, _SYSTEM)
    assert migrated["override_policy"]["detection_tolerance"] == 7
    assert migrated["override_policy"]["duration_min"] == 45
    assert migrated["override_policy"]["release_strategy"] == "duration"
    assert migrated["absence_delay_min"] == 90


def test_explicit_zone_values_are_never_replaced() -> None:
    zone = _zone()
    migrated = materialize_zone_settings(zone, _SYSTEM)
    assert migrated["override_policy"] == {**_OWN_POLICY, "use_system_default": False}
    assert migrated["absence_delay_min"] == 20


def test_unknown_stored_policy_keys_survive() -> None:
    zone = _zone(override_policy={**_OWN_POLICY, "legacy_key": 1})
    assert materialize_zone_settings(zone, _SYSTEM)["override_policy"]["legacy_key"] == 1


# --- unverifiable historical values are never replaced by defaults --------------------------

@pytest.mark.parametrize("zone", [
    _zone(override_policy="manual"), _zone(override_policy=["x"]), _zone(override_policy=0),
    _zone(override_policy={"use_system_default": "false"}),
    _zone(override_policy={"use_system_default": 1, "release_strategy": "manual"}),
    _zone(absence_delay_use_system_default="false"), _zone(absence_delay_use_system_default=None),
    _zone(absence_delay_min=-5), _zone(absence_delay_min="soon"), _zone(absence_delay_min=True),
    _zone(absence_delay_min=[1]),
], ids=lambda z: str(sorted(z)[:0]) or "case")
def test_unverifiable_zone_values_are_refused(zone) -> None:
    with pytest.raises(ZoneMigrationError):
        validate_legacy_zone_settings(zone, _SYSTEM)


@pytest.mark.parametrize("system", [
    {"system_override_policy": "x"}, {"system_override_policy": ["x"]},
], ids=["str", "list"])
def test_malformed_system_policy_blocks_only_a_deferring_zone(system) -> None:
    deferring = _zone(override_policy={"use_system_default": True})
    with pytest.raises(ZoneMigrationError):
        validate_legacy_zone_settings(deferring, system)
    validate_legacy_zone_settings(_zone(), system)      # an explicit zone does not depend on it


@pytest.mark.parametrize("value", [-1, "x", True, [1]])
def test_malformed_system_delay_blocks_only_a_zone_deferring_the_delay(value) -> None:
    system = {"system_absence_delay_min": value}
    with pytest.raises(ZoneMigrationError):
        validate_legacy_zone_settings(_zone(absence_delay_use_system_default=True), system)
    validate_legacy_zone_settings(_zone(), system)


def test_a_missing_system_entry_defers_only_a_zone_that_depends_on_it() -> None:
    with pytest.raises(ZoneMigrationDeferred):
        validate_legacy_zone_settings(_zone(absence_delay_use_system_default=True), None)
    with pytest.raises(ZoneMigrationDeferred):
        validate_legacy_zone_settings(_zone(override_policy="DROP"), None)   # never configured => defers
    validate_legacy_zone_settings(_zone(), None)                              # explicit zone: fine


def test_documented_missing_keys_use_the_code_defaults() -> None:
    zone = _zone(override_policy="DROP", absence_delay_min="DROP")
    migrated = materialize_zone_settings(zone, {"entry_type": "system"})
    assert migrated["absence_delay_min"] == 30
    assert migrated["override_policy"]["release_strategy"] == "lifecycle"
    assert migrated["override_policy"]["duration_min"] == 120


# --- verification catches a wrong materialization ------------------------------------------

def test_verification_rejects_a_materialization_that_changes_behavior() -> None:
    zone = _zone(override_policy={"use_system_default": True}, absence_delay_use_system_default=True)
    good = materialize_zone_settings(zone, _SYSTEM)
    verify_materialization(zone, _SYSTEM, good)
    for broken in (
        {**good, "absence_delay_min": 91},
        {**good, "override_policy": {**good["override_policy"], "duration_min": 46}},
        {**good, "override_policy": {**good["override_policy"], "detection_tolerance": 1}},
        {**good, "override_policy": {**good["override_policy"], "use_system_default": True}},
        {**good, "absence_delay_use_system_default": True},
        {**good, "unrelated": {"keep": []}},
        {**good, "name": "other"},
    ):
        with pytest.raises(ZoneMigrationError):
            verify_materialization(zone, _SYSTEM, broken)


# --- the write protocol --------------------------------------------------------------------

def test_the_data_and_the_version_are_written_in_one_call() -> None:
    entry, hass = _entry(_zone(override_policy={"use_system_default": True}))
    assert migrate_zone_entry(hass, entry, _SYSTEM) is True
    assert len(hass.config_entries.calls) == 1
    _, kw = hass.config_entries.calls[0]
    assert set(kw) == {"data", "minor_version"} and kw["minor_version"] == 2
    assert entry.minor_version == 2 and entry.data == kw["data"]


def test_an_already_migrated_entry_is_not_rewritten() -> None:
    # PA-2c.1: "already migrated" is judged by the stored content, not by the version alone.
    entry, hass = _entry(materialize_zone_settings(_zone(), _SYSTEM), minor=2)
    before = copy.deepcopy(entry.data)
    assert migrate_zone_entry(hass, entry, _SYSTEM) is False
    assert hass.config_entries.calls == [] and entry.data == before


def test_repeating_the_migration_is_a_no_op() -> None:
    entry, hass = _entry(_zone(override_policy={"use_system_default": True}))
    migrate_zone_entry(hass, entry, _SYSTEM)
    migrate_zone_entry(hass, entry, _SYSTEM)
    assert len(hass.config_entries.calls) == 1


@pytest.mark.parametrize("zone,system,exc", [
    (_zone(override_policy="x"), _SYSTEM, ZoneMigrationError),
    (_zone(absence_delay_min=-1), _SYSTEM, ZoneMigrationError),
    (_zone(absence_delay_use_system_default=True), None, ZoneMigrationDeferred),
])
def test_a_refused_zone_is_not_written_and_not_marked(zone, system, exc) -> None:
    entry, hass = _entry(zone)
    before = copy.deepcopy(entry.data)
    with pytest.raises(exc):
        migrate_zone_entry(hass, entry, system)
    assert hass.config_entries.calls == [] and entry.data == before and entry.minor_version == 1


def test_a_failing_update_leaves_the_entry_unmigrated() -> None:
    entry, hass = _entry(_zone(override_policy={"use_system_default": True}), config=_Config(fail=OSError("disk")))
    before = copy.deepcopy(entry.data)
    with pytest.raises(ZoneMigrationError):
        migrate_zone_entry(hass, entry, _SYSTEM)
    assert entry.data == before and entry.minor_version == 1


def test_an_update_that_applied_before_raising_is_rolled_back_when_it_does_not_verify(monkeypatch) -> None:
    entry, hass = _entry(_zone(override_policy={"use_system_default": True}),
                         config=_Config(fail=RuntimeError("listener"), partial=True))
    before = copy.deepcopy(entry.data)
    monkeypatch.setattr(zm, "_is_migrated", lambda *a, **k: False)
    with pytest.raises(ZoneMigrationError):
        migrate_zone_entry(hass, entry, _SYSTEM)
    assert entry.data == before and entry.minor_version == 1       # restored, not half migrated


def test_a_persisted_state_that_does_not_verify_is_rolled_back(monkeypatch) -> None:
    entry, hass = _entry(_zone(override_policy={"use_system_default": True}))
    before = copy.deepcopy(entry.data)
    real = hass.config_entries.async_update_entry

    def tamper(e, **kw):
        if kw["minor_version"] == 2:
            kw = {**kw, "data": {**kw["data"], "absence_delay_min": 1}}
        return real(e, **kw)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", tamper)
    with pytest.raises(ZoneMigrationError):
        migrate_zone_entry(hass, entry, _SYSTEM)
    assert entry.data == before and entry.minor_version == 1


# --- structural -----------------------------------------------------------------------------

_SRC = (Path(zm.__file__)).read_text(encoding="utf-8")


def test_the_migration_module_updates_entries_with_data_and_version_together() -> None:
    calls = [n for n in ast.walk(ast.parse(_SRC)) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "async_update_entry"]
    assert len(calls) == 2                                  # the migration write and the rollback
    for call in calls:
        assert {k.arg for k in call.keywords} == {"data", "minor_version"}


def test_the_migration_module_never_touches_other_entries_or_the_system_entry() -> None:
    for forbidden in ("async_remove", "async_unload", "async_setup(", "async_entries", "ENTRY_TYPE_SYSTEM",
                      "async_create_task", "async_init"):
        assert forbidden not in _SRC


def test_the_resolvers_are_used_not_a_loader_round_trip() -> None:
    assert "resolve_zone_override_policy" in _SRC and "resolve_zone_absence_delay_min" in _SRC
    assert "from_storage_dict" not in _SRC and "to_storage_dict" not in _SRC


def test_a_wrong_materialization_is_never_written(monkeypatch) -> None:
    """The candidate is verified BEFORE the write: a bug in the materialization must not reach storage."""
    entry, hass = _entry(_zone(override_policy={"use_system_default": True}, absence_delay_use_system_default=True))
    before = copy.deepcopy(entry.data)
    real = zm.materialize_zone_settings
    monkeypatch.setattr(zm, "materialize_zone_settings",
                        lambda z, s: {**real(z, s), "absence_delay_min": 1})
    with pytest.raises(ZoneMigrationError):
        migrate_zone_entry(hass, entry, _SYSTEM)
    assert hass.config_entries.calls == [] and entry.data == before and entry.minor_version == 1
