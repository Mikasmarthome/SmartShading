"""Global zone packages in real Home Assistant: zone-by-zone normal travel.

Several real zone coordinators share the one ``GlobalSerialDispatch``.  Normal
travel of one zone is worked off as one contiguous package before the next zone's
package starts; the pacing (completion + 2 s settle after an intermediate command,
2 s start spacing after an end-position command) holds across the zone boundary;
Safety never waits for a package.  Time is virtual (see the harness in conftest.py).
"""
from __future__ import annotations

import asyncio
import json

import pytest


TRAVEL_S = 20.0             # the simulated cover's travel time (see the harness in conftest.py)
S = TRAVEL_S + 2.0          # one intermediate cover: travel + settle


def _zone_of(cover: str) -> str:
    return cover.split(".")[1][0]


def _order(h) -> str:
    return "".join(c[1].split(".")[1][0] + c[1].split(".")[1][1] for c in h.clock.calls())


def _gaps(h):
    times = [c[0] for c in h.clock.calls()]
    return [round(b - a, 2) for a, b in zip(times, times[1:])]


async def _run(h, *entry_ids):
    h.sun_up()
    tasks = h.refresh(*entry_ids)
    await h.drive(tasks)
    return tasks


# --- 1. two zones, two intermediate covers each --------------------------------------------

async def test_two_zones_intermediate_run_zone_by_zone(zones):
    h = await zones.boot([
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1", "cover.b2"]}, 50, None),
    ])
    await _run(h)
    calls = h.clock.calls()
    assert [_zone_of(c[1]) for c in calls] == ["a", "a", "b", "b"] or [_zone_of(c[1]) for c in calls] == ["b", "b", "a", "a"]
    assert [c[2] for c in calls] == [50] * 4
    # completion + settle between every two commands, also across the zone boundary
    assert _gaps(h) == [S, S, S]
    # exactly one settle at the boundary: the first cover of the second zone starts
    # 2 s after the last completion of the first zone
    last_end = max(t for t in h.clock.times_of("WAIT_END") if t < calls[2][0])
    assert round(calls[2][0] - last_end, 2) == 2.0


# --- 2. three zones, different package lengths -------------------------------------------------

async def test_three_zones_different_lengths_stay_contiguous(zones):
    h = await zones.boot([
        ("ea", {"wa": ["cover.a1", "cover.a2", "cover.a3"]}, 50, None),
        ("eb", {"wb": ["cover.b1"]}, 50, None),
        ("ec", {"wc": ["cover.c1", "cover.c2"]}, 50, None),
    ])
    await _run(h)
    letters = [_zone_of(c[1]) for c in h.clock.calls()]
    assert len(letters) == 6
    runs = [letters[0]] + [b for a, b in zip(letters, letters[1:]) if a != b]
    assert sorted(runs) == ["a", "b", "c"], f"a zone was interleaved: {letters}"
    assert _gaps(h) == [S] * 5


# --- 3. several groups in one zone ---------------------------------------------------------------

async def test_groups_of_one_zone_keep_their_order_before_the_next_zone(zones):
    h = await zones.boot([
        ("ea", {"wa1": ["cover.a1", "cover.a2"], "wa2": ["cover.a3", "cover.a4"]}, 50, None),
        ("eb", {"wb": ["cover.b1"]}, 50, None),
    ])
    await _run(h)
    covers = [c[1] for c in h.clock.calls()]
    assert covers[:4] == ["cover.a1", "cover.a2", "cover.a3", "cover.a4"] or covers[1:] == [
        "cover.a1", "cover.a2", "cover.a3", "cover.a4"]
    assert covers[-1] == "cover.b1" or covers[0] == "cover.b1"
    assert _gaps(h) == [S] * 4


# --- 4. end positions: global 2 s start spacing ----------------------------------------------------

