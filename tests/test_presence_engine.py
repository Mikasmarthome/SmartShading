"""Presence evaluation policy — v1.2.0-beta.1, Beta.1-T5.

Pure unit coverage of models/presence.py (PresencePolicy/PresenceReading) and
engines/presence_engine.py (classify_presence_state/evaluate_presence_policy),
plus config storage round-trip. No Home Assistant dependency, no coordinator
involved (see tests/test_coordinator_presence_policy.py for the wiring-level
tests: _read_presence() legacy-default reproduction, debouncer interaction,
diagnostics, WindowBehaviorMode/Lifecycle/Comfort/Protection regression).

Coverage:
  PP-01  Legacy default (ANY_HOME) reproduces pre-T5 _read_presence() exactly
         — the single most important backward-compatibility guarantee.
  PP-02  One entity, "home".
  PP-03  One entity, "not_home" (away).
  PP-04  Multiple entities, one "home".
  PP-05  Multiple entities, all "not_home".
  PP-06  Mixed valid states (home + away combinations) per policy.
  PP-07  "unknown".
  PP-08  "unavailable".
  PP-09  Only indeterminate states (all unknown/unavailable/missing).
  PP-10  Transition valid -> indeterminate -> valid.
  PP-11  Cold start: no valid state at all -> conservative (INDETERMINATE).
  PP-12  Full truth table for ANY_HOME (2-entity H/A/U grid).
  PP-13  Full truth table for ALL_HOME (2-entity H/A/U grid).
  PP-14  Full truth table for INVERTED_ANY_HOME (2-entity H/A/U grid).
  PP-15  ANY_AWAY/ALL_AWAY redundancy proof (documented in
         engines/presence_engine.py) — spot-checked here against the 3
         implemented policies to confirm no 4th/5th value was silently
         reintroduced.
  PP-16  Invalid/unknown stored PresencePolicy value falls back safely to
         ANY_HOME, never raises.
  PP-17  ConfigFlow/OptionsFlow schema + submission coverage — see
         tests/test_config_flow_presence_policy.py (a real
         homeassistant.helpers.selector stub was added post pre-push-review
         so config_flow.py is now actually importable and exercised, unlike
         the T3/T4 precedent this ticket originally — incorrectly — followed).
  PP-18  (see PP-17.)
  PP-19  Storage round-trip: explicit policy survives serialize/deserialize;
         missing key defaults to ANY_HOME; malformed value falls back safely.
  PP-20  const.py CONF_PRESENCE_POLICY / PRESENCE_POLICY_OPTIONS are stable
         and distinct (import-safe piece of the ConfigFlow wiring).
  PP-21  ALL_HOME asymmetric indeterminate handling (T5 pre-push review
         correction): one home + one unavailable -> INDETERMINATE (not a
         silent PRESENT from the readable subset); one away + one unknown
         -> ABSENT (away is definitive regardless of unknowns); two home +
         one unknown -> INDETERMINATE; all unknown -> INDETERMINATE.
  PP-22  Inversion proof: ANY_HOME and INVERTED_ANY_HOME produce swapped
         verdicts for every state combination that reaches a DEFINITIVE
         result, and produce the SAME (INDETERMINATE) result whenever
         neither can decide — documented explicitly, not just asserted.
  PP-23  Empty entity list: every policy (including INVERTED_ANY_HOME)
         yields INDETERMINATE from evaluate_presence_policy() directly, so
         none can accidentally produce a permanent-absence signal from an
         empty configuration.
"""
from __future__ import annotations

import itertools

from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    from_storage_dict,
    to_storage_dict,
)
from custom_components.smartshading.const import (
    CONF_PRESENCE_POLICY,
    DEFAULT_PRESENCE_POLICY,
    PRESENCE_POLICY_OPTIONS,
)
from custom_components.smartshading.engines.presence_engine import (
    classify_presence_state,
    evaluate_presence_policy,
)
from custom_components.smartshading.models.presence import PresencePolicy, PresenceReading


