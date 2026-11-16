"""PA-1 characterization: how a zone's EFFECTIVE Manual Override policy and
absence delay are derived today from the zone's own stored data and the legacy
System entry's stored data.

These tests pin the CURRENT behavior of ``resolve_zone_override_policy`` and
``resolve_zone_absence_delay_min`` (``config_entry_data.py``).  They are a
regression baseline for the zone-only migration; they are not assertions about
the future architecture.  Where the current behavior is a known weakness it is
pinned as it is and labelled KNOWN WEAKNESS - changing it later must be a
deliberate decision, not an accident.

Expected values come from hand-written tables (what each stored shape means),
never from calling the code under test to produce them.

Stored configuration vs effective behavior:
  * stored = the dicts held in ConfigEntry.data (zone) and in the System entry
  * effective = what the resolvers return for that pair
  The resolvers never mutate their inputs, and the typed loader
  (``from_storage_dict``) does NOT apply any System inheritance.
"""
from __future__ import annotations

import copy
from datetime import time

import pytest

from custom_components.smartshading.config_entry_data import (
    from_storage_dict,
    resolve_zone_absence_delay_min,
    resolve_zone_override_policy,
    to_storage_dict,
)
from custom_components.smartshading.models.override_policy import OverridePolicyConfig

# (strategy, fixed_until, allow_comfort, allow_protection, duration, night, tolerance, safety_timeout)
DEFAULT = ("lifecycle", None, False, False, 120, 720, 10, True)


def _tup(policy: OverridePolicyConfig) -> tuple:
    return (
        policy.release_strategy.value, policy.fixed_until, policy.allow_comfort_actions,
        policy.allow_protection_actions, policy.duration_min, policy.night_duration_min,
        policy.detection_tolerance, policy.safety_timeout_enabled,
    )


# ---------------------------------------------------------------------------
# Hand-written fixtures: what each stored shape MEANS
# ---------------------------------------------------------------------------

_CUSTOM_SYSTEM_POLICY = {
    "release_strategy": "duration", "duration_min": 45, "night_duration_min": 300,
    "detection_tolerance": 99, "allow_comfort_actions": True,
    "allow_protection_actions": True, "safety_timeout_enabled": False,
}

# system label -> (system entry data, effective policy the System side yields)
SYSTEMS: dict[str, tuple[dict | None, tuple]] = {
    "missing": (None, DEFAULT),
    "present_default": ({"entry_type": "system"}, DEFAULT),
    "present_custom": (
        {"entry_type": "system", "system_override_policy": _CUSTOM_SYSTEM_POLICY},
        ("duration", None, True, True, 45, 300, 99, False)),
    "present_fixed_time": (
        {"entry_type": "system", "system_override_policy": {
            "release_strategy": "fixed_time", "fixed_until": "06:30:00"}},
        ("fixed_time", time(6, 30), False, False, 120, 720, 10, True)),
    "fixed_time_without_clock": (
        {"entry_type": "system", "system_override_policy": {"release_strategy": "fixed_time"}},
        ("duration", None, False, False, 120, 720, 10, True)),
    "malformed_values": (
        {"entry_type": "system", "system_override_policy": {
            "release_strategy": "bogus", "duration_min": "abc", "night_duration_min": None,
            "detection_tolerance": [], "fixed_until": "nonsense"}},
        DEFAULT),
    "empty_policy": ({"entry_type": "system", "system_override_policy": {}}, DEFAULT),
    "none_policy": ({"entry_type": "system", "system_override_policy": None}, DEFAULT),
}