@pytest.mark.parametrize("shade,target,initial", [(0, 0, 100)])
async def test_end_position_packages_use_the_global_start_spacing(zones, shade, target, initial):
    covers_a, covers_b = ["cover.a1", "cover.a2", "cover.a3"], ["cover.b1", "cover.b2"]
    h = await zones.boot(
        [("ea", {"wa": covers_a}, shade, None), ("eb", {"wb": covers_b}, shade, None)])
    for cover in covers_a + covers_b:               # moved after boot: no boot-time dispatch
        h.sim.pos[cover] = initial
    h.sim.tick()
    await _run(h)
    calls = h.clock.calls()
    assert len(calls) == 5, calls
    assert all(c[2] == target for c in calls)
    letters = [_zone_of(c[1]) for c in calls]
    runs = [letters[0]] + [b for a, b in zip(letters, letters[1:]) if a != b]
    assert len(runs) == 2, f"zones interleaved: {letters}"
    assert _gaps(h) == [2.0] * 4                    # also across the zone boundary
    assert h.clock.times_of("WAIT_START") == []     # no completion is awaited


# --- 5. mixed targets between zones -----------------------------------------------------------------

async def test_endpoint_zone_after_intermediate_zone_waits_for_completion_and_settle(zones):
    h = await zones.boot([
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1", "cover.b2"]}, 0, None),
    ])
    await _run(h)
    calls = h.clock.calls()
    letters = [_zone_of(c[1]) for c in calls]
    assert letters in (["a", "a", "b", "b"], ["b", "b", "a", "a"]), letters
    if letters[0] == "a":
        # intermediate package first: completion + settle, then the end positions
        assert _gaps(h) == [S, S, 2.0]
    else:
        # end positions first: 2 s after the last START, then the intermediate cover
        assert _gaps(h) == [2.0, 2.0, S]


# --- 21. the end-position skip --------------------------------------------------------------------------

async def test_end_position_group_sends_every_cover_not_only_the_first(zones):
    covers = ["cover.a1", "cover.a2", "cover.a3"]
    h = await zones.boot([("ea", {"wa": covers}, 0, None)])
    await _run(h)
    assert [c[1] for c in h.clock.calls()] == covers
    assert [c[2] for c in h.clock.calls()] == [0, 0, 0]
    assert _gaps(h) == [2.0, 2.0]
    plan = h.coord("ea")._last_plan_completion
    assert plan["items"] == {"dispatched": 3}, plan


# --- 22. diagnostics -----------------------------------------------------------------------------------------

async def test_package_diagnostics_are_json_safe_and_primitive(zones):
    h = await zones.boot([
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1"]}, 50, None),
    ])
    await _run(h)
    for entry_id in ("ea", "eb"):
        diag = h.coord(entry_id).lifecycle_diagnostics()
        text = json.dumps(diag)
        assert "cover." not in text and "ea" not in diag["packages"]["last"]
        package = diag["last_plan_completion"]["package"]
        assert package["outcome"] == "granted" and "wait_ms" in package and "position" in package
        assert diag["packages"]["this_zone"] == "idle"
        assert diag["packages"]["global"]["active"] is False
        assert diag["packages"]["global"]["queued"] == 0
        assert diag["packages"]["pacing"]["basis"] in ("observed", "estimated")
    positions = [h.coord(i)._last_package.get("position") for i in ("ea", "eb")]
    assert sorted(positions) == [0, 1], "the second zone queued behind the first"


# ============================================================================
# Safety never waits for a package
# ============================================================================

RAIN = "binary_sensor.rain_c"


def _normal(h, since=0.0):
    return [c for c in h.clock.calls() if c[2] == 50 and c[0] >= since]


def _retractions(h):
    return [c for c in h.clock.calls() if c[2] == 0]


