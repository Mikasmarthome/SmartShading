"""PA-5 (real Home Assistant): the upgraded installation RUNS exactly like a zone-only one.

The same scenario is played twice with real coordinators, the real shared dispatch and pacing,
simulated covers and virtual time (see conftest.py):

* ``zone_only``       - zones that carry their settings themselves (a fresh installation);
* ``legacy_upgrade``  - the published beta.3.2 model: a legacy System entry holding the global
  defaults and zones that defer to it.  Every entry is stored BEFORE Home Assistant starts, so
  the migration and the retirement of the System entry happen during the start, concurrently
  with the first evaluations.

Both modes must produce the very same cover commands (target, order, virtual time) and end with
the same effective settings; the upgraded one must also end without a System entry.  A control
test shows the comparison is sensitive: a different inherited value changes the log.

What this does NOT prove: physical cover behavior.  Covers are simulated, time is virtual.

Run with the rest of the real-HA suite (Linux, macOS or WSL)::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
PERSON = "person.mika"
RAIN = "binary_sensor.pa5_rain"
ABSENCE_MIN = 2


async def _assert_started_like_a_zone_only_install(h: UpgradeHarness) -> None:
    for entry_id, entry in h.entries.items():
        assert entry.state is ConfigEntryState.LOADED, entry_id
        assert h.effective(entry_id)["absence"] == ABSENCE_MIN
        assert h.effective(entry_id)["duration"] == 45 and h.effective(entry_id)["strategy"] == "duration"
    assert h.systems() == []                                  # retired during the start (or never there)
    assert h.serial is h.coord(next(iter(h.entries)))._serial_dispatch


async def _rounds(h: UpgradeHarness, rounds: int = 10, step: int = 30) -> None:
    for _ in range(rounds):
        await h.fire_ha_timers(step)
        await h.drive(horizon=h.clock.t + 120)


# ---------------------------------------------------------------------------
# Scenarios (each returns the commands in order)
# ---------------------------------------------------------------------------

async def scenario_rain_after_start(h):
    h = await h.boot([("ea", {"wa": ["cover.a1"]}, 50, RAIN), ("eb", {"wb": ["cover.b1"]}, 50, None)])
    await _assert_started_like_a_zone_only_install(h)
    h.sun_up()
    await h.drive(h.refresh())
    h.rain(RAIN)
    await h.drive(h.refresh("ea"))
    await _rounds(h, 4)
    h.rain(RAIN, on=False)
    await _rounds(h, 6)
    return h


async def scenario_rain_at_start(h):
    h.rain_on_at_start = True
    h = await h.boot([("ea", {"wa": ["cover.a1"]}, 50, RAIN), ("eb", {"wb": ["cover.b1"]}, 50, None)])
    await _assert_started_like_a_zone_only_install(h)
    await h.drive(h.refresh())
    await _rounds(h, 4)
    return h


async def _absence_boot(h):
    h.hass.states.async_set(PERSON, "home")
    h.hass.states.async_set("sensor.solar_test", "40")       # measured: nothing asks for shading
    for e in ("ea", "eb"):
        h.zone_extra[e] = {"presence": [PERSON], "absence_position": 20, "comfort_off": True,
                           "solar_sensor": "sensor.solar_test"}
    return await h.boot([("ea", {"wa1": ["cover.a1"], "wa2": ["cover.a2"]}, 50, None),
                         ("eb", {"wb": ["cover.b1"]}, 50, None)])


async def _event_cycles(h, rounds=16):
    for _ in range(rounds):
        h.hass.states.async_set("sensor.solar_test", "40", force_update=True)
        await h.pass_time(30)                     # Home Assistant's clock AND the integration's own clock


async def scenario_absence(h):
    h = await _absence_boot(h)
    await _assert_started_like_a_zone_only_install(h)
    h.sun_up()
    await h.drive(h.refresh())
    h.hass.states.async_set(PERSON, "not_home")
    await _event_cycles(h)
    h.hass.states.async_set(PERSON, "home")
    await _event_cycles(h)
    return h


async def scenario_manual_stop(h):
    h = await h.boot([("ea", {"wa": ["cover.a1"]}, 50, None)])
    await _assert_started_like_a_zone_only_install(h)
    h.sun_up()
    tasks = h.refresh("ea")
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
    for callback in list(h.coord("ea")._cover_ownership._listeners["ea"]):
        callback()
    await h.settle()
    await h.drive(tasks)
    h.sim.motion.pop("cover.a1", None)                        # a person stops the cover half-way
    h.sim.pos["cover.a1"] = 75
    h.sim.publish("cover.a1")
    for _ in range(8):
        await h.fire_ha_timers(100)
        await h.drive(h.refresh("ea"))
        await h.drive(horizon=h.clock.t + 30)
    return h


async def scenario_restart_cycle(h):
    h = await h.boot([("ea", {"wa": ["cover.a1"]}, 50, RAIN), ("eb", {"wb": ["cover.b1"]}, 50, None)])
    await _assert_started_like_a_zone_only_install(h)
    h.sun_up()
    await h.drive(h.refresh())
    await _rounds(h, 3)
    settled = len(h.commands())
    import custom_components.smartshading as ss
    for _ in range(2):                                        # two restarts of the whole domain
        for entry_id in list(h.entries):
            await h.hass.config_entries.async_unload(entry_id)
        await ss.async_setup(h.hass, {})
        for entry_id in list(h.entries):
            assert await h.hass.config_entries.async_setup(entry_id)
        await h.hass.async_block_till_done()
        await h.drive(h.refresh())
        await _rounds(h, 3)
    assert h.systems() == []
    h.extra_after_restart = len(h.commands()) - settled
    return h


async def scenario_shared_cover(h):
    """Two zones claim the same cover: one owner sends, the other must not."""
    h = await h.boot([("ea", {"wa": ["cover.s1"]}, 50, None), ("eb", {"wb": ["cover.s1"]}, 50, None)])
    await _assert_started_like_a_zone_only_install(h)
    h.sun_up()
    await h.drive(h.refresh())
    await _rounds(h, 3)
    return h


async def scenario_priority(h):
    """Zone A is travelling to a comfort target; zone B then gets rain: B's Safety must not wait."""
    h = await h.boot([("ea", {"wa": ["cover.a1"]}, 50, None), ("eb", {"wb": ["cover.b1"]}, 50, RAIN)])
    await _assert_started_like_a_zone_only_install(h)
    h.sun_up()
    tasks = h.refresh("ea")
    await h.drive(tasks, until=lambda: h.clock.times_of("WAIT_START", "cover.a1"))
    h.rain(RAIN)
    tasks += h.refresh("eb")
    await h.drive(tasks)
    await _rounds(h, 3)
    return h