def _legacy_read_presence(raw_states: list[str | None]) -> bool:
    """Faithful reimplementation of the exact pre-T5
    Coordinator._read_presence() algorithm, used only as an independent
    oracle for PP-01 — NOT the code under test, so this isn't a tautology
    against evaluate_presence_policy() itself."""
    if not raw_states:
        return True
    any_usable_reading = False
    for raw in raw_states:
        if raw is None or raw in ("unknown", "unavailable"):
            continue
        any_usable_reading = True
        if raw == "home":
            return True
    if not any_usable_reading:
        return True
    return False


# ---------------------------------------------------------------------------
# PP-01 — legacy default reproduces pre-T5 behavior exactly.
# ---------------------------------------------------------------------------

class TestLegacyDefaultReproducesPreT5:
    def test_matches_legacy_oracle_across_many_combinations(self):
        states = [None, "home", "not_home", "unknown", "unavailable", "extended_away"]
        for combo in itertools.product(states, repeat=2):
            raw_states = list(combo)
            reading = evaluate_presence_policy(raw_states, PresencePolicy.ANY_HOME)
            # Policy-independent safe-default mapping applied by the caller
            # (Coordinator._read_presence()): INDETERMINATE -> True.
            new_result = reading is not PresenceReading.ABSENT
            legacy_result = _legacy_read_presence(raw_states)
            assert new_result == legacy_result, f"mismatch for {raw_states}: new={new_result} legacy={legacy_result}"

    def test_empty_list_matches_legacy_default_present(self):
        assert evaluate_presence_policy([], PresencePolicy.ANY_HOME) is PresenceReading.INDETERMINATE
        assert _legacy_read_presence([]) is True


# ---------------------------------------------------------------------------
# PP-02 .. PP-05 — basic single/multi-entity cases.
# ---------------------------------------------------------------------------

class TestBasicCases:
    def test_one_entity_home(self):
        assert evaluate_presence_policy(["home"], PresencePolicy.ANY_HOME) is PresenceReading.PRESENT

    def test_one_entity_not_home(self):
        assert evaluate_presence_policy(["not_home"], PresencePolicy.ANY_HOME) is PresenceReading.ABSENT

    def test_multiple_entities_one_home(self):
        assert evaluate_presence_policy(["not_home", "home"], PresencePolicy.ANY_HOME) is PresenceReading.PRESENT

    def test_multiple_entities_all_not_home(self):
        assert evaluate_presence_policy(["not_home", "not_home"], PresencePolicy.ANY_HOME) is PresenceReading.ABSENT


# ---------------------------------------------------------------------------
# PP-06 — mixed valid states per policy (the case that actually
# distinguishes ANY_HOME from ALL_HOME).
# ---------------------------------------------------------------------------

class TestMixedStatesPerPolicy:
    def test_one_home_one_away_any_home_is_present(self):
        assert evaluate_presence_policy(["home", "not_home"], PresencePolicy.ANY_HOME) is PresenceReading.PRESENT

    def test_one_home_one_away_all_home_is_absent(self):
        assert evaluate_presence_policy(["home", "not_home"], PresencePolicy.ALL_HOME) is PresenceReading.ABSENT

    def test_all_home_requires_unanimity(self):
        assert evaluate_presence_policy(["home", "home"], PresencePolicy.ALL_HOME) is PresenceReading.PRESENT
        assert evaluate_presence_policy(["home", "home", "not_home"], PresencePolicy.ALL_HOME) is PresenceReading.ABSENT


# ---------------------------------------------------------------------------
# PP-07 .. PP-09 — unknown/unavailable/all-indeterminate.
# ---------------------------------------------------------------------------

