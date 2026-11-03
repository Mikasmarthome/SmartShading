"""Own commands are not manual interventions: override detection in real Home Assistant.

Real zone coordinators, the real shared dispatch, the real override detector, own
command bookkeeping, cover ownership and Safety listeners; simulated covers and virtual
time (see the harness in conftest.py).  A cover that was commanded by SmartShading and
has not reported its new position yet -- the plan was cut short by Safety or by a newer
plan, or the feedback lags -- must not be read as manual interference on the next
evaluation, while a real manual move stays detected.
"""
from __future__ import annotations

import pytest
from homeassistant.util import dt as dt_util

RAIN = "binary_sensor.rain_c"
GUARD_WAIT_S = 100          # one step of controlled Home Assistant time (see _later)


def _override(h, entry, window):
    return h.coord(entry)._override_detector.get(window, dt_util.utcnow())


def _diag(h, entry, window):
    return h.coord(entry).data.execution_diagnostics[window]


def _learned_overrides(h, entry, window):
    return h.coord(entry)._learning_store.get_overrides(window)


def _manual_move(h, cover, ha_position):
    """A person drives the cover somewhere (no SmartShading command involved)."""
    h.sim.motion.pop(cover, None)
    h.sim.pos[cover] = ha_position
    h.sim.publish(cover)


async def _later(h, rounds=8):
    """Let Home Assistant's own timers (debounce, minimum action interval) run out."""
    for _ in range(rounds):
        await h.fire_ha_timers(GUARD_WAIT_S)
        await h.drive(horizon=h.clock.t + 30)


def _sent_to(h, cover, target):
    return [c for c in h.clock.calls() if c[1] == cover and c[2] == target]


async def _cut_after_first_command(h, entry="ea", cover="cover.a1"):
    """Zone ``entry`` starts its plan; once ``cover`` has been commanded (and has not
    reported anything new) the running plan is invalidated -- the roles of its covers
    change, exactly as on a claim change -- so the cycle ends mid-travel."""
    h.sun_up()
    tasks = h.refresh(entry)
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", cover))
    coordinator = h.coord(entry)
    registry = coordinator._cover_ownership
    for callback in list(registry._listeners[entry]):
        callback()
    await h.settle()
    assert cover in {c[1] for c in h.clock.calls()}
    return tasks


# --- 1. own normal target, delayed feedback ------------------------------------------------

async def test_own_normal_command_with_delayed_feedback_is_not_an_override(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)], learning=("ea",))
    tasks = await _cut_after_first_command(h)
    assert h.coord("ea")._own_motion, "the plan was cut while a1 had not reported yet"
    await h.drive(tasks)
    await _later(h)
    assert _override(h, "ea", "wa") is None
    assert _diag(h, "ea", "wa").command_blocked_reason != "manual_override"
    assert len(_sent_to(h, "cover.a1", 50)) == 1, "the travelling cover must not be commanded again"
    assert _learned_overrides(h, "ea", "wa") == []


# --- 2. own Safety command, delayed feedback -----------------------------------------------

async def test_own_safety_command_with_delayed_feedback_is_not_an_override(zones):
    h = await zones.boot([("ec", {"wc": ["cover.c1"]}, 50, RAIN)], learning=("ec",))
    h.sun_up()
    h.rain(RAIN)
    await h.drive(until=lambda: _sent_to(h, "cover.c1", 0))
    await h.settle()
    h.rain(RAIN, on=False)                  # the retraction has not been reported back yet
    await h.drive(h.refresh("ec"))
    await _later(h, 22)                     # past the Safety release hold: the zone is judged afresh
    # the Safety hold is over and the zone was evaluated afresh (a normal evaluator
    # decided); the cover still shows its old position and is not held back as overridden
    assert _diag(h, "ec", "wc").tier_decided_by == "GlareEvaluator"
    assert _override(h, "ec", "wc") is None
    assert _diag(h, "ec", "wc").command_blocked_reason != "manual_override"
    assert _learned_overrides(h, "ec", "wc") == []


# --- 3. first cover at the target, sibling not ----------------------------------------------