SCENARIOS = {
    "rain_after_start": scenario_rain_after_start,
    "rain_at_start": scenario_rain_at_start,
    "absence": scenario_absence,
    "manual_stop": scenario_manual_stop,
    "restart_cycle": scenario_restart_cycle,
    "shared_cover": scenario_shared_cover,
    "priority": scenario_priority,
}

#: The commands (virtual second, "cover->target") of the zone-only baseline.  Recorded once from a
#: fresh installation; the upgraded installation has to reproduce them exactly.  The repeated
#: identical Tier-1 commands at one instant are pre-existing behavior (one per evaluation while
#: the simulated cover has not reported its new position yet), identical in both modes, and are
#: handed to the separate Dispatch Timing investigation (see the PA-5 notes in the roadmap).
GOLDEN: dict[str, list] = {
    "rain_after_start": [(0.0, "cover.a1->50"), (22.0, "cover.b1->50"),
                         (42.0, "cover.a1->0"), (42.0, "cover.a1->0"), (42.0, "cover.a1->0")],
    "rain_at_start": [(0.0, "cover.a1->0"), (0.0, "cover.a1->0"), (0.0, "cover.a1->0")],
    "absence": None,        # order-insensitive across zones, see check_absence_log()
    "manual_stop": [(0.0, "cover.a1->50")],
    "restart_cycle": [(0.0, "cover.a1->50"), (22.0, "cover.b1->50")],
    "shared_cover": [(0.0, "cover.s1->50")],
    "priority": [(0.0, "cover.a1->50"), (0.0, "cover.b1->0"), (0.0, "cover.b1->0")],
}


#: Absence: both zones reach their (equal) delay at the same virtual instant.  Which independent
#: zone the shared dispatcher serves first is NOT a contract (observed: either "B, A1, A2" or
#: "A1, A2, B" - same in both installation modes, in both phases).  What IS contractual and is
#: checked exactly: the shared pacing slots (virtual seconds), the multiset of commands, every
#: cover's own ordered sequence, the order of covers inside one zone, and that closing finishes
#: before the reopening starts.
#: DT-2: the closing starts at the absence deadline itself (ABSENCE_MIN = 2 minutes = 120 s after the
#: household left), not at the next periodic update (it used to be 330 s); the pacing between the
#: covers (travel + settle) and the reopening after coming home follow from that.
ABSENCE_SLOTS = [120.0, 142.0, 164.0, 574.0, 576.0, 578.0]
ABSENCE_CLOSE = ["cover.a1->20", "cover.a2->20", "cover.b1->20"]
ABSENCE_OPEN = ["cover.a1->100", "cover.a2->100", "cover.b1->100"]
MIN_SLOT_GAP_S = 2.0                                  # global dispatch interval