class TestIndeterminateStates:
    def test_unknown_alone_is_indeterminate(self):
        assert evaluate_presence_policy(["unknown"], PresencePolicy.ANY_HOME) is PresenceReading.INDETERMINATE

    def test_unavailable_alone_is_indeterminate(self):
        assert evaluate_presence_policy(["unavailable"], PresencePolicy.ANY_HOME) is PresenceReading.INDETERMINATE

    def test_only_indeterminate_states_across_entities(self):
        assert evaluate_presence_policy(
            ["unknown", "unavailable", None], PresencePolicy.ANY_HOME
        ) is PresenceReading.INDETERMINATE

    def test_indeterminate_entity_is_ignored_not_blocking_when_others_determinate(self):
        """A mix of one determinate + one indeterminate entity still yields
        a definitive verdict — indeterminate entities are excluded from the
        quantifier, not treated as blocking it (see module docstring "proof")."""
        assert evaluate_presence_policy(["home", "unknown"], PresencePolicy.ANY_HOME) is PresenceReading.PRESENT
        assert evaluate_presence_policy(["not_home", "unknown"], PresencePolicy.ANY_HOME) is PresenceReading.ABSENT


# ---------------------------------------------------------------------------
# PP-10 — valid -> indeterminate -> valid transition.
# ---------------------------------------------------------------------------

class TestTransitionSequence:
    def test_valid_indeterminate_valid_each_evaluated_independently(self):
        """evaluate_presence_policy() is stateless/pure — each call is
        independent, so a transition sequence is just three independent
        evaluations. The debouncer (untouched by T5) owns any temporal
        continuity — see tests/test_coordinator_presence_policy.py."""
        assert evaluate_presence_policy(["home"], PresencePolicy.ANY_HOME) is PresenceReading.PRESENT
        assert evaluate_presence_policy(["unknown"], PresencePolicy.ANY_HOME) is PresenceReading.INDETERMINATE
        assert evaluate_presence_policy(["home"], PresencePolicy.ANY_HOME) is PresenceReading.PRESENT


# ---------------------------------------------------------------------------
# PP-11 — cold start: no valid state at all.
# ---------------------------------------------------------------------------

class TestColdStart:
    def test_cold_start_no_valid_state_is_indeterminate(self):
        assert evaluate_presence_policy([None, None], PresencePolicy.ANY_HOME) is PresenceReading.INDETERMINATE

    def test_cold_start_maps_to_conservative_present_for_debounce(self):
        reading = evaluate_presence_policy([None, None], PresencePolicy.ANY_HOME)
        present_for_debounce = reading is not PresenceReading.ABSENT
        assert present_for_debounce is True  # never falsely trigger absence from missing data


# ---------------------------------------------------------------------------
# PP-12 .. PP-14 — full 2-entity truth tables per policy.
# ---------------------------------------------------------------------------

_STATES = {"home": "home", "away": "not_home", "unknown": "unknown"}


class TestAnyHomeTruthTable:
    _EXPECTED = {
        ("home", "home"): PresenceReading.PRESENT,
        ("home", "away"): PresenceReading.PRESENT,
        ("away", "home"): PresenceReading.PRESENT,
        ("home", "unknown"): PresenceReading.PRESENT,
        ("unknown", "home"): PresenceReading.PRESENT,
        ("away", "away"): PresenceReading.ABSENT,
        ("away", "unknown"): PresenceReading.ABSENT,
        ("unknown", "away"): PresenceReading.ABSENT,
        ("unknown", "unknown"): PresenceReading.INDETERMINATE,
    }

    def test_full_grid(self):
        for (a, b), expected in self._EXPECTED.items():
            raw = [_STATES[a], _STATES[b]]
            assert evaluate_presence_policy(raw, PresencePolicy.ANY_HOME) is expected, (a, b)


