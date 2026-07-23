"""Tests for the smartshading.clear_manual_override service (v1.2.0-beta.1,
T10.1) — custom_components/smartshading/services.py.

Uses lightweight duck-typed hass/config-entry/coordinator fakes rather than
the full real-SmartShadingCoordinator HA-stub technique used elsewhere —
this module's own logic (target resolution, dispatch to the right
coordinator/window, error surfacing, registration bookkeeping) is
self-contained and does not need a real coordinator to exercise. The
Learning/diagnostics-accuracy fix (T10.1, coordinator.py) is covered
separately in test_coordinator_explicit_clear_reason.py against the real
Coordinator per-cycle loop.

Coverage:
  SVC-01  Successful clear of an active override, any release strategy.
  SVC-02  No active override for the targeted window: no-op, no error.
  SVC-03  Unknown/invalid target entity_id: raises ServiceValidationError.
  SVC-04  No target selected at all: raises ServiceValidationError.
  SVC-05  Multiple config entries: correct window resolved to its OWN
          coordinator, not the first/wrong one.
  SVC-06  Multiple targeted windows in one call: all are cleared.
  SVC-07  Only the targeted window is touched — a second, non-targeted
          window's override is left alone.
  SVC-08  A device/area-expanded target that resolves to entities NOT
          belonging to any SmartShading window is ignored (not fatal) as
          long as at least one entity does resolve.
  SVC-09  Service registration is idempotent: calling async_setup_services
          twice (simulating a second zone entry) registers only once.
  SVC-10  async_unload_services_if_no_zone_entries_remain only removes the
          service when no OTHER zone entry is still loaded.
  SVC-11  async_unload_services_if_no_zone_entries_remain is a no-op while
          another zone entry is still loaded.
"""
from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# HA stubs — services.py needs voluptuous (real), homeassistant.core,
# homeassistant.exceptions, homeassistant.helpers.config_validation,
# homeassistant.helpers.entity_registry, homeassistant.helpers.service.
# ---------------------------------------------------------------------------


def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _ServiceValidationError(Exception):
    pass


class _ConfigEntryState:
    LOADED = "loaded"
    NOT_LOADED = "not_loaded"


# NOTE: conftest.py already registers comprehensive baseline stubs for
# homeassistant, homeassistant.core, homeassistant.config_entries, and
# homeassistant.helpers (its own docstring specifically warns against an
# alphabetically-earlier test file registering a WEAKER version and
# poisoning every later-collected file for the rest of the session) — so
# every module below that conftest already provides is extended additively
# (setattr for any missing attribute) rather than replaced outright. Only
# the modules conftest does NOT already stub (exceptions, config_validation,
# service) are registered directly.
sys.modules.setdefault("homeassistant", _stub("homeassistant"))

_core_mod = sys.modules.setdefault("homeassistant.core", _stub("homeassistant.core"))
for _attr, _val in (("HomeAssistant", object), ("ServiceCall", object), ("callback", lambda fn: fn)):
    if not hasattr(_core_mod, _attr):
        setattr(_core_mod, _attr, _val)

sys.modules["homeassistant.exceptions"] = _stub(
    "homeassistant.exceptions",
    ServiceValidationError=_ServiceValidationError,
)

_entries_mod = sys.modules.setdefault("homeassistant.config_entries", _stub("homeassistant.config_entries"))
for _attr, _val in (("ConfigEntry", object), ("ConfigEntryState", _ConfigEntryState)):
    if not hasattr(_entries_mod, _attr):
        setattr(_entries_mod, _attr, _val)

sys.modules.setdefault("homeassistant.helpers", _stub("homeassistant.helpers"))
sys.modules["homeassistant.helpers.config_validation"] = _stub(
    "homeassistant.helpers.config_validation",
    # Only ever called once, at services.py import time, to build a module
    # -level schema constant that these tests never actually invoke (HA
    # itself validates call.data before the handler runs) — a passthrough
    # is sufficient.
    make_entity_service_schema=lambda schema, **kw: (lambda data: data),
)

_er_mod = sys.modules.setdefault("homeassistant.helpers.entity_registry", _stub("homeassistant.helpers.entity_registry"))
if not hasattr(_er_mod, "async_get"):
    _er_mod.async_get = lambda hass: None

sys.modules["homeassistant.helpers.service"] = _stub(
    "homeassistant.helpers.service",
    async_extract_referenced_entity_ids=lambda hass, call: None,
)

