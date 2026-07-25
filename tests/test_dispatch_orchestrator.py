"""Tests for cover_control/dispatch_orchestrator.py — pure wait-computation
for the T11 dispatch strategy (v1.2.0-beta.1).

Coverage:
  ORC-01  PARALLEL: no wait regardless of elapsed time.
  ORC-02  SPACED: reproduces the pre-T11 fixed-interval behavior exactly at
          the default (2.0s).
  ORC-03  SPACED: waits the remainder when less than the interval elapsed;
          no wait once the interval has fully elapsed.
  ORC-04  SEQUENTIAL: no pre-dispatch wait (its gating is the completion
          wait, computed separately) unless a zone boundary forces one.
  ORC-05  zone_batching forces start_interval_s at a zone boundary
          regardless of mode (including PARALLEL/SEQUENTIAL).
  ORC-06  zone_batching does NOT add a wait for non-boundary intents.
  ORC-07  First dispatch ever (time_since_last_dispatch_s=None) is always
          immediate, regardless of mode/interval.
  ORC-08  requires_completion_wait is True only for SEQUENTIAL.
  ORC-09  Invalid/edge interval values (0, negative clamped to 0) behave
          sanely — never a negative wait.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.smartshading.cover_control.dispatch_orchestrator import (
    effective_interval_s,
    requires_completion_wait,
    resolve_pre_dispatch_wait,
)
from custom_components.smartshading.models.dispatch_config import (
    DispatchConfig,
    DispatchMode,
)


class TestParallelMode:
    def test_no_wait_regardless_of_elapsed(self) -> None:
        config = DispatchConfig(mode=DispatchMode.PARALLEL)
        for elapsed in (0.0, 0.5, 100.0):
            wait = resolve_pre_dispatch_wait(
                config, is_first_in_zone_group=False, time_since_last_dispatch_s=elapsed,
            )
            assert wait == timedelta(0)


class TestSpacedMode:
    def test_default_reproduces_pre_t11_interval(self) -> None:
        config = DispatchConfig()  # SPACED, 2.0s default
        assert config.mode is DispatchMode.SPACED
        assert config.start_interval_s == 2.0

    def test_waits_remainder(self) -> None:
        config = DispatchConfig(mode=DispatchMode.SPACED, start_interval_s=2.0)
        wait = resolve_pre_dispatch_wait(
            config, is_first_in_zone_group=False, time_since_last_dispatch_s=0.5,
        )
        assert wait == timedelta(seconds=1.5)

    def test_no_wait_once_interval_elapsed(self) -> None:
        config = DispatchConfig(mode=DispatchMode.SPACED, start_interval_s=2.0)
        wait = resolve_pre_dispatch_wait(
            config, is_first_in_zone_group=False, time_since_last_dispatch_s=2.5,
        )
        assert wait == timedelta(0)

    def test_custom_interval_respected(self) -> None:
        config = DispatchConfig(mode=DispatchMode.SPACED, start_interval_s=5.0)
        wait = resolve_pre_dispatch_wait(
            config, is_first_in_zone_group=False, time_since_last_dispatch_s=1.0,
        )
        assert wait == timedelta(seconds=4.0)


class TestSequentialMode:
    def test_no_pre_dispatch_wait_without_zone_boundary(self) -> None:
        config = DispatchConfig(mode=DispatchMode.SEQUENTIAL, start_interval_s=5.0)
        wait = resolve_pre_dispatch_wait(
            config, is_first_in_zone_group=False, time_since_last_dispatch_s=0.0,
        )
        assert wait == timedelta(0)

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
        wait = resolve_pre_dispatch_wait(
            config, is_first_in_zone_group=True, time_since_last_dispatch_s=0.0,
        )
        assert wait == timedelta(seconds=3.0)

    @pytest.mark.parametrize("mode", [DispatchMode.PARALLEL, DispatchMode.SEQUENTIAL])
    def test_no_extra_wait_for_non_boundary_intent(self, mode: DispatchMode) -> None:
        config = DispatchConfig(mode=mode, start_interval_s=3.0, zone_batching=True)
        wait = resolve_pre_dispatch_wait(
            config, is_first_in_zone_group=False, time_since_last_dispatch_s=0.0,
        )
        assert wait == timedelta(0)

    def test_disabled_zone_batching_no_boundary_wait(self) -> None:
        config = DispatchConfig(mode=DispatchMode.PARALLEL, start_interval_s=3.0, zone_batching=False)
        wait = resolve_pre_dispatch_wait(
            config, is_first_in_zone_group=True, time_since_last_dispatch_s=0.0,
        )
        assert wait == timedelta(0)


class TestFirstDispatchEverIsAlwaysImmediate:
    @pytest.mark.parametrize("mode", list(DispatchMode))
    def test_none_elapsed_means_no_wait(self, mode: DispatchMode) -> None:
        config = DispatchConfig(mode=mode, start_interval_s=5.0, zone_batching=True)
        wait = resolve_pre_dispatch_wait(
            config, is_first_in_zone_group=True, time_since_last_dispatch_s=None,
        )
        assert wait == timedelta(0)


class TestNeverNegativeWait:
    def test_zero_interval_never_negative(self) -> None:
        config = DispatchConfig(mode=DispatchMode.SPACED, start_interval_s=0.0)
        wait = resolve_pre_dispatch_wait(
            config, is_first_in_zone_group=False, time_since_last_dispatch_s=0.0,
        )
        assert wait == timedelta(0)

    def test_effective_interval_never_negative(self) -> None:
        config = DispatchConfig(mode=DispatchMode.SPACED, start_interval_s=0.0)
        assert effective_interval_s(config, is_first_in_zone_group=False) == 0.0
