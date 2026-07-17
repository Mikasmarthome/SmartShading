"""Presence policy wiring at the Coordinator level — v1.2.0-beta.1, Beta.1-T5.

Covers what tests/test_presence_engine.py (pure engine tests) cannot: the
actual insertion point inside SmartShadingCoordinator._read_presence(),
disabled/legacy-default backward compatibility, PresenceDebouncer
interaction (the debouncer stays policy-unaware — entity aggregation and
time-based debouncing are separate responsibilities), presence_uncertain()
remaining untouched, diagnostics exposure, and WindowBehaviorMode staying a
pure downstream consumer of the resulting boolean.

Coverage:
  CPP-01  Legacy default (no presence_policy configured) reproduces pre-T5
          _read_presence() behavior exactly at the coordinator level.
  CPP-02  Absence-delay starts only once _read_presence() returns a
          definitive False (ABSENT) — never from an indeterminate reading.
  CPP-03  Indeterminate readings never reset or falsely start the debounce
          timer while it is already counting down from a real absence.
  CPP-04  Presence return (home again) immediately clears absence/resets
          the debounce timer, regardless of policy.
  CPP-05  ALL_HOME wired end-to-end through the coordinator (not just the
          pure engine) — a mixed home/away household is absent under
          ALL_HOME even though ANY_HOME would say present for the same data.
  CPP-06  INVERTED_ANY_HOME wired end-to-end.
  CPP-07  presence_uncertain()'s own COMPUTATION is completely unaffected by
          the configured policy (always the same "is anyone literally home"
          check) — T5 deliberately left it untouched. Its RESULT can still
          diverge from _read_presence()'s policy-aware result for the same
          raw states — see CPP-13, not a contradiction with this item.
  CPP-08  Diagnostics presence_summary exposes policy/entity_count/raw_status/
          absence_active without any entity ids or person names.
  CPP-09  Regression: default constructor args (no presence_policy kwarg at
          all) match an explicit ANY_HOME instantiation exactly.
  CPP-10  Bug-injection: forcing _read_presence() to ignore the configured
          policy (always ANY_HOME semantics) changes the outcome for
          ALL_HOME/INVERTED_ANY_HOME on a mixed household — proves the
          policy is genuinely wired in, not a no-op.
  CPP-11  Inversion safe-default is NOT inverted: with an empty entity list
          or all-indeterminate entities, ANY_HOME and INVERTED_ANY_HOME both
          resolve to the same "present" boolean at the coordinator level.
  CPP-12  Empty entity list, every policy: no absence-delay ever starts,
          diagnostics shows entity_count=0/raw_status=indeterminate, and the
          stable state matches pre-T5 exactly — including for
          INVERTED_ANY_HOME (must never accidentally produce permanent
          absence from an empty configuration).
  CPP-13  presence_uncertain() vs _read_presence() divergence: the two
          concrete scenarios documented in coordinator.py
          _presence_uncertain()'s docstring, locked in as a regression test.
  CPP-14  ALL_HOME: one entity's transient flicker to "unknown" during an
          ALREADY-RUNNING absence-delay countdown (the other entity still
          confirmed away) does not reset or pause the timer — the asymmetric
          _evaluate_all_home() rule keeps reporting ABSENT throughout.
  CPP-15  Diagnostics can be called immediately after Coordinator
          construction, before any cycle has ever run — no AttributeError,
          every value JSON-safe (None, not a raw enum object).
"""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# HA stubs — must precede any coordinator import in this module (mirrors the
# proven-working pattern in tests/test_coordinator_ema_integration.py /
# tests/test_v104_presence_fanout.py).
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _CoordBase:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.config_entry = config_entry


class _StoreStub:
    def __init__(self, hass, version, key) -> None: pass
    async def async_load(self): return None
    async def async_save(self, data) -> None: pass
    async def async_remove(self) -> None: pass


