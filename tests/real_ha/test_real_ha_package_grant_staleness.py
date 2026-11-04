"""A plan that turns stale right after its package was granted sends nothing.

The package is granted atomically when the zone's turn comes; between the grant and the
first command the plan can still be replaced (generation bump), cancelled or the entry
unloaded.  Nothing of such an old plan may reach a cover, the package must be released
for the next zone, the reservation must not stay behind, and the shared pacing must not be
changed by a plan that never started a travel.  Real coordinators, the real shared dispatch,
simulated covers and virtual time (see conftest.py).
"""
from __future__ import annotations

import pytest

S = 22.0           # one intermediate cover: travel + settle (see test_real_ha_zone_packages.py)


class _HookedPackage:
    """The real package context; ``hook`` runs between the grant and the first command."""

    def __init__(self, context, hook) -> None:
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_hook", hook)

    def __getattr__(self, name):
        return getattr(self._context, name)

    def __setattr__(self, name, value):
        setattr(self._context, name, value)

    async def __aenter__(self):
        await self._context.__aenter__()
        if self._context.granted:
            self._hook()
        return self

    async def __aexit__(self, *exc):
        return await self._context.__aexit__(*exc)


def _stale_after_grant(h, coordinator, how, probe, delay=0.0):
    """Make the zone's next package, once granted, belong to a plan that is no longer
    current: replaced (generation bump), cancelled or both -- exactly where a newer
    evaluation or an ownership change can arrive."""
    serial = h.serial
    real = serial.package

    def hook():
        if probe:
            return
        pacing = serial.pacing
        probe.update(basis=pacing.basis, remaining=pacing.remaining(h.clock.t))
        if delay:
            # the plan turns stale while it waits out the boundary of the previous zone
            import asyncio

            async def later():
                await h.clock.sleep(delay)
                bump()

            probe["task"] = asyncio.ensure_future(later())
        else:
            bump()

    def bump():
        if how in ("generation", "both"):
            coordinator._dispatch_generation += 1
        if how in ("cancelled", "both"):
            coordinator._active_dispatch_cancellation.set()

    def package(zone_key, owner, **kwargs):
        context = real(zone_key, owner, **kwargs)
        return _HookedPackage(context, hook) if owner is coordinator else context

    serial.package = package


def _restore(h):
    h.serial.__dict__.pop("package", None)


@pytest.mark.parametrize("delay", [0.0, 1.0], ids=["at-grant", "during-boundary-wait"])
@pytest.mark.parametrize("how", ["generation", "cancelled", "both"])
async def test_a_plan_made_stale_right_after_the_grant_sends_nothing_and_frees_the_line(zones, how, delay):
    h = await zones.boot([
        ("ea", {"wa": ["cover.a1", "cover.a2"]}, 50, None),
        ("eb", {"wb": ["cover.b1", "cover.b2"]}, 50, None),
        ("ec", {"wc": ["cover.c1"]}, 50, None),
    ])
    probe: dict = {}
    _stale_after_grant(h, h.coord("eb"), how, probe, delay)
    h.sun_up()
    tasks = h.refresh("ea", "eb", "ec")
    await h.drive(tasks)
    _restore(h)
    sent = [c[1] for c in h.clock.calls()]
    assert probe, "zone B's package was granted"
    if delay:
        assert probe["remaining"] > delay, "the stale moment lies inside the boundary wait"
    assert not any(c.startswith("cover.b") for c in sent), f"the old plan sent {sent}"
    # the line moved on: zone C's package was granted after B's was handed back
    assert sent == ["cover.a1", "cover.a2", "cover.c1"], sent
    assert h.serial.packages.queued_count == 0 and not h.serial.packages.active
    assert h.serial.packages.stats()["kept_positions"] == 0
    assert h.coord("eb")._package_state == "none"
    # nothing was started by the stale plan, so it left the boundary as it found it
    assert probe["basis"] != "timeout_unconfirmed"
    # the next zone started only after zone A's travel and settle -- not earlier and not later
    calls = h.clock.calls()
    assert round(calls[2][0] - calls[1][0], 2) == S
    # the zone is evaluated afresh and sends its movements once, through its own package
    for _ in range(8):
        await h.fire_ha_timers(100)
        await h.drive(h.refresh("eb"))
    b = [c for c in h.clock.calls() if c[1].startswith("cover.b")]
    assert [c[1] for c in b] == ["cover.b1", "cover.b2"], b
    assert round(b[1][0] - b[0][0], 2) == S
    assert h.serial.packages.queued_count == 0 and not h.serial.packages.active


async def test_a_plan_stale_after_the_grant_leaves_the_pacing_untouched(zones):
    h = await zones.boot([
        ("ea", {"wa": ["cover.a1"]}, 50, None),
        ("eb", {"wb": ["cover.b1"]}, 50, None),
    ])
    probe: dict = {}
    _stale_after_grant(h, h.coord("eb"), "both", probe)
    h.sun_up()
    tasks = h.refresh("ea", "eb")
    await h.drive(tasks)
    _restore(h)
    pacing = h.serial.pacing
    # after the stale grant the line is free and the basis is the real one of zone A's travel
    assert pacing.basis == probe["basis"]
    assert [c[1] for c in h.clock.calls()] == ["cover.a1"]
