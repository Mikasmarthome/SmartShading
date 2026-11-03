"""A normal command is never sent around the zone package and the pacing.

Every allowed normal command of a cycle is a member of that cycle's plan and reaches the
cover only through the package and the shared pacing.  The only way an allowed normal
command can be missing from the plan's results is a change between building the plan and
assembling the cycle's results (the startup grace ends, or a cover gains its role for
this window while the plan runs).  Such a command is refused, not sent on a side path;
the change itself triggers a fresh evaluation that plans it properly.  Real coordinators,
the real shared dispatch, simulated covers and virtual time (see conftest.py).
"""
from __future__ import annotations

import pytest

S = 22.0           # one intermediate cover: travel + settle (see test_real_ha_zone_packages.py)


async def _later(h, rounds=8):
    for _ in range(rounds):
        await h.fire_ha_timers(100)
        await h.drive(horizon=h.clock.t + 30)


def _after_plan(coordinator, hook):
    """Run ``hook`` once the cycle's comfort plan has finished and its results are
    about to be assembled -- the moment between building the plan and using it."""
    real = coordinator._run_comfort_dispatch_for_cycle

    async def run_then_hook(*args, **kwargs):
        result = await real(*args, **kwargs)
        hook()
        return result

    coordinator._run_comfort_dispatch_for_cycle = run_then_hook


def _restore(coordinator, *names):
    """Drop the instance-level replacements (the class methods are the real ones).  The
    shared monkeypatch of the harness must stay in place, so it is not undone."""
    for name in names:
        coordinator.__dict__.pop(name, None)


async def test_command_missed_by_the_plan_because_startup_grace_ended_is_refused_not_sent(
    zones,
):
    h = await zones.boot([("ea", {"wa": ["cover.a1"]}, 50, None)])
    coordinator = h.coord("ea")
    coordinator._startup_cycles_remaining = 1                    # grace while the plan is built ...
    _after_plan(coordinator, lambda: setattr(coordinator, "_startup_cycles_remaining", 0))
    h.sun_up()
    await h.drive(h.refresh("ea"))
    assert h.clock.calls() == [], "nothing may be sent around the package"       # ... over afterwards
    assert coordinator._package_stats["normal_bypass_refused"] == 1
    assert coordinator.data.execution_diagnostics["wa"].last_command_status == "not_attempted"
    _restore(coordinator, "_run_comfort_dispatch_for_cycle")
    # the next evaluation decides afresh and sends it once, through the plan
    await _later(h)
    assert [c[1:] for c in h.clock.calls()] == [("cover.a1", 50)]
    assert coordinator._package_stats["normal_bypass_refused"] == 1


async def test_sibling_cover_the_plan_did_not_know_is_refused_and_planned_next_time(
    zones,
):
    h = await zones.boot([
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1"]}, 50, None),
    ])
    coordinator = h.coord("ea")
    plan_built = {"done": False}
    real = coordinator._dispatchable_cover_ids

    def dispatchable(window, is_safety):
        covers = real(window, is_safety)
        # while the plan is built the second cover is not this window's to command; by the
        # time the results are assembled it is (its role changed under the running plan)
        return covers if plan_built["done"] else [c for c in covers if c != "cover.a2"]

    coordinator._dispatchable_cover_ids = dispatchable
    _after_plan(coordinator, lambda: plan_built.update(done=True))
    h.sun_up()
    await h.drive(h.refresh("ea", "eb"))
    sent = [c[1] for c in h.clock.calls()]
    assert sent == ["cover.a1", "cover.b1"], "the unplanned cover is not sent on a side path"
    assert coordinator._package_stats["normal_bypass_refused"] == 1
    _restore(coordinator, "_run_comfort_dispatch_for_cycle", "_dispatchable_cover_ids")
    await _later(h)
    sent = [c[1] for c in h.clock.calls()]
    assert sent.count("cover.a2") == 1 and sent.count("cover.a1") == 1 and sent.count("cover.b1") == 1
    a2 = [c for c in h.clock.calls() if c[1] == "cover.a2"][0]
    previous = max(c[0] for c in h.clock.calls() if c[1] != "cover.a2")
    assert a2[0] - previous >= 2.0, "it waits for the shared pacing like any other normal command"
    assert coordinator._package_stats["normal_bypass_refused"] == 1


@pytest.mark.parametrize("initial", [{"cover.a1": 50}, {"cover.a1": 100}])
async def test_a_planned_command_is_sent_exactly_once_and_never_by_the_fall_through(zones, initial):
    h = await zones.boot(
        [("ea", {"wa": ["cover.a1"]}, 50, None), ("eb", {"wb": ["cover.b1"]}, 50, None)],
        initial=initial)
    h.sun_up()
    await h.drive(h.refresh("ea", "eb"))
    await _later(h, 2)
    calls = h.clock.calls()
    expected = ["cover.b1"] if initial["cover.a1"] == 50 else ["cover.a1", "cover.b1"]
    assert [c[1] for c in calls] == expected, "no-op sends nothing, the rest exactly once"
    for entry in ("ea", "eb"):
        assert h.coord(entry)._package_stats["normal_bypass_refused"] == 0
    gaps = [round(b[0] - a[0], 2) for a, b in zip(calls, calls[1:])]
    assert all(g >= S for g in gaps), gaps