class TestAllHomeTruthTable:
    """ALL_HOME uses an ASYMMETRIC indeterminate rule (T5 pre-push review
    correction — see engines/presence_engine.py _evaluate_all_home()):
    ABSENT is definitive the moment any entity is confirmed away, regardless
    of other indeterminate entities (robustness for an in-progress
    absence-delay countdown); PRESENT requires every entity to be positively
    confirmed home — a single indeterminate entity (with no one confirmed
    away) means unanimity cannot be confirmed, so the honest result is
    INDETERMINATE rather than presence based on whoever happens to be
    readable."""

    _EXPECTED = {
        ("home", "home"): PresenceReading.PRESENT,
        ("home", "away"): PresenceReading.ABSENT,
        ("away", "home"): PresenceReading.ABSENT,
        ("home", "unknown"): PresenceReading.INDETERMINATE,   # cannot confirm unanimity
        ("unknown", "home"): PresenceReading.INDETERMINATE,
        ("away", "away"): PresenceReading.ABSENT,
        ("away", "unknown"): PresenceReading.ABSENT,           # away is definitive regardless of unknowns
        ("unknown", "away"): PresenceReading.ABSENT,
        ("unknown", "unknown"): PresenceReading.INDETERMINATE,
    }

    def test_full_grid(self):
        for (a, b), expected in self._EXPECTED.items():
            raw = [_STATES[a], _STATES[b]]
            assert evaluate_presence_policy(raw, PresencePolicy.ALL_HOME) is expected, (a, b)


class TestInvertedAnyHomeTruthTable:
    _EXPECTED = {
        ("home", "home"): PresenceReading.ABSENT,
        ("home", "away"): PresenceReading.ABSENT,
        ("away", "home"): PresenceReading.ABSENT,
        ("home", "unknown"): PresenceReading.ABSENT,
        ("unknown", "home"): PresenceReading.ABSENT,
        ("away", "away"): PresenceReading.PRESENT,
        ("away", "unknown"): PresenceReading.PRESENT,
        ("unknown", "away"): PresenceReading.PRESENT,
        ("unknown", "unknown"): PresenceReading.INDETERMINATE,
    }

    def test_full_grid(self):
        for (a, b), expected in self._EXPECTED.items():
            raw = [_STATES[a], _STATES[b]]
            assert evaluate_presence_policy(raw, PresencePolicy.INVERTED_ANY_HOME) is expected, (a, b)


# ---------------------------------------------------------------------------
# PP-15 — ANY_AWAY/ALL_AWAY redundancy proof (spot-check against the 3
# implemented policies + only 3 enum members exist).
# ---------------------------------------------------------------------------

class TestNoRedundantPolicyValues:
    def test_exactly_three_policy_values(self):
        assert {p.value for p in PresencePolicy} == {"any_home", "all_home", "inverted_any_home"}

    def test_all_home_present_condition_equals_hypothetical_any_away_negation(self):
        """Proof spot-check: for every determinate-only combination, ALL_HOME's
        verdict equals what a hypothetical ANY_AWAY policy ("absent iff any
        away") would produce — confirming they'd be exactly redundant, which
        is why only ALL_HOME is implemented."""
        determinate_states = ["home", "not_home"]
        for combo in itertools.product(determinate_states, repeat=3):
            raw = list(combo)
            all_home_reading = evaluate_presence_policy(raw, PresencePolicy.ALL_HOME)
            hypothetical_any_away_present = not any(s == "not_home" for s in raw)
            hypothetical_reading = (
                PresenceReading.PRESENT if hypothetical_any_away_present else PresenceReading.ABSENT
            )
            assert all_home_reading is hypothetical_reading, raw

    def test_any_home_present_condition_equals_hypothetical_all_away_negation(self):
        determinate_states = ["home", "not_home"]
        for combo in itertools.product(determinate_states, repeat=3):
            raw = list(combo)
            any_home_reading = evaluate_presence_policy(raw, PresencePolicy.ANY_HOME)
            hypothetical_all_away_present = not all(s == "not_home" for s in raw)
            hypothetical_reading = (
                PresenceReading.PRESENT if hypothetical_all_away_present else PresenceReading.ABSENT
            )
            assert any_home_reading is hypothetical_reading, raw


