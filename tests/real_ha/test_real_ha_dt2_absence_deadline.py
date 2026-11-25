"""DT-2 (real Home Assistant): the absence activation deadline asks for an evaluation on time.

The absence delay counts from the first evaluation that finds the household away
(``absent_since``).  Until now the evaluation that finds the delay elapsed was the next
periodic one (up to the 5 minute update interval later).  A single point-in-time timer on
``absent_since + absence_delay_min`` now requests a NORMAL evaluation at that moment; it
never sends a command itself, so the dispatch, Safety, override and ownership rules decide
exactly as before.

Every scenario records the absence start, the configured deadline, when the timer was
armed / cancelled / fired, when evaluations ran and which commands went out when (virtual
cover-clock time).  The evaluation time is NOT the time a motor starts: a command waits for
whatever the shared dispatch is doing, and that is shown too.

Real coordinators, config entries and Home Assistant timers (freezegun time); simulated
covers.  Run with the rest of the real-HA suite::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

PERSON = "person.mika"
PERSON2 = "person.second"
RAIN = "binary_sensor.dt2_rain"
ABSENCE_HA = 20
DELAY_MIN = 3
DELAY = timedelta(minutes=DELAY_MIN)
POLL = timedelta(minutes=5)          # the coordinator's periodic update interval


def _extra(delay=DELAY_MIN, presence=(PERSON,)):
    return {"presence": list(presence), "absence_position": ABSENCE_HA, "comfort_off": True,
            "solar_sensor": "sensor.solar_test", "absence_delay_min": delay}


class Probe:
    """What happened, with timestamps: evaluations of every zone and the timer's life."""

    def __init__(self, h) -> None:
        self.h = h
        self.evaluations: dict[str, list[datetime]] = {}
        for entry_id in h.entries:
            self._count(entry_id)

    def _count(self, entry_id) -> None:
        coordinator = self.h.coord(entry_id)
        original = coordinator._async_update_data
        seen = self.evaluations.setdefault(entry_id, [])

        async def counted():
            seen.append(dt_util.utcnow())
            return await original()

        coordinator._async_update_data = counted

    def evaluations_between(self, entry_id, start, end) -> int:
        return sum(1 for t in self.evaluations[entry_id] if start <= t <= end)

    def stats(self, entry_id) -> dict:
        return self.h.coord(entry_id)._absence_deadline_stats

    def timer(self, entry_id):
        return self.h.coord(entry_id)._absence_deadline_utc


async def _boot(zones, specs, extras, *, home=True):
    zones.hass.states.async_set(PERSON, "home")
    zones.hass.states.async_set(PERSON2, "home")
    zones.hass.states.async_set("sensor.solar_test", "40")
    for entry_id, extra in extras.items():
        zones.zone_extra[entry_id] = dict(extra)
    h = await zones.boot(specs)
    h.sun_up()
    await h.drive(h.refresh())
    return h, Probe(h)


async def _poke_solar(h) -> None:
    h.hass.states.async_set("sensor.solar_test", "40", force_update=True)    # a live sensor never ages out


async def _go_away(h, entity=PERSON):
    """The household leaves: the presence event causes the evaluation that starts the delay."""
    h.hass.states.async_set(entity, "not_home")
    await h.settle()
    await h.drive()
    return dt_util.utcnow()


async def _pass(h, seconds: float) -> None:
    """Let Home Assistant time pass (due timers fire) and the integration finish what that starts."""
    await _poke_solar(h)
    await h.fire_ha_timers(seconds)
    await h.drive()


async def _until(h, moment: datetime) -> None:
    await _pass(h, (moment - dt_util.utcnow()).total_seconds())


def _a1_commands(h):
    return [c for c in h.clock.calls() if c[1] == "cover.a1"]


async def _one_zone(zones, delay=DELAY_MIN):
    return await _boot(zones, [("ea", {"w": ["cover.a1"]}, 50, None)], {"ea": _extra(delay)})


# ---------------------------------------------------------------------------
# The timing contract
# ---------------------------------------------------------------------------