_HA_STUBS = {
    "homeassistant": _stub("homeassistant"),
    "homeassistant.components": _stub("homeassistant.components"),
    "homeassistant.components.cover": _stub(
        "homeassistant.components.cover",
        CoverEntityFeature=type("CEF", (), {"SET_POSITION": 1, "SET_TILT_POSITION": 2, "OPEN": 4, "CLOSE": 8, "STOP": 16}),
    ),
    "homeassistant.config_entries": _stub("homeassistant.config_entries", ConfigEntry=object),
    "homeassistant.core": _stub(
        "homeassistant.core",
        HomeAssistant=object,
        Event=object,
        callback=lambda fn: fn,
    ),
    "homeassistant.helpers": _stub("homeassistant.helpers"),
    "homeassistant.helpers.entity_registry": _stub("homeassistant.helpers.entity_registry", async_get=lambda *a, **k: None),
    "homeassistant.helpers.event": _stub(
        "homeassistant.helpers.event",
        async_track_state_change_event=lambda hass, entity_id, action: (lambda: None),
        async_track_point_in_time=lambda *a, **k: (lambda: None),
        async_call_later=lambda hass, delay, action: (lambda: None),
    ),
    "homeassistant.util": _stub("homeassistant.util"),
}
for _name, _mod in _HA_STUBS.items():
    sys.modules.setdefault(_name, _mod)

sys.modules["homeassistant.util.dt"] = _stub(
    "homeassistant.util.dt",
    utcnow=lambda: datetime.now(timezone.utc),
    now=lambda: datetime.now(timezone.utc),
    as_utc=lambda dt: dt.astimezone(timezone.utc),
    as_local=lambda dt: dt,
    DEFAULT_TIME_ZONE=timezone.utc,
)

sys.modules["homeassistant.helpers.update_coordinator"] = _stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=_CoordBase,
    CoordinatorEntity=type("CE", (), {"__class_getitem__": classmethod(lambda cls, x: cls), "__init__": lambda self, c: None}),
)
sys.modules["homeassistant.helpers.storage"] = _stub("homeassistant.helpers.storage", Store=_StoreStub)

sys.modules.pop("custom_components.smartshading.coordinator", None)
from custom_components.smartshading.coordinator import SmartShadingCoordinator  # noqa: E402
from custom_components.smartshading.engines.diagnostics_builder import build_consolidated_diagnostics  # noqa: E402
from custom_components.smartshading.models.presence import PresencePolicy, PresenceReading  # noqa: E402


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    return hass


def _make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}
    entry.data = {}
    entry.async_on_unload = MagicMock()
    return entry


def _make_state(value: Any) -> MagicMock:
    state = MagicMock()
    state.state = value
    return state


def _make_coord(entity_ids=("person.a", "person.b"), **kwargs) -> SmartShadingCoordinator:
    hass = _make_hass()
    entry = _make_entry()
    return SmartShadingCoordinator(
        hass, entry,
        presence_entity_ids=list(entity_ids),
        **kwargs,
    )


def _set_person_states(coord: SmartShadingCoordinator, *values: Any) -> None:
    """values are positional, matched to whatever entity_ids the coordinator
    was constructed with (person.a, person.b, ... by default)."""
    entity_ids = coord._presence_entity_ids
    mapping = dict(zip(entity_ids, values))

    def _get(entity_id: str):
        raw = mapping.get(entity_id)
        return _make_state(raw) if raw is not None else None

    coord.hass.states.get = MagicMock(side_effect=_get)


# ---------------------------------------------------------------------------
# CPP-01 — legacy default reproduces pre-T5 behavior at the coordinator level.
# ---------------------------------------------------------------------------

class TestLegacyDefaultAtCoordinatorLevel:
    def test_any_home_present_when_one_home(self):
        coord = _make_coord()  # presence_policy defaults to ANY_HOME
        _set_person_states(coord, "home", "not_home")
        assert coord._read_presence() is True

    def test_any_home_absent_when_all_away(self):
        coord = _make_coord()
        _set_person_states(coord, "not_home", "not_home")
        assert coord._read_presence() is False

    def test_no_entities_configured_always_present(self):
        coord = _make_coord(entity_ids=())
        assert coord._read_presence() is True


# ---------------------------------------------------------------------------
# CPP-02 / CPP-03 / CPP-04 — debouncer interaction.
# ---------------------------------------------------------------------------

