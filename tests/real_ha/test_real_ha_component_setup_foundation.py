"""PA-2a (real Home Assistant): the component-level setup foundation.

``async_setup`` enumerates every zone entry (disabled ones included) and runs a read-only
preparation hook for each.  It must not change how zones start, must not write anything,
must contain only its own dedicated failure type, and must not touch the legacy System entry.

Run with the rest of the real-HA suite (Linux, macOS or WSL)::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

import copy
import logging
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntryDisabler, ConfigEntryState
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.smartshading as ss
from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    to_storage_dict,
)
from custom_components.smartshading.const import (
    CONF_ENTRY_TYPE,
    DATA_GLOBAL_DISPATCH,
    DOMAIN,
    ENTRY_TYPE_SYSTEM,
    ENTRY_TYPE_ZONE,
)
from custom_components.smartshading.cover_control.global_dispatch_throttle import GlobalSerialDispatch
from custom_components.smartshading.models.cover_group import CoverGroup
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig

_SYSTEM_DATA = {
    CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM,
    "system_override_policy": {
        "release_strategy": "duration", "duration_min": 45, "night_duration_min": 300,
        "detection_tolerance": 99, "allow_comfort_actions": True,
        "allow_protection_actions": True, "safety_timeout_enabled": False,
    },
    "system_absence_delay_min": 90,
}
# A zone that defers on everything runs with the System policy/delay and its own tolerance.
_SYSTEM_EFFECTIVE = {"strategy": "duration", "duration": 45, "night": 300, "tolerance": 10,
                     "safety_timeout": False, "absence": 90}


def _zone(entry_id: str, *, deferring: bool = False, disabled: bool = False) -> MockConfigEntry:
    zone = ZoneConfig(id=f"zone_{entry_id}", name=f"Zone {entry_id}")
    window = WindowConfig(
        id=f"window_{entry_id}", name="South", zone_id=zone.id, azimuth=180.0, floor_level=0,
        cover_group_id=f"cg_{entry_id}")
    group = CoverGroup(id=f"cg_{entry_id}", window_id=window.id, cover_ids=[f"cover.test_{entry_id}"])
    data = {
        **to_storage_dict(SmartShadingConfigEntryData(
            name=f"Zone {entry_id}", use_home_location=False,
            zones=[zone], windows=[window], cover_groups=[group])),
        CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE,
    }
    if deferring:
        data.pop("override_policy", None)
        data["absence_delay_min"] = 20
        data["absence_delay_use_system_default"] = True
    return MockConfigEntry(
        domain=DOMAIN, data=data, entry_id=entry_id,
        disabled_by=ConfigEntryDisabler.USER if disabled else None)


def _system() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, data=copy.deepcopy(_SYSTEM_DATA), source="system",
                           entry_id="system", title="SmartShading System")


def _add(hass, *entries: MockConfigEntry) -> None:
    for e in entries:
        if e.domain == DOMAIN and e.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_SYSTEM and e.source != SOURCE_IGNORE:
            hass.states.async_set(f"cover.test_{e.entry_id}", "closed", {"current_position": 0})
        e.add_to_hass(hass)


async def _start(hass) -> bool:
    ok = await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return ok


def _effective(entry) -> dict[str, Any]:
    c = entry.runtime_data.coordinator
    return {
        "strategy": c._override_release_strategy.value, "duration": c._override_duration_min,
        "night": c._override_night_duration_min, "tolerance": c._override_detection_tolerance,
        "safety_timeout": c._override_safety_timeout_enabled, "absence": c._absence_delay_min,
    }


def _snapshot(hass) -> dict[str, dict[str, Any]]:
    return {
        e.entry_id: {
            "data": copy.deepcopy(dict(e.data)), "options": copy.deepcopy(dict(e.options)),
            "version": e.version, "minor_version": e.minor_version, "title": e.title,
            "unique_id": e.unique_id, "source": e.source, "disabled_by": e.disabled_by,
        }
        for e in hass.config_entries.async_entries()
        if e.domain in (DOMAIN, "other_domain")
    }


@pytest.fixture
def prepared(monkeypatch):
    """Record every entry id the preparation hook is called for (hook behavior unchanged)."""
    calls: list[str] = []
    original = ss._prepare_zone_entry

    def spy(entry):
        calls.append(entry.entry_id)
        original(entry)

    monkeypatch.setattr(ss, "_prepare_zone_entry", spy)
    return calls


class TestComponentSetup:
    async def test_component_setup_succeeds_without_any_entry(self, hass, prepared) -> None:
        assert await _start(hass)
        assert DOMAIN in hass.config.components
        assert prepared == []

    async def test_component_setup_succeeds_and_zones_start_as_before(self, hass, prepared) -> None:
        a, b = _zone("a"), _zone("b")
        _add(hass, a, b)
        assert await _start(hass)
        assert a.state is ConfigEntryState.LOADED and b.state is ConfigEntryState.LOADED
        assert sorted(prepared) == ["a", "b"]

    async def test_the_component_has_no_yaml_setup(self, hass) -> None:
        assert await async_setup_component(hass, DOMAIN, {}) is True


class TestDiscovery:
    async def test_enabled_and_disabled_zones_are_both_discovered(self, hass, prepared) -> None:
        enabled, disabled = _zone("on"), _zone("off", disabled=True)
        _add(hass, enabled, disabled)
        assert await _start(hass)
        assert sorted(prepared) == ["off", "on"]
        assert enabled.state is ConfigEntryState.LOADED
        assert disabled.state is ConfigEntryState.NOT_LOADED  # discovery does not start it
        assert disabled.disabled_by is ConfigEntryDisabler.USER

    async def test_ignored_system_and_unrelated_entries_are_not_prepared(self, hass, prepared) -> None:
        zone = _zone("a")
        ignored = MockConfigEntry(domain=DOMAIN, data={}, source=SOURCE_IGNORE, entry_id="ignored")
        other = MockConfigEntry(domain="other_domain", data={"k": "v"}, entry_id="other")
        _add(hass, zone, _system(), ignored, other)
        before_other = (other.data, other.state)
        assert await _start(hass)
        assert prepared == ["a"]
        assert ignored.state is ConfigEntryState.NOT_LOADED and dict(ignored.data) == {}
        assert (other.data, other.state) == before_other

    async def test_an_entry_without_entry_type_is_a_zone(self, hass, prepared) -> None:
        legacy = _zone("legacy")
        data = dict(legacy.data)
        data.pop(CONF_ENTRY_TYPE)
        legacy = MockConfigEntry(domain=DOMAIN, data=data, entry_id="legacy")
        _add(hass, legacy)
        assert await _start(hass)
        assert prepared == ["legacy"]


class TestFailureContainment:
    async def test_a_preparation_failure_does_not_stop_independent_zones(
        self, hass, monkeypatch, caplog
    ) -> None:
        seen: list[str] = []

        def hook(entry):
            seen.append(entry.entry_id)
            if entry.entry_id == "bad":
                raise ss.SmartShadingPreparationError("injected")

        monkeypatch.setattr(ss, "_prepare_zone_entry", hook)
        first, bad, last, off = _zone("first"), _zone("bad"), _zone("last"), _zone("off", disabled=True)
        _add(hass, first, bad, last, off)
        with caplog.at_level(logging.WARNING):
            assert await _start(hass)
        assert seen == ["first", "bad", "last", "off"]  # the loop went on after the failure
        assert first.state is ConfigEntryState.LOADED and last.state is ConfigEntryState.LOADED
        assert "bad" in caplog.text and "injected" in caplog.text

    async def test_an_unexpected_error_is_not_swallowed(self, hass, monkeypatch, caplog) -> None:
        def hook(entry):
            raise RuntimeError("unrelated bug")

        monkeypatch.setattr(ss, "_prepare_zone_entry", hook)
        zone = _zone("a")
        _add(hass, zone)
        with caplog.at_level(logging.ERROR):
            assert await _start(hass) is False
        assert "unrelated bug" in caplog.text
        assert zone.state is not ConfigEntryState.LOADED

    async def test_the_helper_lets_unexpected_errors_propagate(self, hass, monkeypatch) -> None:
        def hook(entry):
            raise ValueError("boom")

        monkeypatch.setattr(ss, "_prepare_zone_entry", hook)
        _add(hass, _zone("a"))
        with pytest.raises(ValueError):
            ss._prepare_zone_entries(hass)


class TestNothingChanges:
    async def test_system_derived_settings_are_unchanged(self, hass, prepared) -> None:
        zone = _zone("a", deferring=True)
        _add(hass, zone, _system())
        assert await _start(hass)
        assert _effective(zone) == _SYSTEM_EFFECTIVE

    async def test_no_data_option_or_version_is_written(self, hass, prepared, monkeypatch) -> None:
        updates: list[Any] = []
        original = hass.config_entries.async_update_entry

        def spy(entry, **kwargs):
            updates.append((entry.entry_id, sorted(kwargs)))
            return original(entry, **kwargs)

        monkeypatch.setattr(hass.config_entries, "async_update_entry", spy)
        other = MockConfigEntry(domain="other_domain", data={"k": "v"}, entry_id="other")
        _add(hass, _zone("a", deferring=True), _zone("off", disabled=True), _system(), other)
        before = _snapshot(hass)
        assert await _start(hass)
        assert updates == []
        assert _snapshot(hass) == before
        for e in hass.config_entries.async_entries(DOMAIN):
            assert (e.version, e.minor_version) == (1, 1)

    async def test_the_system_entry_is_neither_removed_nor_recreated(self, hass, prepared) -> None:
        system = _system()
        _add(hass, _zone("a"), _zone("off", disabled=True), system)
        assert await _start(hass)
        systems = [e for e in hass.config_entries.async_entries(DOMAIN)
                   if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SYSTEM]
        assert systems == [system] and system.entry_id == "system"
        assert system.state is ConfigEntryState.LOADED
        assert dict(system.data) == _SYSTEM_DATA

    async def test_tier1_dispatch_and_ownership_infrastructure_is_unchanged(self, hass, prepared) -> None:
        a, b = _zone("a"), _zone("b")
        _add(hass, a, b)
        assert await _start(hass)
        dispatch = hass.data[DOMAIN][DATA_GLOBAL_DISPATCH]
        assert type(dispatch) is GlobalSerialDispatch
        assert a.runtime_data.coordinator._serial_dispatch is dispatch
        assert b.runtime_data.coordinator._serial_dispatch is dispatch
        assert hass.services.has_service(DOMAIN, "clear_manual_override")


class TestRepeatedSetupAndReload:
    async def test_repeated_setup_and_reload_create_no_duplicates(self, hass, prepared) -> None:
        a, b = _zone("a"), _zone("b")
        _add(hass, a, b)
        assert await _start(hass)
        assert sorted(prepared) == ["a", "b"]
        dispatch = hass.data[DOMAIN][DATA_GLOBAL_DISPATCH]
        services = sorted(hass.services.async_services().get(DOMAIN, {}))

        # a second component setup is a no-op for Home Assistant and runs nothing again
        assert await _start(hass)
        for _ in range(2):
            assert await hass.config_entries.async_reload("a")
            await hass.async_block_till_done()
        listeners = dict(hass.bus.async_listeners())
        assert await hass.config_entries.async_reload("a")
        await hass.async_block_till_done()

        assert sorted(prepared) == ["a", "b"]          # component setup ran exactly once
        assert hass.data[DOMAIN][DATA_GLOBAL_DISPATCH] is dispatch
        assert sorted(hass.services.async_services().get(DOMAIN, {})) == services
        assert dict(hass.bus.async_listeners()) == listeners   # steady state: nothing accumulates
        assert a.state is ConfigEntryState.LOADED and b.state is ConfigEntryState.LOADED


class _NotAMapping:
    """Readable like stored data (``.get``) but not a Mapping: what the real guard rejects."""

    def get(self, key, default=None):
        return default


class _FakeEntry:
    def __init__(self, entry_id: str, data: Any) -> None:
        self.entry_id, self.data, self.source = entry_id, data, "user"
        self.version, self.minor_version = 1, 1


class TestRealPreparationGuard:
    """The REAL ``_prepare_zone_entry`` guard, with nothing monkeypatched in its place.

    Home Assistant entries always carry mapping data, so the guard can only be exercised
    with a controlled double; the surrounding enumeration is the real production code."""

    async def test_valid_mapping_data_is_accepted_and_left_untouched(self, hass) -> None:
        entry = _zone("a")
        _add(hass, entry)
        before = (copy.deepcopy(dict(entry.data)), entry.version, entry.minor_version)
        assert ss._prepare_zone_entry(entry) is None
        assert (dict(entry.data), entry.version, entry.minor_version) == before

    async def test_non_mapping_data_raise_the_dedicated_error(self) -> None:
        with pytest.raises(ss.SmartShadingPreparationError, match="bad"):
            ss._prepare_zone_entry(_FakeEntry("bad", _NotAMapping()))
        with pytest.raises(ss.SmartShadingPreparationError):
            ss._prepare_zone_entry(_FakeEntry("bad", ["not", "a", "mapping"]))

    async def test_a_failing_zone_is_not_prepared_but_the_others_are(self, hass, monkeypatch, caplog) -> None:
        first, last = _zone("first"), _zone("last")
        _add(hass, first, last)
        bad = _FakeEntry("bad", _NotAMapping())
        real = hass.config_entries.async_entries
        monkeypatch.setattr(
            hass.config_entries, "async_entries",
            lambda domain=None, *a, **k: [first, bad, last] if domain == DOMAIN else real(domain, *a, **k))
        before = {e.entry_id: (copy.deepcopy(dict(e.data)), e.version, e.minor_version) for e in (first, last)}
        with caplog.at_level(logging.WARNING):
            prepared = ss._prepare_zone_entries(hass)
        assert prepared == ["first", "last"]          # "bad" is not reported as prepared
        assert "bad" in caplog.text
        assert {e.entry_id: (dict(e.data), e.version, e.minor_version) for e in (first, last)} == before
        assert bad.data.__class__ is _NotAMapping and (bad.version, bad.minor_version) == (1, 1)
