"""Tests for cover_control/dispatch_orchestrator.py — pure wait-computation
for the T11 dispatch strategy (v1.2.0-beta.1).

Coverage:
  ORC-01  PARALLEL: no start-interval regardless of zone-group position.
  ORC-02  SPACED: reproduces the pre-T11 fixed-interval behavior exactly at
          the default (2.0s).
  ORC-03  SPACED: always reports the configured interval (remainder
          subtraction against elapsed time is the coordinator call site's
          responsibility, not this module's — see effective_interval_s()
          docstring).
  ORC-04  SEQUENTIAL: no interval outside a zone boundary (its gating is the
          completion wait, computed separately).
  ORC-05  zone_batching forces start_interval_s at a zone boundary
          regardless of mode (including PARALLEL/SEQUENTIAL).
  ORC-06  zone_batching does NOT add an interval for non-boundary intents.
  ORC-07  requires_completion_wait is True only for SEQUENTIAL.
  ORC-08  Invalid/edge interval values (0) behave sanely.

T17: resolve_pre_dispatch_wait() was removed as confirmed dead code (zero
callers anywhere — the coordinator's real dispatch loop computes the
remainder-against-elapsed-time subtraction inline instead of calling this
wrapper). Its distinct coverage (remainder subtraction, first-dispatch-ever
immediacy) tested a code path that never actually ran in production.
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.cover_control.dispatch_orchestrator import (
    effective_interval_s,
    requires_completion_wait,
)
from custom_components.smartshading.models.dispatch_config import (
    DispatchConfig,
    DispatchMode,
)


class TestParallelMode:
    def test_no_interval_regardless_of_zone_group_position(self) -> None:
        config = DispatchConfig(mode=DispatchMode.PARALLEL)
        assert effective_interval_s(config, is_first_in_zone_group=False) == 0.0
        assert effective_interval_s(config, is_first_in_zone_group=True) == 0.0


class TestSpacedMode:
    def test_default_reproduces_pre_t11_interval(self) -> None:
        config = DispatchConfig()  # SPACED, 2.0s default
        assert config.mode is DispatchMode.SPACED
        assert config.start_interval_s == 2.0

    def test_interval_matches_configured_value(self) -> None:
        config = DispatchConfig(mode=DispatchMode.SPACED, start_interval_s=5.0)
        assert effective_interval_s(config, is_first_in_zone_group=False) == 5.0


class TestSequentialMode:
    def test_no_interval_without_zone_boundary(self) -> None:
        config = DispatchConfig(mode=DispatchMode.SEQUENTIAL, start_interval_s=5.0)
        assert effective_interval_s(config, is_first_in_zone_group=False) == 0.0

    def test_requires_completion_wait_true(self) -> None:
        config = DispatchConfig(mode=DispatchMode.SEQUENTIAL)
        assert requires_completion_wait(config) is True


class TestRequiresCompletionWaitOnlySequential:
    @pytest.mark.parametrize("mode", [DispatchMode.PARALLEL, DispatchMode.SPACED])
    def test_false_for_non_sequential(self, mode: DispatchMode) -> None:
        assert requires_completion_wait(DispatchConfig(mode=mode)) is False


class TestZoneBatching:
    @pytest.mark.parametrize("mode", list(DispatchMode))
    def test_forces_interval_at_zone_boundary_regardless_of_mode(
        self, mode: DispatchMode
    ) -> None:
        config = DispatchConfig(mode=mode, start_interval_s=3.0, zone_batching=True)
        assert effective_interval_s(config, is_first_in_zone_group=True) == 3.0

    @pytest.mark.parametrize("mode", [DispatchMode.PARALLEL, DispatchMode.SEQUENTIAL])
    def test_no_extra_interval_for_non_boundary_intent(
        self, mode: DispatchMode
    ) -> None:
        config = DispatchConfig(mode=mode, start_interval_s=3.0, zone_batching=True)
        assert effective_interval_s(config, is_first_in_zone_group=False) == 0.0

    def test_disabled_zone_batching_no_boundary_interval(self) -> None:
        config = DispatchConfig(mode=DispatchMode.PARALLEL, start_interval_s=3.0, zone_batching=False)
        assert effective_interval_s(config, is_first_in_zone_group=True) == 0.0


class TestNeverNegativeInterval:
    def test_effective_interval_never_negative(self) -> None:
        config = DispatchConfig(mode=DispatchMode.SPACED, start_interval_s=0.0)
        assert effective_interval_s(config, is_first_in_zone_group=False) == 0.0