# zone label -> (zone data, defers_to_system, the zone's OWN effective policy)
ZONES: dict[str, tuple[dict, bool, tuple]] = {
    "never_configured": ({"name": "Z"}, True, DEFAULT),
    "empty_policy": ({"name": "Z", "override_policy": {}}, True, DEFAULT),
    "none_policy": ({"name": "Z", "override_policy": None}, True, DEFAULT),
    "only_tolerance_stored": (
        {"name": "Z", "override_policy": {"detection_tolerance": 33}}, True,
        ("lifecycle", None, False, False, 120, 720, 33, True)),
    "use_system_true": (
        {"name": "Z", "override_policy": {
            "release_strategy": "fixed_time", "fixed_until": "07:00:00",
            "use_system_default": True, "detection_tolerance": 33}}, True,
        ("fixed_time", time(7, 0), False, False, 120, 720, 33, True)),
    "use_system_false": (
        {"name": "Z", "override_policy": {
            "release_strategy": "duration", "duration_min": 15, "use_system_default": False}}, False,
        ("duration", None, False, False, 15, 720, 10, True)),
    "explicit_strategy_without_flag": (
        {"name": "Z", "override_policy": {"release_strategy": "manual"}}, False,
        ("manual", None, False, False, 120, 720, 10, True)),
    "flow_written_full_policy": (
        {"name": "Z", "override_policy": {
            "release_strategy": "first_comfort", "fixed_until": None, "allow_comfort_actions": True,
            "allow_protection_actions": False, "duration_min": 60, "night_duration_min": 600,
            "detection_tolerance": 12, "safety_timeout_enabled": False}}, False,
        ("first_comfort", None, True, False, 60, 600, 12, False)),
    "own_malformed_fields": (
        {"name": "Z", "override_policy": {
            "release_strategy": "bogus", "duration_min": "x", "night_duration_min": -5,
            "use_system_default": False}}, False,
        ("lifecycle", None, False, False, 120, -5, 10, True)),
    "flag_string_false_is_truthy": (
        {"name": "Z", "override_policy": {"release_strategy": "manual", "use_system_default": "false"}},
        True, ("manual", None, False, False, 120, 720, 10, True)),
    "flag_none_is_false": (
        {"name": "Z", "override_policy": {"release_strategy": "manual", "use_system_default": None}},
        False, ("manual", None, False, False, 120, 720, 10, True)),
    "flag_zero_is_false": (
        {"name": "Z", "override_policy": {"release_strategy": "manual", "use_system_default": 0}},
        False, ("manual", None, False, False, 120, 720, 10, True)),
    "pre_t10_legacy_keys_only": (
        {"name": "Z", "override_policy": {
            "break_on_lifecycle": False, "duration_mode": "fixed_time", "duration_min": 77}}, True,
        # no release_strategy key => defers; the zone's OWN reading would be DURATION (fixed_time without clock)
        ("duration", None, False, False, 77, 720, 10, True)),
}


def _expected(zone_label: str, system_label: str) -> tuple:
    _, defers, own = ZONES[zone_label]
    if not defers:
        return own
    system_side = SYSTEMS[system_label][1]
    # detection_tolerance is ALWAYS the zone's own value
    return (*system_side[:6], own[6], system_side[7])


class TestOverridePolicyEffectiveMatrix:
    @pytest.mark.parametrize("system_label", sorted(SYSTEMS))
    @pytest.mark.parametrize("zone_label", sorted(ZONES))
    def test_effective_policy(self, zone_label: str, system_label: str) -> None:
        zone_raw = copy.deepcopy(ZONES[zone_label][0])
        system_raw = copy.deepcopy(SYSTEMS[system_label][0])
        assert _tup(resolve_zone_override_policy(zone_raw, system_raw)) == _expected(zone_label, system_label)

    def test_matrix_covers_both_inheritance_outcomes_with_distinct_values(self) -> None:
        """Guard against a vacuous matrix: deferring and own zones must differ from
        the System side for the custom System policy."""
        sys_custom = SYSTEMS["present_custom"][1]
        deferring = _expected("never_configured", "present_custom")
        own = _expected("use_system_false", "present_custom")
        assert deferring[:6] == sys_custom[:6] and deferring != own
        assert own[4] == 15 and own[4] != sys_custom[4]

    # --- explicit spot checks (independent of the tables) ---------------

    def test_never_configured_zone_gets_the_system_values_but_its_own_tolerance(self) -> None:
        resolved = resolve_zone_override_policy(
            {"name": "Z", "override_policy": {"detection_tolerance": 25}},
            {"system_override_policy": {"release_strategy": "manual", "duration_min": 200,
                                        "detection_tolerance": 99}})
        assert resolved.release_strategy.value == "manual"
        assert resolved.duration_min == 200
        assert resolved.detection_tolerance == 25  # never the System's 99

    def test_never_configured_zone_without_own_tolerance_gets_the_hard_default_not_the_systems(self) -> None:
        resolved = resolve_zone_override_policy(
            {"name": "Z"}, {"system_override_policy": {"detection_tolerance": 99}})
        assert resolved.detection_tolerance == 10

    def test_flow_written_zone_never_defers_even_if_system_is_customised(self) -> None:
        zone = ZONES["flow_written_full_policy"][0]
        resolved = resolve_zone_override_policy(zone, SYSTEMS["present_custom"][0])
        assert resolved.duration_min == 60 and resolved.release_strategy.value == "first_comfort"

    def test_a_default_dataclass_zone_stored_by_to_storage_dict_does_not_defer(self) -> None:
        """A zone written by the config flow (to_storage_dict) always stores
        release_strategy and no use_system_default flag, so it uses its own policy."""
        from custom_components.smartshading.config_entry_data import SmartShadingConfigEntryData

        stored = to_storage_dict(SmartShadingConfigEntryData(name="Z", use_home_location=False))
        assert "release_strategy" in stored["override_policy"]
        assert "use_system_default" not in stored["override_policy"]
        resolved = resolve_zone_override_policy(stored, SYSTEMS["present_custom"][0])
        assert _tup(resolved) == DEFAULT  # own default, NOT the System's custom policy

    def test_pre_t10_zone_defers_and_ignores_its_own_legacy_keys(self) -> None:
        """A stored policy with only legacy keys has no release_strategy, so it defers;
        its own legacy duration (77) is NOT used while the System side applies."""
        resolved = resolve_zone_override_policy(
            ZONES["pre_t10_legacy_keys_only"][0], SYSTEMS["present_custom"][0])
        assert resolved.duration_min == 45

    # --- KNOWN WEAKNESS: non-dict stored values raise ---------------------

    @pytest.mark.parametrize("bad", ["garbage", [1], 5, True])
    def test_known_weakness_non_dict_zone_policy_raises_attribute_error(self, bad) -> None:
        with pytest.raises(AttributeError):
            resolve_zone_override_policy({"name": "Z", "override_policy": bad}, None)

    @pytest.mark.parametrize("falsy", [0, "", [], False])
    def test_falsy_non_dict_zone_policy_is_treated_like_never_configured(self, falsy) -> None:
        resolved = resolve_zone_override_policy(
            {"name": "Z", "override_policy": falsy}, SYSTEMS["present_custom"][0])
        assert resolved.duration_min == 45

    @pytest.mark.parametrize("bad", ["garbage", [1], 5])
    def test_known_weakness_non_dict_system_policy_raises_for_a_deferring_zone(self, bad) -> None:
        with pytest.raises(AttributeError):
            resolve_zone_override_policy({"name": "Z"}, {"system_override_policy": bad})

    @pytest.mark.parametrize("bad", ["garbage", [1], 5])
    def test_non_dict_system_policy_is_never_read_for_a_zone_with_its_own_policy(self, bad) -> None:
        resolved = resolve_zone_override_policy(
            ZONES["use_system_false"][0], {"system_override_policy": bad})
        assert resolved.duration_min == 15