sys.modules.pop("custom_components.smartshading.services", None)
from custom_components.smartshading import services as svc  # noqa: E402
from custom_components.smartshading.const import (  # noqa: E402
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_ZONE,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSelected:
    def __init__(self, referenced=(), indirectly_referenced=()) -> None:
        self.referenced = set(referenced)
        self.indirectly_referenced = set(indirectly_referenced)


class _FakeEntityEntry:
    def __init__(self, unique_id: str) -> None:
        self.unique_id = unique_id


class _FakeEntityRegistry:
    def __init__(self, entries: dict[str, str]) -> None:
        # entity_id -> unique_id
        self._entries = entries

    def async_get(self, entity_id: str):
        uid = self._entries.get(entity_id)
        return _FakeEntityEntry(uid) if uid is not None else None


class _FakeCoordinator:
    def __init__(self, windows: dict) -> None:
        self.windows = windows
        self.clear_calls: list[str] = []
        self._clear_results: dict[str, bool] = {}

    def set_clear_result(self, window_id: str, result: bool) -> None:
        self._clear_results[window_id] = result

    async def async_clear_manual_override(self, window_id: str) -> bool:
        self.clear_calls.append(window_id)
        return self._clear_results.get(window_id, True)


class _FakeRuntimeData:
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator


class _FakeConfigEntry:
    def __init__(self, entry_id: str, coordinator, state=_ConfigEntryState.LOADED) -> None:
        self.entry_id = entry_id
        self.data = {CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE}
        self.runtime_data = _FakeRuntimeData(coordinator)
        self.state = state


class _FakeServiceRegistry:
    def __init__(self) -> None:
        self._services: dict[tuple, object] = {}
        self.register_calls = 0
        self.remove_calls = 0

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self._services

    def async_register(self, domain, service, handler, schema=None) -> None:
        self._services[(domain, service)] = handler
        self.register_calls += 1

    def async_remove(self, domain, service) -> None:
        self._services.pop((domain, service), None)
        self.remove_calls += 1


class _FakeConfigEntries:
    def __init__(self, entries: list) -> None:
        self._entries = entries

    def async_entries(self, domain: str):
        return list(self._entries)


class _FakeHass:
    def __init__(self, entries: list) -> None:
        self.config_entries = _FakeConfigEntries(entries)
        self.services = _FakeServiceRegistry()


def _uid(entry_id: str, window_id: str) -> str:
    return f"{entry_id}_{window_id}_override_active"


def _entity_id_for(zone: str, window: str) -> str:
    return f"binary_sensor.{zone}_{window}_override_active"


def _patch_extract(monkeypatch, entity_ids) -> None:
    monkeypatch.setattr(
        svc, "async_extract_referenced_entity_ids",
        lambda hass, call: _FakeSelected(referenced=entity_ids),
    )


def _patch_registry(monkeypatch, entity_to_uid: dict[str, str]) -> None:
    monkeypatch.setattr(svc.er, "async_get", lambda hass: _FakeEntityRegistry(entity_to_uid))


class _FakeCall:
    def __init__(self, data=None) -> None:
        self.data = data or {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSuccessfulClear:
    def test_clears_active_override(self, monkeypatch) -> None:
        coord = _FakeCoordinator({"w1": object()})
        hass = _FakeHass([_FakeConfigEntry("e1", coord)])
        eid = _entity_id_for("z1", "w1")
        _patch_extract(monkeypatch, [eid])
        _patch_registry(monkeypatch, {eid: _uid("e1", "w1")})

        asyncio.run(svc._async_handle_clear_manual_override(hass, _FakeCall()))
        assert coord.clear_calls == ["w1"]


class TestNoActiveOverride:
    def test_no_op_no_error(self, monkeypatch) -> None:
        coord = _FakeCoordinator({"w1": object()})
        coord.set_clear_result("w1", False)
        hass = _FakeHass([_FakeConfigEntry("e1", coord)])
        eid = _entity_id_for("z1", "w1")
        _patch_extract(monkeypatch, [eid])
        _patch_registry(monkeypatch, {eid: _uid("e1", "w1")})

        # Must not raise.
        asyncio.run(svc._async_handle_clear_manual_override(hass, _FakeCall()))
        assert coord.clear_calls == ["w1"]


class TestInvalidTarget:
    def test_unknown_entity_raises(self, monkeypatch) -> None:
        coord = _FakeCoordinator({"w1": object()})
        hass = _FakeHass([_FakeConfigEntry("e1", coord)])
        _patch_extract(monkeypatch, ["binary_sensor.not_smartshading"])
        _patch_registry(monkeypatch, {})  # entity_registry has no entry for it

        with pytest.raises(svc.ServiceValidationError):
            asyncio.run(svc._async_handle_clear_manual_override(hass, _FakeCall()))
        assert coord.clear_calls == []

    def test_entity_belonging_to_another_integration_raises(self, monkeypatch) -> None:
        coord = _FakeCoordinator({"w1": object()})
        hass = _FakeHass([_FakeConfigEntry("e1", coord)])
        eid = "binary_sensor.some_other_integration_thing"
        _patch_extract(monkeypatch, [eid])
        _patch_registry(monkeypatch, {eid: "unrelated_unique_id"})

        with pytest.raises(svc.ServiceValidationError):
            asyncio.run(svc._async_handle_clear_manual_override(hass, _FakeCall()))


class TestNoTargetSelected:
    def test_empty_target_raises(self, monkeypatch) -> None:
        coord = _FakeCoordinator({"w1": object()})
        hass = _FakeHass([_FakeConfigEntry("e1", coord)])
        _patch_extract(monkeypatch, [])
        _patch_registry(monkeypatch, {})

        with pytest.raises(svc.ServiceValidationError):
            asyncio.run(svc._async_handle_clear_manual_override(hass, _FakeCall()))
        assert coord.clear_calls == []


class TestMultipleConfigEntries:
    def test_resolves_to_the_correct_entrys_coordinator(self, monkeypatch) -> None:
        coord_a = _FakeCoordinator({"w1": object()})
        coord_b = _FakeCoordinator({"w1": object()})  # same window_id, DIFFERENT entry/coordinator
        hass = _FakeHass([_FakeConfigEntry("entryA", coord_a), _FakeConfigEntry("entryB", coord_b)])
        eid = _entity_id_for("zoneB", "w1")
        _patch_extract(monkeypatch, [eid])
        _patch_registry(monkeypatch, {eid: _uid("entryB", "w1")})

        asyncio.run(svc._async_handle_clear_manual_override(hass, _FakeCall()))
        assert coord_a.clear_calls == []
        assert coord_b.clear_calls == ["w1"]


class TestMultipleTargetedWindows:
    def test_all_targeted_windows_cleared(self, monkeypatch) -> None:
        coord = _FakeCoordinator({"w1": object(), "w2": object(), "w3": object()})
        hass = _FakeHass([_FakeConfigEntry("e1", coord)])
        eid1, eid2 = _entity_id_for("z1", "w1"), _entity_id_for("z1", "w2")
        _patch_extract(monkeypatch, [eid1, eid2])
        _patch_registry(monkeypatch, {eid1: _uid("e1", "w1"), eid2: _uid("e1", "w2")})

        asyncio.run(svc._async_handle_clear_manual_override(hass, _FakeCall()))
        assert sorted(coord.clear_calls) == ["w1", "w2"]

    def test_non_targeted_window_untouched(self, monkeypatch) -> None:
        coord = _FakeCoordinator({"w1": object(), "w2": object()})
        hass = _FakeHass([_FakeConfigEntry("e1", coord)])
        eid1 = _entity_id_for("z1", "w1")
        _patch_extract(monkeypatch, [eid1])
        _patch_registry(monkeypatch, {eid1: _uid("e1", "w1")})

        asyncio.run(svc._async_handle_clear_manual_override(hass, _FakeCall()))
        assert coord.clear_calls == ["w1"]
        assert "w2" not in coord.clear_calls


class TestPartiallyResolvableTarget:
    def test_unmatched_entities_are_ignored_not_fatal(self, monkeypatch) -> None:
        coord = _FakeCoordinator({"w1": object()})
        hass = _FakeHass([_FakeConfigEntry("e1", coord)])
        eid_good = _entity_id_for("z1", "w1")
        eid_bad = "sensor.some_unrelated_device_battery"
        _patch_extract(monkeypatch, [eid_good, eid_bad])
        _patch_registry(monkeypatch, {eid_good: _uid("e1", "w1"), eid_bad: "unrelated"})

        asyncio.run(svc._async_handle_clear_manual_override(hass, _FakeCall()))
        assert coord.clear_calls == ["w1"]


class TestServiceRegistrationIdempotent:
    def test_second_registration_is_a_no_op(self) -> None:
        hass = _FakeHass([])
        svc.async_setup_services(hass)
        svc.async_setup_services(hass)
        assert hass.services.register_calls == 1
        assert hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)


class TestServiceUnloadOnLastZoneEntry:
    def test_removed_when_no_zone_entries_remain(self) -> None:
        hass = _FakeHass([_FakeConfigEntry("only_entry", _FakeCoordinator({}))])
        svc.async_setup_services(hass)
        assert hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)

        svc.async_unload_services_if_no_zone_entries_remain(hass, "only_entry")
        assert not hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)

    def test_kept_while_another_zone_entry_still_loaded(self) -> None:
        hass = _FakeHass([
            _FakeConfigEntry("e1", _FakeCoordinator({}), state=_ConfigEntryState.LOADED),
            _FakeConfigEntry("e2", _FakeCoordinator({}), state=_ConfigEntryState.LOADED),
        ])
        svc.async_setup_services(hass)

        svc.async_unload_services_if_no_zone_entries_remain(hass, "e1")
        assert hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)

    def test_removed_when_remaining_entries_are_not_loaded(self) -> None:
        hass = _FakeHass([
            _FakeConfigEntry("e1", _FakeCoordinator({}), state=_ConfigEntryState.LOADED),
            _FakeConfigEntry("e2", _FakeCoordinator({}), state=_ConfigEntryState.NOT_LOADED),
        ])
        svc.async_setup_services(hass)

        svc.async_unload_services_if_no_zone_entries_remain(hass, "e1")
        assert not hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)