async def test_the_deadline_requests_an_evaluation_without_any_sensor_update(zones) -> None:
    h, probe = await _one_zone(zones)
    start = await _go_away(h)
    deadline = start + DELAY
    assert h.coord("ea")._presence_debouncer.absent_since == start
    assert probe.timer("ea") == deadline and probe.stats("ea")["armed"] == 1
    assert _a1_commands(h) == [], "the delay has not elapsed: nothing moves"

    before = len(probe.evaluations["ea"])
    await _until(h, deadline - timedelta(seconds=1))
    assert len(probe.evaluations["ea"]) == before and _a1_commands(h) == [], "nothing before the deadline"
    assert probe.stats("ea")["fired"] == 0 and probe.timer("ea") == deadline

    await _until(h, deadline)
    fired_at = probe.stats("ea")["last_fired_utc"]
    assert probe.stats("ea")["fired"] == 1 and probe.timer("ea") is None
    assert probe.evaluations_between("ea", deadline, deadline + timedelta(seconds=5)) == 1, \
        "exactly one evaluation at the deadline, long before the next periodic one"
    assert [c[2] for c in _a1_commands(h)] == [ABSENCE_HA], "the normal evaluation closed to the absence position"
    assert deadline <= fired_at < deadline + timedelta(seconds=5) < start + POLL


async def test_without_the_timer_the_same_evaluation_would_wait_for_the_periodic_update(zones, monkeypatch) -> None:
    """The before picture: the timer is what removes the wait (it is not the periodic update)."""
    h, probe = await _one_zone(zones)
    monkeypatch.setattr(type(h.coord("ea")), "_sync_absence_deadline_timer", lambda self, active: None)
    start = await _go_away(h)
    await _until(h, start + DELAY + timedelta(seconds=30))
    assert _a1_commands(h) == [], "no timer: the cover waits for the periodic update"
    await _until(h, start + POLL + timedelta(seconds=5))
    assert [c[2] for c in _a1_commands(h)] == [ABSENCE_HA]


async def test_repeated_evaluations_and_notifications_keep_one_timer(zones) -> None:
    h, probe = await _one_zone(zones)
    start = await _go_away(h)
    for _ in range(4):
        await _poke_solar(h)
        await h.drive(h.refresh("ea"))                                # ordinary cycles before the deadline
    h.hass.states.async_set(PERSON, "not_home", {"note": "again"})   # a repeated absence notification
    await h.settle()
    await h.drive()
    stats = probe.stats("ea")
    assert stats["armed"] == 1 and stats["cancelled"] == 0 and probe.timer("ea") == start + DELAY
    assert _a1_commands(h) == []


async def test_the_timer_and_a_normal_update_together_do_not_move_the_cover_twice(zones) -> None:
    h, probe = await _one_zone(zones)
    start = await _go_away(h)
    deadline = start + DELAY
    h.frozen.move_to(deadline)
    h.hass.states.async_set("sensor.solar_test", "40", force_update=True)
    tasks = h.refresh("ea")                                          # a normal update at the very deadline ...
    await _pass(h, 0)                                                # ... and the timer fires
    await h.drive(tasks)
    await _until(h, deadline + timedelta(seconds=40))
    assert [c[2] for c in _a1_commands(h)] == [ABSENCE_HA], "one absence command, however many evaluations"
    assert probe.timer("ea") is None


async def test_zero_delay_needs_no_timer(zones) -> None:
    h, probe = await _one_zone(zones, delay=0)
    await _go_away(h)
    assert [c[2] for c in _a1_commands(h)] == [ABSENCE_HA], "absence is active in the first evaluation"
    assert probe.stats("ea")["armed"] == 0 and probe.timer("ea") is None


# ---------------------------------------------------------------------------
# Coming home around the deadline, uncertain presence
# ---------------------------------------------------------------------------

