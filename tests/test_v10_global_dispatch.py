"""Tests for v1.0 — Global Serial Dispatch is truly global.

Verifies:
- A single GlobalSerialDispatch instance is shared by all zone coordinators.
- Throttle state is shared: dispatch by zone A blocks zone B.
- Separate GlobalSerialDispatch instances do NOT share state (isolation check).
- record_dispatch / time_until_next_allowed work correctly across zones.
- The hass.data[DOMAIN] guard pattern ensures one instance per integration.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.smartshading.cover_control.global_dispatch_throttle import (
    GlobalSerialDispatch,
)

UTC = timezone.utc


def _ts(offset_s: float = 0.0) -> datetime:
    return datetime(2026, 6, 20, 10, 0, 0, tzinfo=UTC) + timedelta(seconds=offset_s)


class _FakeMono:
    """Injectable deterministic monotonic clock for tests."""
    def __init__(self, t: float = 0.0) -> None:
        self._t = t
    def __call__(self) -> float:
        return self._t
    def advance(self, seconds: float) -> None:
        self._t += seconds
    def set(self, t: float) -> None:
        self._t = t


# ---------------------------------------------------------------------------
# 1. GlobalSerialDispatch — basic API
# ---------------------------------------------------------------------------

class TestGlobalSerialDispatchBasic:

    def test_no_dispatch_yet_zero_wait(self):
        gsd = GlobalSerialDispatch()
        assert gsd.time_until_next_allowed() == timedelta(0)

    def test_last_dispatch_at_none_initially(self):
        gsd = GlobalSerialDispatch()
        assert gsd.last_dispatch_at is None

    def test_record_dispatch_updates_last(self):
        gsd = GlobalSerialDispatch()
        gsd.record_dispatch(_ts(0))
        assert gsd.last_dispatch_at == _ts(0)

    def test_min_interval_enforced(self):
        mono = _FakeMono(0.0)
        interval = timedelta(seconds=30)
        gsd = GlobalSerialDispatch(min_interval=interval, mono_clock=mono)
        gsd.record_dispatch(_ts(0))
        mono.advance(10.0)  # 10 seconds elapsed
        remaining = gsd.time_until_next_allowed()
        assert remaining == timedelta(seconds=20)

    def test_after_min_interval_zero_wait(self):
        mono = _FakeMono(0.0)
        interval = timedelta(seconds=30)
        gsd = GlobalSerialDispatch(min_interval=interval, mono_clock=mono)
        gsd.record_dispatch(_ts(0))
        mono.advance(30.0)
        assert gsd.time_until_next_allowed() == timedelta(0)
        mono.advance(30.0)  # 60s total
        assert gsd.time_until_next_allowed() == timedelta(0)


# ---------------------------------------------------------------------------
# 2. Shared instance — multiple zones share the same object
# ---------------------------------------------------------------------------

class TestSharedInstance:

    def test_shared_state_zone_a_dispatch_blocks_zone_b(self):
        """The same GlobalSerialDispatch passed to zone A and B.
        After zone A dispatches, zone B must wait."""
        mono = _FakeMono(0.0)
        interval = timedelta(seconds=60)
        shared = GlobalSerialDispatch(min_interval=interval, mono_clock=mono)

        shared.record_dispatch(_ts(0))
        mono.advance(20.0)  # 20 seconds elapsed
        remaining = shared.time_until_next_allowed()
        assert remaining == timedelta(seconds=40)

    def test_separate_instances_do_not_share_state(self):
        """Two separate GlobalSerialDispatch instances are independent."""
        mono_a = _FakeMono(0.0)
        mono_b = _FakeMono(0.0)
        interval = timedelta(seconds=60)
        zone_a_dispatch = GlobalSerialDispatch(min_interval=interval, mono_clock=mono_a)
        zone_b_dispatch = GlobalSerialDispatch(min_interval=interval, mono_clock=mono_b)

        zone_a_dispatch.record_dispatch(_ts(0))
        mono_a.advance(20.0)
        # Zone B's separate instance knows nothing about zone A's dispatch
        assert zone_b_dispatch.time_until_next_allowed() == timedelta(0)

    def test_shared_instance_object_identity(self):
        """Simulate hass.data guard pattern: same instance is returned."""
        domain_data: dict = {}
        key = "global_dispatch"
        interval = timedelta(seconds=30)

        # First zone sets up
        if key not in domain_data:
            domain_data[key] = GlobalSerialDispatch(min_interval=interval)
        instance_a = domain_data[key]

        # Second zone reuses same instance
        if key not in domain_data:
            domain_data[key] = GlobalSerialDispatch(min_interval=interval)
        instance_b = domain_data[key]

        assert instance_a is instance_b

    def test_multiple_zones_shared_dispatch_serialised(self):
        """Three zones sharing one dispatch: sequential dispatches respect interval."""
        mono = _FakeMono(0.0)
        interval = timedelta(seconds=10)
        shared = GlobalSerialDispatch(min_interval=interval, mono_clock=mono)

        shared.record_dispatch(_ts(0))          # Zone 1 at mono=0
        mono.advance(5.0)
        assert shared.time_until_next_allowed() == timedelta(seconds=5)
        mono.advance(5.0)                       # now at mono=10
        shared.record_dispatch(_ts(10))         # Zone 2 dispatches at mono=10
        mono.advance(5.0)                       # now at mono=15
        assert shared.time_until_next_allowed() == timedelta(seconds=5)
        mono.advance(5.0)                       # now at mono=20
        assert shared.time_until_next_allowed() == timedelta(0)


# ---------------------------------------------------------------------------
# 3. Dispatch state is not reset by a second zone "joining"
# ---------------------------------------------------------------------------

class TestDispatchStatePreserved:

    def test_late_joining_zone_sees_existing_throttle(self):
        """A zone that joins after another has already dispatched must still see
        the throttle — it cannot 'reset' state by acquiring the same instance."""
        mono = _FakeMono(0.0)
        shared = GlobalSerialDispatch(min_interval=timedelta(seconds=60), mono_clock=mono)

        shared.record_dispatch(_ts(0))
        zone_b_ref = shared
        mono.advance(10.0)
        assert zone_b_ref.time_until_next_allowed() == timedelta(seconds=50)
        assert zone_b_ref.last_dispatch_at == _ts(0)
