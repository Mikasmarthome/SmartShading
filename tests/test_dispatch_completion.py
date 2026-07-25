"""Tests for cover_control/dispatch_completion.py — tiered travel-completion
detection for SEQUENTIAL dispatch mode (v1.2.0-beta.1, T11).

Coverage:
  CMP-01  Position-based completion: reliable feedback + within tolerance.
  CMP-02  Position-based: outside tolerance -> not complete (None).
  CMP-03  Position tolerance is inclusive at the boundary, not exact-equality.
  CMP-04  Still moving (opening/closing) -> never complete regardless of
          position.
  CMP-05  Unavailable/unknown entity -> never complete.
  CMP-06  Reliable feedback, idle, position attribute absent -> movement-
          state completion (not silently "no signal").
  CMP-07  Unreliable feedback -> evaluate_travel_status never claims
          completion (tier 2/3 handles these covers exclusively via time).
  CMP-08  estimate_travel_duration_s: known start, full open-to-close range
          uses the full directional duration.
  CMP-09  estimate_travel_duration_s: partial move scales proportionally.
  CMP-10  estimate_travel_duration_s: unknown start falls back to the LONGER
          of the two directional durations (never under-estimate).
  CMP-11  estimate_travel_duration_s: zero delta -> zero duration.
  CMP-12  wait_for_travel_completion (reliable feedback): polls until
          position matches, using injected clock/sleep — no real time spent.
  CMP-13  wait_for_travel_completion (reliable feedback): times out at
          max_wait_s if never reaches target — never waits forever.
  CMP-14  wait_for_travel_completion (unreliable feedback): single bounded
          sleep for the estimated duration, never polls hass.states.
  CMP-15  wait_for_travel_completion (unreliable feedback): estimated
          duration exceeding max_wait_s is capped and reported as timeout.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.smartshading.cover_control.dispatch_completion import (
    CompletionMethod,
    estimate_travel_duration_s,
    evaluate_travel_status,
    wait_for_travel_completion,
)


class TestEvaluateTravelStatusPosition:
    def test_reliable_feedback_within_tolerance_completes(self) -> None:
        method = evaluate_travel_status(
            state="open", attributes={"current_position": 80},
            invert=False, has_reliable_position_feedback=True,
            target_position_ha=78, position_tolerance=5,
        )
        assert method is CompletionMethod.POSITION

    def test_reliable_feedback_outside_tolerance_not_complete(self) -> None:
        method = evaluate_travel_status(
            state="open", attributes={"current_position": 50},
            invert=False, has_reliable_position_feedback=True,
            target_position_ha=80, position_tolerance=5,
        )
        assert method is None

    def test_tolerance_boundary_inclusive(self) -> None:
        method = evaluate_travel_status(
            state="open", attributes={"current_position": 75},
            invert=False, has_reliable_position_feedback=True,
            target_position_ha=80, position_tolerance=5,
        )
        assert method is CompletionMethod.POSITION

    def test_exact_match_not_required(self) -> None:
        # 79 vs target 80, tolerance 5: rounding-friendly, not exact-equality.
        method = evaluate_travel_status(
            state="open", attributes={"current_position": 79},
            invert=False, has_reliable_position_feedback=True,
            target_position_ha=80, position_tolerance=5,
        )
        assert method is CompletionMethod.POSITION


class TestEvaluateTravelStatusMovement:
    @pytest.mark.parametrize("state", ["opening", "closing"])
    def test_still_moving_never_complete(self, state: str) -> None:
        method = evaluate_travel_status(
            state=state, attributes={"current_position": 80},
            invert=False, has_reliable_position_feedback=True,
            target_position_ha=80, position_tolerance=5,
        )
        assert method is None

    @pytest.mark.parametrize("state", [None, "unavailable", "unknown"])
    def test_unavailable_never_complete(self, state) -> None:
        method = evaluate_travel_status(
            state=state, attributes={"current_position": 80},
            invert=False, has_reliable_position_feedback=True,
            target_position_ha=80, position_tolerance=5,
        )
        assert method is None

    def test_reliable_feedback_idle_no_position_attribute_uses_movement_state(self) -> None:
        method = evaluate_travel_status(
            state="open", attributes={},
            invert=False, has_reliable_position_feedback=True,
            target_position_ha=80, position_tolerance=5,
        )
        assert method is CompletionMethod.MOVEMENT_STATE

    def test_unreliable_feedback_never_claims_completion(self) -> None:
        method = evaluate_travel_status(
            state="open", attributes={"current_position": 80},
            invert=False, has_reliable_position_feedback=False,
            target_position_ha=80, position_tolerance=5,
        )
        assert method is None


class TestEstimateTravelDuration:
    def test_full_range_uses_full_directional_duration(self) -> None:
        s = estimate_travel_duration_s(
            start_position_ha=0, target_position_ha=100,
            travel_time_open_s=30.0, travel_time_close_s=25.0,
        )
        assert s == pytest.approx(30.0)

    def test_full_range_closing_direction(self) -> None:
        s = estimate_travel_duration_s(
            start_position_ha=100, target_position_ha=0,
            travel_time_open_s=30.0, travel_time_close_s=25.0,
        )
        assert s == pytest.approx(25.0)

    def test_partial_move_scales_proportionally(self) -> None:
        s = estimate_travel_duration_s(
            start_position_ha=0, target_position_ha=50,
            travel_time_open_s=30.0, travel_time_close_s=30.0,
        )
        assert s == pytest.approx(15.0)

    def test_unknown_start_uses_longer_direction(self) -> None:
        s = estimate_travel_duration_s(
            start_position_ha=None, target_position_ha=80,
            travel_time_open_s=20.0, travel_time_close_s=35.0,
        )
        assert s == pytest.approx(35.0)

    def test_zero_delta_zero_duration(self) -> None:
        s = estimate_travel_duration_s(
            start_position_ha=50, target_position_ha=50,
            travel_time_open_s=30.0, travel_time_close_s=30.0,
        )
        assert s == 0.0


class _FakeState:
    def __init__(self, state, attributes) -> None:
        self.state = state
        self.attributes = attributes


class _FakeStates:
    def __init__(self) -> None:
        self._by_call = []
        self._results = []

    def queue(self, state, attributes):
        self._results.append(_FakeState(state, attributes))

    def get(self, entity_id):
        if not self._results:
            return None
        # Return the last queued state repeatedly once exhausted (simulates
        # a cover holding its final reported state).
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


class _FakeHass:
    def __init__(self) -> None:
        self.states = _FakeStates()


class TestWaitForTravelCompletionReliableFeedback:
    def test_polls_until_position_matches(self) -> None:
        hass = _FakeHass()
        hass.states.queue("opening", {"current_position": 30})
        hass.states.queue("opening", {"current_position": 60})
        hass.states.queue("open", {"current_position": 80})

        fake_time = [0.0]
        sleeps: list[float] = []

        def mono_clock():
            return fake_time[0]

        async def fake_sleep(s):
            sleeps.append(s)
            fake_time[0] += s

        result = asyncio.run(wait_for_travel_completion(
            hass, entity_id="cover.test", target_position_ha=80,
            invert_position=False, has_reliable_position_feedback=True,
            start_position_ha=0, travel_time_open_s=30.0, travel_time_close_s=30.0,
            max_wait_s=40.0, poll_interval_s=1.0,
            mono_clock=mono_clock, sleep=fake_sleep,
        ))
        assert result.method is CompletionMethod.POSITION
        assert result.timed_out is False
        assert len(sleeps) == 2  # two polls before the third (matching) read

    def test_times_out_if_never_reaches_target(self) -> None:
        hass = _FakeHass()
        hass.states.queue("opening", {"current_position": 10})

        fake_time = [0.0]

        def mono_clock():
            return fake_time[0]

        async def fake_sleep(s):
            fake_time[0] += s

        result = asyncio.run(wait_for_travel_completion(
            hass, entity_id="cover.test", target_position_ha=80,
            invert_position=False, has_reliable_position_feedback=True,
            start_position_ha=0, travel_time_open_s=30.0, travel_time_close_s=30.0,
            max_wait_s=5.0, poll_interval_s=1.0,
            mono_clock=mono_clock, sleep=fake_sleep,
        ))
        assert result.method is CompletionMethod.TIMEOUT
        assert result.timed_out is True
        assert result.elapsed_s >= 5.0


class TestWaitForTravelCompletionUnreliableFeedback:
    def test_single_bounded_sleep_never_polls_states(self) -> None:
        hass = _FakeHass()  # no states queued — must never be read

        fake_time = [0.0]
        sleeps: list[float] = []

        def mono_clock():
            return fake_time[0]

        async def fake_sleep(s):
            sleeps.append(s)
            fake_time[0] += s

        result = asyncio.run(wait_for_travel_completion(
            hass, entity_id="cover.test", target_position_ha=80,
            invert_position=False, has_reliable_position_feedback=False,
            start_position_ha=0, travel_time_open_s=20.0, travel_time_close_s=20.0,
            max_wait_s=40.0, poll_interval_s=1.0,
            mono_clock=mono_clock, sleep=fake_sleep,
        ))
        assert result.method is CompletionMethod.TIME_ESTIMATE
        assert result.timed_out is False
        assert sleeps == [pytest.approx(16.0)]  # 80% of the 20s open duration

    def test_estimate_exceeding_max_wait_capped_and_marked_timeout(self) -> None:
        hass = _FakeHass()
        fake_time = [0.0]

        def mono_clock():
            return fake_time[0]

        async def fake_sleep(s):
            fake_time[0] += s

        result = asyncio.run(wait_for_travel_completion(
            hass, entity_id="cover.test", target_position_ha=100,
            invert_position=False, has_reliable_position_feedback=False,
            start_position_ha=0, travel_time_open_s=60.0, travel_time_close_s=60.0,
            max_wait_s=10.0, poll_interval_s=1.0,
            mono_clock=mono_clock, sleep=fake_sleep,
        ))
        assert result.method is CompletionMethod.TIMEOUT
        assert result.timed_out is True
        assert fake_time[0] == pytest.approx(10.0)  # capped at max_wait_s
