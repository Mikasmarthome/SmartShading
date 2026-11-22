"""DT-1 (real Home Assistant): timing of the intermediate completion wait, scenario by scenario.

Real config entries, coordinators, the shared dispatcher and pacing, the real completion wait;
simulated covers on a virtual clock.  Every scenario records the command start, the end of the
wait (decision) with its classification, the first sign of real travel and the next command, and
asserts exact virtual times - both the gain (the end of an observed travel is noticed within one
0.25 s poll instead of one 1.0 s poll) and the absence of any premature release.

These are SIMULATIONS: they show what the integration does with a given cover behavior, not how a
real motor, bus or integration behaves.  The conservative waits (jump / no feedback) exist because
the integration cannot tell a finished travel from an optimistic report without movement evidence.

Run with the rest of the real-HA suite::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.cover_control.dispatch_completion import DEFAULT_POLL_INTERVAL_S

RAIN = "binary_sensor.dt1_rain"
SETTLE = 2.0                  # fixed post-completion pause (and endpoint start spacing)


def _first(h, kind: str, cover: str) -> float:
    times = h.clock.times_of(kind, cover)
    assert times, (kind, cover, h.clock.log)
    return times[0]


def _calls(h, cover: str) -> list[float]:
    return h.clock.times_of("CALL", cover + "->")


def _detail(h, cover: str) -> str:
    return next(w for _, k, w in h.clock.log if k == "WAIT_DETAIL" and w.startswith(cover))


async def _run(h, specs, **boot):
    boot.setdefault("poll_interval_s", DEFAULT_POLL_INTERVAL_S)       # production cadence for every DT-1 scenario
    h = await h.boot(specs, **boot)
    h.sun_up()
    tasks = h.refresh()
    await h.drive(tasks)
    return h


def _watch_subscriptions(h, monkeypatch) -> dict:
    """Count the completion waits' state-change subscriptions and their removal."""
    counts = {"subscribed": 0, "unsubscribed": 0}
    cls = type(h.coord(next(iter(h.entries))))
    original = cls._subscribe_cover_state_changes

    def spy(self, entity_id, on_change):
        counts["subscribed"] += 1
        remove = original(self, entity_id, on_change)

        def unsubscribe():
            counts["unsubscribed"] += 1
            remove()
        return unsubscribe

    monkeypatch.setattr(cls, "_subscribe_cover_state_changes", spy)
    return counts


# ---------------------------------------------------------------------------
# Reliable feedback: the end of the observed travel is noticed within one poll
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("poll, end, next_start", [
    (DEFAULT_POLL_INTERVAL_S, 20.5, 22.5),       # the production 0.25 s poll
    (1.0, 21.0, 23.0),        # the previous 1.0 s poll, for the before/after record
])
async def test_reliable_feedback_the_next_cover_starts_one_settle_after_the_observed_end(zones, poll, end, next_start) -> None:
    h = await _run(zones, [("ea", {"w": ["cover.a1", "cover.a2"]}, 50, None)],
                   travel={"cover.a1": 20.4, "cover.a2": 20.4}, poll_interval_s=poll)
    assert _calls(h, "cover.a1") == [0.0]
    assert _first(h, "WAIT_END", "cover.a1") == end                      # decision: observed end of travel
    assert "completed/position" in [w for _, k, w in h.clock.log if k == "WAIT_END"][0]
    assert "first_movement_s=0.0 observed=True" in _detail(h, "cover.a1")
    assert _calls(h, "cover.a2") == [next_start]                        # exactly ONE 2 s settle after it
    assert next_start - end == SETTLE


async def test_a_slow_motor_with_real_feedback_is_waited_for_beyond_the_estimate(zones) -> None:
    h = await _run(zones, [("ea", {"w": ["cover.a1", "cover.a2"]}, 50, None)],
                   travel={"cover.a1": 35.0, "cover.a2": 35.0})
    assert _first(h, "WAIT_END", "cover.a1") == 35.0                    # the estimate for 50 % would be 15 s
    assert _calls(h, "cover.a2") == [37.0]


async def test_delayed_movement_feedback_is_not_mistaken_for_a_finished_travel(zones) -> None:
    h = await _run(zones, [("ea", {"w": ["cover.a1", "cover.a2"]}, 50, None)],
                   travel={"cover.a1": 20.4, "cover.a2": 20.4}, delay={"cover.a1": 3.0})
    assert "first_movement_s=3.0 observed=True" in _detail(h, "cover.a1")
    assert _first(h, "WAIT_END", "cover.a1") == 23.5
    assert _calls(h, "cover.a2") == [25.5]


# ---------------------------------------------------------------------------
# No trustworthy movement evidence: the conservative estimated wait stays
# ---------------------------------------------------------------------------

