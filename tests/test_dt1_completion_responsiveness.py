"""DT-1 -- responsiveness of the completion wait WITHOUT weakening the A5 anti-jump contract.

Two things got faster, and nothing got more trusting:

* the cover is looked at every 0.25 s (was 1.0 s), so the end of an OBSERVED travel is noticed
  at most 0.25 s after the cover reported it;
* when the caller subscribes to the cover's state changes, every change wakes the wait at once and
  is kept as evidence (a moving state or an intermediate position between two polls is not lost).

What still decides: the cover's CURRENT state judged by the A5 rules.  A target that is reported
without observed travel (an optimistic integration) is a jump, however often it is repeated, and
waits for the distance-scaled estimate; covers without trustworthy feedback wait by estimate.

Time is a scripted fake clock; no test depends on real time.  These are simulations - they do not
prove how a real motor behaves.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.smartshading.cover_control import dispatch_completion as dc
from custom_components.smartshading.cover_control.dispatch_completion import (
    CompletionStatus,
    wait_for_travel_completion,
)

TRAVEL_S = 30.0          # the default per-direction travel time the integration assumes


class World:
    """One cover whose state is a scripted function of time, with a state-change feed."""

    def __init__(self, initial=("open", 0)) -> None:
        self.t = 0.0
        self.state, self.pos = initial
        self.script: list[tuple[float, str, int | None]] = []
        self.listeners: list = []
        self.subscribed = 0
        self.unsubscribed = 0
        self.reads = 0

    def at(self, t: float, state: str, pos: int | None) -> "World":
        self.script.append((t, state, pos))
        self.script.sort(key=lambda row: row[0])
        return self

    def get(self, entity_id: str):
        self.reads += 1
        attrs = {} if self.pos is None else {"current_position": self.pos}
        return SimpleNamespace(state=self.state, attributes=attrs)

    def subscribe(self, entity_id: str, on_change):
        self.subscribed += 1
        self.listeners.append(on_change)

        def unsubscribe() -> None:
            self.unsubscribed += 1
            self.listeners.remove(on_change)
        return unsubscribe

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        end = self.t + max(0.0, seconds)
        while True:
            due = next((row for row in self.script if self.t < row[0] <= end), None)
            if due is None:
                self.t = end
                await asyncio.sleep(0)
                return
            self.script.remove(due)
            self.t, self.state, self.pos = due
            event = SimpleNamespace(data={"new_state": self.get("cover.x")})
            for listener in list(self.listeners):
                listener(event)
            for _ in range(6):               # let a woken wait cancel this sleep before it goes on
                await asyncio.sleep(0)


def hass_of(world: World):
    return SimpleNamespace(states=SimpleNamespace(get=world.get))


def travel(world: World, *, start: int, target: int, begin: float, end: float, steps: int = 10) -> World:
    """Real travel: a moving state with a moving position, then idle at the target."""
    moving = "opening" if target > start else "closing"
    for i in range(steps):
        world.at(begin + (end - begin) * i / steps, moving, round(start + (target - start) * i / steps))
    return world.at(end, "open" if target else "closed", target)


async def wait(world: World, *, start=0, target=50, reliable=True, subscribe=True, max_wait=40.0, **kw):
    return await wait_for_travel_completion(
        hass_of(world), entity_id="cover.x", target_position_ha=target, invert_position=False,
        has_reliable_position_feedback=reliable, start_position_ha=start,
        travel_time_open_s=TRAVEL_S, travel_time_close_s=TRAVEL_S, max_wait_s=max_wait,
        mono_clock=world.now, sleep=world.sleep,
        subscribe_state_changes=world.subscribe if subscribe else None, **kw)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Responsiveness
# ---------------------------------------------------------------------------

class TestResponsiveness:
    def test_the_default_polling_interval_is_a_quarter_of_a_second(self) -> None:
        assert dc.DEFAULT_POLL_INTERVAL_S == 0.25

    @pytest.mark.parametrize("subscribe", [False, True])
    def test_the_end_of_an_observed_travel_is_noticed_within_one_poll(self, subscribe) -> None:
        world = travel(World(("open", 0)), start=0, target=50, begin=0.0, end=20.1)
        res = run(wait(world, subscribe=subscribe))
        assert res.completed and res.method.value == "position" and res.movement_observed is True
        assert 20.1 <= res.elapsed_s <= 20.1 + dc.DEFAULT_POLL_INTERVAL_S + 1e-9, res.elapsed_s

    def test_a_state_change_wakes_the_wait_at_once_even_between_polls(self) -> None:
        world = travel(World(("open", 0)), start=0, target=50, begin=0.0, end=7.3)
        res = run(wait(world, poll_interval_s=10.0))
        assert res.completed and res.method.value == "position"
        assert res.elapsed_s == pytest.approx(7.3), "event-driven: no waiting for the next poll"

    def test_without_the_feed_a_slow_poll_misses_the_travel_and_falls_back_to_the_estimate(self) -> None:
        """Coarse polling cannot tell this finished travel from a jump: it stays conservative."""
        world = travel(World(("open", 0)), start=0, target=50, begin=0.0, end=7.3)
        res = run(wait(world, poll_interval_s=10.0, subscribe=False))
        assert res.method.value == "combined" and res.movement_observed is False
        assert res.elapsed_s == pytest.approx(15.0)

    def test_the_first_sign_of_real_travel_is_timestamped(self) -> None:
        world = World(("open", 0))
        world.at(3.0, "opening", 0)                                  # delayed movement feedback
        travel(world, start=0, target=50, begin=3.0, end=23.4)
        res = run(wait(world))
        assert res.completed and res.first_movement_s == pytest.approx(3.0)
        assert res.elapsed_s == pytest.approx(23.5, abs=0.26)

    def test_a_slow_motor_is_waited_for_by_observation_not_cut_off_at_the_estimate(self) -> None:
        world = travel(World(("open", 0)), start=0, target=50, begin=0.0, end=35.0)    # 50 % estimate = 15 s
        res = run(wait(world, start=0, target=50, max_wait=40.0))
        assert res.completed and res.movement_observed is True and res.elapsed_s >= 35.0


# ---------------------------------------------------------------------------
# The anti-jump contract still holds - also with the state feed
# ---------------------------------------------------------------------------

class TestOptimisticTargetIsNeverEarlyCompletion:
    def test_two_identical_at_target_polls_do_not_complete(self) -> None:
        world = World(("open", 50))                      # the integration reports the target at once
        res = run(wait(world, start=0, target=50, subscribe=False))
        assert res.completed and res.method.value == "combined" and res.movement_observed is False
        assert res.elapsed_s >= 15.0 and world.reads > 20        # many identical idle-at-target polls

    def test_a_flood_of_identical_state_changes_is_not_evidence_of_travel(self) -> None:
        world = World(("open", 50))
        for i in range(1, 60):                           # the cover re-reports the target every 0.2 s
            world.at(i * 0.2, "open", 50)
        res = run(wait(world, start=0, target=50))
        assert res.method.value == "combined" and res.movement_observed is False
        assert res.first_movement_s is None and res.elapsed_s >= 15.0

    def test_reaching_the_target_is_never_a_sign_of_travel(self) -> None:
        world = World(("open", 0)).at(2.0, "open", 50)   # position jumps, state never says moving
        res = run(wait(world, start=0, target=50))
        assert res.method.value == "combined" and res.elapsed_s >= 15.0 and res.first_movement_s is None

    def test_a_short_moving_state_between_two_polls_is_kept_as_evidence_by_the_feed(self) -> None:
        build = lambda: World(("open", 50)).at(1.1, "closing", 50).at(1.2, "open", 50)  # noqa: E731
        with_feed = run(wait(build(), poll_interval_s=5.0))
        without = run(wait(build(), poll_interval_s=5.0, subscribe=False))
        assert with_feed.movement_observed is True and with_feed.method.value == "position"
        assert with_feed.first_movement_s == pytest.approx(1.1)
        assert without.movement_observed is False and without.method.value == "combined"   # unchanged rule

    def test_unknown_start_position_waits_the_longer_directional_estimate(self) -> None:
        res = run(wait(World(("open", 50)), start=None, target=50))
        assert res.method.value == "combined" and res.elapsed_s >= TRAVEL_S

    def test_a_cover_without_trustworthy_feedback_waits_the_estimate_and_never_polls_or_subscribes(self) -> None:
        world = World(("open", 50))
        res = run(wait(world, start=0, target=50, reliable=False))
        assert res.method.value == "time_estimate" and res.elapsed_s == pytest.approx(15.0)
        assert world.reads == 0 and world.subscribed == 0

    @pytest.mark.parametrize("bad", ["unavailable", "unknown"])
    def test_unavailable_or_stale_states_are_neither_evidence_nor_completion(self, bad) -> None:
        world = World((bad, None))
        for i in range(1, 30):
            world.at(i * 1.0, bad, None)
        res = run(wait(world, start=0, target=50))
        assert res.status is CompletionStatus.TIMED_OUT and res.movement_observed is False


# ---------------------------------------------------------------------------
# Nothing is left behind
# ---------------------------------------------------------------------------

class TestAbortEndsTheWaitAtOnce:
    """An abort that arrives while the wait is under way ends it within one step - never at the
    timeout and never as a completion (the executor must not release the next cover)."""

    def test_a_stale_verdict_ends_the_wait_at_the_next_step(self) -> None:
        world = World(("opening", 10))
        res = run(wait(world, abort_check=lambda: (CompletionStatus.STALE, "generation_superseded")
                       if world.t >= 3.0 else None))
        assert res.status is CompletionStatus.STALE and res.completed is False
        assert world.t <= 3.0 + dc.DEFAULT_POLL_INTERVAL_S + 1e-9

    def test_a_cancellation_that_arrives_mid_wait_ends_it_without_advancing_time(self) -> None:
        cancel = asyncio.Event()
        world = World(("opening", 10))
        real_sleep = world.sleep

        async def sleep(seconds: float) -> None:
            await real_sleep(seconds)
            if world.t >= 3.0:
                cancel.set()

        async def go():
            return await asyncio.wait_for(wait_for_travel_completion(
                hass_of(world), entity_id="cover.x", target_position_ha=50, invert_position=False,
                has_reliable_position_feedback=True, start_position_ha=0, travel_time_open_s=30.0,
                travel_time_close_s=30.0, max_wait_s=40.0, mono_clock=world.now, sleep=sleep,
                cancellation=cancel, subscribe_state_changes=world.subscribe), 5)

        res = run(go())
        assert res.status is CompletionStatus.CANCELLED and res.completed is False
        assert world.t <= 3.0 + dc.DEFAULT_POLL_INTERVAL_S + 1e-9


class TestSubscriptionLifecycle:
    def test_the_feed_is_released_after_a_completion_and_after_a_timeout(self) -> None:
        done = travel(World(("open", 0)), start=0, target=50, begin=0.0, end=5.0)
        assert run(wait(done)).completed and (done.subscribed, done.unsubscribed) == (1, 1)
        stuck = World(("opening", 10))
        assert run(wait(stuck)).status is CompletionStatus.TIMED_OUT
        assert (stuck.subscribed, stuck.unsubscribed) == (1, 1) and stuck.listeners == []

    def test_the_feed_is_released_when_the_wait_is_cancelled_or_made_stale(self) -> None:
        cancel = asyncio.Event()
        world = World(("opening", 10))

        async def go():
            task = asyncio.ensure_future(wait(world, cancellation=cancel))
            for _ in range(8):
                await asyncio.sleep(0)
            cancel.set()
            return await asyncio.wait_for(task, 2)

        res = run(go())
        assert res.status is CompletionStatus.CANCELLED
        assert (world.subscribed, world.unsubscribed) == (1, 1)

        stale = World(("opening", 10))
        verdict = {"v": None}
        task_res = run(wait(stale, abort_check=lambda: verdict["v"] or (
            (CompletionStatus.STALE, "generation_superseded") if stale.reads > 3 else None)))
        assert task_res.status is CompletionStatus.STALE and task_res.cancel_reason == "generation_superseded"
        assert (stale.subscribed, stale.unsubscribed) == (1, 1)

    def test_the_feed_is_released_when_the_task_itself_is_cancelled(self) -> None:
        world = World(("opening", 10))

        async def go():
            task = asyncio.ensure_future(wait(world))
            for _ in range(8):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        run(go())
        assert (world.subscribed, world.unsubscribed) == (1, 1)

    def test_a_failing_state_read_still_releases_the_feed(self) -> None:
        world = World(("opening", 10))

        def broken(entity_id):
            raise RuntimeError("state machine gone")

        hass = SimpleNamespace(states=SimpleNamespace(get=broken))

        async def go():
            with pytest.raises(RuntimeError):
                await wait_for_travel_completion(
                    hass, entity_id="cover.x", target_position_ha=50, invert_position=False,
                    has_reliable_position_feedback=True, start_position_ha=0, travel_time_open_s=30.0,
                    travel_time_close_s=30.0, max_wait_s=5.0, mono_clock=world.now, sleep=world.sleep,
                    subscribe_state_changes=world.subscribe)

        run(go())
        assert (world.subscribed, world.unsubscribed) == (1, 1)