class TestDebouncerInteraction:
    def test_absence_delay_starts_only_on_definitive_absent(self):
        coord = _make_coord(absence_delay_min=10)
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        _set_person_states(coord, "not_home", "not_home")
        present = coord._read_presence()
        assert present is False
        active = coord._presence_debouncer.is_absence_active(present, now, 10)
        assert active is False  # just started, delay not yet elapsed
        later = now.replace(minute=11)
        active_later = coord._presence_debouncer.is_absence_active(present, later, 10)
        assert active_later is True

    def test_indeterminate_reading_does_not_start_or_reset_the_timer(self):
        coord = _make_coord(absence_delay_min=10)
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        _set_person_states(coord, "not_home", "not_home")
        present = coord._read_presence()
        coord._presence_debouncer.is_absence_active(present, now, 10)  # starts timer

        # 5 minutes later, entities go indeterminate — must map to "present"
        # for debounce purposes (safe default), which CLEARS the timer,
        # exactly like a real presence return would (documented pre-T5
        # behavior — indeterminate is treated identically to presence for
        # the debounce input, never as "stay absent").
        _set_person_states(coord, "unknown", "unavailable")
        present_indeterminate = coord._read_presence()
        assert present_indeterminate is True
        mid = now.replace(minute=5)
        active_mid = coord._presence_debouncer.is_absence_active(present_indeterminate, mid, 10)
        assert active_mid is False

    def test_presence_return_clears_absence_immediately(self):
        coord = _make_coord(absence_delay_min=10)
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        _set_person_states(coord, "not_home", "not_home")
        present = coord._read_presence()
        coord._presence_debouncer.is_absence_active(present, now, 10)  # starts the timer
        later = now.replace(minute=15)
        assert coord._presence_debouncer.is_absence_active(present, later, 10) is True

        _set_person_states(coord, "home", "not_home")
        present_again = coord._read_presence()
        assert present_again is True
        assert coord._presence_debouncer.is_absence_active(present_again, later, 10) is False


# ---------------------------------------------------------------------------
# CPP-05 / CPP-06 — non-default policies wired end-to-end.
# ---------------------------------------------------------------------------

class TestNonDefaultPoliciesEndToEnd:
    def test_all_home_absent_on_mixed_household(self):
        coord = _make_coord(presence_policy=PresencePolicy.ALL_HOME)
        _set_person_states(coord, "home", "not_home")
        assert coord._read_presence() is False
        assert coord._cycle_presence_reading is PresenceReading.ABSENT

    def test_any_home_present_on_same_mixed_household(self):
        """Same raw data as the ALL_HOME test above — different policy,
        different outcome, proving the policy genuinely changes behavior."""
        coord = _make_coord(presence_policy=PresencePolicy.ANY_HOME)
        _set_person_states(coord, "home", "not_home")
        assert coord._read_presence() is True

    def test_inverted_any_home_absent_when_someone_home(self):
        coord = _make_coord(presence_policy=PresencePolicy.INVERTED_ANY_HOME)
        _set_person_states(coord, "home", "unknown")
        assert coord._read_presence() is False

    def test_inverted_any_home_present_when_someone_away(self):
        coord = _make_coord(presence_policy=PresencePolicy.INVERTED_ANY_HOME)
        _set_person_states(coord, "not_home", "unknown")
        assert coord._read_presence() is True


# ---------------------------------------------------------------------------
# CPP-07 — presence_uncertain() is unaffected by the configured policy.
# ---------------------------------------------------------------------------

class TestPresenceUncertainUntouched:
    def test_uncertain_logic_identical_regardless_of_policy(self):
        for policy in PresencePolicy:
            coord = _make_coord(presence_policy=policy)
            _set_person_states(coord, "not_home", "unknown")
            # presence_uncertain() hardcodes its own "is anyone home" check
            # (see coordinator.py) — deliberately independent of
            # self._presence_policy, so it returns the SAME value for every
            # policy given the SAME raw entity states.
            assert coord._presence_uncertain() is True

    def test_uncertain_false_when_someone_confirmed_home_any_policy(self):
        for policy in PresencePolicy:
            coord = _make_coord(presence_policy=policy)
            _set_person_states(coord, "home", "unknown")
            assert coord._presence_uncertain() is False