async def test_an_optimistic_target_report_never_releases_the_next_cover_early(zones) -> None:
    """The cover shows the target position from the first poll on and never says 'moving' while
    its motor runs for 20 s.  The wait is the 15 s estimate for 50 % (never the idle-at-target
    reading), classified 'combined', and the next cover starts one settle later."""
    h = await _run(zones, [("ea", {"w": ["cover.a1", "cover.a2"]}, 50, None)],
                   behaviours={"cover.a1": "jump"})
    assert h.sim.pos["cover.a1"] == 50                                   # reported at the very start
    assert _first(h, "WAIT_END", "cover.a1") == 15.0
    assert [w for _, k, w in h.clock.log if k == "WAIT_END"][0] == "cover.a1 completed/combined"
    assert "observed=False" in _detail(h, "cover.a1") and "first_movement_s=None" in _detail(h, "cover.a1")
    assert _calls(h, "cover.a2") == [17.0]


async def test_a_cover_without_state_feedback_waits_the_estimate_for_an_unknown_start(zones) -> None:
    h = await _run(zones, [("ea", {"w": ["cover.a1", "cover.a2"]}, 50, None)],
                   behaviours={"cover.a1": "assumed"})
    # no position is known, so the longer directional estimate (30 s default) is used - never less
    # (relative to the command: the assumed cover's first command waited 2 s on boot-time pacing)
    started = _calls(h, "cover.a1")[0]
    assert _first(h, "WAIT_END", "cover.a1") - started == 30.0
    assert [w for _, k, w in h.clock.log if k == "WAIT_END"][0] == "cover.a1 completed/time_estimate"
    assert _calls(h, "cover.a2") == [started + 30.0 + SETTLE]


# ---------------------------------------------------------------------------
# Order and pacing across zones, endpoints and Safety
# ---------------------------------------------------------------------------

async def test_mixed_zones_run_one_intermediate_travel_at_a_time_with_one_settle(zones) -> None:
    h = await _run(zones, [("ea", {"w1": ["cover.a1"]}, 50, None), ("eb", {"w2": ["cover.b1"]}, 50, None)],
                   travel={"cover.a1": 20.4, "cover.b1": 20.4})
    assert _calls(h, "cover.a1") == [0.0]
    assert _calls(h, "cover.b1") == [_first(h, "WAIT_END", "cover.a1") + SETTLE] == [22.5]


async def test_endpoint_commands_are_spaced_by_the_start_interval_and_never_wait_for_the_travel(zones) -> None:
    h = await _run(zones, [("ee", {"w": ["cover.e1", "cover.e2", "cover.e3"]}, 0, None)])
    starts = sorted(t for c in ("cover.e1", "cover.e2", "cover.e3") for t in _calls(h, c))
    assert starts == [0.0, 2.0, 4.0]
    assert h.clock.times_of("WAIT_START") == []                          # no completion wait for 0 / 100 %


async def test_an_intermediate_travel_and_an_endpoint_of_another_zone_keep_the_settle_between_them(zones) -> None:
    h = await _run(zones, [("ea", {"w1": ["cover.a1"]}, 50, None), ("eb", {"w2": ["cover.b1"]}, 0, None)],
                   travel={"cover.a1": 20.4})
    a, b = _calls(h, "cover.a1"), _calls(h, "cover.b1")
    assert len(a) == len(b) == 1
    if a[0] < b[0]:                                      # the intermediate travel first: its end + settle
        assert b[0] == _first(h, "WAIT_END", "cover.a1") + SETTLE == 22.5
    else:                                                # the endpoint first: only the start interval
        assert b[0] == 0.0 and a[0] == SETTLE


async def test_safety_is_dispatched_at_once_during_a_completion_wait_and_leaves_no_subscription(zones, monkeypatch) -> None:
    h = await zones.boot([("ea", {"w1": ["cover.a1", "cover.a2"]}, 50, None),
                          ("eb", {"w2": ["cover.b1"]}, 50, RAIN)],
                         poll_interval_s=DEFAULT_POLL_INTERVAL_S)
    counts = _watch_subscriptions(h, monkeypatch)
    h.sun_up()
    tasks = h.refresh("ea")
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
    rain_at = h.clock.t
    h.rain(RAIN)
    tasks += h.refresh("eb")
    await h.drive(tasks)
    assert _calls(h, "cover.a1") == [0.0] and _calls(h, "cover.a2") == []       # the sequence never went on
    assert _calls(h, "cover.b1") and _calls(h, "cover.b1")[0] == rain_at         # Safety: not one second late
    assert counts["subscribed"] >= 1 and counts["subscribed"] == counts["unsubscribed"]


async def test_unloading_during_a_completion_wait_leaves_no_subscription_and_no_further_command(zones, monkeypatch) -> None:
    h = await zones.boot([("ea", {"w1": ["cover.a1", "cover.a2"]}, 50, None)],
                         poll_interval_s=DEFAULT_POLL_INTERVAL_S)
    counts = _watch_subscriptions(h, monkeypatch)
    h.sun_up()
    tasks = h.refresh("ea")
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
    assert counts["subscribed"] == 1 and counts["unsubscribed"] == 0
    assert await h.hass.config_entries.async_unload("ea")
    h.clock.release_all()
    await h.settle()
    assert all(task.done() for task in tasks)
    assert counts["subscribed"] == counts["unsubscribed"] == 1
    assert _calls(h, "cover.a2") == []