async def test_first_cover_at_target_and_sibling_not_is_no_override(zones):
    h = await zones.boot(
        [("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)],
        initial={"cover.a1": 50, "cover.a2": 100}, learning=("ea",))
    h.sun_up()
    tasks = h.refresh("ea")
    await h.drive(tasks)
    await _later(h, 2)
    assert [c[1] for c in h.clock.calls()] == ["cover.a2"]
    assert _override(h, "ea", "wa") is None
    assert _learned_overrides(h, "ea", "wa") == []


# --- 4. real manual intervention during a normal run -----------------------------------------

async def test_manual_move_during_a_normal_run_is_still_detected(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    h.sun_up()
    tasks = h.refresh("ea")
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
    _manual_move(h, "cover.a1", 10)         # far outside the way from 100 to 50
    await h.drive(tasks)
    more = h.refresh("ea")
    await h.drive(more)
    override = _override(h, "ea", "wa")
    assert override is not None and override.override_position == 90
    assert _diag(h, "ea", "wa").command_blocked_reason == "manual_override"
    await h.settle()


# --- 5. real manual intervention after a normal run -------------------------------------------

async def test_manual_move_after_a_normal_run_is_detected_at_once(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1"]}, 50, None)])
    h.sun_up()
    tasks = h.refresh("ea")
    await h.drive(tasks)
    assert h.sim.pos["cover.a1"] == 50
    _manual_move(h, "cover.a1", 80)         # between the old start and the target, but the run is over
    more = h.refresh("ea")
    await h.drive(more)
    override = _override(h, "ea", "wa")
    assert override is not None and override.override_position == 20
    assert len(_sent_to(h, "cover.a1", 50)) == 1


# --- 6. Safety during a normal multi-zone sequence ---------------------------------------------

async def test_safety_during_a_multi_zone_sequence_leaves_no_false_override(zones):
    h = await zones.boot([
        ("ea", {"wa": ["cover.a1", "cover.a2", "cover.a3"]}, 50, None),
        ("eb", {"wb": ["cover.b1", "cover.b2"]}, 50, None),
        ("ec", {"wc": ["cover.c1"]}, 50, RAIN),
    ], learning=("ea", "eb"))
    h.sun_up()
    tasks = h.refresh("ea", "eb")
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
    h.rain(RAIN)
    await h.drive(tasks, until=lambda: _sent_to(h, "cover.c1", 0))
    await h.settle()
    await _later(h)
    for entry, window in (("ea", "wa"), ("eb", "wb")):
        assert _override(h, entry, window) is None, entry
        assert _diag(h, entry, window).command_blocked_reason != "manual_override", entry
        assert _learned_overrides(h, entry, window) == [], entry
    # no replay: every normal cover got its target exactly once, none from the old plan twice
    normal = [c[1] for c in h.clock.calls() if c[2] == 50]
    assert sorted(normal) == ["cover.a1", "cover.a2", "cover.a3", "cover.b1", "cover.b2"], normal


# --- 7. Safety ends, both zones are evaluated afresh ---------------------------------------------

async def test_after_safety_both_zones_are_judged_on_their_own_merits(zones):
    h = await zones.boot([
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1", "cover.b2"]}, 50, None),
        ("ec", {"wc": ["cover.c1"]}, 50, RAIN),
    ])
    h.sun_up()
    tasks = h.refresh("ea", "eb")
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
    h.rain(RAIN)
    await h.drive(tasks, until=lambda: _sent_to(h, "cover.c1", 0))
    await h.settle()
    h.rain(RAIN, on=False)
    await _later(h)
    # zone A had commanded a1 before Safety, zone B had not commanded anything: neither
    # is held back by a manual override, and the business reasons are the plain ones
    for entry, window in (("ea", "wa"), ("eb", "wb")):
        assert _override(h, entry, window) is None, entry
    assert {"cover.a1", "cover.a2", "cover.b1", "cover.b2"} <= {c[1] for c in h.clock.calls() if c[2] == 50}


# --- 8. an existing real manual override stays preserved ----------------------------------------

async def test_existing_manual_override_survives_safety_elsewhere(zones):
    h = await zones.boot([
        ("eb", {"wb": ["cover.b1"]}, 50, None),
        ("ec", {"wc": ["cover.c1"]}, 50, RAIN),
    ], initial={"cover.b1": 100})
    h.sun_up()
    await h.drive(h.refresh("eb"))          # b1 is moved to 50 by SmartShading
    _manual_move(h, "cover.b1", 10)         # then a person takes over
    await h.drive(h.refresh("eb"))
    assert _override(h, "eb", "wb") is not None
    sent_before = len(h.clock.calls_of("cover.b"))
    h.rain(RAIN)
    await h.drive(until=lambda: _sent_to(h, "cover.c1", 0))
    await h.settle()
    await _later(h, 3)
    assert _override(h, "eb", "wb") is not None, "Safety in another zone must not release it"
    assert len(h.clock.calls_of("cover.b")) == sent_before, "no command to the overridden cover"


# --- 9. unload / reload and stale bookkeeping ---------------------------------------------------

async def test_reload_forgets_own_motion_and_stale_bookkeeping_expires(zones):
    h = await zones.boot([("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None)])
    tasks = await _cut_after_first_command(h)
    coordinator = h.coord("ea")
    assert coordinator._own_motion, "the cut plan left its own command on record"
    await h.drive(tasks)
    # a person stops the cover half-way after our window has long passed: now it is real
    import custom_components.smartshading.coordinator as cm
    h.monkeypatch.setattr(cm, "_monotonic", lambda: h.clock.t + 10_000.0)
    _manual_move(h, "cover.a1", 70)
    await h.drive(h.refresh("ea"))
    assert _override(h, "ea", "wa") is not None
    assert not coordinator._own_motion, "an expired record is dropped when it is consulted"
    # unload and reload: the new instance starts without any record
    task = __import__("asyncio").ensure_future(h.hass.config_entries.async_unload("ea"))
    await h.drive([task])
    h.entries.pop("ea")
    await h.add_zone("ea", {"wa": ["cover.a1", "cover.a2"]}, 50)
    assert h.coord("ea")._own_motion == {}


# --- 10. nothing is learned from own movements ---------------------------------------------------

@pytest.mark.parametrize("cut", ["replaced_plan", "safety"])
async def test_no_learning_record_from_own_movements(zones, cut):
    h = await zones.boot([
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("ec", {"wc": ["cover.c1"]}, 50, RAIN),
    ], learning=("ea", "ec"))
    if cut == "replaced_plan":
        tasks = await _cut_after_first_command(h)
        await h.drive(tasks)
    else:
        h.sun_up()
        tasks = h.refresh("ea")
        await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
        h.rain(RAIN)
        await h.drive(tasks, until=lambda: _sent_to(h, "cover.c1", 0))
        await h.settle()
    await _later(h)
    for entry, window in (("ea", "wa"), ("ec", "wc")):
        assert _learned_overrides(h, entry, window) == [], entry