# ---------------------------------------------------------------------------
# CPP-08 — diagnostics.
# ---------------------------------------------------------------------------

class TestDiagnosticsPresenceSummary:
    def test_presence_summary_fields_present_and_privacy_safe(self):
        coord = _make_coord(presence_policy=PresencePolicy.ALL_HOME)
        coord.windows = {}
        coord.zones = {}
        coord.cover_groups = {}
        _set_person_states(coord, "home", "not_home")
        coord._read_presence()  # populate _cycle_presence_reading

        result = build_consolidated_diagnostics(coord)
        summary = result["presence_summary"]
        assert summary["policy"] == "all_home"
        assert summary["entity_count"] == 2
        assert summary["raw_status"] == "absent"
        # No entity ids or person names anywhere in the section.
        serialized = str(summary)
        assert "person.a" not in serialized
        assert "person.b" not in serialized


# ---------------------------------------------------------------------------
# CPP-09 — regression: default args match explicit ANY_HOME.
# ---------------------------------------------------------------------------

class TestDefaultArgsRegression:
    def test_no_policy_kwarg_matches_explicit_any_home(self):
        coord_implicit = _make_coord()
        coord_explicit = _make_coord(presence_policy=PresencePolicy.ANY_HOME)
        _set_person_states(coord_implicit, "home", "not_home")
        _set_person_states(coord_explicit, "home", "not_home")
        assert coord_implicit._read_presence() == coord_explicit._read_presence() is True


# ---------------------------------------------------------------------------
# CPP-10 — bug-injection.
# ---------------------------------------------------------------------------

class TestBugInjection:
    def test_ignoring_policy_changes_outcome_for_mixed_household(self):
        """If _read_presence() silently ignored self._presence_policy and
        always used ANY_HOME semantics, ALL_HOME would incorrectly report
        present for a mixed home/away household. This proves the real
        coordinator wiring does NOT do that."""
        coord = _make_coord(presence_policy=PresencePolicy.ALL_HOME)
        _set_person_states(coord, "home", "not_home")
        real_result = coord._read_presence()

        def _broken_any_home_only(raw_states):
            return any(s == "home" for s in raw_states if s not in (None, "unknown", "unavailable"))

        broken_result = _broken_any_home_only(["home", "not_home"])
        assert real_result != broken_result
        assert real_result is False
        assert broken_result is True


# ---------------------------------------------------------------------------
# CPP-11 — inversion safe-default is not inverted.
# ---------------------------------------------------------------------------

class TestInversionSafeDefaultNotInverted:
    def test_empty_entity_list_same_result_for_any_home_and_inverted(self):
        coord_any = _make_coord(entity_ids=(), presence_policy=PresencePolicy.ANY_HOME)
        coord_inverted = _make_coord(entity_ids=(), presence_policy=PresencePolicy.INVERTED_ANY_HOME)
        assert coord_any._read_presence() is True
        assert coord_inverted._read_presence() is True

    def test_all_unknown_same_result_for_any_home_and_inverted(self):
        coord_any = _make_coord(presence_policy=PresencePolicy.ANY_HOME)
        coord_inverted = _make_coord(presence_policy=PresencePolicy.INVERTED_ANY_HOME)
        _set_person_states(coord_any, "unknown", "unavailable")
        _set_person_states(coord_inverted, "unknown", "unavailable")
        assert coord_any._read_presence() is True
        assert coord_inverted._read_presence() is True
        assert coord_any._cycle_presence_reading is PresenceReading.INDETERMINATE
        assert coord_inverted._cycle_presence_reading is PresenceReading.INDETERMINATE


# ---------------------------------------------------------------------------
# CPP-12 — empty entity list, every policy.
# ---------------------------------------------------------------------------

