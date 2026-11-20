"""PA-REL (real Home Assistant): what the other suites do not pin about an upgraded installation.

* Persistent zone data (Learning, Forecast, Safety hold stores) is neither removed nor rewritten by
  the migration, the System entry's retirement, restarts or the removal of another zone; the stored
  data of a disabled zone stays byte-identical.
* Entity identities (ids and unique ids) of the zones are the same after the upgrade, after restarts
  and after another zone is removed, and no entity or device of the legacy System entry exists.
* A zone that cannot be set up does not stop the healthy zones from running (fail closed per zone).

Run with the rest of the real-HA suite::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

import copy
import importlib

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from custom_components.smartshading.const import DOMAIN
from custom_components.smartshading.engines.learning_persistence import (
    LEARNING_STORAGE_KEY,
    LEARNING_STORE_VERSION,
)
from custom_components.smartshading.engines.safety_hold_persistence import (
    STORAGE_KEY as SAFETY_HOLD_KEY,
    STORAGE_VERSION as SAFETY_HOLD_VERSION,
)

# The helpers of the PA-5 migration matrix (same directory, same fixtures) are reused, not copied.
_matrix = importlib.import_module("test_real_ha_pa5_migration_matrix")
_add, _start, _restart, _systems = _matrix._add, _matrix._start, _matrix._restart, _matrix._systems
_legacy, _independent, _system, _zone = _matrix._legacy, _matrix._independent, _matrix._system, _matrix._zone
_effective, _issue_ids, _OWN_EFFECTIVE = _matrix._effective, _matrix._issue_ids, _matrix._OWN_EFFECTIVE


def _store_keys(entry_id: str) -> dict[str, tuple[int, str]]:
    from custom_components.smartshading.engines.forecast_persistence import STORAGE_KEY as FC_KEY
    from custom_components.smartshading.engines.forecast_persistence import STORAGE_VERSION as FC_VERSION
    return {
        "learning": (LEARNING_STORE_VERSION, f"{LEARNING_STORAGE_KEY}_{entry_id}"),
        "forecast": (FC_VERSION, f"{FC_KEY}_{entry_id}"),
        "safety_hold": (SAFETY_HOLD_VERSION, f"{SAFETY_HOLD_KEY}_{entry_id}"),
    }


async def _seed_stores(hass, entry_ids) -> dict[str, dict]:
    seeded = {}
    for entry_id in entry_ids:
        for name, (version, key) in _store_keys(entry_id).items():
            payload = {"acceptance_marker": f"{entry_id}:{name}"}
            await Store(hass, version, key).async_save(payload)
            seeded[key] = payload
    return seeded


def _identities(hass, entry_id: str) -> set[tuple[str, str]]:
    return {(e.entity_id, e.unique_id) for e in er.async_get(hass).entities.values()
            if e.config_entry_id == entry_id}


async def test_persistent_data_and_entity_identities_survive_upgrade_restarts_and_removal(
        hass, hass_storage) -> None:
    system_entry = _system()
    a, b = _legacy("a"), _legacy("b")
    off = _legacy("off", disabled=True)
    healthy = _independent("healthy")
    _add(hass, system_entry, a, b, off, healthy)
    seeded = await _seed_stores(hass, ["a", "b", "off", "healthy"])
    disabled_before = {key: copy.deepcopy(hass_storage[key]) for key in seeded if key.endswith("_off")}
    assert len(disabled_before) == 3

    assert await _start(hass)
    assert _systems(hass) == [] and _issue_ids(hass) == []
    for entry in (a, b, healthy):
        assert entry.state is ConfigEntryState.LOADED
    first = {e.entry_id: _identities(hass, e.entry_id) for e in (a, b, healthy)}
    assert all(first.values())

    for _ in range(2):
        await _restart(hass)
        assert {e.entry_id: _identities(hass, e.entry_id) for e in (a, b, healthy)} == first
        assert _systems(hass) == []

    # No legacy System entity or device remains anywhere.
    assert not [e for e in er.async_get(hass).entities.values() if e.config_entry_id == "system"]
    assert not [d for d in dr.async_get(hass).devices.values()
                if (DOMAIN, "smartshading_system") in d.identifiers]

    # Persistent data: nothing was removed (a loaded zone may legitimately rewrite its own file);
    # the disabled zone, never set up, is byte-identical.
    for key in seeded:
        assert key in hass_storage, key
    assert {key: hass_storage[key] for key in disabled_before} == disabled_before

    # Removing another zone removes only that zone's data and does not touch the others' identities.
    await hass.config_entries.async_remove("b")
    await hass.async_block_till_done()
    assert {e.entry_id: _identities(hass, e.entry_id) for e in (a, healthy)} == {
        "a": first["a"], "healthy": first["healthy"]}
    for key in seeded:
        assert (key in hass_storage) is not key.endswith("_b"), key       # only the removed zone's data is gone
    assert _systems(hass) == []


async def test_a_zone_that_cannot_be_set_up_does_not_stop_the_healthy_zones(hass) -> None:
    broken = _zone("broken", override_policy="not a mapping", absence_flag=False)
    healthy = _independent("healthy")
    other = _independent("other")
    _add(hass, _system(), broken, healthy, other)
    assert await _start(hass)
    assert broken.state is ConfigEntryState.SETUP_ERROR
    for entry in (healthy, other):
        assert entry.state is ConfigEntryState.LOADED and _effective(entry) == _OWN_EFFECTIVE
    assert [e.entry_id for e in _systems(hass)] == ["system"]        # kept: the broken zone is not verified
    await hass.config_entries.async_reload("other")
    await hass.async_block_till_done()
    assert healthy.state is other.state is ConfigEntryState.LOADED
    assert broken.state is ConfigEntryState.SETUP_ERROR
