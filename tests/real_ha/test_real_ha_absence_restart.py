"""A restart or reload must not release an absence by itself.

The absence debouncer is runtime-only.  After a restart while the household is still
away, the restored window state is ABSENCE_CLOSED but the debouncer started its
activation delay over, so absence read as inactive and the daytime fallback opened the
cover once the startup grace and the fallback confirmation were over.  A genuine return
home must still release immediately.  Real coordinators and entries (a reload is a new
coordinator restoring from storage), the real presence listener, the shared dispatch,
simulated covers and virtual time (see conftest.py).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

PERSON = "person.mika"
SOLAR = "sensor.solar_test"
RAIN = "binary_sensor.rain_test"
DELAY_MIN = 30
_AFTERNOON = datetime(2026, 8, 11, 12, 30, tzinfo=timezone.utc)
SUN_INSIDE = {
    "azimuth": 180.0, "elevation": 60.0,
    "next_rising": "2026-08-12T05:00:00+00:00", "next_setting": "2026-08-11T19:00:00+00:00",
}
SUN_OUTSIDE = {**SUN_INSIDE, "azimuth": 0.0, "elevation": 30.0}


def _later(h, minutes):
    h.frozen.move_to(datetime.now(timezone.utc) + timedelta(minutes=minutes))
    h.clock.t += minutes * 60
    h.sim.tick()
    if h.hass.states.get(SOLAR) is not None:
        h.hass.states.async_set(SOLAR, "40", force_update=True)     # a real sensor keeps reporting


async def _events(h, rounds=8):
    if h.hass.states.get(SOLAR) is not None:
        h.hass.states.async_set(SOLAR, "40", force_update=True)
    for _ in range(rounds):
        await h.fire_ha_timers(30)
        await h.drive(horizon=h.clock.t + 120)


async def _cycle(h, minutes=1):
    """One more evaluation of every zone, a little later."""
    _later(h, minutes)
    await h.drive(h.refresh())
    await h.settle()


async def _boot(zones, *, sector="inside", delay=DELAY_MIN, absence_position=20, rain=False,
                entries=("ea", "eb")):
    """Two zones, three windows, everybody home.  ``inside``: sun inside every sector with a
    low measured radiation (nothing asks for shading, the fallback is the open);
    ``outside``: sun outside the sectors."""
    hass = zones.hass
    hass.states.async_set(PERSON, "home")
    extra = {"presence": [PERSON], "absence_position": absence_position, "absence_delay_min": delay,
             "comfort_off": True}
    if sector == "inside":
        hass.states.async_set(SOLAR, "40")
        extra["solar_sensor"] = SOLAR
    for entry_id in entries:
        zones.zone_extra[entry_id] = dict(extra)
    specs = [("ea", {"wa1": ["cover.a1"], "wa2": ["cover.a2"]}, 50, RAIN if rain else None),
             ("eb", {"wb": ["cover.b1"]}, 50, RAIN if rain else None)]
    h = await zones.boot([s for s in specs if s[0] in entries])
    hass.states.async_set("sun.sun", "above_horizon", SUN_INSIDE if sector == "inside" else SUN_OUTSIDE)
    h.frozen.move_to(_AFTERNOON)
    await h.drive(h.refresh())
    await h.settle()
    assert h.clock.calls() == [], "everybody home, nothing to do"
    return h


async def _away_and_closed(h, position=20, delay=DELAY_MIN):
    """Leave, let the activation delay pass, and see the windows close."""
    h.hass.states.async_set(PERSON, "not_home")
    await _events(h)
    if delay:
        assert h.clock.calls() == [], "the activation delay of a newly absent household is respected"
        _later(h, delay + 1)
        await h.drive(h.refresh())
        await _events(h)
    covers = sorted(c[1] for c in h.clock.calls())
    assert covers == ["cover.a1", "cover.a2", "cover.b1"], h.clock.calls()
    assert {c[2] for c in h.clock.calls()} == {position}
    for entry_id in h.entries:
        for state in _shading_states(h, entry_id):
            assert state == "absence_closed"


def _shading_states(h, entry_id):
    coordinator = h.coord(entry_id)
    return [coordinator._current_states[w].value for w in coordinator.windows]


async def _restart(h, *entry_ids):
    """Home Assistant restart / integration reload: a new coordinator per entry that restores
    what the old one stored."""
    for entry_id in entry_ids or tuple(h.entries):
        task = asyncio.ensure_future(h.hass.config_entries.async_reload(h.entries[entry_id].entry_id))
        await h.drive([task])
        assert task.result()
    await h.settle()


async def _warm_up(h, cycles=6):
    for _ in range(cycles):
        await _cycle(h)


def _openings(h, since=0):
    return [c for c in h.clock.calls()[since:] if c[2] == 100]


@pytest.mark.parametrize("sector", ["inside", "outside"])
@pytest.mark.parametrize("position", [20, 0])
async def test_restart_while_away_keeps_the_absence_during_the_activation_delay(zones, sector, position):
    h = await _boot(zones, sector=sector, absence_position=position)
    await _away_and_closed(h, position)
    sent = len(h.clock.calls())
    old = {e: h.coord(e) for e in h.entries}

    await _restart(h)

    assert all(h.coord(e) is not old[e] for e in h.entries), "a genuinely new coordinator"
    await _warm_up(h)                           # startup grace, then the fallback confirmation, and more
    assert h.clock.calls()[sent:] == [], "away stays away: no open, no re-close"
    for entry_id in h.entries:
        assert all(s == "absence_closed" for s in _shading_states(h, entry_id))
    assert all(h.sim.pos[c] == position for c in ("cover.a1", "cover.a2", "cover.b1"))


async def test_restart_while_away_with_zero_delay_keeps_the_absence(zones):
    h = await _boot(zones, delay=0)
    await _away_and_closed(h, delay=0)
    sent = len(h.clock.calls())
    await _restart(h)
    await _warm_up(h)
    assert h.clock.calls()[sent:] == []


async def test_the_absence_stays_effective_after_the_restart_delay_has_passed(zones):
    """The resumed absence is the debouncer's own state from then on."""
    h = await _boot(zones)
    await _away_and_closed(h)
    sent = len(h.clock.calls())
    await _restart(h)
    await _warm_up(h, 2)
    _later(h, DELAY_MIN + 5)
    await _warm_up(h, 3)
    assert h.clock.calls()[sent:] == []