async def _rain_scenario(zones, *, rain_on_zone="ec", when):
    """Zone A (3 covers) is the active package, zone B (2 covers) waits, zone C has
    a rain sensor.  ``when`` picks the moment of the rain: ``completion``,
    ``settle`` or ``queued`` (B is still waiting for the package)."""
    h = zones
    await h.boot([
        ("ea", {"wa": ["cover.a1", "cover.a2", "cover.a3"]}, 50, None),
        ("eb", {"wb": ["cover.b1", "cover.b2"]}, 50, None),
        ("ec", {"wc": ["cover.c1"]}, 50, RAIN),
    ])
    h.sun_up()
    tasks = h.refresh("ea", "eb")
    if when == "completion":
        await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
    elif when == "settle":
        await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_END", "cover.a1"))
    else:
        await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
        assert h.coord("eb")._package_state == "waiting", "zone B must be waiting for the package"
    rain_at = h.clock.t
    sent_before = len(h.clock.calls())
    h.rain(RAIN)
    return h, tasks, rain_at, sent_before


@pytest.mark.parametrize("when", ["completion", "settle", "queued"])
async def test_safety_of_a_third_zone_is_sent_at_once_and_ends_the_normal_plans(zones, when):
    h, tasks, rain_at, sent_before = await _rain_scenario(zones, when=when)
    rain_wall = h.coord("ea").data.updated_at
    await h.drive(tasks, until=lambda: any(c[1] == "cover.c1" and c[2] == 0 for c in h.clock.calls()))
    retraction = [c for c in h.clock.calls() if c[1] == "cover.c1" and c[2] == 0]
    assert retraction and retraction[0][0] == rain_at, "Safety must not wait for any package or pacing"
    await h.settle()
    # nothing normal was sent from the preempted plans at the moment of the rain
    assert [c for c in h.clock.calls()[sent_before:] if c[2] == 50 and c[0] == rain_at] == []
    # fresh evaluation, not a replay: every normal cover ends up with its target
    # exactly once (the debounced request is driven with controlled HA time)
    for _ in range(8):
        await h.fire_ha_timers(15)
        await h.drive(horizon=h.clock.t + 30)
    sent = [c[1] for c in h.clock.calls() if c[2] == 50 and c[1] != "cover.c1"]
    assert len(sent) == len(set(sent)), f"a command was replayed: {sent}"
    # zone B's preempted plan was evaluated afresh and sent in full ...
    assert {"cover.b1", "cover.b2"} <= set(sent), sent
    # ... and zone A was evaluated afresh too (its own decision, e.g. the override
    # logic, may still hold the remaining covers back -- that is not a replay)
    assert h.coord("ea").data.updated_at > rain_wall, "zone A was not evaluated again"
    assert h.serial.packages.stats()["queued"] == 0 and not h.serial.packages.active


async def test_safety_in_a_zone_whose_own_cycle_waits_for_the_package(zones):
    h = zones
    await h.boot([
        ("ea", {"wa": ["cover.a1", "cover.a2", "cover.a3"]}, 50, None),
        ("eb", {"wb": ["cover.b1", "cover.b2"]}, 50, "binary_sensor.rain_b"),
    ])
    h.sun_up()
    tasks = h.refresh("ea", "eb")
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
    assert h.coord("eb")._package_state == "waiting"
    assert h.serial.packages.waiting_position("eb", h.coord("eb")) == 1
    rain_at = h.clock.t
    h.rain("binary_sensor.rain_b")
    await h.drive(tasks, until=lambda: len([c for c in _retractions(h) if c[1].startswith("cover.b")]) == 2)
    retract = [c for c in _retractions(h) if c[1].startswith("cover.b")]
    assert sorted(c[1] for c in retract) == ["cover.b1", "cover.b2"]
    assert all(c[0] == rain_at for c in retract), "the waiting zone's own Safety is evaluated and sent at once"
    assert h.clock.calls_of("cover.b") and all(c[2] == 0 for c in h.clock.calls_of("cover.b"))
    await h.settle()