async def test_coming_home_shortly_before_the_deadline_cancels_it(zones) -> None:
    h, probe = await _one_zone(zones)
    start = await _go_away(h)
    deadline = start + DELAY
    await _until(h, deadline - timedelta(seconds=5))
    h.hass.states.async_set(PERSON, "home")
    await h.settle()
    await h.drive()
    assert probe.timer("ea") is None and probe.stats("ea")["cancelled"] == 1
    count = len(probe.evaluations["ea"])
    await _until(h, deadline + timedelta(seconds=30))
    assert probe.stats("ea")["fired"] == 0 and len(probe.evaluations["ea"]) == count
    assert _a1_commands(h) == []


async def test_coming_home_exactly_at_the_deadline_never_closes_the_cover(zones) -> None:
    h, probe = await _one_zone(zones)
    start = await _go_away(h)
    deadline = start + DELAY
    await _until(h, deadline - timedelta(seconds=1))
    h.frozen.move_to(deadline)
    h.hass.states.async_set(PERSON, "home")                          # the presence event ...
    await _pass(h, 0)                                                # ... and the timer race each other
    await _until(h, deadline + timedelta(seconds=40))
    assert [c for c in _a1_commands(h) if c[2] == ABSENCE_HA] == [], "present at the deadline: no absence move"
    assert probe.timer("ea") is None
    stats = probe.stats("ea")
    assert stats["fired"] + stats["cancelled"] == 1, stats


async def test_coming_home_right_after_the_deadline_is_released_and_leaves_no_timer(zones) -> None:
    h, probe = await _one_zone(zones)
    start = await _go_away(h)
    deadline = start + DELAY
    await _until(h, deadline + timedelta(seconds=1))
    assert [c[2] for c in _a1_commands(h)] == [ABSENCE_HA]
    h.hass.states.async_set(PERSON, "home")
    await h.settle()
    await h.drive()
    await _until(h, deadline + timedelta(minutes=31))              # the state guard's minimum duration passes
    assert [c[2] for c in _a1_commands(h)] == [ABSENCE_HA, 50]      # released to the shade target (the sun is up)
    assert probe.timer("ea") is None and probe.stats("ea")["armed"] == 1


async def test_presence_unavailable_resets_the_count_and_recovery_starts_a_new_deadline(zones) -> None:
    h, probe = await _one_zone(zones)
    start = await _go_away(h)
    await _until(h, start + timedelta(seconds=60))
    h.hass.states.async_set(PERSON, "unavailable")                   # undeterminable: the established rule resets the count
    await h.settle()
    await h.drive()
    assert h.coord("ea")._presence_debouncer.absent_since is None and probe.timer("ea") is None
    old_deadline = start + DELAY
    await _until(h, old_deadline + timedelta(seconds=10))
    assert _a1_commands(h) == [] and probe.stats("ea")["fired"] == 0, "the old deadline is gone"
    back = await _go_away(h)                                         # the sensor recovers: away again
    assert probe.timer("ea") == back + DELAY and probe.stats("ea")["armed"] == 2
    await _until(h, back + DELAY + timedelta(seconds=1))
    assert [c[2] for c in _a1_commands(h)] == [ABSENCE_HA]


# ---------------------------------------------------------------------------
# Lifecycle: reload, unload, removal, disabling, restart
# ---------------------------------------------------------------------------

async def test_unload_cancels_the_timer_and_nothing_runs_afterwards(zones) -> None:
    h, probe = await _one_zone(zones)
    start = await _go_away(h)
    coordinator = h.coord("ea")
    assert await h.hass.config_entries.async_unload("ea")
    await h.settle()
    assert coordinator._unsub_absence_deadline is None and coordinator._absence_deadline_utc is None
    await _until(h, start + DELAY + timedelta(seconds=30))
    assert coordinator._absence_deadline_stats["fired"] == 0 and _a1_commands(h) == []


async def test_removing_the_zone_cancels_the_timer(zones) -> None:
    h, probe = await _one_zone(zones)
    start = await _go_away(h)
    coordinator = h.coord("ea")
    await h.hass.config_entries.async_remove("ea")
    await h.hass.async_block_till_done()
    assert coordinator._unsub_absence_deadline is None
    await _until(h, start + DELAY + timedelta(seconds=30))
    assert coordinator._absence_deadline_stats["fired"] == 0 and _a1_commands(h) == []