# ---------------------------------------------------------------------------
# PP-16 — invalid stored value falls back safely.
# ---------------------------------------------------------------------------

class TestInvalidStoredValueFallsBack:
    def test_evaluate_presence_policy_never_raises_for_any_enum_member(self):
        for policy in PresencePolicy:
            evaluate_presence_policy(["home", "not_home", "unknown", None], policy)  # must not raise

    def test_classify_presence_state_never_raises(self):
        for raw in (None, "", "home", "not_home", "unknown", "unavailable", "Home", 123, "extended_away"):
            classify_presence_state(raw)  # must not raise


# ---------------------------------------------------------------------------
# PP-19 — storage round-trip.
# ---------------------------------------------------------------------------

class TestStorageRoundTrip:
    def test_default_is_any_home(self):
        data = SmartShadingConfigEntryData(name="Test", use_home_location=True)
        assert data.presence_policy is PresencePolicy.ANY_HOME

    def test_explicit_value_survives_round_trip(self):
        data = SmartShadingConfigEntryData(
            name="Test", use_home_location=True, presence_policy=PresencePolicy.ALL_HOME,
        )
        stored = to_storage_dict(data)
        assert stored["presence_policy"] == "all_home"
        restored = from_storage_dict(stored)
        assert restored.presence_policy is PresencePolicy.ALL_HOME

    def test_missing_key_defaults_to_any_home(self):
        raw = {"name": "Test", "use_home_location": True}
        restored = from_storage_dict(raw)
        assert restored.presence_policy is PresencePolicy.ANY_HOME

    def test_malformed_value_falls_back_to_any_home(self):
        raw = {"name": "Test", "use_home_location": True, "presence_policy": "vacation_mode_bogus"}
        restored = from_storage_dict(raw)
        assert restored.presence_policy is PresencePolicy.ANY_HOME

    def test_non_string_value_falls_back_to_any_home(self):
        raw = {"name": "Test", "use_home_location": True, "presence_policy": 42}
        restored = from_storage_dict(raw)
        assert restored.presence_policy is PresencePolicy.ANY_HOME

    def test_inverted_any_home_survives_round_trip(self):
        data = SmartShadingConfigEntryData(
            name="Test", use_home_location=True, presence_policy=PresencePolicy.INVERTED_ANY_HOME,
        )
        stored = to_storage_dict(data)
        restored = from_storage_dict(stored)
        assert restored.presence_policy is PresencePolicy.INVERTED_ANY_HOME


# ---------------------------------------------------------------------------
# PP-20 — const.py keys are stable and distinct.
# ---------------------------------------------------------------------------

class TestConstKeys:
    def test_conf_key_and_default_and_options(self):
        assert CONF_PRESENCE_POLICY == "presence_policy"
        assert DEFAULT_PRESENCE_POLICY == "any_home"
        assert set(PRESENCE_POLICY_OPTIONS) == {"any_home", "all_home", "inverted_any_home"}
        assert set(PRESENCE_POLICY_OPTIONS) == {p.value for p in PresencePolicy}


# ---------------------------------------------------------------------------
# PP-21 — ALL_HOME asymmetric indeterminate handling (concrete scenarios).
# ---------------------------------------------------------------------------