async def test_safety_in_the_active_package_during_execution(zones):
    h = zones
    await h.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, "binary_sensor.rain_a"),
                  ("eb", {"wb": ["cover.b1"]}, 50, None)])
    h.sun_up()
    tasks = h.refresh("ea", "eb")
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
    rain_at = h.clock.t
    h.rain("binary_sensor.rain_a")
    await h.drive(tasks, until=lambda: len(_retractions(h)) >= 2)
    assert {c[1] for c in _retractions(h)} >= {"cover.a1", "cover.a2"}
    assert all(c[0] == rain_at for c in _retractions(h))
    assert [c for c in _normal(h, rain_at) if c[1] in ("cover.a2",)] == []
    await h.settle()


async def test_several_simultaneous_safety_requests_all_overtake_the_package_queue(zones):
    h = zones
    await h.boot([
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1"]}, 50, "binary_sensor.rain_b"),
        ("ec", {"wc": ["cover.c1"]}, 50, RAIN),
    ])
    h.sun_up()
    tasks = h.refresh("ea")
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
    rain_at = h.clock.t
    h.rain("binary_sensor.rain_b")
    h.rain(RAIN)
    await h.drive(tasks, until=lambda: {c[1] for c in _retractions(h)} >= {"cover.b1", "cover.c1"})
    assert {c[1] for c in _retractions(h)} >= {"cover.b1", "cover.c1"}
    assert all(c[0] == rain_at for c in _retractions(h) if c[1] in ("cover.b1", "cover.c1"))
    await h.settle()


# ============================================================================
# Waiting packages: coalescence, fairness, replacement
# ============================================================================

async def _active_and_waiting(zones, specs):
    h = await zones.boot(specs)
    h.sun_up()
    tasks = h.refresh(*[spec[0] for spec in specs])
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
    return h, tasks


async def test_waiting_package_is_coalesced_and_the_old_generation_never_sends(zones):
    h, tasks = await _active_and_waiting(zones, [
        ("ea", {"wa": ["cover.a1", "cover.a2", "cover.a3"]}, 50, None),
        ("eb", {"wb": ["cover.b1", "cover.b2"]}, 50, None),
    ])
    assert h.coord("eb")._package_state == "waiting"
    more = h.refresh("eb") + h.refresh("eb") + h.refresh("eb")
    await h.settle()
    assert h.serial.packages.queued_count == 1, "one waiting package per zone"
    await h.drive(tasks + more)
    covers = [c[1] for c in h.clock.calls()]
    assert covers == ["cover.a1", "cover.a2", "cover.a3", "cover.b1", "cover.b2"], covers
    assert _gaps(h) == [S] * 4
    assert h.serial.packages.queued_count == 0 and not h.serial.packages.active


async def test_frequent_updates_of_one_zone_do_not_starve_the_others(zones):
    h, tasks = await _active_and_waiting(zones, [
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1"]}, 50, None),
        ("ec", {"wc": ["cover.c1"]}, 50, None),
    ])
    more = []
    for _ in range(6):                      # zone B keeps being re-evaluated while it waits
        more += h.refresh("eb")
        await h.settle()
    await h.drive(tasks + more)
    zones_in_order = [_zone_of(c[1]) for c in h.clock.calls()]
    assert zones_in_order == ["a", "a", "b", "c"], zones_in_order    # B kept its place before C
    assert _gaps(h) == [S] * 3


async def test_replacing_the_active_package_during_travel_keeps_the_boundary(zones):
    h, tasks = await _active_and_waiting(zones, [
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1", "cover.b2"]}, 50, None),
    ])
    started = h.clock.times_of("WAIT_START", "cover.a1")[0]
    more = h.refresh("ea")                  # the running package is replaced mid-travel
    await h.drive(tasks + more)
    calls = h.clock.calls()
    b_calls = [c for c in calls if c[1].startswith("cover.b")]
    assert [c[1] for c in b_calls] == ["cover.b1", "cover.b2"], calls
    # the abandoned travel is not forgotten: the next package starts only after its
    # conservative end (estimated travel + settle), never right at the replacement
    assert b_calls[0][0] - started >= 17.0, (started, b_calls)


