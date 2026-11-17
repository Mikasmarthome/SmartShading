"""PA-2c.1 (stub): the System retirement eligibility check, the recovery of zones that depend on
the System again, and the structural guarantees around them.

Behavior against real ConfigEntries and the Options Flow is in
``tests/real_ha/test_real_ha_system_retirement_gate.py``.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.smartshading import system_retirement as sr
from custom_components.smartshading.system_retirement import (
    assess_system_retirement,
    zone_independence_reasons,
)
from custom_components.smartshading.zone_settings_migration import (
    ZoneMigrationDeferred,
    ZoneMigrationError,
    materialize_zone_settings,
    migrate_zone_entry,
)

_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"

_SYSTEM = {
    "entry_type": "system",
    "system_override_policy": {"release_strategy": "duration", "duration_min": 45,
                               "night_duration_min": 300, "detection_tolerance": 99},
    "system_absence_delay_min": 90,
}


def _independent(**over):
    zone = {
        "name": "Z", "zones": [{"id": "z"}], "unrelated": {"keep": [1]},
        "override_policy": {"release_strategy": "lifecycle", "use_system_default": False,
                            "detection_tolerance": 12, "duration_min": 120},
        "absence_delay_min": 20, "absence_delay_use_system_default": False,
    }
    zone.update(over)
    return zone


def _policy(**kw):
    return {**_independent()["override_policy"], **kw}


def _without(zone, key):
    return {k: v for k, v in zone.items() if k != key}


def _reasons(zone, minor=2, system=_SYSTEM):
    return zone_independence_reasons(zone, minor, system)


# --- what counts as independent ---------------------------------------------------------------

@pytest.mark.parametrize("system", [_SYSTEM, None, {}, {"system_override_policy": "junk"}])
def test_an_explicit_zone_is_independent_whatever_the_system_holds(system) -> None:
    assert _reasons(_independent(), system=system) == ()


@pytest.mark.parametrize("zone", [
    {"name": "Z"},
    {"name": "Z", "override_policy": {"release_strategy": "manual"}, "absence_delay_use_system_default": True},
    {"name": "Z", "override_policy": {"use_system_default": True}, "absence_delay_min": 5},
    {"name": "Z", "override_policy": {"x": 1}, "absence_delay_min": "9"},
])
@pytest.mark.parametrize("system", [_SYSTEM, None])
def test_the_materialization_always_produces_an_independent_zone(zone, system) -> None:
    assert _reasons(materialize_zone_settings(zone, system), system=system) == ()


@pytest.mark.parametrize("minor, code", [
    (1, sr.MINOR_VERSION_UNMIGRATED), (0, sr.MINOR_VERSION_UNMIGRATED),
    (3, sr.MINOR_VERSION_UNKNOWN), ("2", sr.MINOR_VERSION_INVALID), (None, sr.MINOR_VERSION_INVALID),
    (True, sr.MINOR_VERSION_INVALID), (2.0, sr.MINOR_VERSION_INVALID),
])
def test_the_version_must_be_exactly_the_migrated_one(minor, code) -> None:
    assert code in _reasons(_independent(), minor=minor)


def test_version_two_alone_never_makes_a_zone_independent() -> None:
    for zone, code in (
        (_independent(override_policy=_policy(use_system_default=True)), sr.OVERRIDE_INHERITANCE_ACTIVE),
        (_independent(absence_delay_use_system_default=True), sr.ABSENCE_INHERITANCE_ACTIVE),
    ):
        assert code in _reasons(zone, minor=2)


def test_the_content_alone_does_not_replace_the_version() -> None:
    assert _reasons(_independent(), minor=1) == (sr.MINOR_VERSION_UNMIGRATED,)


@pytest.mark.parametrize("zone, code", [
    (_independent(override_policy=None), sr.OVERRIDE_POLICY_MISSING),
    (_without(_independent(), "override_policy"), sr.OVERRIDE_POLICY_MISSING),
    (_independent(override_policy=[1]), sr.OVERRIDE_POLICY_MALFORMED),
    (_independent(override_policy="x"), sr.OVERRIDE_POLICY_MALFORMED),
    # the implicit "never configured" sentinel: no release_strategy defers even with the flag False
    (_independent(override_policy={k: v for k, v in _policy().items() if k != "release_strategy"}),
     sr.OVERRIDE_POLICY_IMPLICIT_INHERITANCE),
    (_independent(override_policy=_policy(release_strategy="bogus")), sr.OVERRIDE_POLICY_INVALID),
    (_independent(override_policy=_policy(release_strategy=None)), sr.OVERRIDE_POLICY_INVALID),
    (_independent(override_policy={k: v for k, v in _policy().items() if k != "use_system_default"}),
     sr.OVERRIDE_FLAG_MISSING),
    (_independent(override_policy=_policy(use_system_default="false")), sr.OVERRIDE_FLAG_INVALID),
    (_independent(override_policy=_policy(use_system_default=0)), sr.OVERRIDE_FLAG_INVALID),
    (_independent(override_policy=_policy(use_system_default=None)), sr.OVERRIDE_FLAG_INVALID),
    (_independent(override_policy=_policy(use_system_default=True)), sr.OVERRIDE_INHERITANCE_ACTIVE),
    (_independent(override_policy=_policy(detection_tolerance=None)), sr.DETECTION_TOLERANCE_INVALID),
    (_independent(override_policy=_policy(detection_tolerance=0)), sr.DETECTION_TOLERANCE_INVALID),
    (_independent(override_policy=_policy(detection_tolerance="12")), sr.DETECTION_TOLERANCE_INVALID),
    (_independent(override_policy=_policy(detection_tolerance=True)), sr.DETECTION_TOLERANCE_INVALID),
    (_independent(absence_delay_min=None), sr.ABSENCE_DELAY_INVALID),
    (_without(_independent(), "absence_delay_min"), sr.ABSENCE_DELAY_INVALID),
    (_independent(absence_delay_min=-1), sr.ABSENCE_DELAY_INVALID),
    (_independent(absence_delay_min="20"), sr.ABSENCE_DELAY_INVALID),
    (_independent(absence_delay_min=True), sr.ABSENCE_DELAY_INVALID),
    (_independent(absence_delay_min=2.5), sr.ABSENCE_DELAY_INVALID),
    (_without(_independent(), "absence_delay_use_system_default"), sr.ABSENCE_FLAG_MISSING),
    (_independent(absence_delay_use_system_default="false"), sr.ABSENCE_FLAG_INVALID),
    (_independent(absence_delay_use_system_default=1), sr.ABSENCE_FLAG_INVALID),
    (_independent(absence_delay_use_system_default=True), sr.ABSENCE_INHERITANCE_ACTIVE),
])
def test_every_unverifiable_or_inheriting_shape_closes_the_gate_with_its_own_reason(zone, code) -> None:
    assert code in _reasons(zone)


@pytest.mark.parametrize("zone", [None, [], "x", 5, ()])
def test_unreadable_zone_data_closes_the_gate_without_raising(zone) -> None:
    assert _reasons(zone) == (sr.DATA_UNREADABLE,)


def test_a_system_dependent_effective_value_closes_the_gate_even_if_every_flag_looks_clean(monkeypatch) -> None:
    """The effective check is judged by the runtime resolvers, independent of the flags."""
    real = sr.resolve_zone_override_policy
    monkeypatch.setattr(
        sr, "resolve_zone_override_policy",
        lambda zone, system: real({**zone, "override_policy": {}}, system))
    assert sr.OVERRIDE_EFFECTIVE_DEPENDS_ON_SYSTEM in _reasons(_independent())

    monkeypatch.undo()
    monkeypatch.setattr(sr, "resolve_zone_absence_delay_min",
                        lambda zone, system: (system or {}).get("system_absence_delay_min", 30))
    assert sr.ABSENCE_EFFECTIVE_DEPENDS_ON_SYSTEM in _reasons(_independent())


def test_a_dependency_visible_only_with_unusual_system_data_is_still_detected(monkeypatch) -> None:
    """The actual System data may coincide with the zone's own values; the probe cannot."""
    probe_delay = sr._PROBE_SYSTEM["system_absence_delay_min"]
    monkeypatch.setattr(
        sr, "resolve_zone_absence_delay_min",
        lambda zone, system: probe_delay if (system or {}).get("system_absence_delay_min") == probe_delay
        else zone["absence_delay_min"])
    assert sr.ABSENCE_EFFECTIVE_DEPENDS_ON_SYSTEM in _reasons(_independent())


