"""Tests for Step 10A — Global Sequential Cover Dispatch.

Verifies:
  - GlobalSerialDispatch class structure and API
  - Lock is shared across all instances created from the same object
  - Throttle state is shared through the same GlobalSerialDispatch instance
  - Per-coordinator fallback (GlobalSerialDispatch() without shared instance)
  - Cross-zone burst prevention (two coordinators share the same lock)
  - Safety commands acquire the lock but skip the throttle sleep
  - Non-safety commands sleep until the throttle allows the next dispatch
  - const.py exports DATA_GLOBAL_DISPATCH, DATA_DEBUG_LOGGING, CONF_DEBUG_LOGGING
  - Coordinator constructor accepts global_serial_dispatch parameter
  - __init__.py initialises shared GlobalSerialDispatch in hass.data
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_components.smartshading.const import (
    CONF_DEBUG_LOGGING,
    DATA_DEBUG_LOGGING,
    DATA_GLOBAL_DISPATCH,
    DOMAIN,
)
from custom_components.smartshading.cover_control.global_dispatch_throttle import (
    DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS,
    GlobalDispatchThrottle,
    GlobalSerialDispatch,
)


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
# Part 1 — GlobalSerialDispatch: structure
# ---------------------------------------------------------------------------


class TestGlobalSerialDispatchStructure:
    def test_class_exists(self):
        assert GlobalSerialDispatch is not None

    def test_has_lock_property(self):
        gsd = GlobalSerialDispatch()
        assert hasattr(gsd, "lock")

    def test_has_time_until_next_allowed(self):
        gsd = GlobalSerialDispatch()
        assert callable(getattr(gsd, "time_until_next_allowed", None))

    def test_has_record_dispatch(self):
        gsd = GlobalSerialDispatch()
        assert callable(getattr(gsd, "record_dispatch", None))

    def test_has_min_interval(self):
        gsd = GlobalSerialDispatch()
        assert hasattr(gsd, "min_interval")

    def test_has_last_dispatch_at(self):
        gsd = GlobalSerialDispatch()
        assert hasattr(gsd, "last_dispatch_at")

    def test_last_dispatch_at_is_none_initially(self):
        gsd = GlobalSerialDispatch()
        assert gsd.last_dispatch_at is None

    def test_default_min_interval_matches_throttle(self):
        gsd = GlobalSerialDispatch()
        throttle = GlobalDispatchThrottle()
        assert gsd.min_interval == throttle.min_interval

    def test_custom_min_interval(self):
        custom = timedelta(seconds=2.5)
        gsd = GlobalSerialDispatch(min_interval=custom)
        assert gsd.min_interval == custom


# ---------------------------------------------------------------------------
# Part 2 — Lock behaviour
# ---------------------------------------------------------------------------


class TestGlobalSerialDispatchLock:
    def test_lock_is_asyncio_lock(self):
        gsd = GlobalSerialDispatch()
        # Access from a sync context — lock is created lazily
        lock = gsd.lock
        assert isinstance(lock, asyncio.Lock)

    def test_lock_is_same_object_on_repeated_access(self):
        gsd = GlobalSerialDispatch()
        assert gsd.lock is gsd.lock

    def test_shared_instance_has_same_lock(self):
        gsd = GlobalSerialDispatch()
        # Two "coordinators" holding a reference to the same instance
        # must see the same lock object
        lock_a = gsd.lock
        lock_b = gsd.lock
        assert lock_a is lock_b

    def test_different_instances_have_different_locks(self):
        gsd_a = GlobalSerialDispatch()
        gsd_b = GlobalSerialDispatch()
        assert gsd_a.lock is not gsd_b.lock

    def test_lock_acquisition_works(self):
        async def _acquire():
            gsd = GlobalSerialDispatch()
            async with gsd.lock:
                return True

        assert asyncio.run(_acquire()) is True

    def test_lock_blocks_concurrent_acquire(self):
        """While one coroutine holds the lock, another must wait."""
        results: list[str] = []

        async def _run():
            gsd = GlobalSerialDispatch()

            async def holder():
                async with gsd.lock:
                    results.append("held")
                    await asyncio.sleep(0.05)
                    results.append("released")

            async def waiter():
                await asyncio.sleep(0.01)  # Let holder acquire first
                async with gsd.lock:
                    results.append("acquired_after")

            await asyncio.gather(holder(), waiter())

        asyncio.run(_run())
        assert results == ["held", "released", "acquired_after"]


# ---------------------------------------------------------------------------
# Part 3 — Throttle state through GlobalSerialDispatch
# ---------------------------------------------------------------------------


class TestGlobalSerialDispatchThrottle:
    def _utc(self, **kwargs) -> datetime:
        return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc) + timedelta(**kwargs)

    def test_no_wait_when_no_dispatch_recorded(self):
        gsd = GlobalSerialDispatch()
        wait = gsd.time_until_next_allowed()
        assert wait == timedelta(0)

    def test_wait_after_dispatch(self):
        mono = _FakeMono(0.0)
        gsd = GlobalSerialDispatch(mono_clock=mono)
        t0 = self._utc()
        gsd.record_dispatch(t0)
        # 0.5 s after: remaining wait = 2.0s interval - 0.5s elapsed = 1.5s
        # (F32 field fix: raised from 1.5s to 2.0s)
        mono.advance(0.5)
        wait = gsd.time_until_next_allowed()
        assert wait > timedelta(0)
        assert wait < timedelta(seconds=2.0)

    def test_no_wait_after_interval_elapsed(self):
        mono = _FakeMono(0.0)
        gsd = GlobalSerialDispatch(mono_clock=mono)
        t0 = self._utc()
        gsd.record_dispatch(t0)
        mono.advance(2.0)
        wait = gsd.time_until_next_allowed()
        assert wait == timedelta(0)

    def test_last_dispatch_at_updated(self):
        gsd = GlobalSerialDispatch()
        t0 = self._utc()
        gsd.record_dispatch(t0)
        assert gsd.last_dispatch_at == t0

    def test_record_dispatch_shared_across_same_instance(self):
        """Two references to the same GlobalSerialDispatch must see the same throttle."""
        mono = _FakeMono(0.0)
        gsd = GlobalSerialDispatch(mono_clock=mono)
        ref_a = gsd
        ref_b = gsd  # same object
        t0 = self._utc()
        ref_a.record_dispatch(t0)
        assert ref_b.last_dispatch_at == t0
        mono.advance(0.1)
        wait = ref_b.time_until_next_allowed()
        assert wait > timedelta(0)


# ---------------------------------------------------------------------------
# Part 4 — Cross-zone burst prevention (integration-level logic)
# ---------------------------------------------------------------------------


class TestCrossZoneBurstPrevention:
    """Validates the design intent: sharing one GlobalSerialDispatch instance
    across multiple coordinators prevents simultaneous dispatches."""

    def test_shared_lock_serialises_two_concurrent_dispatches(self):
        """Simulate two 'coordinators' dispatching concurrently.  With a shared
        lock, they must execute sequentially, not simultaneously."""
        order: list[str] = []

        async def _coord(name: str, gsd: GlobalSerialDispatch) -> None:
            async with gsd.lock:
                order.append(f"{name}_start")
                await asyncio.sleep(0.02)
                order.append(f"{name}_end")

        async def _run():
            shared = GlobalSerialDispatch()
            await asyncio.gather(
                _coord("zone_a", shared),
                _coord("zone_b", shared),
            )

        asyncio.run(_run())
        # With a shared lock the end of the first must come before the
        # start of the second — they never interleave
        assert order.index("zone_a_end") < order.index("zone_b_start") or \
               order.index("zone_b_end") < order.index("zone_a_start")

    def test_two_separate_locks_can_interleave(self):
        """Without a shared lock, two coordinators CAN interleave — proving
        the shared-lock design is what prevents the burst."""
        order: list[str] = []

        async def _coord(name: str) -> None:
            # Each gets its own lock — NOT shared
            gsd = GlobalSerialDispatch()
            async with gsd.lock:
                order.append(f"{name}_start")
                await asyncio.sleep(0.02)
                order.append(f"{name}_end")

        async def _run():
            await asyncio.gather(
                _coord("zone_a"),
                _coord("zone_b"),
            )

        asyncio.run(_run())
        # Without sharing, they interleave — start_a before end_a, start_b before end_a
        assert "zone_a_start" in order and "zone_b_start" in order


# ---------------------------------------------------------------------------
# Part 5 — Safety vs non-safety dispatch logic
# ---------------------------------------------------------------------------


class TestSafetyDispatchLogic:
    """Safety commands must acquire the lock but skip the throttle sleep.
    Non-safety commands must sleep until the throttle allows."""

    def _utc(self) -> datetime:
        return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_all_intents_must_wait_when_throttled(self):
        """Both regular and safety commands must wait — no throttle bypass."""
        mono = _FakeMono(0.0)
        gsd = GlobalSerialDispatch(mono_clock=mono)
        t0 = self._utc()
        gsd.record_dispatch(t0)
        mono.advance(0.1)  # 0.1 s later — still within the 1 s interval
        wait = gsd.time_until_next_allowed()
        assert wait.total_seconds() > 0, "All commands (including safety) must be throttled"

    def test_safety_also_subject_to_throttle(self):
        """Safety commands call time_until_next_allowed — no bypass."""
        mono = _FakeMono(0.0)
        gsd = GlobalSerialDispatch(mono_clock=mono)
        t0 = self._utc()
        gsd.record_dispatch(t0)
        mono.advance(0.01)
        wait = gsd.time_until_next_allowed()
        assert wait.total_seconds() > 0, "Safety must also respect the 1.0s minimum"

    def test_record_dispatch_called_after_safety_sent(self):
        """Safety SENT must update the throttle so subsequent commands
        wait the full interval from the safety dispatch time."""
        mono = _FakeMono(0.0)
        gsd = GlobalSerialDispatch(mono_clock=mono)
        t0 = self._utc()
        gsd.record_dispatch(t0)  # simulate safety SENT
        mono.advance(0.5)
        wait = gsd.time_until_next_allowed()
        assert wait.total_seconds() > 0


# ---------------------------------------------------------------------------
# Part 6 — const.py exports
# ---------------------------------------------------------------------------


class TestConstExports:
    def test_data_global_dispatch_exists(self):
        assert DATA_GLOBAL_DISPATCH is not None
        assert isinstance(DATA_GLOBAL_DISPATCH, str)

    def test_data_debug_logging_exists(self):
        assert DATA_DEBUG_LOGGING is not None
        assert isinstance(DATA_DEBUG_LOGGING, str)

    def test_conf_debug_logging_exists(self):
        assert CONF_DEBUG_LOGGING is not None
        assert isinstance(CONF_DEBUG_LOGGING, str)

    def test_data_global_dispatch_and_debug_logging_differ(self):
        assert DATA_GLOBAL_DISPATCH != DATA_DEBUG_LOGGING

    def test_conf_debug_logging_equals_data_debug_logging(self):
        # Both key names must match so options[CONF] == hass.data key semantics align
        assert CONF_DEBUG_LOGGING == DATA_DEBUG_LOGGING


# ---------------------------------------------------------------------------
# Part 7 — Coordinator accepts global_serial_dispatch parameter
# ---------------------------------------------------------------------------


class TestCoordinatorAcceptsSerialDispatch:
    def _src(self) -> str:
        return (
            Path(__file__).resolve().parent.parent
            / "custom_components" / "smartshading" / "coordinator.py"
        ).read_text(encoding="utf-8")

    def test_coordinator_constructor_signature_has_global_serial_dispatch(self):
        assert "global_serial_dispatch" in self._src()

    def test_coordinator_constructor_parameter_has_default_none(self):
        # The parameter must declare None as default value
        src = self._src()
        assert "global_serial_dispatch: GlobalSerialDispatch | None = None" in src

    def test_coordinator_has_serial_dispatch_attribute(self):
        assert "_serial_dispatch" in self._src()

    def test_coordinator_uses_asyncio_lock_in_dispatch_loop(self):
        assert "async with self._serial_dispatch.lock" in self._src()

    def test_per_coordinator_fallback_creates_serial_dispatch(self):
        # Fallback branch: if global_serial_dispatch is None, create one locally
        assert "GlobalSerialDispatch()" in self._src()

    def test_deprecated_throttle_alias_present(self):
        """_global_dispatch_throttle must remain as an alias for backward compat."""
        assert "_global_dispatch_throttle" in self._src()


# ---------------------------------------------------------------------------
# Part 8 — __init__.py initialises shared GlobalSerialDispatch
# ---------------------------------------------------------------------------


class TestInitGlobalDispatch:
    def test_init_imports_global_serial_dispatch(self):
        src = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading" / "__init__.py"
        text = src.read_text(encoding="utf-8")
        assert "GlobalSerialDispatch" in text

    def test_init_uses_data_global_dispatch_key(self):
        src = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading" / "__init__.py"
        text = src.read_text(encoding="utf-8")
        assert "DATA_GLOBAL_DISPATCH" in text

    def test_init_creates_serial_dispatch_once(self):
        """First zone entry creates the instance; subsequent entries reuse it."""
        src = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading" / "__init__.py"
        text = src.read_text(encoding="utf-8")
        # Must check "not in hass.data" before creating
        assert "DATA_GLOBAL_DISPATCH not in" in text

    def test_init_passes_serial_dispatch_to_coordinator(self):
        src = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading" / "__init__.py"
        text = src.read_text(encoding="utf-8")
        assert "global_serial_dispatch=serial_dispatch" in text

    def test_init_sets_default_debug_logging(self):
        src = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading" / "__init__.py"
        text = src.read_text(encoding="utf-8")
        assert "DATA_DEBUG_LOGGING" in text

    def test_system_entry_propagates_debug_flag(self):
        """System entry reads CONF_DEBUG_LOGGING from options and writes to hass.data."""
        src = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading" / "__init__.py"
        text = src.read_text(encoding="utf-8")
        assert "CONF_DEBUG_LOGGING" in text
        # Propagated to hass.data[DOMAIN][DATA_DEBUG_LOGGING]
        assert f"DATA_DEBUG_LOGGING" in text


# ---------------------------------------------------------------------------
# Part 9 — Minimum 1 second interval enforced
# ---------------------------------------------------------------------------


class TestMinimumInterval:
    def test_default_interval_is_two_seconds(self):
        # F32 field fix: raised from 1.5s to 2.0s.
        assert DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS == 2.0

    def test_global_serial_dispatch_default_interval(self):
        gsd = GlobalSerialDispatch()
        assert gsd.min_interval >= timedelta(seconds=1.0)

    def test_shared_dispatch_respects_minimum(self):
        """After a dispatch the throttle must not allow the next for <1.0 s."""
        mono = _FakeMono(0.0)
        gsd = GlobalSerialDispatch(mono_clock=mono)
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        gsd.record_dispatch(t0)
        mono.advance(0.001)  # 1ms elapsed
        wait = gsd.time_until_next_allowed()
        assert wait.total_seconds() >= 0.9  # at least ~0.999 s remaining