async def test_a_reload_during_a_pending_absence_restarts_the_delay_and_the_old_callback_is_dead(zones) -> None:
    """Restart semantics are unchanged: the debouncer is runtime-only, so a pending (not yet active)
    absence counts from the first evaluation after the reload.  The old timer never fires."""
    h, probe = await _one_zone(zones)
    start = await _go_away(h)
    old = h.coord("ea")
    await _until(h, start + timedelta(seconds=60))
    assert await h.hass.config_entries.async_reload("ea")
    await h.hass.async_block_till_done()
    new = h.coord("ea")
    assert new is not old and old._unsub_absence_deadline is None
    probe._count("ea")
    reloaded = dt_util.utcnow()
    await h.drive(h.refresh("ea"))
    assert new._presence_debouncer.absent_since is not None and new._absence_deadline_utc is not None
    assert new._absence_deadline_utc >= reloaded + DELAY - timedelta(seconds=1)
    await _until(h, start + DELAY + timedelta(seconds=10))           # past the OLD deadline
    assert old._absence_deadline_stats["fired"] == 0 and new._absence_deadline_stats["fired"] == 0
    assert _a1_commands(h) == []
    await _until(h, new._absence_deadline_utc + timedelta(seconds=1))
    assert [c[2] for c in _a1_commands(h)] == [ABSENCE_HA]


async def test_a_changed_delay_takes_effect_through_the_reload_and_replaces_the_timer(zones) -> None:
    h, probe = await _one_zone(zones, delay=3)
    start = await _go_away(h)
    old = h.coord("ea")
    entry = h.entries["ea"]
    h.hass.config_entries.async_update_entry(entry, data={**entry.data, "absence_delay_min": 1})
    assert await h.hass.config_entries.async_reload("ea")
    await h.hass.async_block_till_done()
    new = h.coord("ea")
    await h.drive(h.refresh("ea"))
    assert old._unsub_absence_deadline is None
    assert new._absence_delay_min == 1
    assert new._absence_deadline_utc is not None
    assert new._absence_deadline_utc - new._presence_debouncer.absent_since == timedelta(minutes=1)
    await _until(h, new._absence_deadline_utc + timedelta(seconds=1))
    assert [c[2] for c in _a1_commands(h)] == [ABSENCE_HA]
    assert old._absence_deadline_stats["fired"] == 0


async def test_an_active_absence_survives_a_restart_without_a_timer_or_a_reopen(zones) -> None:
    h, probe = await _one_zone(zones)
    start = await _go_away(h)
    await _until(h, start + DELAY + timedelta(seconds=40))
    assert [c[2] for c in _a1_commands(h)] == [ABSENCE_HA]
    assert await h.hass.config_entries.async_reload("ea")
    await h.hass.async_block_till_done()
    await h.drive(h.refresh("ea"))
    await _pass(h, 40)
    new = h.coord("ea")
    assert new._unsub_absence_deadline is None, "absence is already active: nothing is pending"
    assert [c[2] for c in _a1_commands(h)] == [ABSENCE_HA], "the household is still away: nothing is released"


async def test_a_disabled_zone_has_no_timer_and_a_reactivated_one_starts_cleanly(zones) -> None:
    from homeassistant.config_entries import ConfigEntryDisabler

    h, probe = await _one_zone(zones)
    start = await _go_away(h)
    coordinator = h.coord("ea")
    await h.hass.config_entries.async_set_disabled_by("ea", ConfigEntryDisabler.USER)
    await h.hass.async_block_till_done()
    assert coordinator._unsub_absence_deadline is None
    await _until(h, start + DELAY + timedelta(seconds=30))
    assert _a1_commands(h) == [] and coordinator._absence_deadline_stats["fired"] == 0
    await h.hass.config_entries.async_set_disabled_by("ea", None)
    await h.hass.async_block_till_done()
    new = h.coord("ea")
    assert new is not coordinator
    await _poke_solar(h)
    await h.drive(h.refresh("ea"))
    back = dt_util.utcnow()
    assert new._absence_deadline_utc is not None and new._absence_deadline_utc >= back + DELAY - timedelta(seconds=30)