def test_the_effective_tolerance_is_the_zones_own() -> None:
    zone = _independent(override_policy=_policy(detection_tolerance=12))
    own = sr._override_policy_from_storage(zone["override_policy"])
    assert own.detection_tolerance == 12
    assert sr.resolve_zone_override_policy(zone, sr._PROBE_SYSTEM).detection_tolerance == 12


def test_the_probe_differs_from_the_plausible_system_values_in_every_inherited_field() -> None:
    probe = sr._override_policy_from_storage(sr._PROBE_SYSTEM["system_override_policy"])
    default = sr._override_policy_from_storage(None)
    for field in ("release_strategy", "allow_comfort_actions", "allow_protection_actions",
                  "duration_min", "night_duration_min", "detection_tolerance",
                  "safety_timeout_enabled", "fixed_until"):
        assert getattr(probe, field) != getattr(default, field), field
    assert sr._PROBE_SYSTEM["system_absence_delay_min"] not in (30, 0)


def test_reasons_are_stable_non_sensitive_codes() -> None:
    zone = _independent(name="Secret Villa", override_policy=_policy(use_system_default=True))
    for code in _reasons(zone):
        assert code.replace("_", "").isalpha() and code.islower() and "Villa" not in code


def test_checking_twice_gives_the_same_answer_and_changes_nothing() -> None:
    zone = _independent(override_policy=_policy(use_system_default=True))
    before = copy.deepcopy(zone)
    assert _reasons(zone) == _reasons(zone) and zone == before


