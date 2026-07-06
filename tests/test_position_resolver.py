"""Tests for PositionResolver.

Contract:
  - max(target_position) wins — the most-shaded candidate is returned.
  - None entries in the input sequence are silently ignored.
  - WindowDecision entries with target_position=None are ignored.
  - Returns None when no candidate provides a concrete position.
  - Tie-breaking: first entry in the sequence wins (Python max() stability).
  - No HA-convention conversion: all positions are internal (0=open, 100=shaded).

Canonical scenario names used in user spec:
  "Absence 50 + Solar 70 → Solar gewinnt"
  "Absence 70 + Solar 50 → Absence gewinnt"
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.evaluators.position_resolver import PositionResolver
from custom_components.smartshading.models.window_decision import WindowDecision
from custom_components.smartshading.state_machine.states import ShadingState


# ---------------------------------------------------------------------------
# Helper factory — avoids repeating boilerplate in every test
# ---------------------------------------------------------------------------

def _decision(position: int | None, state: ShadingState, decided_by: str = "Test") -> WindowDecision:
    return WindowDecision(
        window_id="w1",
        shading_state=state,
        target_position=position,
        decided_by=decided_by,
    )


def _absence(position: int) -> WindowDecision:
    return _decision(position, ShadingState.ABSENCE_CLOSED, decided_by="AbsenceEvaluator")


def _solar(state: ShadingState, position: int) -> WindowDecision:
    return _decision(position, state, decided_by="SolarEvaluator")


# ---------------------------------------------------------------------------
# Core spec scenarios (from user requirement)
# ---------------------------------------------------------------------------

class TestPositionResolverCoreScenarios:
    def test_absence_50_solar_70_solar_wins(self) -> None:
        """Absence 50 + Solar 70 → Solar gewinnt."""
        result = PositionResolver.resolve([
            _absence(50),
            _solar(ShadingState.NORMAL_SHADE, 70),
        ])
        assert result is not None
        assert result.target_position == 70
        assert result.decided_by == "SolarEvaluator"

    def test_absence_70_solar_50_absence_wins(self) -> None:
        """Absence 70 + Solar 50 → Absence gewinnt."""
        result = PositionResolver.resolve([
            _absence(70),
            _solar(ShadingState.LIGHT_SHADE, 50),
        ])
        assert result is not None
        assert result.target_position == 70
        assert result.decided_by == "AbsenceEvaluator"

    def test_none_floors_are_ignored(self) -> None:
        """None entries (inactive tiers) must not interfere."""
        result = PositionResolver.resolve([
            None,
            _solar(ShadingState.LIGHT_SHADE, 60),
            None,
        ])
        assert result is not None
        assert result.target_position == 60

    def test_all_none_returns_none(self) -> None:
        """All-None input → None (TierOrchestrator interprets as OPEN)."""
        result = PositionResolver.resolve([None, None, None])
        assert result is None

    def test_empty_sequence_returns_none(self) -> None:
        result = PositionResolver.resolve([])
        assert result is None


# ---------------------------------------------------------------------------
# Tie-breaking
# ---------------------------------------------------------------------------

class TestPositionResolverTieBreaking:
    def test_equal_positions_first_wins(self) -> None:
        """Tie-breaking: first candidate in sequence wins (Python max() stability)."""
        first = _decision(70, ShadingState.ABSENCE_CLOSED, decided_by="AbsenceEvaluator")
        second = _decision(70, ShadingState.NORMAL_SHADE, decided_by="SolarEvaluator")
        result = PositionResolver.resolve([first, second])
        assert result is not None
        assert result.target_position == 70
        assert result.decided_by == "AbsenceEvaluator"

    def test_equal_positions_reversed_order(self) -> None:
        """Tie-breaking: first entry wins — reversed order picks the other."""
        first = _decision(70, ShadingState.NORMAL_SHADE, decided_by="SolarEvaluator")
        second = _decision(70, ShadingState.ABSENCE_CLOSED, decided_by="AbsenceEvaluator")
        result = PositionResolver.resolve([first, second])
        assert result is not None
        assert result.decided_by == "SolarEvaluator"


# ---------------------------------------------------------------------------
# target_position=None entries are ignored
# ---------------------------------------------------------------------------

class TestPositionResolverNonePosition:
    def test_none_target_position_is_skipped(self) -> None:
        """A WindowDecision with target_position=None is treated as inactive."""
        no_pos = _decision(None, ShadingState.OPEN, decided_by="SomeEvaluator")
        real = _solar(ShadingState.LIGHT_SHADE, 60)
        result = PositionResolver.resolve([no_pos, real])
        assert result is not None
        assert result.target_position == 60

    def test_all_none_positions_returns_none(self) -> None:
        d1 = _decision(None, ShadingState.OPEN)
        d2 = _decision(None, ShadingState.OPEN)
        assert PositionResolver.resolve([d1, d2]) is None


# ---------------------------------------------------------------------------
# Three Tier 4 floors + one Tier 5 (realistic multi-evaluator scenario)
# ---------------------------------------------------------------------------

class TestPositionResolverMultipleTiers:
    def test_three_floors_highest_wins(self) -> None:
        """Three active Tier 4 floors: the highest position wins."""
        result = PositionResolver.resolve([
            _absence(70),                              # Tier 4a
            _decision(85, ShadingState.ABSENCE_CLOSED, "HeatEvaluator"),   # Tier 4b (future)
            _decision(65, ShadingState.ABSENCE_CLOSED, "GlareEvaluator"),  # Tier 4c (future)
        ])
        assert result is not None
        assert result.target_position == 85
        assert result.decided_by == "HeatEvaluator"

    def test_tier5_higher_than_all_tier4_floors(self) -> None:
        result = PositionResolver.resolve([
            _absence(70),                              # Tier 4 floor
            _solar(ShadingState.STRONG_SHADE, 90),    # Tier 5
        ])
        assert result is not None
        assert result.target_position == 90

    def test_tier4_floor_higher_than_tier5(self) -> None:
        result = PositionResolver.resolve([
            _absence(80),                              # Tier 4 floor
            _solar(ShadingState.LIGHT_SHADE, 60),     # Tier 5
        ])
        assert result is not None
        assert result.target_position == 80

    def test_single_candidate_is_returned(self) -> None:
        d = _solar(ShadingState.NORMAL_SHADE, 75)
        assert PositionResolver.resolve([d]) is d


# ---------------------------------------------------------------------------
# No HA convention conversion (positions are already internal)
# ---------------------------------------------------------------------------

class TestPositionResolverNoHaConversion:
    def test_position_100_means_fully_shaded(self) -> None:
        """Internal 100 = fully shaded — PositionResolver does NOT subtract from 100."""
        d = _decision(100, ShadingState.NIGHT_CLOSED, "NightEvaluator")
        result = PositionResolver.resolve([d])
        assert result is not None
        assert result.target_position == 100  # not 0

    def test_position_0_means_open(self) -> None:
        """Internal 0 = open — still a valid position candidate."""
        d = _decision(0, ShadingState.OPEN, "SomeEvaluator")
        result = PositionResolver.resolve([d])
        assert result is not None
        assert result.target_position == 0