async def test_timeout_releases_with_the_bounded_settle_and_the_rest_is_not_repeated(zones):
    h = await zones.boot(
        [("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
         ("eb", {"wb": ["cover.b1"]}, 50, None)],
        silent=("cover.a1",))
    await _run(h)
    calls = h.clock.calls()
    assert [c[1] for c in calls] == ["cover.a1", "cover.b1"], calls      # a2 is not started
    ended = h.clock.times_of("WAIT_END", "cover.a1")[0]
    assert round(calls[1][0] - ended, 2) == 2.0
    assert any(k == "WAIT_END" and "timeout" in w for _, k, w in h.clock.log)
    assert h.coord("ea")._last_package["pacing_basis"] == "timeout_unconfirmed"


# ============================================================================
# Unload / reload, ownership
# ============================================================================

async def _unload(h, entry_id):
    task = asyncio.ensure_future(h.hass.config_entries.async_unload(entry_id))
    await h.drive([task])
    assert task.result() is True
    h.entries.pop(entry_id)         # nothing may touch the removed runtime_data any more


async def test_unloading_a_waiting_zone_removes_only_its_ticket(zones):
    h, tasks = await _active_and_waiting(zones, [
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1"]}, 50, None),
        ("ec", {"wc": ["cover.c1"]}, 50, None),
    ])
    assert h.serial.packages.queued_count == 2
    await _unload(h, "eb")
    assert h.serial.packages.queued_count == 1, "only the unloaded zone's ticket is gone"
    await h.drive(tasks)
    assert [c[1] for c in h.clock.calls()] == ["cover.a1", "cover.a2", "cover.c1"]
    assert h.serial.packages.queued_count == 0 and not h.serial.packages.active


async def test_unloading_the_active_zone_releases_the_package_for_the_next(zones):
    h, tasks = await _active_and_waiting(zones, [
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1"]}, 50, None),
    ])
    started = h.clock.times_of("WAIT_START", "cover.a1")[0]
    await _unload(h, "ea")
    await h.drive(tasks)
    calls = h.clock.calls()
    assert [c[1] for c in calls] == ["cover.a1", "cover.b1"], calls
    assert calls[1][0] - started >= 17.0        # bounded conservative pacing, not zero
    assert h.serial.packages.queued_count == 0 and not h.serial.packages.active
    assert h.serial.packages.stats()["kept_positions"] == 0


async def test_reloading_a_zone_starts_clean_and_sends_its_package_once(zones):
    h, tasks = await _active_and_waiting(zones, [
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1"]}, 50, None),
    ])
    await _unload(h, "eb")
    await h.drive(tasks)
    assert h.serial.packages.queued_count == 0
    await h.add_zone("eb", {"wb": ["cover.b1"]}, 50)
    more = h.refresh("eb")
    await h.drive(more)
    assert [c[1] for c in h.clock.calls()].count("cover.b1") == 1
    assert h.serial.packages.queued_count == 0 and not h.serial.packages.active


async def test_ownership_change_during_a_package_drops_the_stale_plan(zones):
    h, tasks = await _active_and_waiting(zones, [
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1"]}, 50, None),
    ])
    stale = h.coord("ea")._ownership_stats["plan_invalidations"]
    # the roles of zone A's covers change under the running plan: the registry calls
    # the coordinator's real ownership listener, exactly as on a claim change
    registry = h.coord("ea")._cover_ownership
    for callback in list(registry._listeners["ea"]):
        callback()
    await h.drive(tasks)
    assert h.coord("ea")._ownership_stats["plan_invalidations"] == stale + 1
    assert [c[1] for c in h.clock.calls()].count("cover.a2") <= 1, "the stale plan is not replayed"
    assert h.serial.packages.queued_count == 0 and not h.serial.packages.active


# ============================================================================
# Normal fall-through and cover-specific validation
# ============================================================================

async def test_covers_without_movement_never_fall_through_around_the_package(zones):
    h = await zones.boot(
        [("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
         ("eb", {"wb": ["cover.b1", "cover.b2"]}, 50, None)],
        initial={"cover.a1": 50})
    await _run(h)
    calls = h.clock.calls()
    assert "cover.a1" not in [c[1] for c in calls]       # already there: nothing is sent
    assert sorted(c[1] for c in calls) == ["cover.a2", "cover.b1", "cover.b2"]
    assert _gaps(h) == [S, S]
    for entry_id in ("ea", "eb"):
        assert h.coord(entry_id)._package_stats["normal_bypass_refused"] == 0


async def _three_covers(zones, *, initial=None, motion=None, shade=0, unavailable=()):
    covers = ["cover.a1", "cover.a2", "cover.a3"]
    h = await zones.boot([("ea", {"wa": covers}, shade, None)], initial={c: 100 for c in covers})
    for cover, pos in (initial or {}).items():
        h.sim.pos[cover] = pos
    for cover, spec in (motion or {}).items():
        h.sim.motion[cover] = spec
    h.sim.tick()
    for cover in unavailable:
        h.sim.covers.remove(cover)          # the simulation must not publish over it
        h.hass.states.async_set(cover, "unavailable")
    await _run(h)
    return h


async def test_first_cover_travelling_does_not_hide_the_others(zones):
    # the base read cover 1 only: a moving first cover skipped covers 2 and 3
    h = await _three_covers(zones, motion={"cover.a1": (0.0, 100, 0)})
    assert [c[1] for c in h.clock.calls()] == ["cover.a2", "cover.a3"]


async def test_first_cover_at_target_does_not_hide_the_others(zones):
    h = await _three_covers(zones, initial={"cover.a1": 0})
    assert [c[1] for c in h.clock.calls()] == ["cover.a2", "cover.a3"]
    assert _gaps(h) == [2.0]


async def test_second_cover_already_travelling_to_the_target_is_not_sent_again(zones):
    h = await _three_covers(zones, motion={"cover.a2": (0.0, 100, 0)})
    assert [c[1] for c in h.clock.calls()] == ["cover.a1", "cover.a3"]


async def test_unavailable_cover_is_skipped_and_the_rest_still_moves(zones):
    h = await _three_covers(zones, unavailable=("cover.a2",))
    assert [c[1] for c in h.clock.calls()] == ["cover.a1", "cover.a3"]


async def test_intermediate_positions_with_different_actual_positions(zones):
    h = await _three_covers(zones, shade=50, initial={"cover.a1": 50, "cover.a2": 100, "cover.a3": 20})
    assert [c[1] for c in h.clock.calls()] == ["cover.a2", "cover.a3"]
    assert _gaps(h) == [S]
    # the completion wait reads the cover that was really sent, not the first one
    assert [w for _, k, w in h.clock.log if k == "WAIT_START"] == ["cover.a2", "cover.a3"]


async def test_a_debounced_refresh_drops_the_plan_of_a_waiting_zone(zones):
    h, tasks = await _active_and_waiting(zones, [
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1"]}, 50, None),
    ])
    cancellation = h.coord("eb")._active_dispatch_cancellation
    assert h.coord("eb")._package_state == "waiting" and not cancellation.is_set()
    h.coord("eb")._request_refresh(h.entries["eb"], "test_refresh")
    assert cancellation.is_set(), "the waiting plan holds the refresh lock and must end at once"
    await h.drive(tasks)
    await h.fire_ha_timers(15)
    await h.drive(horizon=h.clock.t + 60)
    assert [c[1] for c in h.clock.calls()].count("cover.b1") <= 1
