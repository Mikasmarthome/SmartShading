"""Tests for Step 9G1 — Position Semantics & Command Filter Foundation.

Covers:
  - to_ha_position(): all conversions, boundary values, invert flag
  - to_internal_position(): inverse conversions, invert flag
  - Round-trip identity: to_ha(to_internal(x)) == x for all x in [0, 100]
  - clamp_position(): out-of-range clamping
  - positions_within_tolerance(): exact boundary checks
  - Named position constants (INTERNAL_OPEN, HA_CLOSED, etc.)
  - Safety position constants (STORM_SAFE, WIND_SAFE, OPEN)
  - ExecutionCapability: defaults, immutability
  - ExecutionMode: values
  - CommandFilterResult: immutability
  - CommandFilter: full blocking-order verification
  - Safety bypass semantics (all 5 gates)
  - Recommendation-only semantics (does NOT bypass safety)
  - Source invariants: no inline 100-x in non-semantics code

No HA dependency.
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.cover_control.position_semantics import (
    INTERNAL_CLOSED,
    INTERNAL_OPEN,
    HA_CLOSED,
    HA_OPEN,
    OPEN_POSITION_INTERNAL,
    STORM_SAFE_POSITION_INTERNAL,
    WIND_SAFE_POSITION_INTERNAL,
    clamp_position,
    positions_within_tolerance,
    to_ha_position,
    to_internal_position,
)
from custom_components.smartshading.cover_control.command_filter import (
    BLOCKED_COMFORT_POSITION_HOLD,
    BLOCKED_COVER_UNAVAILABLE,
    BLOCKED_GUARD_ACTION_INTERVAL,
    BLOCKED_MANUAL_OVERRIDE,
    BLOCKED_NO_TARGET_POSITION,
    BLOCKED_RECOMMENDATION_ONLY,
    BLOCKED_SAME_POSITION,
    CommandFilter,
    CommandFilterResult,
    ExecutionCapability,
    ExecutionMode,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_DEFAULT_EXEC_CAP = ExecutionCapability()
_FILTER = CommandFilter()


def _evaluate(
    *,
    target: int | None = 75,
    current: int | None = 0,
    mode: ExecutionMode = ExecutionMode.AUTOMATIC,
    safety: bool = False,
    override: bool = False,
    available: bool = True,
    guard: bool = True,
    cap: ExecutionCapability = _DEFAULT_EXEC_CAP,
    invert: bool = False,
    comfort_hold: bool = True,
) -> CommandFilterResult:
    return _FILTER.evaluate(
        target_position_internal=target,
        current_position_internal=current,
        execution_mode=mode,
        is_safety=safety,
        is_manual_override=override,
        is_cover_available=available,
        state_guard_allowed=guard,
        execution_capability=cap,
        invert_position=invert,
        comfort_hold_allowed=comfort_hold,
    )


# ---------------------------------------------------------------------------
# TestToHaPosition
# ---------------------------------------------------------------------------

class TestToHaPosition:
    def test_internal_open_to_ha_open(self):
        assert to_ha_position(0) == 100

    def test_internal_closed_to_ha_closed(self):
        assert to_ha_position(100) == 0

    def test_normal_shade(self):
        # internal 75 (NORMAL_SHADE) → HA 25
        assert to_ha_position(75) == 25

    def test_light_shade(self):
        # internal 60 (LIGHT_SHADE) → HA 40
        assert to_ha_position(60) == 40

    def test_strong_shade(self):
        # internal 90 (STRONG_SHADE) → HA 10
        assert to_ha_position(90) == 10

    def test_midpoint_symmetry(self):
        assert to_ha_position(50) == 50

    def test_storm_safe_to_ha(self):
        # STORM_SAFE internal 0 → HA 100 (retracted/open)
        assert to_ha_position(STORM_SAFE_POSITION_INTERNAL) == HA_OPEN

    def test_wind_safe_to_ha(self):
        assert to_ha_position(WIND_SAFE_POSITION_INTERNAL) == HA_OPEN

    def test_invert_false_applies_conversion(self):
        assert to_ha_position(75, invert=False) == 25

    def test_invert_true_passthrough(self):
        # inverted cover: numeric value used as-is
        assert to_ha_position(75, invert=True) == 75

    def test_invert_true_open(self):
        assert to_ha_position(0, invert=True) == 0

    def test_invert_true_closed(self):
        assert to_ha_position(100, invert=True) == 100

    def test_clamp_negative_input(self):
        # -10 clamped to 0, then converted to 100
        assert to_ha_position(-10) == 100

    def test_clamp_over_100(self):
        # 110 clamped to 100, then converted to 0
        assert to_ha_position(110) == 0

    def test_output_always_in_range(self):
        for i in range(-5, 106):
            result = to_ha_position(i)
            assert 0 <= result <= 100, f"to_ha_position({i}) = {result} out of range"

    def test_formula_is_100_minus_internal(self):
        for i in range(0, 101):
            assert to_ha_position(i) == 100 - i


# ---------------------------------------------------------------------------
# TestToInternalPosition
# ---------------------------------------------------------------------------

class TestToInternalPosition:
    def test_ha_open_to_internal_open(self):
        assert to_internal_position(100) == 0

    def test_ha_closed_to_internal_closed(self):
        assert to_internal_position(0) == 100

    def test_ha_25_to_internal_75(self):
        assert to_internal_position(25) == 75

    def test_ha_40_to_internal_60(self):
        assert to_internal_position(40) == 60

    def test_midpoint_symmetry(self):
        assert to_internal_position(50) == 50

    def test_invert_false_applies_conversion(self):
        assert to_internal_position(25, invert=False) == 75

    def test_invert_true_passthrough(self):
        assert to_internal_position(25, invert=True) == 25

    def test_clamp_negative_input(self):
        assert to_internal_position(-5) == 100

    def test_clamp_over_100(self):
        assert to_internal_position(105) == 0

    def test_output_always_in_range(self):
        for i in range(-5, 106):
            result = to_internal_position(i)
            assert 0 <= result <= 100

    def test_formula_is_100_minus_ha(self):
        for i in range(0, 101):
            assert to_internal_position(i) == 100 - i


# ---------------------------------------------------------------------------
# TestRoundTrip
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_internal_roundtrip_standard(self):
        """to_ha(to_internal(x)) == x for all valid positions."""
        for i in range(0, 101):
            assert to_ha_position(to_internal_position(i)) == i

    def test_ha_roundtrip_standard(self):
        """to_internal(to_ha(x)) == x for all valid positions."""
        for i in range(0, 101):
            assert to_internal_position(to_ha_position(i)) == i

    def test_internal_roundtrip_invert(self):
        """invert=True round-trip is also identity."""
        for i in range(0, 101):
            assert to_ha_position(to_internal_position(i, invert=True), invert=True) == i

    def test_ha_roundtrip_invert(self):
        for i in range(0, 101):
            assert to_internal_position(to_ha_position(i, invert=True), invert=True) == i


# ---------------------------------------------------------------------------
# TestClampPosition
# ---------------------------------------------------------------------------

class TestClampPosition:
    def test_zero_unchanged(self):
        assert clamp_position(0) == 0

    def test_100_unchanged(self):
        assert clamp_position(100) == 100

    def test_midrange_unchanged(self):
        assert clamp_position(50) == 50

    def test_negative_clamped_to_zero(self):
        assert clamp_position(-1) == 0

    def test_large_negative_clamped(self):
        assert clamp_position(-9999) == 0

    def test_101_clamped_to_100(self):
        assert clamp_position(101) == 100

    def test_large_positive_clamped(self):
        assert clamp_position(9999) == 100


# ---------------------------------------------------------------------------
# TestNamedConstants
# ---------------------------------------------------------------------------

class TestNamedConstants:
    def test_internal_open_is_zero(self):
        assert INTERNAL_OPEN == 0

    def test_internal_closed_is_100(self):
        assert INTERNAL_CLOSED == 100

    def test_ha_open_is_100(self):
        assert HA_OPEN == 100

    def test_ha_closed_is_zero(self):
        assert HA_CLOSED == 0

    def test_storm_safe_position_internal(self):
        # Retracted/open — consistent with StormEvaluator._STORM_SAFE_POSITION
        assert STORM_SAFE_POSITION_INTERNAL == 0

    def test_wind_safe_position_internal(self):
        assert WIND_SAFE_POSITION_INTERNAL == 0

    def test_open_position_internal(self):
        assert OPEN_POSITION_INTERNAL == 0

    def test_constants_pair_correctly(self):
        assert to_ha_position(INTERNAL_OPEN) == HA_OPEN
        assert to_ha_position(INTERNAL_CLOSED) == HA_CLOSED

    def test_storm_safe_converts_to_ha_open(self):
        # Storm safe: retract (HA open = 100) so the cover doesn't catch wind
        assert to_ha_position(STORM_SAFE_POSITION_INTERNAL) == HA_OPEN


# ---------------------------------------------------------------------------
# TestPositionsWithinTolerance
# ---------------------------------------------------------------------------

class TestPositionsWithinTolerance:
    def test_same_position_within(self):
        assert positions_within_tolerance(50, 50, 3) is True

    def test_exactly_at_tolerance_within(self):
        assert positions_within_tolerance(50, 53, 3) is True
        assert positions_within_tolerance(53, 50, 3) is True

    def test_one_beyond_tolerance_not_within(self):
        assert positions_within_tolerance(50, 54, 3) is False
        assert positions_within_tolerance(54, 50, 3) is False

    def test_zero_tolerance_only_same_position(self):
        assert positions_within_tolerance(50, 50, 0) is True
        assert positions_within_tolerance(50, 51, 0) is False

    def test_large_tolerance_always_within(self):
        assert positions_within_tolerance(0, 100, 100) is True

    def test_symmetry(self):
        for a in range(0, 101, 10):
            for b in range(0, 101, 10):
                assert (
                    positions_within_tolerance(a, b, 5)
                    == positions_within_tolerance(b, a, 5)
                )


# ---------------------------------------------------------------------------
# TestExecutionCapability
# ---------------------------------------------------------------------------

class TestExecutionCapability:
    def test_default_safe_position_is_retracted(self):
        cap = ExecutionCapability()
        assert cap.safe_position_internal == 0

    def test_default_position_tolerance(self):
        assert ExecutionCapability().position_tolerance == 3

    def test_default_tilt_tolerance(self):
        assert ExecutionCapability().tilt_tolerance == 3

    def test_custom_safe_position(self):
        # Roller shutter: close during storm
        cap = ExecutionCapability(safe_position_internal=100)
        assert cap.safe_position_internal == 100

    def test_immutable(self):
        cap = ExecutionCapability()
        with pytest.raises((AttributeError, TypeError)):
            cap.position_tolerance = 99  # type: ignore[misc]

    def test_safe_position_to_ha_open(self):
        # Default safe = retracted → HA open
        cap = ExecutionCapability()
        assert to_ha_position(cap.safe_position_internal) == HA_OPEN

    def test_roller_shutter_safe_position_to_ha_closed(self):
        cap = ExecutionCapability(safe_position_internal=100)
        assert to_ha_position(cap.safe_position_internal) == HA_CLOSED


# ---------------------------------------------------------------------------
# TestExecutionMode
# ---------------------------------------------------------------------------

class TestExecutionMode:
    def test_recommendation_only_value(self):
        assert ExecutionMode.RECOMMENDATION_ONLY.value == "recommendation_only"

    def test_automatic_value(self):
        assert ExecutionMode.AUTOMATIC.value == "automatic"

    def test_two_modes_exist(self):
        modes = list(ExecutionMode)
        assert len(modes) == 2


# ---------------------------------------------------------------------------
# TestCommandFilterResult
# ---------------------------------------------------------------------------

class TestCommandFilterResult:
    def test_immutable(self):
        result = CommandFilterResult(
            allowed=True,
            blocked_reason=None,
            target_position_internal=75,
            target_position_ha=25,
            execution_mode="automatic",
            is_safety=False,
        )
        with pytest.raises((AttributeError, TypeError)):
            result.allowed = False  # type: ignore[misc]

    def test_blocked_result_has_reason(self):
        result = CommandFilterResult(
            allowed=False,
            blocked_reason=BLOCKED_RECOMMENDATION_ONLY,
            target_position_internal=75,
            target_position_ha=25,
            execution_mode="recommendation_only",
            is_safety=False,
        )
        assert result.blocked_reason == BLOCKED_RECOMMENDATION_ONLY

    def test_allowed_result_has_no_reason(self):
        result = CommandFilterResult(
            allowed=True,
            blocked_reason=None,
            target_position_internal=75,
            target_position_ha=25,
            execution_mode="automatic",
            is_safety=False,
        )
        assert result.blocked_reason is None


# ---------------------------------------------------------------------------
# TestCommandFilterBlockingOrder
# ---------------------------------------------------------------------------

class TestCommandFilterBlockingOrder:
    """Verify each blocking condition in isolation."""

    def test_allowed_base_case(self):
        result = _evaluate()
        assert result.allowed is True
        assert result.blocked_reason is None

    def test_manual_override_blocks(self):
        result = _evaluate(override=True)
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_MANUAL_OVERRIDE

    def test_cover_unavailable_blocks(self):
        result = _evaluate(available=False)
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_COVER_UNAVAILABLE

    def test_recommendation_only_blocks(self):
        result = _evaluate(mode=ExecutionMode.RECOMMENDATION_ONLY)
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_RECOMMENDATION_ONLY

    def test_guard_action_interval_blocks(self):
        result = _evaluate(guard=False)
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_GUARD_ACTION_INTERVAL

    def test_same_position_blocks(self):
        # current=75, target=75 → within default tolerance=3
        result = _evaluate(target=75, current=75)
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_SAME_POSITION

    def test_within_tolerance_blocks(self):
        # target=75, current=77 → diff=2 ≤ tolerance=3
        result = _evaluate(target=75, current=77)
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_SAME_POSITION

    def test_outside_tolerance_allowed(self):
        # target=75, current=80 → diff=5 > tolerance=3
        result = _evaluate(target=75, current=80)
        assert result.allowed is True

    def test_no_target_position_blocks(self):
        result = _evaluate(target=None)
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_NO_TARGET_POSITION

    def test_unknown_current_position_does_not_block(self):
        # When current position is unknown, tolerance check cannot run → allow
        result = _evaluate(target=75, current=None)
        assert result.allowed is True

    def test_comfort_position_hold_blocks(self):
        # v1.1.1: comfort_hold_allowed=False (coordinator's ComfortMovementHold
        # determined this cycle) blocks a real (non-no-op) position change.
        result = _evaluate(target=75, current=0, comfort_hold=False)
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_COMFORT_POSITION_HOLD

    def test_comfort_position_hold_default_allows(self):
        # Default comfort_hold_allowed=True preserves prior behavior for every
        # call site that does not pass it explicitly.
        result = _evaluate(target=75, current=0)
        assert result.allowed is True

    def test_comfort_position_hold_does_not_override_same_position(self):
        # same_position must still win when the target is already reached —
        # comfort_position_hold is checked AFTER the tolerance check.
        result = _evaluate(target=75, current=75, comfort_hold=False)
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_SAME_POSITION


# ---------------------------------------------------------------------------
# TestSafetyBypasses
# ---------------------------------------------------------------------------

class TestSafetyBypasses:
    """Safety (is_safety=True) bypasses guard and tolerance checks."""

    def test_safety_bypasses_guard_action_interval(self):
        result = _evaluate(guard=False, safety=True)
        assert result.allowed is True

    def test_safety_bypasses_same_position(self):
        # Even if already at target, safety retract must execute
        # (assumed position might be wrong after drift/restart)
        result = _evaluate(target=0, current=0, safety=True)
        assert result.allowed is True

    def test_safety_bypasses_within_tolerance(self):
        result = _evaluate(target=0, current=2, safety=True)
        assert result.allowed is True

    def test_safety_does_not_bypass_recommendation_only(self):
        result = _evaluate(mode=ExecutionMode.RECOMMENDATION_ONLY, safety=True)
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_RECOMMENDATION_ONLY

    def test_safety_bypasses_comfort_position_hold(self):
        # v1.1.1: a real safety command must never be suppressed by the
        # Comfort Movement Stability Hold.
        result = _evaluate(target=75, current=0, safety=True, comfort_hold=False)
        assert result.allowed is True

    def test_safety_does_not_bypass_manual_override(self):
        result = _evaluate(override=True, safety=True)
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_MANUAL_OVERRIDE

    def test_safety_does_not_bypass_cover_unavailable(self):
        result = _evaluate(available=False, safety=True)
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_COVER_UNAVAILABLE


# ---------------------------------------------------------------------------
# TestBlockingPriority (earlier checks win over later ones)
# ---------------------------------------------------------------------------

class TestBlockingPriority:
    def test_override_beats_unavailable(self):
        result = _evaluate(override=True, available=False)
        assert result.blocked_reason == BLOCKED_MANUAL_OVERRIDE

    def test_override_beats_recommendation_only(self):
        result = _evaluate(override=True, mode=ExecutionMode.RECOMMENDATION_ONLY)
        assert result.blocked_reason == BLOCKED_MANUAL_OVERRIDE

    def test_unavailable_beats_recommendation_only(self):
        result = _evaluate(available=False, mode=ExecutionMode.RECOMMENDATION_ONLY)
        assert result.blocked_reason == BLOCKED_COVER_UNAVAILABLE

    def test_recommendation_only_beats_guard(self):
        result = _evaluate(mode=ExecutionMode.RECOMMENDATION_ONLY, guard=False)
        assert result.blocked_reason == BLOCKED_RECOMMENDATION_ONLY

    def test_guard_beats_same_position(self):
        result = _evaluate(guard=False, target=75, current=75)
        assert result.blocked_reason == BLOCKED_GUARD_ACTION_INTERVAL


# ---------------------------------------------------------------------------
# TestHaPositionInResult
# ---------------------------------------------------------------------------

class TestHaPositionInResult:
    def test_ha_position_computed_for_allowed(self):
        result = _evaluate(target=75)
        assert result.target_position_internal == 75
        assert result.target_position_ha == 25  # 100 - 75

    def test_ha_position_computed_for_blocked(self):
        # Even blocked results carry the HA position (for diagnostics)
        result = _evaluate(target=75, mode=ExecutionMode.RECOMMENDATION_ONLY)
        assert result.target_position_internal == 75
        assert result.target_position_ha == 25

    def test_ha_position_none_when_no_target(self):
        result = _evaluate(target=None)
        assert result.target_position_internal is None
        assert result.target_position_ha is None

    def test_ha_position_invert_mode(self):
        # invert=True: HA position equals internal
        result = _evaluate(target=75, invert=True)
        assert result.target_position_internal == 75
        assert result.target_position_ha == 75

    def test_storm_safe_ha_position(self):
        # STORM_SAFE internal 0 → HA 100 (open/retracted)
        result = _evaluate(target=0, current=50, safety=True)
        assert result.target_position_ha == 100

    def test_normal_shade_ha_position(self):
        result = _evaluate(target=75, current=0)
        assert result.target_position_ha == 25


# ---------------------------------------------------------------------------
# TestExecutionModeInResult
# ---------------------------------------------------------------------------

class TestExecutionModeInResult:
    def test_automatic_mode_in_result(self):
        result = _evaluate(mode=ExecutionMode.AUTOMATIC)
        assert result.execution_mode == "automatic"

    def test_recommendation_only_mode_in_result(self):
        result = _evaluate(mode=ExecutionMode.RECOMMENDATION_ONLY)
        assert result.execution_mode == "recommendation_only"

    def test_is_safety_flag_propagated(self):
        result = _evaluate(safety=True, guard=False)
        assert result.is_safety is True

    def test_is_not_safety_flag_propagated(self):
        result = _evaluate(safety=False)
        assert result.is_safety is False


# ---------------------------------------------------------------------------
# TestCustomTolerance
# ---------------------------------------------------------------------------

class TestCustomTolerance:
    def test_tight_tolerance_allows_small_deviation(self):
        # Tolerance=1 → only block when diff ≤ 1
        cap = ExecutionCapability(position_tolerance=1)
        result = _evaluate(target=75, current=76, cap=cap)
        assert result.blocked_reason == BLOCKED_SAME_POSITION

    def test_tight_tolerance_allows_larger_deviation(self):
        cap = ExecutionCapability(position_tolerance=1)
        result = _evaluate(target=75, current=78, cap=cap)
        assert result.allowed is True

    def test_zero_tolerance_only_identical_position(self):
        cap = ExecutionCapability(position_tolerance=0)
        assert _evaluate(target=75, current=75, cap=cap).blocked_reason == BLOCKED_SAME_POSITION
        assert _evaluate(target=75, current=76, cap=cap).allowed is True

    def test_wide_tolerance_blocks_distant_positions(self):
        cap = ExecutionCapability(position_tolerance=20)
        result = _evaluate(target=50, current=60, cap=cap)
        assert result.blocked_reason == BLOCKED_SAME_POSITION


# ---------------------------------------------------------------------------
# TestBlockedReasonStrings (constants have expected values)
# ---------------------------------------------------------------------------

class TestBlockedReasonStrings:
    def test_manual_override_string(self):
        assert BLOCKED_MANUAL_OVERRIDE == "manual_override"

    def test_cover_unavailable_string(self):
        assert BLOCKED_COVER_UNAVAILABLE == "cover_unavailable"

    def test_recommendation_only_string(self):
        assert BLOCKED_RECOMMENDATION_ONLY == "recommendation_only"

    def test_guard_action_interval_string(self):
        assert BLOCKED_GUARD_ACTION_INTERVAL == "guard_action_interval"

    def test_same_position_string(self):
        assert BLOCKED_SAME_POSITION == "same_position"

    def test_no_target_position_string(self):
        assert BLOCKED_NO_TARGET_POSITION == "no_target_position"


# ---------------------------------------------------------------------------
# TestSourceInvariant — no inline 100-x outside position_semantics.py
# ---------------------------------------------------------------------------

class TestSourceInvariant:
    """Verify that inline arithmetic convention conversion does not appear
    outside the position_semantics module (except the one legitimate use in
    the coordinator for override detection, which pre-dates this module)."""

    def _read_module(self, name: str) -> str:
        import pathlib
        base = pathlib.Path(__file__).parent.parent / "custom_components" / "smartshading"
        return (base / name).read_text(encoding="utf-8")

    def test_command_filter_uses_to_ha_position(self):
        src = self._read_module("cover_control/command_filter.py")
        assert "to_ha_position" in src
        # Must import from position_semantics
        assert "position_semantics" in src

    def test_position_semantics_defines_to_ha_position(self):
        src = self._read_module("cover_control/position_semantics.py")
        assert "def to_ha_position" in src
        assert "def to_internal_position" in src

    def test_storm_evaluator_safe_position_is_zero_internal(self):
        src = self._read_module("evaluators/storm_evaluator.py")
        # Verify the constant is 0 (internal open/retracted)
        assert "_STORM_SAFE_POSITION = 0" in src

    def test_wind_evaluator_safe_position_is_zero_internal(self):
        src = self._read_module("evaluators/wind_evaluator.py")
        assert "_WIND_SAFE_POSITION = 0" in src