# ---------------------------------------------------------------------------
# Absence delay
# ---------------------------------------------------------------------------

class TestAbsenceDelayEffectiveValue:
    @pytest.mark.parametrize(
        ("zone_raw", "system_raw", "expected"),
        [
            # flag True -> System value, zone's own value ignored
            ({"absence_delay_min": 20, "absence_delay_use_system_default": True},
             {"system_absence_delay_min": 90}, 90),
            # flag False -> own value
            ({"absence_delay_min": 20, "absence_delay_use_system_default": False},
             {"system_absence_delay_min": 90}, 20),
            # flag missing -> own value (no behavior change for existing installs)
            ({"absence_delay_min": 20}, {"system_absence_delay_min": 90}, 20),
            # own value missing -> hard default 30
            ({}, {"system_absence_delay_min": 90}, 30),
            ({"absence_delay_use_system_default": False}, None, 30),
            # explicit valid values incl. zero
            ({"absence_delay_min": 0}, None, 0),
            ({"absence_delay_min": 15}, None, 15),
            # invalid own values -> 30
            ({"absence_delay_min": "abc"}, None, 30),
            ({"absence_delay_min": None}, None, 30),
            ({"absence_delay_min": True}, None, 30),
            ({"absence_delay_min": []}, None, 30),
            ({"absence_delay_min": {}}, None, 30),
            # coercions
            ({"absence_delay_min": 12.7}, None, 12),
            ({"absence_delay_min": "45"}, None, 45),
            # no clamping at the resolver: negative stays negative
            ({"absence_delay_min": -5}, None, -5),
            ({"absence_delay_min": "-5"}, None, -5),
        ],
    )
    def test_own_value_cases(self, zone_raw, system_raw, expected) -> None:
        assert resolve_zone_absence_delay_min(copy.deepcopy(zone_raw), copy.deepcopy(system_raw)) == expected

    @pytest.mark.parametrize(
        ("system_raw", "expected"),
        [
            (None, 30),                                          # System entry missing
            ({"entry_type": "system"}, 30),                      # present, default (no key)
            ({"system_absence_delay_min": 90}, 90),              # present, non-default
            ({"system_absence_delay_min": 0}, 0),
            ({"system_absence_delay_min": "90"}, 90),
            ({"system_absence_delay_min": 7.9}, 7),
            ({"system_absence_delay_min": "abc"}, 30),           # malformed value
            ({"system_absence_delay_min": None}, 30),
            ({"system_absence_delay_min": True}, 30),
            ({"system_absence_delay_min": [90]}, 30),
            ({"system_absence_delay_min": -10}, -10),            # not clamped
            ({}, 30),
        ],
    )
    def test_deferring_zone_reads_the_system_value(self, system_raw, expected) -> None:
        zone = {"absence_delay_min": 20, "absence_delay_use_system_default": True}
        assert resolve_zone_absence_delay_min(zone, copy.deepcopy(system_raw)) == expected

    @pytest.mark.parametrize(
        ("flag", "defers"),
        [(True, True), (1, True), ("true", True), ("false", True), ("0", True),   # truthy strings
         (False, False), (0, False), (None, False), ("", False)],
    )
    def test_flag_truthiness_is_python_truthiness(self, flag, defers) -> None:
        zone = {"absence_delay_min": 20, "absence_delay_use_system_default": flag}
        result = resolve_zone_absence_delay_min(zone, {"system_absence_delay_min": 90})
        assert result == (90 if defers else 20)

    def test_known_weakness_non_dict_system_data_raises_for_a_deferring_zone(self) -> None:
        zone = {"absence_delay_min": 20, "absence_delay_use_system_default": True}
        with pytest.raises(AttributeError):
            resolve_zone_absence_delay_min(zone, "garbage")  # type: ignore[arg-type]

    def test_system_data_is_not_read_for_an_own_value_zone(self) -> None:
        # a malformed System payload must not matter to a zone that does not defer
        assert resolve_zone_absence_delay_min({"absence_delay_min": 20}, "garbage") == 20  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Stored configuration is not effective behavior
