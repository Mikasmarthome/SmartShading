"""Tests for engines/heat_hysteresis.py — pure entry/exit hysteresis decision
function for heat protection (v1.2.0-beta.1, T9).

Contract (see module docstring of engines/heat_hysteresis.py):
  - Entry: outdoor >= outdoor_entry_c OR indoor >= indoor_entry_c (inclusive-high).
  - Exit: only once EVERY enabled+available signal has dropped strictly
    below (entry - hysteresis_c) (exclusive-low).
  - hysteresis_c=0 reproduces the exact legacy flat-threshold comparison.
  - Missing data never triggers entry; missing data while active never
    releases (fail-safe hold, mirrors SafetyHold's sensor_unavailable
    extend-not-release precedent).
"""
from __future__ import annotations

from custom_components.smartshading.engines.heat_hysteresis import (
    REASON_DISABLED,
    REASON_ENTERED,
    REASON_EXITED,
    REASON_HELD_BY_HYSTERESIS,
    REASON_HELD_MISSING_DATA,
    REASON_INSUFFICIENT_DATA,
    REASON_NOT_NEEDED,
    resolve_heat_needed,
)

_OUTDOOR_ENTRY = 26.0
_INDOOR_ENTRY = 24.0
_HYST = 1.0  # outdoor exit=25.0, indoor exit=23.0


def _resolve(**overrides):
    kwargs = dict(
        outdoor_temp_c=None,
        indoor_temp_c=None,
        outdoor_entry_c=_OUTDOOR_ENTRY,
        indoor_entry_c=_INDOOR_ENTRY,
        hysteresis_c=_HYST,
        previously_active=False,
    )
    kwargs.update(overrides)
    return resolve_heat_needed(**kwargs)


class TestDisabled:
    def test_both_thresholds_none_never_active(self) -> None:
        r = _resolve(outdoor_entry_c=None, indoor_entry_c=None, outdoor_temp_c=30.0, indoor_temp_c=30.0)
        assert r.active is False
        assert r.reason == REASON_DISABLED

    def test_disabled_even_if_previously_active(self) -> None:
        r = _resolve(outdoor_entry_c=None, indoor_entry_c=None, previously_active=True)
        assert r.active is False
        assert r.reason == REASON_DISABLED


class TestEntryBoundary:
    def test_outdoor_just_below_entry_no_fire(self) -> None:
        r = _resolve(outdoor_temp_c=25.9, indoor_temp_c=None, indoor_entry_c=None)
        assert r.active is False
        assert r.reason == REASON_NOT_NEEDED

    def test_outdoor_exactly_at_entry_fires(self) -> None:
        r = _resolve(outdoor_temp_c=26.0, indoor_temp_c=None, indoor_entry_c=None)
        assert r.active is True
        assert r.reason == REASON_ENTERED

    def test_outdoor_just_above_entry_fires(self) -> None:
        r = _resolve(outdoor_temp_c=26.1, indoor_temp_c=None, indoor_entry_c=None)
        assert r.active is True
        assert r.reason == REASON_ENTERED

    def test_indoor_just_below_entry_no_fire(self) -> None:
        r = _resolve(outdoor_temp_c=None, outdoor_entry_c=None, indoor_temp_c=23.9)
        assert r.active is False

    def test_indoor_exactly_at_entry_fires(self) -> None:
        r = _resolve(outdoor_temp_c=None, outdoor_entry_c=None, indoor_temp_c=24.0)
        assert r.active is True
        assert r.reason == REASON_ENTERED

    def test_indoor_just_above_entry_fires(self) -> None:
        r = _resolve(outdoor_temp_c=None, outdoor_entry_c=None, indoor_temp_c=24.1)
        assert r.active is True

    def test_or_logic_outdoor_alone_sufficient(self) -> None:
        r = _resolve(outdoor_temp_c=30.0, indoor_temp_c=10.0)
        assert r.active is True

    def test_or_logic_indoor_alone_sufficient(self) -> None:
        r = _resolve(outdoor_temp_c=10.0, indoor_temp_c=30.0)
        assert r.active is True

    def test_neither_meets_entry_no_fire(self) -> None:
        r = _resolve(outdoor_temp_c=20.0, indoor_temp_c=20.0)
        assert r.active is False