class TestAllHomeAsymmetricScenarios:
    def test_one_home_one_unavailable_is_indeterminate_not_present(self):
        """The scenario the pre-push review specifically flagged: silently
        ignoring the unavailable entity would have declared PRESENT from
        just the readable subset, contradicting "ALL home"."""
        assert evaluate_presence_policy(["home", "unavailable"], PresencePolicy.ALL_HOME) is PresenceReading.INDETERMINATE

    def test_one_away_one_unknown_is_absent(self):
        """Robustness case: a confirmed absence is definitive even when a
        second entity's status is genuinely unknown."""
        assert evaluate_presence_policy(["not_home", "unknown"], PresencePolicy.ALL_HOME) is PresenceReading.ABSENT

    def test_two_home_one_unknown_is_indeterminate(self):
        assert evaluate_presence_policy(
            ["home", "home", "unknown"], PresencePolicy.ALL_HOME
        ) is PresenceReading.INDETERMINATE

    def test_all_unknown_is_indeterminate(self):
        assert evaluate_presence_policy(
            ["unknown", "unknown", "unknown"], PresencePolicy.ALL_HOME
        ) is PresenceReading.INDETERMINATE

    def test_flicker_during_established_presence_stays_indeterminate_not_absent(self):
        """A single-entity household, currently home, whose tracker blips to
        unavailable: must not become a false ABSENT (only becomes
        INDETERMINATE, which maps to "present" for the debounce input at
        the Coordinator layer — see test_coordinator_presence_policy.py)."""
        assert evaluate_presence_policy(["unavailable"], PresencePolicy.ALL_HOME) is PresenceReading.INDETERMINATE

    def test_flicker_of_one_entity_during_confirmed_absence_stays_absent(self):
        """Two-person household, both away (absence-delay counting down);
        ONE tracker blips to unknown mid-countdown. ALL_HOME must keep
        reporting ABSENT (not fall back to INDETERMINATE/present) as long as
        the OTHER entity is still confirmed away — otherwise a flaky sensor
        could spuriously cancel a real, still-active absence sequence."""
        assert evaluate_presence_policy(["not_home", "unknown"], PresencePolicy.ALL_HOME) is PresenceReading.ABSENT


# ---------------------------------------------------------------------------
# PP-22 — inversion proof: ANY_HOME vs INVERTED_ANY_HOME.
# ---------------------------------------------------------------------------

class TestInversionProof:
    def test_definitive_verdicts_are_swapped_for_every_combination(self):
        states = ["home", "not_home"]
        for combo in itertools.product(states, repeat=3):
            raw = list(combo)
            any_home = evaluate_presence_policy(raw, PresencePolicy.ANY_HOME)
            inverted = evaluate_presence_policy(raw, PresencePolicy.INVERTED_ANY_HOME)
            assert any_home is not PresenceReading.INDETERMINATE
            assert inverted is not PresenceReading.INDETERMINATE
            assert (any_home is PresenceReading.PRESENT) == (inverted is PresenceReading.ABSENT), raw
            assert (any_home is PresenceReading.ABSENT) == (inverted is PresenceReading.PRESENT), raw

    def test_indeterminate_cases_are_not_inverted_both_stay_indeterminate(self):
        """When neither policy can reach a verdict, BOTH return
        INDETERMINATE (not swapped) — inversion only applies to definitive
        verdicts. The Coordinator then maps INDETERMINATE to "present" for
        BOTH policies identically (see
        test_coordinator_presence_policy.py::TestInversionSafeDefaultNotInverted),
        so "inverted" must not be read as "the safe default is inverted too"."""
        for raw in ([], ["unknown"], ["unavailable", None], ["unknown", "unavailable"]):
            any_home = evaluate_presence_policy(raw, PresencePolicy.ANY_HOME)
            inverted = evaluate_presence_policy(raw, PresencePolicy.INVERTED_ANY_HOME)
            assert any_home is PresenceReading.INDETERMINATE
            assert inverted is PresenceReading.INDETERMINATE


# ---------------------------------------------------------------------------
# PP-23 — empty entity list, every policy.
# ---------------------------------------------------------------------------

class TestEmptyEntityListEveryPolicy:
    def test_empty_list_is_indeterminate_for_every_policy(self):
        for policy in PresencePolicy:
            assert evaluate_presence_policy([], policy) is PresenceReading.INDETERMINATE, policy