async def test_coming_home_after_a_restart_releases_on_the_first_evaluation(zones):
    h = await _boot(zones)
    await _away_and_closed(h)
    await _restart(h)
    await _warm_up(h, 3)
    sent = len(h.clock.calls())

    h.hass.states.async_set(PERSON, "home")
    await _events(h)

    assert sorted((c[1], c[2]) for c in h.clock.calls()[sent:]) == [
        ("cover.a1", 100), ("cover.a2", 100), ("cover.b1", 100)]


async def test_household_back_during_the_downtime_is_released_after_the_restart(zones):
    """Stale restored state: the absence ended while Home Assistant was down."""
    h = await _boot(zones)
    await _away_and_closed(h)
    sent = len(h.clock.calls())
    h.hass.states.async_set(PERSON, "home")
    await _restart(h)
    await _warm_up(h, 3)
    assert sorted((c[1], c[2]) for c in h.clock.calls()[sent:]) == [
        ("cover.a1", 100), ("cover.a2", 100), ("cover.b1", 100)]


async def test_a_second_departure_after_a_restart_is_a_fresh_absence(zones):
    """The resume is one-shot: it does not shortcut the delay of a later departure."""
    h = await _boot(zones)
    await _away_and_closed(h)
    await _restart(h)
    await _warm_up(h, 2)
    h.hass.states.async_set(PERSON, "home")
    await _events(h)
    _later(h, 5)
    sent = len(h.clock.calls())
    h.hass.states.async_set(PERSON, "not_home")
    await _events(h)
    assert h.clock.calls()[sent:] == [], "a newly absent household waits for its delay again"
    _later(h, DELAY_MIN + 1)
    await h.drive(h.refresh())
    await _events(h)
    assert sorted(c[1] for c in h.clock.calls()[sent:]) == ["cover.a1", "cover.a2", "cover.b1"]


async def test_presence_unavailable_at_the_restart_stays_conservative_then_resumes_the_absence(zones):
    h = await _boot(zones)
    await _away_and_closed(h)
    sent = len(h.clock.calls())
    h.hass.states.async_set(PERSON, "unavailable")
    await _restart(h)
    await _warm_up(h, 4)
    assert h.clock.calls()[sent:] == [], "undeterminable presence neither opens nor closes"

    h.hass.states.async_set(PERSON, "not_home")          # the entity hydrates: still away
    await _events(h)
    await _warm_up(h, 4)
    assert h.clock.calls()[sent:] == [], "and the absence is simply still in force"


async def test_a_newly_absent_household_is_not_resumed_by_a_reload(zones):
    """No previous ABSENCE_CLOSED: the restart does not shortcut the activation delay."""
    h = await _boot(zones)
    h.hass.states.async_set(PERSON, "not_home")
    await _events(h)
    assert h.clock.calls() == []
    await _restart(h)
    await _warm_up(h, 3)
    assert h.clock.calls() == [], "the delay started over; nothing closes yet"
    _later(h, DELAY_MIN + 1)
    await h.drive(h.refresh())
    await _events(h)
    assert sorted(c[1] for c in h.clock.calls()) == ["cover.a1", "cover.a2", "cover.b1"]


async def test_safety_still_overrides_the_resumed_absence(zones):
    h = await _boot(zones, rain=True)
    await _away_and_closed(h)
    await _restart(h)
    await _warm_up(h, 2)
    sent = len(h.clock.calls())

    h.rain(RAIN, True)
    await _events(h)
    assert len(h.clock.calls()) > sent, "rain protection acts although the household is away"


async def test_two_zones_restarting_together_leave_the_dispatch_order_and_pacing_alone(zones):
    h = await _boot(zones)
    await _away_and_closed(h)
    await _restart(h)                      # both zones, one after the other
    await _warm_up(h, 3)
    sent = len(h.clock.calls())
    h.hass.states.async_set(PERSON, "home")
    await _events(h)
    opened = h.clock.calls()[sent:]
    assert sorted(c[1] for c in opened) == ["cover.a1", "cover.a2", "cover.b1"]
    times = [c[0] for c in opened]
    assert times == sorted(times) and len(set(times)) == len(times), "one after the other"
