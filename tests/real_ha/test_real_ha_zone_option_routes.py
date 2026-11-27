"""Real Home Assistant: the zone-local Manual Override and Presence option routes.

A zone that owns its settings (freshly created, converted from an older version, or enabled
again after having been disabled) gets the detection tolerance and the release choice on the
first Manual Override page, reaches all five choices and their detail pages, writes nothing
before the last page and runs what was chosen, also after a reload.  A zone that has not been
converted keeps the gate page.  The form texts are covered by tests/test_zone_option_texts.py.

Run with the rest of the real-HA suite (Linux, macOS or WSL)::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.smartshading as ss
from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    to_storage_dict,
)
from custom_components.smartshading.const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_ZONE
from custom_components.smartshading.models.cover_group import CoverGroup
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig

_FIRST_PAGE = {
    "override_detection_tolerance", "override_release_choice", "override_allow_comfort_actions",
    "override_allow_protection_actions", "override_edit_timeouts",
}
_CHOICE_ORDER = ["duration", "fixed_time", "lifecycle", "next_decision", "manual"]

# release choice -> (detail page behind the first page, its input, stored and running strategy)
_CHOICES: dict[str, tuple[str | None, dict[str, Any], str]] = {
    "duration": ("override_duration",
                 {"override_duration_min": 33, "override_night_duration_min": 44}, "duration"),
    "fixed_time": ("override_fixed_time", {"override_fixed_until": "06:30"}, "fixed_time"),
    "lifecycle": (None, {}, "lifecycle"),
    "next_decision": ("override_decision",
                      {"override_decision_filter": "comfort"}, "first_comfort"),
    "manual": (None, {}, "manual"),
}

_OWN_POLICY = {
    "release_strategy": "first_protection", "fixed_until": "09:15:00", "duration_min": 71,
    "night_duration_min": 333, "allow_comfort_actions": True, "allow_protection_actions": False,
    "safety_timeout_enabled": False, "detection_tolerance": 9, "use_system_default": False,
}

_LANGUAGES = sorted(p.stem for p in (Path(ss.__file__).parent / "translations").glob("*.json"))


def _entry(minor: int, policy: dict | None = None, **extra: Any) -> MockConfigEntry:
    zone = ZoneConfig(id="zone_a", name="Zone A")
    window = WindowConfig(id="window_a", name="South", zone_id=zone.id, azimuth=180.0,
                          floor_level=0, cover_group_id="cg_a")
    group = CoverGroup(id="cg_a", window_id=window.id, cover_ids=["cover.test_a"])
    data = {**to_storage_dict(SmartShadingConfigEntryData(
        name="Zone A", use_home_location=False, zones=[zone], windows=[window],
        cover_groups=[group])), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE}
    if policy is not None:
        data["override_policy"] = copy.deepcopy(policy)
    return MockConfigEntry(domain=DOMAIN, data=data, entry_id="a", minor_version=minor, **extra)


async def _setup(hass, entry: MockConfigEntry) -> MockConfigEntry:
    hass.states.async_set("cover.test_a", "closed", {"current_position": 0})
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return entry


async def _start_zone(hass, minor: int, policy: dict | None = None) -> MockConfigEntry:
    """minor=2 is what the config flow creates today; minor=1 is converted at start."""
    entry = await _setup(hass, _entry(minor, policy))
    assert entry.minor_version == 2
    return entry


async def _open(hass, *path: str) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init("a")
    for step in path:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": step})
    return result


async def _override_page(hass) -> dict[str, Any]:
    result = await _open(hass, "advanced", "manual_override")
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual_override"
    return result


def _fields(result) -> set[str]:
    return {str(key) for key in result["data_schema"].schema}


def _key(result, name: str):
    return next(key for key in result["data_schema"].schema if str(key) == name)


def _default(result, name: str):
    return _key(result, name).default()


async def _send(hass, result, user_input: dict[str, Any]) -> dict[str, Any]:
    return await hass.config_entries.options.async_configure(result["flow_id"], user_input)


def _first_page_input(choice: str, tolerance: int = 7, comfort: bool = False,
                      protection: bool = False, timeouts: bool = False) -> dict[str, Any]:
    return {
        "override_detection_tolerance": tolerance, "override_release_choice": choice,
        "override_allow_comfort_actions": comfort, "override_allow_protection_actions": protection,
        "override_edit_timeouts": timeouts,
    }


async def _finish(hass, result) -> None:
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()


def _running(entry) -> dict[str, Any]:
    c = entry.runtime_data.coordinator
    return {
        "strategy": c._override_release_strategy.value, "tolerance": c._override_detection_tolerance,
        "duration": c._override_duration_min, "night": c._override_night_duration_min,
        "safety_timeout": c._override_safety_timeout_enabled,
    }


@pytest.fixture
def writes(hass, monkeypatch):
    """Writes of entry DATA; Home Assistant itself also stores the unchanged options afterwards."""
    log: list[str] = []
    original = hass.config_entries.async_update_entry

    def spy(entry, **kwargs):
        if "data" in kwargs:
            log.append(entry.entry_id)
        return original(entry, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", spy)
    return log


# --- routing ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("minor", [2, 1], ids=["fresh", "converted"])
async def test_the_first_page_holds_tolerance_and_release_choice(hass, minor) -> None:
    await _start_zone(hass, minor)
    result = await _override_page(hass)
    assert _fields(result) == _FIRST_PAGE
    assert [str(k) for k in result["data_schema"].schema][0] == "override_detection_tolerance"
    selector = result["data_schema"].schema[_key(result, "override_release_choice")]
    assert selector.config["options"] == _CHOICE_ORDER
    assert selector.config["translation_key"] == "override_release_choice"
    assert _default(result, "override_detection_tolerance") == 10
    assert _default(result, "override_release_choice") == "lifecycle"


async def test_a_zone_enabled_again_gets_the_same_first_page(hass) -> None:
    from homeassistant.config_entries import ConfigEntryDisabler

    entry = await _setup(hass, _entry(1, disabled_by=ConfigEntryDisabler.USER))
    assert await hass.config_entries.async_set_disabled_by("a", None)
    await hass.async_block_till_done()
    if entry.minor_version != 2:                      # converted by the next start
        ss._prepare_zone_entries(hass)
        assert await hass.config_entries.async_reload("a")
        await hass.async_block_till_done()
    assert entry.minor_version == 2
    assert _fields(await _override_page(hass)) == _FIRST_PAGE


async def test_a_zone_that_is_not_converted_keeps_the_gate_page(hass, monkeypatch) -> None:
    monkeypatch.setattr(ss, "_migrate_zone_entry", lambda hass_, entry: False)
    entry = await _setup(hass, _entry(1, _OWN_POLICY))
    assert entry.minor_version == 1
    result = await _override_page(hass)
    assert _fields(result) == {"override_use_system_default", "override_detection_tolerance"}
    result = await _send(hass, result, {"override_use_system_default": False,
                                        "override_detection_tolerance": 8})
    assert result["step_id"] == "manual_override_custom"
    assert _fields(result) == _FIRST_PAGE - {"override_detection_tolerance"}
    await _finish(hass, await _send(hass, result, {
        "override_release_choice": "manual", "override_allow_comfort_actions": False,
        "override_allow_protection_actions": False, "override_edit_timeouts": False}))
    assert entry.data["override_policy"]["release_strategy"] == "manual"
    assert entry.data["override_policy"]["detection_tolerance"] == 8
    assert entry.minor_version == 1                   # saving options is not a conversion


async def test_a_zone_that_is_not_converted_can_still_defer(hass, monkeypatch) -> None:
    monkeypatch.setattr(ss, "_migrate_zone_entry", lambda hass_, entry: False)
    entry = await _setup(hass, _entry(1, _OWN_POLICY))
    result = await _override_page(hass)
    await _finish(hass, await _send(hass, result, {"override_use_system_default": True,
                                                   "override_detection_tolerance": 6}))
    assert entry.data["override_policy"] == {**_OWN_POLICY, "use_system_default": True,
                                             "detection_tolerance": 6}


@pytest.mark.parametrize("minor", [2, 1], ids=["fresh", "converted"])
async def test_the_presence_gate_of_a_zone_offers_its_own_settings_only(hass, minor) -> None:
    await _start_zone(hass, minor)
    presence = await _open(hass, "presence")
    assert "absence_delay_use_system_default" not in _fields(presence)
    presence = await _send(hass, presence, {"presence_entity_ids": [], "presence_policy": "any_home"})
    assert presence["step_id"] == "presence_custom" and _fields(presence) == {"absence_delay_min"}


# --- the five choices -------------------------------------------------------------------------------

@pytest.mark.parametrize("minor", [2, 1], ids=["fresh", "converted"])
@pytest.mark.parametrize("choice", list(_CHOICES))
async def test_every_release_choice_is_reachable_persisted_and_run(hass, writes, minor, choice) -> None:
    entry = await _start_zone(hass, minor)
    detail_step, detail_input, strategy = _CHOICES[choice]
    writes.clear()

    result = await _send(hass, await _override_page(hass),
                         _first_page_input(choice, comfort=True, protection=True))
    if detail_step is not None:
        assert result["step_id"] == detail_step and writes == []     # nothing saved yet
        result = await _send(hass, result, detail_input)
    await _finish(hass, result)

    assert writes == ["a"]                                           # exactly one save
    policy = entry.data["override_policy"]
    assert policy["release_strategy"] == strategy
    assert policy["use_system_default"] is False and policy["detection_tolerance"] == 7
    assert policy["allow_comfort_actions"] is True and policy["allow_protection_actions"] is True
    assert _running(entry)["strategy"] == strategy and _running(entry)["tolerance"] == 7
    stored = copy.deepcopy(dict(entry.data))
    assert await hass.config_entries.async_reload("a")
    await hass.async_block_till_done()
    ss._prepare_zone_entries(hass)                                   # what a restart runs
    assert dict(entry.data) == stored
    assert _running(entry)["strategy"] == strategy and _running(entry)["tolerance"] == 7


async def test_duration_stores_day_and_night(hass) -> None:
    entry = await _start_zone(hass, 2)
    result = await _send(hass, await _override_page(hass), _first_page_input("duration"))
    assert _fields(result) == {"override_duration_min", "override_night_duration_min"}
    await _finish(hass, await _send(hass, result, _CHOICES["duration"][1]))
    assert (_running(entry)["duration"], _running(entry)["night"]) == (33, 44)


@pytest.mark.parametrize(("filter_", "strategy"), [
    ("any", "first_any_decision"), ("comfort", "first_comfort"), ("protection", "first_protection")])
async def test_the_decision_filter_maps_to_the_stored_strategy(hass, filter_, strategy) -> None:
    entry = await _start_zone(hass, 2)
    result = await _send(hass, await _override_page(hass), _first_page_input("next_decision"))
    await _finish(hass, await _send(hass, result, {"override_decision_filter": filter_}))
    assert entry.data["override_policy"]["release_strategy"] == strategy
    result = await _override_page(hass)
    assert _default(result, "override_release_choice") == "next_decision"
    result = await _send(hass, result, _first_page_input("next_decision"))
    assert _default(result, "override_decision_filter") == filter_


@pytest.mark.parametrize("choice", ["lifecycle", "next_decision", "manual"])
async def test_the_optional_timeout_page(hass, writes, choice) -> None:
    entry = await _start_zone(hass, 2)
    writes.clear()
    result = await _send(hass, await _override_page(hass), _first_page_input(choice, timeouts=True))
    if choice == "next_decision":
        result = await _send(hass, result, {"override_decision_filter": "any"})
    assert result["step_id"] == "override_timeout" and writes == []
    await _finish(hass, await _send(hass, result, {
        "override_safety_timeout_enabled": False, "override_duration_min": 15,
        "override_night_duration_min": 25}))
    assert writes == ["a"]
    assert (_running(entry)["safety_timeout"], _running(entry)["duration"],
            _running(entry)["night"]) == (False, 15, 25)


@pytest.mark.parametrize("choice", ["duration", "fixed_time"])
async def test_choices_with_their_own_deadline_have_no_timeout_page(hass, choice) -> None:
    await _start_zone(hass, 2)
    result = await _send(hass, await _override_page(hass), _first_page_input(choice, timeouts=True))
    detail_step, detail_input, _ = _CHOICES[choice]
    assert result["step_id"] == detail_step
    await _finish(hass, await _send(hass, result, detail_input))


# --- prefill, unrelated values, invalid input, cancel -------------------------------------------------

async def test_the_page_is_prefilled_with_the_stored_policy(hass) -> None:
    await _start_zone(hass, 2, _OWN_POLICY)
    result = await _override_page(hass)
    assert {name: _default(result, name) for name in _FIRST_PAGE} == {
        "override_detection_tolerance": 9, "override_release_choice": "next_decision",
        "override_allow_comfort_actions": True, "override_allow_protection_actions": False,
        "override_edit_timeouts": False}


async def test_changing_the_strategy_keeps_the_values_its_pages_do_not_edit(hass) -> None:
    entry = await _start_zone(hass, 2, _OWN_POLICY)
    await _finish(hass, await _send(hass, await _override_page(hass),
                                    _first_page_input("manual", tolerance=9, comfort=True)))
    assert entry.data["override_policy"] == {**_OWN_POLICY, "release_strategy": "manual"}


@pytest.mark.parametrize("bad", [
    {"override_detection_tolerance": 0}, {"override_detection_tolerance": 100000},
    {"override_release_choice": "sometime"},
], ids=["tolerance-low", "tolerance-high", "choice"])
async def test_invalid_input_is_rejected_and_the_same_page_accepts_the_correction(hass, writes, bad) -> None:
    entry = await _start_zone(hass, 2, _OWN_POLICY)
    before = copy.deepcopy(dict(entry.data))
    writes.clear()
    result = await _override_page(hass)
    with pytest.raises(InvalidData):
        await _send(hass, result, {**_first_page_input("manual"), **bad})
    assert writes == [] and dict(entry.data) == before
    assert any(f["flow_id"] == result["flow_id"] and f["step_id"] == "manual_override"
               for f in hass.config_entries.options.async_progress())
    await _finish(hass, await _send(hass, result, _first_page_input("manual")))
    assert entry.data["override_policy"]["release_strategy"] == "manual"


async def test_an_empty_end_time_keeps_the_page_and_the_first_page_choices(hass, writes) -> None:
    entry = await _start_zone(hass, 2)
    writes.clear()
    result = await _send(hass, await _override_page(hass),
                         _first_page_input("fixed_time", tolerance=4, comfort=True))
    result = await _send(hass, result, {})
    assert result["step_id"] == "override_fixed_time"
    assert result["errors"] == {"base": "override_fixed_until_required"} and writes == []
    await _finish(hass, await _send(hass, result, {"override_fixed_until": "06:30"}))
    policy = entry.data["override_policy"]
    assert (policy["detection_tolerance"], policy["allow_comfort_actions"]) == (4, True)


@pytest.mark.parametrize("choice", ["lifecycle", "duration", "next_decision"])
async def test_leaving_before_the_last_page_changes_nothing(hass, writes, choice) -> None:
    entry = await _start_zone(hass, 2, _OWN_POLICY)
    before, running = copy.deepcopy(dict(entry.data)), _running(entry)
    writes.clear()
    result = await _override_page(hass)
    # lifecycle saves from the first page unless the optional timeout page is asked for
    result = await _send(hass, result, _first_page_input(
        choice, tolerance=3, timeouts=choice == "lifecycle"))
    assert result["type"] is FlowResultType.FORM and result["step_id"] != "manual_override"
    hass.config_entries.options.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert writes == [] and dict(entry.data) == before and _running(entry) == running
    result = await _override_page(hass)                      # a new run starts from what is stored
    assert _default(result, "override_detection_tolerance") == 9
    assert _default(result, "override_release_choice") == "next_decision"


# --- translations as Home Assistant resolves them ------------------------------------------------------

@pytest.mark.parametrize("language", _LANGUAGES)
async def test_home_assistant_resolves_every_text_of_the_first_page(hass, language) -> None:
    await _start_zone(hass, 2)
    result = await _override_page(hass)
    translations = await async_get_translations(hass, language, "options", [DOMAIN])
    source = json.loads((Path(ss.__file__).parent / "translations" / f"{language}.json")
                        .read_text(encoding="utf-8"))["options"]["step"]["manual_override"]
    prefix = f"component.{DOMAIN}.options.step.manual_override"
    assert translations[f"{prefix}.description"] == source["description"]
    for field in _fields(result):
        for block in ("data", "data_description"):
            assert translations[f"{prefix}.{block}.{field}"] == source[block][field], (field, block)
    selectors = await async_get_translations(hass, language, "selector", [DOMAIN])
    for choice in _CHOICE_ORDER:
        assert selectors[f"component.{DOMAIN}.selector.override_release_choice.options.{choice}"]
