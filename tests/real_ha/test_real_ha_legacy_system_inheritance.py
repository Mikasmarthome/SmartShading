"""PA-1 characterization (real Home Assistant): what a zone actually RUNS with when its
Manual Override policy / absence delay come from, or defer to, the legacy System entry.

This pins the CURRENT behavior of ``_async_setup_zone_entry`` (``__init__.py``) which reads
the System entry's stored ``data`` (not its runtime state) and hands the resolved values to
the coordinator.  It is a regression baseline for the zone-only migration, not a statement
about the future architecture.  Known weaknesses are pinned as they are and labelled.

The orphan/creation/removal lifecycle itself is covered by
``test_real_ha_system_entry_lifecycle.py``; this module only adds what that one does not:
the effective values, disabled-zone data persistence, several zones with different
settings, reload baselines and the untouched stored configuration.

Run with the rest of the real-HA suite (Linux, macOS or WSL)::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

import copy
from typing import Any

from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    to_storage_dict,
)
from custom_components.smartshading.const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_SYSTEM,
    ENTRY_TYPE_ZONE,
)
from custom_components.smartshading.models.cover_group import CoverGroup
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig

_SYSTEM_POLICY = {
    "release_strategy": "duration", "duration_min": 45, "night_duration_min": 300,
    "detection_tolerance": 99, "allow_comfort_actions": True,
    "allow_protection_actions": True, "safety_timeout_enabled": False,
}
_SYSTEM_DATA = {
    CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM,
    "system_override_policy": _SYSTEM_POLICY,
    "system_absence_delay_min": 90,
}
# What a zone runs with when nothing is customised anywhere
_DEFAULTS = {"strategy": "lifecycle", "duration": 120, "night": 720, "tolerance": 10,
             "safety_timeout": True, "absence": 30}


def _zone(
    entry_id: str, *, override_policy: Any = "keep", absence_delay: Any = "keep",
    absence_flag: Any = "keep", disabled_by=None,
) -> MockConfigEntry:
    """A zone entry as the config flow would store it, then adjusted to a legacy shape.

    ``"keep"`` leaves the stored value as written by ``to_storage_dict``; ``None`` removes
    the key; anything else replaces it."""
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
    for key, value in (("override_policy", override_policy), ("absence_delay_min", absence_delay),
                       ("absence_delay_use_system_default", absence_flag)):
        if value == "keep":
            continue
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return MockConfigEntry(domain=DOMAIN, data=data, entry_id=entry_id, disabled_by=disabled_by)


def _system(entry_id: str = "system", data: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, data=copy.deepcopy(data if data is not None else _SYSTEM_DATA),
        source="system", entry_id=entry_id, title="SmartShading System")


def _cover(hass, *names: str) -> None:
    for name in names:
        hass.states.async_set(f"cover.test_{name}", "closed", {"current_position": 0})


async def _setup(hass, entry: MockConfigEntry) -> None:
    _cover(hass, entry.entry_id)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _effective(entry: MockConfigEntry) -> dict[str, Any]:
    c = entry.runtime_data.coordinator
    return {
        "strategy": c._override_release_strategy.value, "duration": c._override_duration_min,
        "night": c._override_night_duration_min, "tolerance": c._override_detection_tolerance,
        "safety_timeout": c._override_safety_timeout_enabled, "absence": c._absence_delay_min,
    }


# What a zone that defers on everything runs with: the System policy and delay, but the
# zone's own detection tolerance (default 10) - the System's tolerance of 99 never applies.
_SYSTEM_EFFECTIVE = {"strategy": "duration", "duration": 45, "night": 300, "tolerance": 10,
                     "safety_timeout": False, "absence": 90}


def _systems(hass):
    return [e for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SYSTEM]


def _add_unrelated(hass):
    other = MockConfigEntry(domain="other_domain", data={"k": "v"}, entry_id="other")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other_domain", "dev1")})
    entity = er.async_get(hass).async_get_or_create(
        "sensor", "other_domain", "uid1", config_entry=other, device_id=device.id)
    hass.states.async_set("sensor.unrelated", "7")
    return other, device, entity


def _assert_unrelated_untouched(hass, unrelated) -> None:
    other, device, entity = unrelated
    assert hass.config_entries.async_get_entry("other") is other
    assert other.data == {"k": "v"}
    assert dr.async_get(hass).async_get(device.id) is not None
    assert er.async_get(hass).async_get(entity.entity_id) is not None
    assert hass.states.get("sensor.unrelated").state == "7"


class TestZoneSetupReadsTheSystemEntryData:
    async def test_a_deferring_zone_set_up_before_the_system_entry_runs_gets_the_system_values(self, hass) -> None:
        """The System entry is only registered, never set up: the zone reads its STORED data."""
        system = _system()
        system.add_to_hass(hass)
        zone = _zone("a", override_policy={"release_strategy": "manual", "use_system_default": True},
                     absence_delay=20, absence_flag=True)
        await _setup(hass, zone)
        assert _effective(zone) == {**_SYSTEM_EFFECTIVE}
        assert system.data == _SYSTEM_DATA  # stored System data untouched

    async def test_a_deferring_zone_started_together_with_the_system_entry_gets_the_same_values(self, hass) -> None:
        # A System entry without any zone is an orphan and removes itself when set up, so the
        # zone is registered as well.  Setting up the first registered entry starts the whole
        # domain, i.e. both entries are set up by Home Assistant in one go.
        system = _system()
        zone = _zone("a", override_policy={"use_system_default": True, "release_strategy": "manual"},
                     absence_delay=20, absence_flag=True)
        _cover(hass, "a")
        system.add_to_hass(hass)
        zone.add_to_hass(hass)
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()
        assert system.state is ConfigEntryState.LOADED and zone.state is ConfigEntryState.LOADED
        assert _effective(zone) == _SYSTEM_EFFECTIVE

    async def test_the_system_entry_is_found_by_type_not_by_registration_order(self, hass) -> None:
        """Zones registered before AND after the System entry all find its data; a zone's own
        data is never mistaken for the System entry's."""
        first = _zone("first", override_policy=None, absence_delay=20, absence_flag=True)
        system = _system()
        last = _zone("last", override_policy=None, absence_delay=20, absence_flag=True)
        for e in (first, system, last):
            _cover(hass, e.entry_id)
            e.add_to_hass(hass)
        assert [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)] == ["first", "system", "last"]
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()
        assert _effective(first) == _SYSTEM_EFFECTIVE
        assert _effective(last) == _SYSTEM_EFFECTIVE

    async def test_a_zone_that_never_configured_override_defers_but_keeps_its_own_tolerance(self, hass) -> None:
        system = _system()
        system.add_to_hass(hass)
        zone = _zone("a", override_policy=None)  # no override_policy key at all
        await _setup(hass, zone)
        assert _effective(zone)["duration"] == 45
        assert _effective(zone)["tolerance"] == 10  # never the System's 99

    async def test_a_flow_written_zone_ignores_a_customised_system_entry(self, hass) -> None:
        """A zone stored by the config flow carries release_strategy and no flag: it does
        not defer, whatever the System entry holds."""
        system = _system()
        system.add_to_hass(hass)
        zone = _zone("a", absence_delay=20)
        await _setup(hass, zone)
        assert _effective(zone) == {**_DEFAULTS, "absence": 20}

    async def test_the_zone_data_is_never_modified_by_setup(self, hass) -> None:
        system = _system()
        system.add_to_hass(hass)
        zone = _zone("a", override_policy={"use_system_default": True, "release_strategy": "manual"},
                     absence_delay=20, absence_flag=True)
        before = copy.deepcopy(dict(zone.data))
        await _setup(hass, zone)
        assert dict(zone.data) == before  # stored config is not the effective config

    async def test_a_missing_system_entry_means_defaults_at_the_first_setup(self, hass) -> None:
        """Zone setup schedules creation of the System entry; the zone resolved its values
        BEFORE it existed, so the first setup runs with the code defaults."""
        assert _systems(hass) == []
        zone = _zone("a", override_policy=None, absence_delay=20, absence_flag=True)
        await _setup(hass, zone)
        assert _effective(zone) == _DEFAULTS
        created = _systems(hass)
        assert len(created) == 1
        assert dict(created[0].data) == {CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM}  # created empty

    async def test_a_present_but_empty_system_entry_means_defaults_after_a_reload(self, hass) -> None:
        zone = _zone("a", override_policy=None, absence_delay=20, absence_flag=True)
        await _setup(hass, zone)
        assert len(_systems(hass)) == 1
        assert await hass.config_entries.async_reload(zone.entry_id)
        await hass.async_block_till_done()
        assert _effective(zone) == _DEFAULTS


