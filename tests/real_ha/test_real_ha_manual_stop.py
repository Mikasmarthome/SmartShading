"""A manual stop of a cover SmartShading had commanded is respected (real Home Assistant).

SmartShading sends an intermediate target; the plan is cut short (here: the roles of the
window's covers change under the running plan, the same cut a Safety request or a newer
evaluation makes) while the cover is under way.  A person then stops the cover half-way.
The only signal the integration has is what the cover entity reports: a stationary state at
a position clearly between where it started and where it was sent.  That is not a delayed
report (a cover that has not reported yet still stands where it started, or shows
opening/closing) and not an arrival, so the next evaluation must not drive it again.
Real coordinators, the real shared dispatch, simulated covers and virtual time.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util

GUARD_WAIT_S = 100


def _override(h, entry, window):
    return h.coord(entry)._override_detector.get(window, dt_util.utcnow())


def _sent_to(h, cover, target):
    return [c for c in h.clock.calls() if c[1] == cover and c[2] == target]


async def _later(h, rounds=8):
    for _ in range(rounds):
        await h.fire_ha_timers(GUARD_WAIT_S)
        await h.drive(h.refresh("ea"))
        await h.drive(horizon=h.clock.t + 30)


def _stop_halfway(h, cover, ha_position):
    """A person stops the cover: it stands still at ``ha_position`` and reports so."""
    h.sim.motion.pop(cover, None)
    h.sim.pos[cover] = ha_position
    h.sim.publish(cover)


async def _cut_while_travelling(h, entry="ea", cover="cover.a1"):
    h.sun_up()
    tasks = h.refresh(entry)
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", cover))
    for callback in list(h.coord(entry)._cover_ownership._listeners[entry]):
        callback()
    await h.settle()
    return tasks


async def test_manual_stop_halfway_is_not_driven_again(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1"]}, 50, None)])
    tasks = await _cut_while_travelling(h)
    await h.drive(tasks)
    assert len(_sent_to(h, "cover.a1", 50)) == 1
    _stop_halfway(h, "cover.a1", 75)         # started at 100, was sent to 50, stopped at 75
    await _later(h)
    assert len(_sent_to(h, "cover.a1", 50)) == 1, "the stopped cover must not be commanded again"
    assert _override(h, "ea", "wa") is not None, "the manual stop is protected like any override"


# --- counter-probes: what must NOT be mistaken for a manual stop --------------------------------

async def test_cover_that_has_not_reported_yet_is_not_taken_for_stopped(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1"]}, 50, None)])
    tasks = await _cut_while_travelling(h)
    await h.drive(tasks)
    # delayed feedback: the cover reports movement but still its starting position
    h.sim.motion.pop("cover.a1", None)
    h.sim.covers.remove("cover.a1")          # the simulation must not publish over it
    h.hass.states.async_set("cover.a1", "closing", {"supported_features": 15, "current_position": 100})
    await _later(h, 3)
    assert _override(h, "ea", "wa") is None
    assert len(_sent_to(h, "cover.a1", 50)) == 1, "no second command for a command still under way"


async def test_cover_reporting_movement_halfway_is_not_taken_for_stopped(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1"]}, 50, None)])
    tasks = await _cut_while_travelling(h)
    await h.drive(tasks)
    h.sim.motion.pop("cover.a1", None)
    h.sim.covers.remove("cover.a1")
    h.hass.states.async_set("cover.a1", "closing", {"supported_features": 15, "current_position": 75})
    await _later(h, 3)
    assert _override(h, "ea", "wa") is None
    assert len(_sent_to(h, "cover.a1", 50)) == 1


async def test_safety_still_overrides_a_manually_stopped_cover(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1"]}, 50, "binary_sensor.rain_a")])
    tasks = await _cut_while_travelling(h)
    await h.drive(tasks)
    _stop_halfway(h, "cover.a1", 75)
    await _later(h, 2)
    assert _override(h, "ea", "wa") is not None
    h.rain("binary_sensor.rain_a")
    await h.drive(until=lambda: _sent_to(h, "cover.a1", 0))
    assert _sent_to(h, "cover.a1", 0), "Safety keeps its priority over the respected stop"
    await h.settle()


async def test_stopped_sibling_is_not_driven_again(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    h.sun_up()
    tasks = h.refresh("ea")
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a2"))
    for callback in list(h.coord("ea")._cover_ownership._listeners["ea"]):
        callback()
    await h.settle()
    await h.drive(tasks)
    assert len(_sent_to(h, "cover.a2", 50)) == 1
    _stop_halfway(h, "cover.a2", 75)
    await _later(h, 4)
    assert len(_sent_to(h, "cover.a2", 50)) == 1, "the stopped sibling is not driven again"
    items = h.coord("ea")._last_plan_completion["items"]
    assert "skipped:manual_stop" in items, items


async def test_cover_without_reliable_position_feedback_gives_no_stop_signal(zones):
    from custom_components.smartshading.cover_control.cover_entity_snapshot import (
        build_cover_entity_snapshot,
    )
    h = await zones.boot([("ea", {"wa": ["cover.a1"]}, 50, None)])
    coordinator = h.coord("ea")
    coordinator._own_motion["cover.a1"] = (0, 50, h.clock.t + 1_000.0)
    for reliable, expected in ((True, True), (False, False)):
        snapshot = build_cover_entity_snapshot(
            entity_id="cover.a1", state="open", attributes={"current_position": 75},
            invert=False, has_reliable_position_feedback=reliable)
        assert coordinator._own_motion_stopped_halfway("cover.a1", snapshot) is expected


async def test_cover_standing_at_its_start_is_no_manual_stop(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1"]}, 50, None)], learning=("ea",))
    tasks = await _cut_while_travelling(h)
    await h.drive(tasks)
    # the command has not been carried out (yet): the cover is stationary where it started
    h.sim.motion.pop("cover.a1", None)
    h.sim.pos["cover.a1"] = 100
    h.sim.publish("cover.a1")
    await _later(h, 3)
    assert _override(h, "ea", "wa") is None, "no movement happened, so nobody stopped anything"
    assert h.coord("ea")._learning_store.get_overrides("wa") == []


# --- a stop at a further cover of the group, whatever the automation wants next ----------------

def _retarget(h, entry, ha_position):
    """The automation wants another position now.  The 60-minute comfort position hold is
    taken as elapsed (it is a rule of its own, not what these tests are about)."""
    from custom_components.smartshading.models.config import ShadePositionDefaults
    coordinator = h.coord(entry)
    coordinator.shade_position_defaults = ShadePositionDefaults(
        light_shade_position=ha_position, normal_shade_position=ha_position,
        strong_shade_position=ha_position)
    coordinator._comfort_movement_holds.clear()


async def _cut_during_second_cover(h, entry="ea", covers=("cover.a1", "cover.a2")):
    """The first cover of the group arrives at 50, the second one is under way when the
    plan is cut (the roles of the covers change under the running plan)."""
    h.sun_up()
    tasks = h.refresh(entry)
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", covers[1]))
    for callback in list(h.coord(entry)._cover_ownership._listeners[entry]):
        callback()
    await h.settle()
    await h.drive(tasks)
    assert len(_sent_to(h, covers[1], 50)) == 1


async def _group_after_new_target(h, stop):
    await _cut_during_second_cover(h)
    if stop:
        _stop_halfway(h, "cover.a2", 75)         # between its start (100) and its target (50)
    _retarget(h, "ea", 30)                       # the automation now wants 30: a real trip for both
    await h.fire_ha_timers(GUARD_WAIT_S * 4)     # past the minimum action interval
    await h.drive(h.refresh("ea"))


async def test_stopped_second_cover_is_not_driven_to_a_new_target(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    await _group_after_new_target(h, stop=True)
    assert _sent_to(h, "cover.a1", 30), "the other cover of the group still makes its legitimate trip"
    assert _sent_to(h, "cover.a2", 30) == [], f"the stopped cover was driven again: {h.clock.calls()}"
    assert len(h.clock.calls_of("cover.a2")) == 1
    items = h.coord("ea")._last_plan_completion["items"]
    assert "skipped:manual_stop" in items, items          # the cover-specific check, no other guard


async def test_second_cover_without_a_stop_is_driven_to_the_new_target(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    await _group_after_new_target(h, stop=False)
    assert _sent_to(h, "cover.a1", 30) and _sent_to(h, "cover.a2", 30), h.clock.calls()


async def _waiting_package_after_stop(h, stop):
    """Zone B's second cover was commanded, the plan cut; zone A then holds the package, zone B
    plans a new target and waits; only now does the cover stop -- before B may send."""
    await _cut_during_second_cover(h, "eb", ("cover.b1", "cover.b2"))
    _retarget(h, "eb", 30)
    await h.fire_ha_timers(GUARD_WAIT_S * 4)            # past the minimum action interval of B
    tasks_a = h.refresh("ea")
    await h.drive(tasks_a, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
    tasks_b = h.refresh("eb")
    await h.drive(tasks_b + tasks_a, until=lambda: h.coord("eb")._package_state == "waiting")
    assert h.coord("eb")._package_state == "waiting", "B must plan its package while A holds the line"
    if stop:
        _stop_halfway(h, "cover.b2", 75)
    await h.drive(tasks_a + tasks_b)


async def test_stop_after_planning_is_caught_by_the_revalidation_before_sending(zones):
    h = await zones.boot([
        ("ea", {"wa": ["cover.a1"]}, 50, None),
        ("eb", {"wb": ["cover.b1", "cover.b2"]}, 50, None),
    ])
    await _waiting_package_after_stop(h, stop=True)
    assert _sent_to(h, "cover.b1", 30), "the other cover of the group is driven"
    assert _sent_to(h, "cover.b2", 30) == [], f"sent past the stop: {h.clock.calls()}"
    items = h.coord("eb")._last_plan_completion["items"]
    assert "skipped:manual_stop" in items, items          # the revalidation itself, no other guard


async def test_waiting_package_without_a_stop_sends_the_second_cover(zones):
    h = await zones.boot([
        ("ea", {"wa": ["cover.a1"]}, 50, None),
        ("eb", {"wb": ["cover.b1", "cover.b2"]}, 50, None),
    ])
    await _waiting_package_after_stop(h, stop=False)
    assert _sent_to(h, "cover.b1", 30) and _sent_to(h, "cover.b2", 30), h.clock.calls()


async def test_a_stop_does_not_hold_back_the_covers_after_it_in_the_group(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2", "cover.a3"]}, 50, None)])
    await _cut_during_second_cover(h)
    _stop_halfway(h, "cover.a2", 75)
    _retarget(h, "ea", 30)
    await h.fire_ha_timers(GUARD_WAIT_S * 4)
    await h.drive(h.refresh("ea"))
    assert _sent_to(h, "cover.a1", 30) and _sent_to(h, "cover.a3", 30), h.clock.calls()
    assert _sent_to(h, "cover.a2", 30) == []


# --- the stop outlives the bounded own-motion window ---------------------------------------------

def _count_cycles(h, entry="ea"):
    """Times (virtual) at which the coordinator ran an evaluation cycle."""
    coordinator = h.coord(entry)
    seen = []
    original = coordinator._async_update_data

    async def counting(*args, **kwargs):
        seen.append(h.clock.t)
        return await original(*args, **kwargs)

    h.monkeypatch.setattr(coordinator, "_async_update_data", counting)
    return seen


def _window_end(h, cover, entry="ea"):
    return h.coord(entry)._own_motion[cover][2]


async def test_stop_first_seen_in_the_regular_cycle_after_the_window_is_respected(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    await _cut_during_second_cover(h)
    _stop_halfway(h, "cover.a2", 75)             # the cover stands still and reports it
    window_end = _window_end(h, "cover.a2")
    _retarget(h, "ea", 30)                       # the automation wants 30 from now on
    cycles = _count_cycles(h)
    await h.pass_time(max(window_end - h.clock.t + 1, GUARD_WAIT_S * 4))   # the next look is after the window
    assert cycles, "the coordinator's own periodic cycle must have run (no manual refresh)"
    assert min(cycles) > window_end, (cycles, window_end)
    assert _sent_to(h, "cover.a1", 30), (
        f"the other cover makes its legitimate trip: {h.clock.calls()} {cycles} "
        f"{h.coord('ea')._last_plan_completion}")
    assert _sent_to(h, "cover.a2", 30) == [], f"the stopped cover was driven again: {h.clock.calls()}"
    assert len(h.clock.calls_of("cover.a2")) == 1


async def test_stop_is_recognised_by_the_cycle_even_when_no_command_is_planned(zones):
    from custom_components.smartshading.coordinator import _OWN_MOTION_JUDGE_GRACE_S
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    window_end = await _second_cover_after_cut(h, stop=True)
    await h.pass_time(window_end - h.clock.t + 1)    # the regular look; the target is unchanged: nothing planned
    assert "cover.a2" in h.coord("ea")._cover_stop_holds, "recognised by the look itself"
    assert len(h.clock.calls_of("cover.a2")) == 1
    await h.pass_time(_OWN_MOTION_JUDGE_GRACE_S + 60)    # the reference itself is long gone by now
    assert "cover.a2" not in h.coord("ea")._own_motion
    _retarget(h, "ea", 30)
    await h.pass_time(GUARD_WAIT_S * 4)
    assert _sent_to(h, "cover.a1", 30)
    assert _sent_to(h, "cover.a2", 30) == [], f"driven although its stop was recognised: {h.clock.calls()}"


async def test_a_detected_stop_is_protected_beyond_the_own_motion_window(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    await _cut_during_second_cover(h)
    _stop_halfway(h, "cover.a2", 75)
    window_end = _window_end(h, "cover.a2")
    _retarget(h, "ea", 30)
    await h.fire_ha_timers(GUARD_WAIT_S * 4)     # HA time only: the own-motion window stays open
    await h.drive(h.refresh("ea"))
    assert h.clock.t <= window_end
    assert "skipped:manual_stop" in h.coord("ea")._last_plan_completion["items"]
    await h.pass_time(window_end - h.clock.t + 1)    # the own-motion window is over ...
    assert h.clock.t > window_end
    assert _override(h, "ea", "wa") is None, "... and no regular override release is involved"
    _retarget(h, "ea", 20)                       # a2 stays at 75, a new trip is due
    await h.fire_ha_timers(GUARD_WAIT_S * 4)
    await h.drive(h.refresh("ea"))
    assert _sent_to(h, "cover.a1", 20), "the other cover keeps driving"
    assert _sent_to(h, "cover.a2", 20) == [], f"the stop was forgotten with the window: {h.clock.calls()}"
    assert _sent_to(h, "cover.a2", 30) == []


async def _second_cover_after_cut(h, *, stop, entry="ea", covers=("cover.a1", "cover.a2")):
    await _cut_during_second_cover(h, entry, covers)
    if stop:
        _stop_halfway(h, covers[1], 75)
    return _window_end(h, covers[1], entry)


async def _regular_cycle_after(h, window_end):
    await h.pass_time(max(window_end - h.clock.t + 1, GUARD_WAIT_S * 4))


# --- counter-probes of the late recognition ------------------------------------------------------

async def test_regular_cycle_after_the_window_without_a_stop_drives_the_second_cover(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    window_end = await _second_cover_after_cut(h, stop=False)    # a2 simply arrives at 50
    _retarget(h, "ea", 30)
    await _regular_cycle_after(h, window_end)
    assert _sent_to(h, "cover.a1", 30) and _sent_to(h, "cover.a2", 30), h.clock.calls()
    assert h.coord("ea")._cover_stop_holds == {}


async def test_cover_standing_at_its_start_after_the_window_is_no_stop(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)], learning=("ea",))
    await _cut_during_second_cover(h)
    window_end = _window_end(h, "cover.a2")
    h.sim.motion.pop("cover.a2", None)           # the command never took effect: it stands at its start
    h.sim.pos["cover.a2"] = 100
    h.sim.publish("cover.a2")
    _retarget(h, "ea", 30)
    await _regular_cycle_after(h, window_end)
    assert _sent_to(h, "cover.a2", 30), "nobody stopped anything: the cover is driven"
    assert h.coord("ea")._cover_stop_holds == {}
    assert h.coord("ea")._learning_store.get_overrides("wa") == []


async def test_an_old_reference_is_no_evidence_for_a_stop(zones):
    from custom_components.smartshading.coordinator import _OWN_MOTION_JUDGE_GRACE_S
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    await _cut_during_second_cover(h)
    window_end = _window_end(h, "cover.a2")
    h.sim.motion.pop("cover.a2", None)
    h.sim.covers.remove("cover.a2")
    h.hass.states.async_set("cover.a2", "unavailable")           # nobody can look at it
    _retarget(h, "ea", 30)
    await h.pass_time(window_end - h.clock.t + _OWN_MOTION_JUDGE_GRACE_S + 60)
    assert "cover.a2" not in h.coord("ea")._own_motion, "the reference is dropped unused"
    h.hass.states.async_set("cover.a2", "open", {"supported_features": 15, "current_position": 75})
    await h.pass_time(GUARD_WAIT_S * 4)          # much later the cover shows an in-between position
    assert h.coord("ea")._cover_stop_holds == {}, "no stop is concluded from a pair that old"
    assert _sent_to(h, "cover.a2", 30), h.clock.calls()


async def test_cover_without_reliable_position_feedback_is_never_held(zones):
    from custom_components.smartshading.cover_control.cover_entity_snapshot import (
        build_cover_entity_snapshot,
    )
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    coordinator = h.coord("ea")
    for reliable, held in ((False, False), (True, True)):
        coordinator._cover_stop_holds.clear()
        coordinator._own_motion["cover.a2"] = (0, 50, h.clock.t - 10.0)      # the window is over
        snapshot = build_cover_entity_snapshot(
            entity_id="cover.a2", state="open", attributes={"current_position": 75},
            invert=False, has_reliable_position_feedback=reliable)
        assert coordinator._judge_own_motion("wa", "cover.a2", snapshot) is held
        assert ("cover.a2" in coordinator._cover_stop_holds) is held
        assert "cover.a2" not in coordinator._own_motion


async def test_the_first_cover_of_the_group_is_left_to_the_window_override(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    h.sun_up()
    tasks = h.refresh("ea")
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
    for callback in list(h.coord("ea")._cover_ownership._listeners["ea"]):
        callback()
    await h.settle()
    await h.drive(tasks)
    _stop_halfway(h, "cover.a1", 75)
    await _later(h, 2)
    assert _override(h, "ea", "wa") is not None
    assert h.coord("ea")._cover_stop_holds == {}, "the window override speaks for the first cover"


# --- lifetime of the protection ------------------------------------------------------------------

async def test_the_stop_hold_follows_the_override_duration_and_then_ends(zones):
    from datetime import timedelta
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    await _group_after_new_target(h, stop=True)
    hold = h.coord("ea")._cover_stop_holds["cover.a2"]
    assert hold.expires_at - hold.started_at == timedelta(minutes=120)       # the override duration
    await h.pass_time(100 * 60)
    _retarget(h, "ea", 20)
    await h.fire_ha_timers(GUARD_WAIT_S * 4)
    await h.drive(h.refresh("ea"))
    assert _sent_to(h, "cover.a2", 20) == [], "still inside the duration: still held"
    await h.pass_time(21 * 60)                   # past the regular end of the hold
    _retarget(h, "ea", 10)
    await h.fire_ha_timers(GUARD_WAIT_S * 4)
    await h.drive(h.refresh("ea"))
    assert _sent_to(h, "cover.a2", 10), f"after the regular release it drives again: {h.clock.calls()}"
    assert h.coord("ea")._cover_stop_holds == {}


async def test_clearing_the_manual_override_releases_the_stop_hold(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    await _group_after_new_target(h, stop=True)
    assert "cover.a2" in h.coord("ea")._cover_stop_holds
    assert await h.coord("ea").async_clear_manual_override("wa") is True
    assert h.coord("ea")._cover_stop_holds == {}
    await h.fire_ha_timers(GUARD_WAIT_S * 4)
    await h.drive(h.refresh("ea"))
    assert _sent_to(h, "cover.a2", 30), "after the explicit release the cover is driven"


async def test_losing_the_claim_on_the_cover_drops_what_this_zone_knew(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    await _group_after_new_target(h, stop=True)
    coordinator = h.coord("ea")
    assert "cover.a2" in coordinator._cover_stop_holds
    h.monkeypatch.setattr(coordinator, "_cover_role", lambda window_id, cover_id: "quarantined")
    for callback in list(coordinator._cover_ownership._listeners["ea"]):
        callback()
    await h.settle()
    assert coordinator._cover_stop_holds == {} and coordinator._own_motion == {}


async def test_safety_drives_a_held_cover_and_does_not_end_the_hold(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, "binary_sensor.rain_a")])
    await _group_after_new_target(h, stop=True)
    assert "cover.a2" in h.coord("ea")._cover_stop_holds
    h.rain("binary_sensor.rain_a")
    await h.drive(until=lambda: _sent_to(h, "cover.a2", 0))
    assert _sent_to(h, "cover.a2", 0), "Safety keeps its priority over the hold"
    await h.settle()
    assert "cover.a2" in h.coord("ea")._cover_stop_holds, "Safety alone does not delete the recognised stop"


async def test_hold_protects_a_waiting_package_after_the_window(zones):
    from datetime import datetime, timezone
    h = await zones.boot([
        ("ea", {"wa": ["cover.a1"]}, 50, None),
        ("eb", {"wb": ["cover.b1", "cover.b2"]}, 50, None),
    ])
    window_end = await _second_cover_after_cut(
        h, stop=True, entry="eb", covers=("cover.b1", "cover.b2"))
    _retarget(h, "eb", 30)
    await h.fire_ha_timers(GUARD_WAIT_S * 4)
    await h.drive(h.refresh("eb"))
    assert "cover.b2" in h.coord("eb")._cover_stop_holds        # recognised, protected beyond the window
    # let the window run out on both clocks without firing the zones' own periodic timers,
    # so the order of the two zones is the one this test sets up
    skip = max(window_end - h.clock.t + 1, 1) + GUARD_WAIT_S * 4
    h.clock.t += skip
    h.frozen.move_to(datetime.now(timezone.utc) + timedelta(seconds=skip))
    assert h.clock.t > window_end
    _retarget(h, "eb", 20)
    _retarget(h, "ea", 20)                       # zone A has a trip of its own to make
    started = len(h.clock.times_of("WAIT_START", "cover.a1"))
    tasks_a = h.refresh("ea")
    await h.drive(tasks_a, until=lambda: len(h.clock.times_of("WAIT_START", "cover.a1")) > started)
    tasks_b = h.refresh("eb")
    await h.drive(tasks_b + tasks_a, until=lambda: h.coord("eb")._package_state == "waiting")
    assert h.coord("eb")._package_state == "waiting", "zone B must plan its package behind zone A"
    await h.drive(tasks_a + tasks_b)
    assert _sent_to(h, "cover.a1", 20)
    assert _sent_to(h, "cover.b1", 20), "the other cover of the waiting package is driven"
    assert _sent_to(h, "cover.b2", 20) == [], f"sent past the hold: {h.clock.calls()}"
    assert "skipped:manual_stop" in h.coord("eb")._last_plan_completion["items"]


async def test_switching_active_control_on_releases_the_stop_hold(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    await _group_after_new_target(h, stop=True)
    assert "cover.a2" in h.coord("ea")._cover_stop_holds
    await h.coord("ea").async_set_zone_active_control_enabled("z_ea", True)
    await h.settle()
    assert h.coord("ea")._cover_stop_holds == {}


async def test_a_lifecycle_transition_releases_the_stop_hold(zones):
    from custom_components.smartshading.models.lifecycle import LifecycleState
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    await _group_after_new_target(h, stop=True)
    coordinator = h.coord("ea")
    assert "cover.a2" in coordinator._cover_stop_holds
    assert coordinator._lifecycle_state is LifecycleState.DAY
    coordinator._lifecycle_state = LifecycleState.NIGHT      # the next look sees the night end: a transition
    await h.drive(h.refresh("ea"))
    assert coordinator._lifecycle_state is not LifecycleState.NIGHT      # a real transition happened
    assert coordinator._cover_stop_holds == {}, "the new phase of the day ends the hold"


async def test_the_explanation_by_own_travel_ends_with_the_window(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1"]}, 50, None)])
    tasks = await _cut_while_travelling(h)
    await h.drive(tasks)
    window_end = _window_end(h, "cover.a1")
    h.sim.motion.pop("cover.a1", None)           # still reporting a travel, long after it should be over
    h.sim.covers.remove("cover.a1")
    h.hass.states.async_set("cover.a1", "closing", {"supported_features": 15, "current_position": 75})
    await h.pass_time(max(window_end - h.clock.t + 1, GUARD_WAIT_S * 4))
    assert _override(h, "ea", "wa") is not None, "an open-ended explanation would hide this for ever"


async def test_three_cover_group_stop_seen_after_the_window_holds_back_only_the_stopped_cover(zones):
    covers = ("cover.a1", "cover.a2", "cover.a3")
    h = await zones.boot([("ea", {"wa": list(covers)}, 50, None)])
    await _cut_during_second_cover(h)
    _stop_halfway(h, "cover.a2", 75)
    window_end = _window_end(h, "cover.a2")
    _retarget(h, "ea", 30)
    cycles = _count_cycles(h)
    await h.pass_time(max(window_end - h.clock.t + 1, GUARD_WAIT_S * 4))
    assert cycles and min(cycles) > window_end, "first looked at after the own-motion window"
    assert _sent_to(h, "cover.a1", 30) and _sent_to(h, "cover.a3", 30), h.clock.calls()
    assert _sent_to(h, "cover.a2", 30) == [], f"the stopped cover was driven again: {h.clock.calls()}"
    assert len(h.clock.calls_of("cover.a2")) == 1


async def test_a_plan_made_invalid_by_a_new_decision_does_not_get_round_the_stop_hold(zones):
    covers = ("cover.a1", "cover.a2", "cover.a3")
    h = await zones.boot([("ea", {"wa": list(covers)}, 50, None)])
    await _cut_during_second_cover(h)
    _stop_halfway(h, "cover.a2", 75)
    _retarget(h, "ea", 30)
    await h.fire_ha_timers(GUARD_WAIT_S * 4)
    await h.drive(h.refresh("ea"))
    assert "cover.a2" in h.coord("ea")._cover_stop_holds
    # a new trip begins; while the first cover is under way a newer decision invalidates the plan
    _retarget(h, "ea", 20)
    await h.fire_ha_timers(GUARD_WAIT_S * 4)
    started = len(h.clock.times_of("WAIT_START", "cover.a1"))
    tasks = h.refresh("ea")
    await h.drive(tasks, until=lambda: len(h.clock.times_of("WAIT_START", "cover.a1")) > started)
    assert _sent_to(h, "cover.a1", 20)
    calls_at_cut = len(h.clock.calls())
    for callback in list(h.coord("ea")._cover_ownership._listeners["ea"]):
        callback()
    await h.settle()
    await h.drive(tasks)
    await h.drive(horizon=h.clock.t + 600)
    assert len(h.clock.calls_of("cover.a2")) == 1, f"the old plan got round the hold: {h.clock.calls()}"
    assert len(_sent_to(h, "cover.a3", 20)) <= 1, f"the old plan was replayed: {h.clock.calls()}"
    assert "cover.a2" in h.coord("ea")._cover_stop_holds
    assert len(h.clock.calls()) - calls_at_cut <= 1, h.clock.calls()[calls_at_cut:]


# --- the hold survives a restart (same storage as the override) -----------------------------------

def _raw_hold(window="wa", **changes):
    now = dt_util.utcnow()
    entry = {"window_id": window, "position": 25, "started_at": now.isoformat(),
             "expires_at": (now + timedelta(minutes=90)).isoformat()}
    entry.update(changes)
    return entry


async def test_active_stop_hold_survives_a_restart_and_keeps_protecting_until_released(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    await _group_after_new_target(h, stop=True)
    old = h.coord("ea")
    assert "cover.a2" in old._cover_stop_holds
    expires_at = old._cover_stop_holds["cover.a2"].expires_at
    calls_before = list(h.clock.calls())
    h.frozen.move_to(dt_util.utcnow() + timedelta(minutes=30))     # half an hour passes before the restart
    assert await h.hass.config_entries.async_reload("ea")
    await h.settle()
    new = h.coord("ea")
    assert new is not old
    restored = new._cover_stop_holds.get("cover.a2")
    assert restored is not None and restored.expires_at == expires_at, "same expiry, not renewed"
    assert list(h.clock.calls()) == calls_before, "the restart replays nothing"
    _retarget(h, "ea", 20)
    await h.fire_ha_timers(GUARD_WAIT_S * 4)
    await h.drive(h.refresh("ea"))
    assert _sent_to(h, "cover.a1", 20), "the other cover keeps driving"
    assert _sent_to(h, "cover.a2", 20) == [], f"the restored hold did not protect: {h.clock.calls()}"
    assert await new.async_clear_manual_override("wa") is True            # the legitimate release
    await h.fire_ha_timers(GUARD_WAIT_S * 4)
    await h.drive(h.refresh("ea"))
    assert _sent_to(h, "cover.a2", 20), "after the release the cover is driven again"


async def test_expired_stop_hold_is_not_restored(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    await _group_after_new_target(h, stop=True)
    await h.pass_time(121 * 60)                              # past the end of the hold
    assert await h.hass.config_entries.async_reload("ea")
    await h.settle()
    assert h.coord("ea")._cover_stop_holds == {}


async def test_only_a_valid_hold_of_a_further_cover_this_zone_commands_is_restored(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    coordinator = h.coord("ea")
    now = dt_util.utcnow()
    raw = {
        "cover.a2": _raw_hold(),
        "cover.zz": _raw_hold(),                                       # no such cover
        "cover.a1": _raw_hold(),                                       # the window's first cover
        "cover.a3": _raw_hold(window="wx"),                            # no such window
        "cover.old": _raw_hold(expires_at=(now - timedelta(minutes=1)).isoformat()),   # expired
        "cover.nz": _raw_hold(expires_at=(now + timedelta(hours=1)).replace(tzinfo=None).isoformat()),
        "cover.bad1": _raw_hold(position="far"),
        "cover.bad2": {"window_id": "wa"},
        "cover.bad3": "garbage",
        "cover.bad4": None,
    }
    coordinator._restore_cover_stop_holds(raw, now)
    assert set(coordinator._cover_stop_holds) == {"cover.a2"}
    coordinator._cover_stop_holds.clear()
    coordinator._restore_cover_stop_holds({"cover.a2": _raw_hold(window="wx")}, now)
    assert coordinator._cover_stop_holds == {}
    coordinator._restore_cover_stop_holds(
        {"cover.a2": _raw_hold(expires_at=(now - timedelta(seconds=1)).isoformat())}, now)
    assert coordinator._cover_stop_holds == {}, "an expired hold is never resurrected"
    h.monkeypatch.setattr(coordinator, "_cover_role", lambda window_id, cover_id: "quarantined")
    coordinator._restore_cover_stop_holds({"cover.a2": _raw_hold()}, now)
    assert coordinator._cover_stop_holds == {}, "a cover this zone no longer commands gets no hold"


async def test_a_cover_removed_from_the_group_does_not_regain_its_hold_after_a_restart(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2", "cover.a3"]}, 50, None)])
    await _group_after_new_target(h, stop=True)
    coordinator = h.coord("ea")
    assert "cover.a2" in coordinator._cover_stop_holds
    saved = coordinator._cover_stop_holds_snapshot(dt_util.utcnow())
    assert "cover.a2" in saved
    group = coordinator.cover_groups["cg_wa"]
    object.__setattr__(group, "cover_ids", ["cover.a1", "cover.a3"])      # a2 is gone from the new configuration
    coordinator._cover_stop_holds.clear()
    coordinator._restore_cover_stop_holds(saved, dt_util.utcnow())
    assert coordinator._cover_stop_holds == {}


@pytest.mark.parametrize("raw", [None, [], "x", 7, {"cover.a2": 1}, {1: {"window_id": "wa"}}])
def test_corrupt_stored_holds_never_break_the_start(raw):
    from datetime import datetime, timezone
    from custom_components.smartshading.engines.learning_persistence import (
        LearningPersistenceConfig, deserialize_into_learning_store, serialize_learning_store,
    )
    from custom_components.smartshading.engines.learning_store import LearningStore
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    data = serialize_learning_store(LearningStore(), LearningPersistenceConfig(), now)
    data["cover_stop_holds"] = raw
    extras = deserialize_into_learning_store(data, LearningStore(), LearningPersistenceConfig(), now)
    assert isinstance(extras.cover_stop_holds, dict)
    assert all(isinstance(k, str) and isinstance(v, dict) for k, v in extras.cover_stop_holds.items())


# --- the release strategies of the override release the stop hold too ------------------------------

def _decide_as(h, category):
    """Make the decision of this cycle a ``category`` one (the real release path stays in use)."""
    from dataclasses import replace
    from custom_components.smartshading.state_machine.states import DecisionCategory
    orchestrator = h.coord("ea")._tier_orchestrator
    original = orchestrator.evaluate_window

    def evaluate_window(wdi):
        return replace(original(wdi), category=DecisionCategory[category])

    h.monkeypatch.setattr(orchestrator, "evaluate_window", evaluate_window)


@pytest.mark.parametrize("strategy, category, released", [
    ("FIRST_COMFORT", "COMFORT", True),
    ("FIRST_COMFORT", "PROTECTION", False),
    ("FIRST_PROTECTION", "PROTECTION", True),
    ("FIRST_PROTECTION", "COMFORT", False),
    ("FIRST_ANY_DECISION", "COMFORT", True),
    ("FIRST_ANY_DECISION", "PROTECTION", True),
    ("DURATION", "COMFORT", False),
    ("DURATION", "PROTECTION", False),
    ("MANUAL", "COMFORT", False),
    ("LIFECYCLE", "PROTECTION", False),
])
async def test_the_release_strategy_decides_whether_a_decision_releases_the_stop_hold(
    zones, strategy, category, released,
):
    from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    h.coord("ea")._override_release_strategy = OverrideReleaseStrategy[strategy]
    await _group_after_new_target(h, stop=True)
    assert "cover.a2" in h.coord("ea")._cover_stop_holds, "not released by the decision of its own cycle"
    assert _sent_to(h, "cover.a2", 30) == []
    _decide_as(h, category)
    _retarget(h, "ea", 20)
    await h.fire_ha_timers(GUARD_WAIT_S * 4)
    await h.drive(h.refresh("ea"))
    assert _sent_to(h, "cover.a1", 20), "the other cover drives on in either case"
    if released:
        assert "cover.a2" not in h.coord("ea")._cover_stop_holds
        assert _sent_to(h, "cover.a2", 20), f"released by the decision, the cover drives: {h.clock.calls()}"
    else:
        assert "cover.a2" in h.coord("ea")._cover_stop_holds
        assert _sent_to(h, "cover.a2", 20) == [], f"released by a decision that does not qualify: {h.clock.calls()}"


async def test_a_release_decision_only_releases_the_holds_of_its_own_window(zones):
    from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy
    from custom_components.smartshading.state_machine.states import DecisionCategory
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"], "wb": ["cover.b1", "cover.b2"]}, 50, None)])
    coordinator = h.coord("ea")
    coordinator._override_release_strategy = OverrideReleaseStrategy.FIRST_ANY_DECISION
    now = dt_util.utcnow()
    coordinator._restore_cover_stop_holds(
        {"cover.a2": _raw_hold("wa"), "cover.b2": _raw_hold("wb")}, now)
    assert set(coordinator._cover_stop_holds) == {"cover.a2", "cover.b2"}
    later = now + timedelta(minutes=5)
    coordinator._release_stop_holds_by_policy("wa", DecisionCategory.HOLD, later)
    assert set(coordinator._cover_stop_holds) == {"cover.a2", "cover.b2"}, "no such decision, nothing released"
    coordinator._release_stop_holds_by_policy("wb", DecisionCategory.COMFORT, later)
    assert set(coordinator._cover_stop_holds) == {"cover.a2"}


async def test_a_stop_recognised_in_a_cycle_is_not_released_by_that_cycles_own_decision(zones):
    from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    h.coord("ea")._override_release_strategy = OverrideReleaseStrategy.FIRST_PROTECTION   # the decision here is a protection one
    await _cut_during_second_cover(h)
    _stop_halfway(h, "cover.a2", 75)
    window_end = _window_end(h, "cover.a2")
    _retarget(h, "ea", 30)
    await h.pass_time(max(window_end - h.clock.t + 1, GUARD_WAIT_S * 4))     # the look that recognises it
    assert "cover.a2" in h.coord("ea")._cover_stop_holds
    assert _sent_to(h, "cover.a1", 30) and _sent_to(h, "cover.a2", 30) == [], h.clock.calls()
    _retarget(h, "ea", 20)                                                    # the next decision releases it
    await h.fire_ha_timers(GUARD_WAIT_S * 4)
    await h.drive(h.refresh("ea"))
    assert "cover.a2" not in h.coord("ea")._cover_stop_holds
    assert _sent_to(h, "cover.a2", 20)