# ---------------------------------------------------------------------------
# Zones, Safety, overrides and the dispatch
# ---------------------------------------------------------------------------

async def test_two_zones_with_different_delays_are_evaluated_each_at_its_own_deadline(zones) -> None:
    h, probe = await _boot(
        zones,
        [("ea", {"wa": ["cover.a1"]}, 50, None), ("eb", {"wb": ["cover.b1"]}, 50, None)],
        {"ea": _extra(2), "eb": _extra(4)})
    start = await _go_away(h)
    a_deadline, b_deadline = start + timedelta(minutes=2), start + timedelta(minutes=4)
    assert probe.timer("ea") == a_deadline and probe.timer("eb") == b_deadline
    await _until(h, a_deadline)
    assert [c[1] for c in h.clock.calls()] == ["cover.a1"], "only the zone whose deadline passed moves"
    assert probe.evaluations_between("eb", a_deadline, a_deadline + timedelta(seconds=5)) == 0
    await _until(h, b_deadline)
    assert sorted(c[1] for c in h.clock.calls()) == ["cover.a1", "cover.b1"]


async def test_zones_with_the_same_deadline_are_evaluated_together_but_their_motors_start_one_after_the_other(zones) -> None:
    """The evaluation is on time for both; the shared dispatch decides when each motor starts."""
    h, probe = await _boot(
        zones,
        [("ea", {"wa": ["cover.a1"]}, 50, None), ("eb", {"wb": ["cover.b1"]}, 50, None)],
        {"ea": _extra(), "eb": _extra()})
    start = await _go_away(h)
    deadline = start + DELAY
    await _until(h, deadline)
    await _until(h, deadline + timedelta(seconds=1))
    await h.drive(horizon=h.clock.t + 120)
    for entry_id in ("ea", "eb"):
        assert probe.evaluations_between(entry_id, deadline, deadline + timedelta(seconds=5)) >= 1
    starts = sorted(t for t, cover, _ in h.clock.calls() if cover in ("cover.a1", "cover.b1"))
    assert len(starts) == 2 and starts[1] - starts[0] >= 2.0, starts      # at least the 2 s spacing, normally the travel


async def test_safety_wins_over_the_absence_move_at_the_deadline(zones) -> None:
    h, probe = await _boot(zones, [("ea", {"w": ["cover.a1"]}, 50, RAIN)], {"ea": _extra()})
    start = await _go_away(h)
    deadline = start + DELAY
    await _until(h, deadline - timedelta(seconds=2))
    h.rain(RAIN)
    await h.settle()
    await h.drive()
    await _until(h, deadline + timedelta(seconds=20))
    assert _a1_commands(h), "Safety moved the cover"
    assert ABSENCE_HA not in [c[2] for c in _a1_commands(h)], _a1_commands(h)


async def test_a_manual_override_blocks_the_absence_move_at_the_deadline(zones) -> None:
    h, probe = await _one_zone(zones)
    h.sim.pos["cover.a1"] = 70                                       # a person set the cover themself
    h.sim.publish("cover.a1")
    await _poke_solar(h)
    await h.drive(h.refresh("ea"))
    start = await _go_away(h)
    await _until(h, start + DELAY + timedelta(seconds=40))
    assert probe.stats("ea")["fired"] == 1, "the evaluation was requested on time"
    assert ABSENCE_HA not in [c[2] for c in _a1_commands(h)], _a1_commands(h)


async def test_a_second_person_still_home_keeps_the_zone_present_and_the_timer_off(zones) -> None:
    h, probe = await _boot(zones, [("ea", {"w": ["cover.a1"]}, 50, None)], {"ea": _extra(presence=(PERSON, PERSON2))})
    await _go_away(h, PERSON)
    assert probe.timer("ea") is None and probe.stats("ea")["armed"] == 0