class TestChangesToTheSystemEntryAreOnlyPickedUpOnZoneReload:
    async def test_a_changed_system_default_reaches_a_deferring_zone_only_after_a_reload(self, hass) -> None:
        system = _system(data={CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM})
        system.add_to_hass(hass)
        zone = _zone("a", override_policy=None, absence_delay=20, absence_flag=True)
        await _setup(hass, zone)
        assert _effective(zone) == _DEFAULTS
        hass.config_entries.async_update_entry(system, data=copy.deepcopy(_SYSTEM_DATA))
        await hass.async_block_till_done()
        assert _effective(zone) == _DEFAULTS  # running coordinator keeps its values
        assert await hass.config_entries.async_reload(zone.entry_id)
        await hass.async_block_till_done()
        assert _effective(zone) == _SYSTEM_EFFECTIVE


class TestSeveralZonesWithDifferentSettings:
    async def test_each_zone_resolves_independently_against_one_system_entry(self, hass) -> None:
        unrelated = _add_unrelated(hass)
        system = _system()
        system.add_to_hass(hass)
        defers = _zone("defers", override_policy=None, absence_delay=20, absence_flag=True)
        own = _zone("own", override_policy={
            "release_strategy": "first_protection", "duration_min": 15, "use_system_default": False,
            "detection_tolerance": 25}, absence_delay=20, absence_flag=False)
        mixed = _zone("mixed", override_policy={"detection_tolerance": 33}, absence_delay=55)
        for z in (defers, own, mixed):
            await _setup(hass, z)

        assert _effective(defers) == _SYSTEM_EFFECTIVE | {"tolerance": 10}
        assert _effective(own) == {
            "strategy": "first_protection", "duration": 15, "night": 720, "tolerance": 25,
            "safety_timeout": True, "absence": 20}
        assert _effective(mixed) == _SYSTEM_EFFECTIVE | {"tolerance": 33, "absence": 55}
        assert len(_systems(hass)) == 1 and dict(system.data) == _SYSTEM_DATA
        _assert_unrelated_untouched(hass, unrelated)

    async def test_a_changed_system_default_changes_only_the_deferring_values_after_reload(self, hass) -> None:
        system = _system()
        system.add_to_hass(hass)
        defers = _zone("defers", override_policy=None, absence_delay=20, absence_flag=True)
        own = _zone("own", override_policy={"release_strategy": "manual", "use_system_default": False},
                    absence_delay=20)
        for z in (defers, own):
            await _setup(hass, z)
        own_before = _effective(own)
        new_policy = {**_SYSTEM_POLICY, "duration_min": 200}
        hass.config_entries.async_update_entry(
            system, data={**_SYSTEM_DATA, "system_override_policy": new_policy, "system_absence_delay_min": 5})
        await hass.async_block_till_done()
        for z in (defers, own):
            assert await hass.config_entries.async_reload(z.entry_id)
        await hass.async_block_till_done()
        assert _effective(defers)["duration"] == 200 and _effective(defers)["absence"] == 5
        assert _effective(own) == own_before


