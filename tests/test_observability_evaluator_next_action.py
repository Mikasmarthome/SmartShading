"""build_next_action() must never crash the coordinator — v1.1.2 field fix.

Real-world crash (Option B / Night Contact, office/north window, window
opened at night):

    File ".../coordinator.py", line 4146, in _async_update_data
        next_action=build_next_action(new_state, current_state, self.shade_position_defaults),
    File ".../engines/observability_evaluator.py", line 77, in build_next_action
        }[new_state]
    KeyError: <ShadingState.NIGHT_VENT: 'night_vent'>

Root cause: Night Contact Option B correctly produces ShadingState.NIGHT_VENT
(engines/night_contact_hold.py), but build_next_action()'s display-only
action-label mapping never included NIGHT_VENT (or RAIN_SAFE — also a real
Tier-1 safety state missing from the same dict). A dict[[...]][new_state]
lookup with no matching key raises KeyError, which propagates out of
_async_update_data() and crashes the whole coordinator refresh — every
SmartShading entity in that zone goes unavailable in Home Assistant.

build_next_action() is documented as "display-only ... no command is ever
sent in this phase" (see WindowObservation.next_action, a diagnostic
sensor-attribute field only) — it must never be able to crash real dispatch
by construction. The v1.1.2 fix maps NIGHT_VENT and RAIN_SAFE explicitly and
replaces the raw dict[key] lookup with a safe fallback for any future/unknown
ShadingState.

Not related to ComfortMovementHold: the comfort hold only gates whether a
CommandFilter intent is dispatched; it never touches new_state or
build_next_action(), and this crash happens whether or not a comfort hold is
active.
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.engines.observability_evaluator import (
    build_next_action,
    build_reason,
)
from custom_components.smartshading.models.config import ShadePositionDefaults
from custom_components.smartshading.state_machine.states import ShadingState

_DEFAULTS = ShadePositionDefaults()

# Every state currently defined on ShadingState — kept as an explicit tuple
# (not `list(ShadingState)`) so a newly-added enum member is caught by
# test_all_shading_states_are_explicitly_enumerated_here below, forcing this
# test file to be updated deliberately rather than silently passing.
_ALL_STATES_AT_TIME_OF_WRITING = (
    ShadingState.STORM_SAFE,
    ShadingState.WIND_SAFE,
    ShadingState.RAIN_SAFE,
    ShadingState.MANUAL_OVERRIDE,
    ShadingState.NIGHT_CLOSED,
    ShadingState.NIGHT_VENT,
    ShadingState.ABSENCE_CLOSED,
    ShadingState.STRONG_SHADE,
    ShadingState.NORMAL_SHADE,
    ShadingState.LIGHT_SHADE,
    ShadingState.OPEN,
)


class TestAllShadingStatesEnumerated:
    def test_all_shading_states_are_explicitly_enumerated_here(self):
        # If this fails, a new ShadingState was added — update
        # _ALL_STATES_AT_TIME_OF_WRITING above AND verify build_next_action()
        # (and build_reason()) handle it before relying on the safe fallback.
        assert set(ShadingState) == set(_ALL_STATES_AT_TIME_OF_WRITING)


class TestBuildNextActionDoesNotCrash:
    """The exact regression: every real ShadingState must produce a string,
    never raise, regardless of current_state."""

    @pytest.mark.parametrize("state", _ALL_STATES_AT_TIME_OF_WRITING)
    def test_every_state_from_open_current_does_not_raise(self, state):
        result = build_next_action(state, ShadingState.OPEN, _DEFAULTS)
        assert isinstance(result, str)
        assert result != ""

    @pytest.mark.parametrize("state", _ALL_STATES_AT_TIME_OF_WRITING)
    def test_every_state_as_current_and_new_does_not_raise(self, state):
        # new_state == current_state is the NO_ACTION short-circuit — cover
        # every state in both roles.
        result = build_next_action(state, state, _DEFAULTS)
        assert result == "NO_ACTION"


class TestNightVentMapping:
    def test_night_vent_does_not_raise_the_reported_keyerror(self):
        # The exact crash from the field report.
        result = build_next_action(
            ShadingState.NIGHT_VENT, ShadingState.NIGHT_CLOSED, _DEFAULTS
        )
        assert result == "MOVE_TO_NIGHT_VENT"

    def test_night_vent_from_open_current(self):
        result = build_next_action(ShadingState.NIGHT_VENT, ShadingState.OPEN, _DEFAULTS)
        assert result == "MOVE_TO_NIGHT_VENT"

    def test_return_to_night_closed_from_night_vent_does_not_raise(self):
        # The Option B return-to-night direction (NIGHT_VENT -> NIGHT_CLOSED)
        # must also be safe.
        result = build_next_action(
            ShadingState.NIGHT_CLOSED, ShadingState.NIGHT_VENT, _DEFAULTS
        )
        assert result == "MOVE_TO_0"


class TestRainSafeMapping:
    def test_rain_safe_does_not_raise(self):
        # RAIN_SAFE was the second real state missing from the mapping.
        result = build_next_action(ShadingState.RAIN_SAFE, ShadingState.OPEN, _DEFAULTS)
        assert result == "MOVE_TO_0"

    def test_rain_safe_matches_storm_and_wind_safe_style(self):
        # All three Tier-1 safety states are simplified identically here
        # (this function has no hardware-type input to compute a real
        # per-cover safe position — same limitation as before this fix).
        storm = build_next_action(ShadingState.STORM_SAFE, ShadingState.OPEN, _DEFAULTS)
        wind = build_next_action(ShadingState.WIND_SAFE, ShadingState.OPEN, _DEFAULTS)
        rain = build_next_action(ShadingState.RAIN_SAFE, ShadingState.OPEN, _DEFAULTS)
        assert storm == wind == rain == "MOVE_TO_0"


class TestUnknownStateFallbackIsSafe:
    """A future ShadingState added without updating this file's mapping must
    degrade to a safe, informative label — never raise."""

    def test_unmapped_enum_like_object_falls_back_without_raising(self):
        class _FutureShadingState:
            value = "future_state"

        result = build_next_action(_FutureShadingState(), ShadingState.OPEN, _DEFAULTS)
        assert result == "UNKNOWN_STATE:future_state"

    def test_fallback_never_raises_even_without_value_attribute(self):
        result = build_next_action("not_a_real_state", ShadingState.OPEN, _DEFAULTS)
        assert result.startswith("UNKNOWN_STATE:")


class TestBuildReasonAlreadySafeForAllStates:
    """build_reason() uses an if/elif chain with a final catch-all return —
    unlike the old build_next_action() dict lookup, it was never at risk of
    KeyError. Covered here so both display-only helpers have regression
    coverage against the same class of crash."""

    @pytest.mark.parametrize("state", _ALL_STATES_AT_TIME_OF_WRITING)
    def test_every_state_produces_a_reason_without_raising(self, state):
        reason, reason_code = build_reason(state, comfort=None)
        assert isinstance(reason, str) and reason
        assert isinstance(reason_code, str) and reason_code

    def test_night_vent_reason_does_not_raise(self):
        # NIGHT_VENT has no explicit branch in build_reason() — it falls
        # through to the NO_SUN/HYSTERESIS_EXIT default, which is safe but
        # not very descriptive. Documented here, not changed (out of scope
        # for this crash fix — build_reason never crashed).
        reason, reason_code = build_reason(ShadingState.NIGHT_VENT, comfort=None)
        assert isinstance(reason, str)
        assert isinstance(reason_code, str)
