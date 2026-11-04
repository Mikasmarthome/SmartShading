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