class TestDisabledZone:
    async def test_a_disabled_zone_keeps_its_stored_configuration_untouched(self, hass) -> None:
        unrelated = _add_unrelated(hass)
        system = _system()
        system.add_to_hass(hass)
        disabled = _zone("off", override_policy=None, absence_delay=20, absence_flag=True,
                         disabled_by=ConfigEntryDisabler.USER)
        disabled.add_to_hass(hass)
        before = copy.deepcopy(dict(disabled.data))
        live = _zone("on", override_policy=None, absence_delay=20, absence_flag=True)
        await _setup(hass, live)
        hass.config_entries.async_update_entry(system, data={CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM})
        assert await hass.config_entries.async_reload(live.entry_id)
        await hass.async_block_till_done()
        assert disabled.state is ConfigEntryState.NOT_LOADED
        assert dict(disabled.data) == before
        assert getattr(disabled, "runtime_data", None) is None
        _assert_unrelated_untouched(hass, unrelated)

    async def test_a_disabled_zone_enabled_later_resolves_against_the_system_data_of_that_moment(self, hass) -> None:
        system = _system()
        system.add_to_hass(hass)
        disabled = _zone("off", override_policy=None, absence_delay=20, absence_flag=True,
                         disabled_by=ConfigEntryDisabler.USER)
        disabled.add_to_hass(hass)
        _cover(hass, "off")
        assert dict(disabled.data)["absence_delay_min"] == 20
        hass.config_entries.async_update_entry(
            system, data={**_SYSTEM_DATA, "system_absence_delay_min": 61})
        await hass.config_entries.async_set_disabled_by(disabled.entry_id, None)
        await hass.async_block_till_done()
        assert disabled.state is ConfigEntryState.LOADED
        assert _effective(disabled)["absence"] == 61
        assert dict(disabled.data)["absence_delay_min"] == 20  # stored value untouched


