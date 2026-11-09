"""Coming home releases an absence close on the FIRST presence-triggered evaluation.

The F29 exit-debounce used to delay the daytime fallback OPEN by one more evaluation
for a window inside its solar sector right after an absence close; a window outside
its sector opened at once.  Real coordinators, the real presence listener, the real
shared dispatch and pacing, simulated covers and virtual time (see conftest.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

PERSON = "person.mika"
ABSENCE_HA = 20
SHADE_HA = 50
#: inside the solar sector, but so low that nothing asks for shading (fallback open)
WEAK_SUN = {
    "azimuth": 180.0, "elevation": 8.0,
    "next_rising": "2026-08-12T05:00:00+00:00", "next_setting": "2026-08-11T19:00:00+00:00",
}
_AFTERNOON = datetime(2026, 8, 11, 12, 30, tzinfo=timezone.utc)


def _count_cycles(h, entry_id):
    """Record, after each completed evaluation of the zone, how many commands the
    shared dispatch had sent by then."""
    coordinator = h.coord(entry_id)
    original = coordinator._async_update_data
    seen: list[int] = []

    async def counted():
        result = await original()
        seen.append(len(h.clock.calls()))
        return result

    coordinator._async_update_data = counted
    return seen


async def _event_cycles(h, rounds=10):
    if h.hass.states.get("sensor.solar_test") is not None:
        # a real sensor keeps reporting; the reading must not age out under the frozen clock
        h.hass.states.async_set("sensor.solar_test", "40", force_update=True)
    for _ in range(rounds):
        await h.fire_ha_timers(30)
        await h.drive(horizon=h.clock.t + 120)


async def _boot_home(zones, sector="inside"):
    """``inside``: strong sun inside every window's solar sector, comfort goals off, so
    the measured radiation is low, so nothing asks for shading (the daytime fallback is the open).  ``outside``: the sun
    is outside the sector (comfort goals on, as by default)."""
    zones.hass.states.async_set(PERSON, "home")
    extra = {"presence": [PERSON], "absence_position": ABSENCE_HA, "comfort_off": sector == "inside"}
    if sector == "inside":
        zones.hass.states.async_set("sensor.solar_test", "40")        # measured: far below any shade threshold
        extra["solar_sensor"] = "sensor.solar_test"
    zones.zone_extra["ea"] = dict(extra)
    zones.zone_extra["eb"] = dict(extra)
    h = await zones.boot([
        ("ea", {"wa1": ["cover.a1"], "wa2": ["cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1"]}, 50, None),
    ])
    _sun(h, None if sector == "inside" else WEAK_SUN)
    await h.drive(h.refresh())
    return h


async def _go_away(h):
    h.hass.states.async_set(PERSON, "not_home")
    await _event_cycles(h)
    return h


def _sun(h, attributes):
    if attributes is None:
        h.sun_up()                              # the harness's strong in-sector sun
        return
    h.frozen.move_to(_AFTERNOON)
    h.hass.states.async_set("sun.sun", "above_horizon", attributes)


def _hold(h, entry_id, window_id):
    return h.coord(entry_id)._comfort_movement_holds[window_id]


@pytest.mark.parametrize("sector", ["inside", "outside"])
async def test_every_window_opens_on_the_first_evaluation_after_coming_home(zones, sector):
    h = await _boot_home(zones, sector)
    assert h.clock.calls() == [], "already open, sun without a shading need: nothing to send"

    await _go_away(h)
    away = h.clock.calls()
    assert sorted((c[1], c[2]) for c in away) == [
        ("cover.a1", ABSENCE_HA), ("cover.a2", ABSENCE_HA), ("cover.b1", ABSENCE_HA)]

    cycles = {e: _count_cycles(h, e) for e in ("ea", "eb")}
    h.hass.states.async_set(PERSON, "home")
    await _event_cycles(h)

    opened = [c for c in h.clock.calls()[len(away):]]
    assert sorted((c[1], c[2]) for c in opened) == [
        ("cover.a1", 100), ("cover.a2", 100), ("cover.b1", 100)]
    # ... and every one of them went out within the first evaluation the event caused
    for entry_id, covers in (("ea", {"cover.a1", "cover.a2"}), ("eb", {"cover.b1"})):
        first = cycles[entry_id][0]
        sent_by_then = {c[1] for c in h.clock.calls()[len(away):first] if c[2] == 100}
        assert covers <= sent_by_then, (entry_id, cycles[entry_id], h.clock.calls())


async def test_shading_still_required_on_return_sends_the_protective_target_not_an_open(zones):
    h = await _boot_home(zones, "outside")
    await _go_away(h)
    away = len(h.clock.calls())
    _sun(h, None)                               # strong sun is there when they come home
    # the sun-shade is a different state than the absence close: let the state guard's
    # minimum duration pass (the open below is a direct transition and never needs this)
    h.frozen.move_to(datetime.now(timezone.utc) + timedelta(minutes=30))
    h.hass.states.async_set(PERSON, "home")
    await _event_cycles(h)
    after = h.clock.calls()[away:]
    assert sorted((c[1], c[2]) for c in after) == [
        ("cover.a1", SHADE_HA), ("cover.a2", SHADE_HA), ("cover.b1", SHADE_HA)]
    assert all(c[2] != 100 for c in after), "no open in between"


async def test_window_state_and_hold_are_ordinary_again_after_the_release(zones):
    h = await _boot_home(zones)
    await _go_away(h)
    h.hass.states.async_set(PERSON, "home")
    await _event_cycles(h)
    sent = len(h.clock.calls())

    for entry_id, window_id in (("ea", "wa1"), ("ea", "wa2"), ("eb", "wb")):
        hold = _hold(h, entry_id, window_id)
        assert hold.absence_release_open_pending is False
        assert hold.pending_fallback_open_release_count == 0

    # nothing is re-sent by later evaluations of the same, settled situation
    await h.drive(h.refresh())
    assert len(h.clock.calls()) == sent


async def test_still_away_after_a_restart_style_second_cycle_does_not_open(zones):
    h = await _boot_home(zones)
    await _go_away(h)
    sent = len(h.clock.calls())
    await h.drive(h.refresh())
    assert len(h.clock.calls()) == sent, "away stays away: nothing is released"
    for entry_id, window_id in (("ea", "wa1"), ("eb", "wb")):
        assert _hold(h, entry_id, window_id).absence_release_open_pending is False


async def test_unconfirmed_presence_does_not_count_as_coming_home(zones):
    """A person entity that is merely unavailable is not 'home': whatever the fallback
    does with it, the exception for a genuine release must not switch on."""
    h = await _boot_home(zones)
    await _go_away(h)
    h.hass.states.async_set(PERSON, "unavailable")
    await _event_cycles(h)
    for entry_id, window_id in (("ea", "wa1"), ("ea", "wa2"), ("eb", "wb")):
        assert _hold(h, entry_id, window_id).absence_release_open_pending is False


async def test_a_later_departure_and_return_is_a_fresh_release(zones):
    h = await _boot_home(zones)
    await _go_away(h)
    h.hass.states.async_set(PERSON, "home")
    await _event_cycles(h)
    first_round = len(h.clock.calls())
    h.clock.t += 60                             # the covers arrive ...
    h.sim.tick()
    await h.settle()
    h.frozen.move_to(datetime.now(timezone.utc) + timedelta(minutes=30))   # ... and the guard's interval passes
    await _go_away(h)
    h.hass.states.async_set(PERSON, "home")
    await _event_cycles(h)
    second = h.clock.calls()[first_round:]
    assert {(c[1], c[2]) for c in second if c[2] == 100} == {
        ("cover.a1", 100), ("cover.a2", 100), ("cover.b1", 100)}