class TestActiveHeldByHysteresis:
    def test_value_between_entry_and_exit_stays_active(self) -> None:
        # Example from the ticket: entry 24.0 / exit 23.0, 23.6 stays active.
        r = _resolve(
            outdoor_temp_c=None, outdoor_entry_c=None,
            indoor_temp_c=23.6, previously_active=True,
        )
        assert r.active is True
        assert r.reason == REASON_HELD_BY_HYSTERESIS

    def test_repeated_calls_at_same_held_value_are_idempotent(self) -> None:
        state = True
        for _ in range(5):
            r = _resolve(
                outdoor_temp_c=None, outdoor_entry_c=None,
                indoor_temp_c=23.6, previously_active=state,
            )
            state = r.active
        assert state is True

    def test_short_fluctuation_within_band_never_exits(self) -> None:
        readings = [25.5, 25.2, 25.8, 25.1, 25.0]  # all within (23.0, 26.0)
        state = True
        for temp in readings:
            r = _resolve(outdoor_temp_c=temp, indoor_temp_c=None, indoor_entry_c=None, previously_active=state)
            state = r.active
            assert state is True

    def test_disabled_signal_never_blocks_exit(self) -> None:
        # Only outdoor enabled; indoor disabled (None) must not prevent exit.
        r = _resolve(
            outdoor_temp_c=24.9, indoor_temp_c=None, indoor_entry_c=None,
            previously_active=True,
        )
        assert r.active is False
        assert r.reason == REASON_EXITED


class TestExitBoundary:
    def test_exactly_at_exit_bound_still_held(self) -> None:
        # exit = entry - hysteresis = 23.0; strict "<" means 23.0 itself is held.
        r = _resolve(outdoor_temp_c=None, outdoor_entry_c=None, indoor_temp_c=23.0, previously_active=True)
        assert r.active is True
        assert r.reason == REASON_HELD_BY_HYSTERESIS

    def test_just_below_exit_releases(self) -> None:
        r = _resolve(outdoor_temp_c=None, outdoor_entry_c=None, indoor_temp_c=22.9, previously_active=True)
        assert r.active is False
        assert r.reason == REASON_EXITED

    def test_both_signals_must_drop_below_exit_to_release(self) -> None:
        # Outdoor still hot, indoor cooled — stays active (OR-entry, AND-exit).
        r = _resolve(outdoor_temp_c=25.5, indoor_temp_c=22.0, previously_active=True)
        assert r.active is True
        assert r.reason == REASON_HELD_BY_HYSTERESIS

    def test_both_below_exit_releases(self) -> None:
        r = _resolve(outdoor_temp_c=24.5, indoor_temp_c=22.0, previously_active=True)
        assert r.active is False
        assert r.reason == REASON_EXITED

    def test_re_entry_after_exit(self) -> None:
        r1 = _resolve(outdoor_temp_c=None, outdoor_entry_c=None, indoor_temp_c=22.5, previously_active=True)
        assert r1.active is False
        r2 = _resolve(outdoor_temp_c=None, outdoor_entry_c=None, indoor_temp_c=24.0, previously_active=r1.active)
        assert r2.active is True
        assert r2.reason == REASON_ENTERED


class TestMissingData:
    def test_missing_data_not_previously_active_no_fire(self) -> None:
        r = _resolve(outdoor_temp_c=None, indoor_temp_c=None, previously_active=False)
        assert r.active is False
        assert r.reason == REASON_INSUFFICIENT_DATA

    def test_missing_data_previously_active_holds(self) -> None:
        r = _resolve(outdoor_temp_c=None, indoor_temp_c=None, previously_active=True)
        assert r.active is True
        assert r.reason == REASON_HELD_MISSING_DATA

    def test_one_signal_missing_other_available_uses_available(self) -> None:
        r = _resolve(outdoor_temp_c=30.0, indoor_temp_c=None, previously_active=False)
        assert r.active is True

    def test_missing_signal_treated_as_still_hot_for_exit(self) -> None:
        # outdoor enabled but no reading this cycle; indoor cooled below exit.
        r = _resolve(outdoor_temp_c=None, indoor_temp_c=22.0, previously_active=True)
        assert r.active is True
        assert r.reason == REASON_HELD_BY_HYSTERESIS


class TestHysteresisZeroIsLegacyBehavior:
    def test_zero_hysteresis_releases_immediately_below_entry(self) -> None:
        r = _resolve(
            outdoor_temp_c=None, outdoor_entry_c=None,
            indoor_temp_c=23.9, hysteresis_c=0.0, previously_active=True,
        )
        assert r.active is False
        assert r.reason == REASON_EXITED

    def test_zero_hysteresis_matches_flat_threshold_at_boundary(self) -> None:
        r = _resolve(
            outdoor_temp_c=None, outdoor_entry_c=None,
            indoor_temp_c=24.0, hysteresis_c=0.0, previously_active=True,
        )
        assert r.active is True  # still >= entry, exit bound == entry here