class TestReloadAndRestartBaseline:
    async def test_unload_and_set_up_everything_again_changes_no_stored_data(self, hass) -> None:
        unrelated = _add_unrelated(hass)
        system = _system()
        zones = [
            _zone("defers", override_policy=None, absence_delay=20, absence_flag=True),
            _zone("own", override_policy={"release_strategy": "manual", "use_system_default": False},
                  absence_delay=20),
        ]
        for z in zones:
            _cover(hass, z.entry_id)
            z.add_to_hass(hass)
        system.add_to_hass(hass)
        assert await async_setup_component(hass, DOMAIN, {})  # the zones exist: no orphan
        await hass.async_block_till_done()
        assert system.state is ConfigEntryState.LOADED
        assert all(z.state is ConfigEntryState.LOADED for z in zones)
        effective_before = {z.entry_id: _effective(z) for z in zones}
        stored_before = {e.entry_id: copy.deepcopy(dict(e.data)) for e in hass.config_entries.async_entries(DOMAIN)}
        options_before = {e.entry_id: copy.deepcopy(dict(e.options)) for e in hass.config_entries.async_entries(DOMAIN)}

        for e in [*zones, system]:
            assert await hass.config_entries.async_unload(e.entry_id)
        await hass.async_block_till_done()
        for e in [system, *zones]:
            assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()

        assert {z.entry_id: _effective(z) for z in zones} == effective_before
        assert {e.entry_id: dict(e.data) for e in hass.config_entries.async_entries(DOMAIN)} == stored_before
        assert {e.entry_id: dict(e.options) for e in hass.config_entries.async_entries(DOMAIN)} == options_before
        assert len(_systems(hass)) == 1
        _assert_unrelated_untouched(hass, unrelated)


class TestKnownWeaknessMalformedSystemData:
    async def test_a_non_dict_system_policy_fails_a_deferring_zone_but_not_a_zone_with_its_own_policy(self, hass) -> None:
        """KNOWN WEAKNESS (PA-1): a non-dict ``system_override_policy`` makes the resolver
        raise while a DEFERRING zone is being set up, so that zone ends in SETUP_ERROR.  Zones
        with their own policy are unaffected.  A migration must neither crash nor substitute
        defaults silently; changing this behavior later has to be a deliberate decision."""
        system = _system(data={CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM, "system_override_policy": "garbage"})
        system.add_to_hass(hass)
        defers = _zone("defers", override_policy=None)
        own = _zone("own", override_policy={"release_strategy": "manual", "use_system_default": False})
        _cover(hass, "defers", "own")
        defers.add_to_hass(hass)
        own.add_to_hass(hass)
        assert await async_setup_component(hass, DOMAIN, {})  # sets up every registered entry
        await hass.async_block_till_done()
        assert defers.state is ConfigEntryState.SETUP_ERROR
        assert own.state is ConfigEntryState.LOADED
        assert _effective(own)["strategy"] == "manual"
        assert dict(system.data)["system_override_policy"] == "garbage"  # untouched

    async def test_a_non_numeric_absence_delay_in_the_system_entry_resolves_to_the_default(self, hass) -> None:
        system = _system(data={CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM, "system_absence_delay_min": "abc"})
        system.add_to_hass(hass)
        zone = _zone("a", absence_delay=20, absence_flag=True)
        await _setup(hass, zone)
        assert _effective(zone)["absence"] == 30