def check_absence_log(got: list) -> None:
    assert [t for t, _ in got] == ABSENCE_SLOTS, got          # delay + pacing, exact
    gaps = [b - a for a, b in zip(ABSENCE_SLOTS, ABSENCE_SLOTS[1:])]
    assert all(g >= MIN_SLOT_GAP_S for g in gaps), gaps
    closing, opening = [c for _, c in got[:3]], [c for _, c in got[3:]]
    assert sorted(closing) == ABSENCE_CLOSE, got              # targets, multiplicity, none missing
    assert sorted(opening) == ABSENCE_OPEN, got
    for cover in ("cover.a1", "cover.a2", "cover.b1"):        # per-cover sequence: close, then open
        assert [c.split("->")[1] for _, c in got if c.startswith(cover + "->")] == ["20", "100"], got
    for phase in (closing, opening):                          # one zone's covers keep their order
        assert phase.index(next(c for c in phase if c.startswith("cover.a1"))) <             phase.index(next(c for c in phase if c.startswith("cover.a2"))), got


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_the_scenario_runs_identically_for_a_fresh_and_an_upgraded_installation(uz, name) -> None:
    h = await SCENARIOS[name](uz)
    got = h.commands()
    if name == "absence":
        check_absence_log(got)
    else:
        assert got == GOLDEN[name], (uz.mode, name)


async def test_a_restart_of_a_settled_installation_sends_no_duplicate_command(uz) -> None:
    h = await scenario_restart_cycle(uz)
    assert h.extra_after_restart == 0, h.commands()


async def test_the_comparison_is_sensitive_to_an_inherited_value(uz_control) -> None:
    """Control: when the System entry's absence delay differs, the upgraded run differs from the
    golden log - proving that equality above is not vacuous."""
    h = await scenario_absence_inherited(uz_control)
    with pytest.raises(AssertionError):
        check_absence_log(h.commands())


async def scenario_absence_inherited(h):
    """scenario_absence without the equal-delay assertion (the delay is deliberately different)."""
    h = await _absence_boot(h)
    h.sun_up()
    await h.drive(h.refresh())
    h.hass.states.async_set(PERSON, "not_home")
    await _event_cycles(h)
    h.hass.states.async_set(PERSON, "home")
    await _event_cycles(h)
    return h


# ---------------------------------------------------------------------------
# Fault probes for the order-insensitive absence check: each defect must be a real assertion failure
# while both legitimate cross-zone orders pass.
# ---------------------------------------------------------------------------

_B_FIRST = [(120.0, "cover.b1->20"), (142.0, "cover.a1->20"), (164.0, "cover.a2->20"),
            (574.0, "cover.b1->100"), (576.0, "cover.a1->100"), (578.0, "cover.a2->100")]
_A_FIRST = [(120.0, "cover.a1->20"), (142.0, "cover.a2->20"), (164.0, "cover.b1->20"),
            (574.0, "cover.a1->100"), (576.0, "cover.a2->100"), (578.0, "cover.b1->100")]


def _with(log, index, value=None, time=None):
    out = list(log)
    out[index] = (out[index][0] if time is None else time, out[index][1] if value is None else value)
    return out


@pytest.mark.parametrize("log", [_B_FIRST, _A_FIRST])
def test_both_legitimate_cross_zone_orders_are_accepted(log) -> None:
    check_absence_log(log)


@pytest.mark.parametrize("defect", {
    "wrong_target": _with(_A_FIRST, 1, "cover.a2->30"),
    "missing_command": _A_FIRST[:-1],
    "duplicate_command": _A_FIRST + [(580.0, "cover.b1->100")],
    "duplicate_in_place": _with(_A_FIRST, 2, "cover.a2->20"),
    "wrong_inherited_delay": [(t + 60.0 if t < 400 else t, c) for t, c in _A_FIRST],
    "pacing_violation": _with(_A_FIRST, 4, time=575.0),
    "reopen_before_close_done": _with(_A_FIRST, 3, "cover.a1->100", 130.0),
    "zone_order_swapped": [(120.0, "cover.a2->20"), (142.0, "cover.a1->20"), (164.0, "cover.b1->20"),
                           (574.0, "cover.a1->100"), (576.0, "cover.a2->100"), (578.0, "cover.b1->100")],
    "cover_never_reopened": _with(_A_FIRST, 5, "cover.b1->20"),
}.values(), ids=lambda v: None)
def test_every_absence_defect_is_a_genuine_assertion_failure(defect) -> None:
    with pytest.raises(AssertionError):
        check_absence_log(defect)