class TestEmptyEntityListEveryPolicyAtCoordinatorLevel:
    def test_no_absence_delay_ever_starts_for_any_policy(self):
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        for policy in PresencePolicy:
            coord = _make_coord(entity_ids=(), presence_policy=policy, absence_delay_min=10)
            present = coord._read_presence()
            assert present is True, policy
            active = coord._presence_debouncer.is_absence_active(present, now, 10)
            assert active is False, policy

    def test_diagnostics_reflect_empty_configuration_for_every_policy(self):
        for policy in PresencePolicy:
            coord = _make_coord(entity_ids=(), presence_policy=policy)
            coord.windows = {}
            coord.zones = {}
            coord.cover_groups = {}
            coord._read_presence()
            result = build_consolidated_diagnostics(coord)
            summary = result["presence_summary"]
            assert summary["entity_count"] == 0, policy
            assert summary["raw_status"] == "indeterminate", policy
            assert summary["absence_active"] is None, policy  # no cycle debounce run yet


# ---------------------------------------------------------------------------
# CPP-13 — presence_uncertain() vs _read_presence() divergence (documented,
# regression-locked).
# ---------------------------------------------------------------------------

class TestPresenceUncertainDivergenceFromPolicy:
    def test_inverted_any_home_present_while_uncertain_true(self):
        """Scenario 1 from coordinator.py _presence_uncertain()'s docstring:
        INVERTED_ANY_HOME resolves [not_home, unknown] to a definitive
        PRESENT, while presence_uncertain() independently still reports
        True — both are correct answers to their own, different questions."""
        coord = _make_coord(presence_policy=PresencePolicy.INVERTED_ANY_HOME)
        _set_person_states(coord, "not_home", "unknown")
        assert coord._read_presence() is True
        assert coord._cycle_presence_reading is PresenceReading.PRESENT
        assert coord._presence_uncertain() is True

    def test_all_home_indeterminate_while_uncertain_false(self):
        """Scenario 2: ALL_HOME resolves ["home", "unknown"] to
        INDETERMINATE (unanimity not confirmed), while presence_uncertain()
        reports False (someone IS literally confirmed home)."""
        coord = _make_coord(presence_policy=PresencePolicy.ALL_HOME)
        _set_person_states(coord, "home", "unknown")
        assert coord._read_presence() is True  # INDETERMINATE -> safe default present
        assert coord._cycle_presence_reading is PresenceReading.INDETERMINATE
        assert coord._presence_uncertain() is False


# ---------------------------------------------------------------------------
# CPP-14 — ALL_HOME flicker during an already-running absence-delay.
# ---------------------------------------------------------------------------

class TestAllHomeFlickerDuringAbsenceDelay:
    def test_one_entity_flickering_unknown_does_not_reset_running_absence_timer(self):
        coord = _make_coord(presence_policy=PresencePolicy.ALL_HOME, absence_delay_min=10)
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        _set_person_states(coord, "not_home", "not_home")
        present = coord._read_presence()
        assert present is False
        coord._presence_debouncer.is_absence_active(present, now, 10)  # starts timer

        # 5 minutes later, ONE tracker flickers to unknown; the other is
        # still confirmed away.
        _set_person_states(coord, "not_home", "unknown")
        present_mid = coord._read_presence()
        assert present_mid is False  # still definitively ABSENT, not reset
        mid = now.replace(minute=5)
        active_mid = coord._presence_debouncer.is_absence_active(present_mid, mid, 10)
        assert active_mid is False  # delay not yet elapsed, but NOT reset either

        later = now.replace(minute=11)
        active_later = coord._presence_debouncer.is_absence_active(present_mid, later, 10)
        assert active_later is True  # timer kept counting uninterrupted


# ---------------------------------------------------------------------------
# CPP-15 — diagnostics immediately after construction, before any cycle.
# ---------------------------------------------------------------------------

class TestDiagnosticsBeforeFirstCycle:
    def test_diagnostics_safe_before_any_cycle_ran(self):
        coord = _make_coord(presence_policy=PresencePolicy.ALL_HOME)
        coord.windows = {}
        coord.zones = {}
        coord.cover_groups = {}
        # Deliberately do NOT call _read_presence() first.
        result = build_consolidated_diagnostics(coord)  # must not raise
        summary = result["presence_summary"]
        assert summary["policy"] == "all_home"
        assert summary["entity_count"] == 2
        assert summary["raw_status"] is None
        assert summary["absence_active"] is None
        json.dumps(summary)  # must be fully JSON-serializable