# --- the assessment over several entries ---------------------------------------------------------

def _entry(entry_id, data, minor=2, disabled=None):
    return SimpleNamespace(entry_id=entry_id, data=data, minor_version=minor, disabled_by=disabled)


def test_all_independent_zones_open_the_gate() -> None:
    result = assess_system_retirement([_entry("a", _independent()), _entry("b", _independent())], _SYSTEM)
    assert result.eligible and result.blocked == () and result.system_entry_present


def test_one_blocked_zone_closes_the_gate_and_is_reported_by_id_and_code_only() -> None:
    bad = _entry("b", _independent(absence_delay_use_system_default=True))
    result = assess_system_retirement([_entry("a", _independent()), bad], _SYSTEM)
    assert not result.eligible
    assert result.summary() == {
        "eligible": False, "zones_checked": 2, "system_entry_present": True,
        "blocked": {"b": [sr.ABSENCE_INHERITANCE_ACTIVE]}}


def test_disabled_zones_count_like_enabled_ones() -> None:
    off = _entry("off", _independent(override_policy=_policy(use_system_default=True)), disabled="user")
    result = assess_system_retirement([_entry("a", _independent()), off], _SYSTEM)
    assert not result.eligible and [z.entry_id for z in result.blocked] == ["off"]
    assert [z.enabled for z in result.zones] == [True, False]


def test_a_mixed_version_installation_stays_closed_until_every_zone_is_migrated() -> None:
    result = assess_system_retirement(
        [_entry("v1", _independent(), minor=1), _entry("v2", _independent())], _SYSTEM)
    assert not result.eligible and result.blocked[0].reasons == (sr.MINOR_VERSION_UNMIGRATED,)


def test_a_missing_or_malformed_system_entry_does_not_hide_a_dependent_zone() -> None:
    zone = _entry("a", _independent(override_policy=_policy(use_system_default=True)))
    for system in (None, {}, {"system_override_policy": 3}):
        assert not assess_system_retirement([zone], system).eligible
    assert assess_system_retirement([zone], None).system_entry_present is False


def test_independent_zones_do_not_need_a_system_entry() -> None:
    assert assess_system_retirement([_entry("a", _independent())], None).eligible


def test_an_unreadable_entry_blocks_and_the_others_are_still_judged() -> None:
    result = assess_system_retirement(
        [_entry("junk", None), _entry("ok", _independent())], _SYSTEM)
    assert result.blocked[0].reasons == (sr.DATA_UNREADABLE,)
    assert result.zones[1].independent


# --- recovery of a migrated zone that depends on the System again ---------------------------------

class _Config:
    def __init__(self):
        self.calls = []

    def async_update_entry(self, entry, **kw):
        self.calls.append(kw)
        if "data" in kw:
            entry.data = kw["data"]
        if "minor_version" in kw:
            entry.minor_version = kw["minor_version"]


def _hass():
    return SimpleNamespace(config_entries=_Config())


def _live(data, minor):
    return SimpleNamespace(entry_id="e", data=copy.deepcopy(data), minor_version=minor, version=1)


@pytest.mark.parametrize("reinherit", [
    {"override_policy": _policy(use_system_default=True)},
    {"absence_delay_use_system_default": True},
    {"override_policy": {k: v for k, v in _policy().items() if k != "use_system_default"}},
    {"override_policy": {k: v for k, v in _policy().items() if k != "release_strategy"}},
    {"absence_delay_use_system_default": None},
])
def test_a_version_two_zone_that_depends_on_the_system_again_is_recovered_with_the_same_effective_values(reinherit) -> None:
    from custom_components.smartshading.config_entry_data import (
        resolve_zone_absence_delay_min, resolve_zone_override_policy)
    zone = _independent(**reinherit)
    zone = {k: v for k, v in zone.items() if v is not None or k != "absence_delay_use_system_default"}
    entry, hass = _live(zone, 2), _hass()
    want = (resolve_zone_override_policy(zone, _SYSTEM), resolve_zone_absence_delay_min(zone, _SYSTEM))
    assert migrate_zone_entry(hass, entry, _SYSTEM) is True
    assert len(hass.config_entries.calls) == 1 and entry.minor_version == 2
    assert _reasons(entry.data) == ()
    assert (resolve_zone_override_policy(entry.data, _SYSTEM),
            resolve_zone_absence_delay_min(entry.data, _SYSTEM)) == want
    assert (resolve_zone_override_policy(entry.data, None),
            resolve_zone_absence_delay_min(entry.data, None)) == want
    assert entry.data["unrelated"] == {"keep": [1]}
    assert migrate_zone_entry(hass, entry, _SYSTEM) is False          # idempotent afterwards
    assert len(hass.config_entries.calls) == 1