# ---------------------------------------------------------------------------

class TestStoredVersusEffective:
    def test_resolvers_never_mutate_their_inputs(self) -> None:
        zone = {"name": "Z", "absence_delay_min": 20, "absence_delay_use_system_default": True,
                "override_policy": {"release_strategy": "manual", "use_system_default": True}}
        system = {"entry_type": "system", "system_absence_delay_min": 90,
                  "system_override_policy": dict(_CUSTOM_SYSTEM_POLICY)}
        zone_before, system_before = copy.deepcopy(zone), copy.deepcopy(system)
        resolve_zone_override_policy(zone, system)
        resolve_zone_absence_delay_min(zone, system)
        assert zone == zone_before
        assert system == system_before

    def test_typed_loader_ignores_system_inheritance(self) -> None:
        """from_storage_dict returns the zone's OWN stored values; only the resolvers apply
        the System default.  A migration must therefore use the resolvers, not the loader."""
        zone = {"name": "Z", "absence_delay_min": 20, "absence_delay_use_system_default": True,
                "override_policy": {"release_strategy": "manual", "duration_min": 11,
                                    "use_system_default": True}}
        system = {"system_absence_delay_min": 90,
                  "system_override_policy": dict(_CUSTOM_SYSTEM_POLICY)}
        loaded = from_storage_dict(copy.deepcopy(zone))
        assert loaded.absence_delay_min == 20 and loaded.absence_delay_use_system_default is True
        assert loaded.override_policy.duration_min == 11
        assert resolve_zone_absence_delay_min(zone, system) == 90
        assert resolve_zone_override_policy(zone, system).duration_min == 45

    def test_typed_round_trip_keeps_the_absence_flag_but_drops_the_override_flag(self) -> None:
        zone = {"name": "Z", "absence_delay_min": 20, "absence_delay_use_system_default": True,
                "override_policy": {"release_strategy": "manual", "use_system_default": True}}
        again = to_storage_dict(from_storage_dict(copy.deepcopy(zone)))
        assert again["absence_delay_use_system_default"] is True
        assert "use_system_default" not in again["override_policy"]
        # consequence: a typed round trip silently turns a deferring override policy into an own one
        assert resolve_zone_override_policy(zone, SYSTEMS["present_custom"][0]).duration_min == 45
        assert resolve_zone_override_policy(again, SYSTEMS["present_custom"][0]).duration_min == 120

    def test_stored_flag_stays_true_while_effective_value_differs_from_stored_value(self) -> None:
        zone = {"absence_delay_min": 20, "absence_delay_use_system_default": True}
        assert zone["absence_delay_min"] == 20
        assert resolve_zone_absence_delay_min(zone, {"system_absence_delay_min": 90}) == 90

    def test_two_zones_with_different_stored_shapes_resolve_independently_against_one_system(self) -> None:
        system = SYSTEMS["present_custom"][0]
        a = resolve_zone_override_policy(ZONES["never_configured"][0], system)
        b = resolve_zone_override_policy(ZONES["use_system_false"][0], system)
        c = resolve_zone_override_policy(ZONES["only_tolerance_stored"][0], system)
        assert (a.duration_min, b.duration_min, c.duration_min) == (45, 15, 45)
        assert (a.detection_tolerance, b.detection_tolerance, c.detection_tolerance) == (10, 10, 33)