def test_a_deferring_version_two_zone_without_a_system_entry_is_deferred_not_rewritten() -> None:
    entry, hass = _live(_independent(absence_delay_use_system_default=True), 2), _hass()
    before = copy.deepcopy(entry.data)
    with pytest.raises(ZoneMigrationDeferred):
        migrate_zone_entry(hass, entry, None)
    assert entry.data == before and hass.config_entries.calls == []


def test_an_unverifiable_version_two_zone_is_refused_and_left_exactly_as_it_was() -> None:
    entry, hass = _live(_independent(absence_delay_use_system_default="false"), 2), _hass()
    before = copy.deepcopy(entry.data)
    with pytest.raises(ZoneMigrationError):
        migrate_zone_entry(hass, entry, _SYSTEM)
    assert entry.data == before and hass.config_entries.calls == []


def test_a_zone_from_a_newer_minor_version_is_never_touched() -> None:
    entry, hass = _live(_independent(absence_delay_use_system_default=True), 3), _hass()
    assert migrate_zone_entry(hass, entry, _SYSTEM) is False and hass.config_entries.calls == []


def test_a_recovery_that_applied_but_does_not_make_the_zone_independent_is_rolled_back(monkeypatch) -> None:
    from custom_components.smartshading import zone_settings_migration as zm
    entry = _live(_independent(absence_delay_use_system_default=True), 2)
    hass = _hass()
    before = copy.deepcopy(entry.data)
    real = zm.materialize_zone_settings
    monkeypatch.setattr(zm, "materialize_zone_settings",
                        lambda z, s: {**real(z, s), "absence_delay_use_system_default": True})
    with pytest.raises(ZoneMigrationError):                 # refused before anything is written
        migrate_zone_entry(hass, entry, _SYSTEM)
    assert entry.data == before and hass.config_entries.calls == []
    # Even if the verification of the candidate missed it, the persisted state must be independent.
    monkeypatch.setattr(zm, "verify_materialization", lambda *a: None)
    with pytest.raises(ZoneMigrationError):
        migrate_zone_entry(hass, entry, _SYSTEM)
    assert entry.data == before and entry.minor_version == 2 and len(hass.config_entries.calls) == 2


# --- structure ----------------------------------------------------------------------------------

_FORBIDDEN = {"async_update_entry", "async_remove", "async_unload", "async_reload", "async_create_issue",
              "async_create_task", "async_init", "async_setup", "async_forward_entry_setups"}


def _calls(path: str) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse((_ROOT / path).read_text(encoding="utf-8"))):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


def test_the_eligibility_module_is_side_effect_free_and_swallows_nothing() -> None:
    assert not _calls("system_retirement.py") & _FORBIDDEN
    tree = ast.parse((_ROOT / "system_retirement.py").read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Try, ast.ExceptHandler))]
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert {"resolve_zone_override_policy", "resolve_zone_absence_delay_min"} <= imported


def test_the_retirement_helpers_never_remove_or_unload_the_system_entry() -> None:
    src = (_ROOT / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for name in ("_assess_system_retirement", "_log_retirement_gate"):
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        assert not {c.func.attr for c in ast.walk(fn)
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)} & _FORBIDDEN
    assert "_assess_system_retirement(hass)" in src and "_zone_entries(hass)" in src


def test_both_gate_steps_and_the_structure_save_are_guarded_in_the_options_flow() -> None:
    src = (_ROOT / "config_flow.py").read_text(encoding="utf-8")
    assert src.count("_zone_settings_are_local()") >= 3
    assert "self._refuse_reinheritance(updates)" in src
    assert 'new_data["override_policy"] = copy.deepcopy(self._config_entry.data["override_policy"])' in src
    assert "    MINOR_VERSION = 2\n" in src and "    VERSION = 1\n" in src


def test_a_flow_created_zone_is_stored_with_an_explicit_independence_marker() -> None:
    from custom_components.smartshading.config_entry_data import (
        SmartShadingConfigEntryData, to_storage_dict)
    stored = to_storage_dict(SmartShadingConfigEntryData(name="Z", use_home_location=False))
    assert _reasons(stored) == ()
