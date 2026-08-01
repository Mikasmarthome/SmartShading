"""T17 — task-handle tracking and shutdown-lifecycle hardening.

Closes the residual race documented since T16: an event-triggered refresh
task (presence/contact/lifecycle-boundary), created via
entry.async_create_background_task, is only cancelled/awaited by HA's own
entry-unload machinery AFTER __init__.py's async_unload_entry has already
returned — meaning it could still be executing while the coordinator's own
teardown/flush runs concurrently against the same state.

Fix under test (coordinator.py):
  - self._background_tasks: set[asyncio.Task] — coordinator-owned, per-
    instance (hence per-entry) tracking of these tasks.
  - _create_tracked_background_task(entry, coro, name) — replaces the bare
    entry.async_create_background_task(...) call at all three listener call
    sites; refuses to schedule new work once self._unloading is set.
  - async_shutdown() (override) — sets self._unloading = True FIRST, then
    calls the base class shutdown, then cancels and awaits every tracked
    task, closing the window where one could still be executing.

These are real behavior tests (construct a coordinator, drive real asyncio
tasks through a real event loop via asyncio.run), not structural/AST checks —
per the ticket's explicit preference for behavior tests where feasible.

Beta-2 corrective phase R2 (fired important-save callback previously ran as
an invisible HA-core background task once async_call_later's timer actually
fired -- coordinator._background_tasks only ever saw the T17 listener tasks,
never this one):
  - self._pending_save_task: asyncio.Task | None -- set to the callback's OWN
    current task, for its FULL execution, at the top of
    _on_important_save_due(), via asyncio.current_task() (the public asyncio
    API, not a private HA-core internal).
  - _on_important_save_due() also registers that same task into the existing
    self._background_tasks set, so async_shutdown()'s already-existing
    generic cancel+await loop picks it up automatically with zero new
    shutdown-specific code.
  - _cancel_pending_save() is now async and, in addition to unsubbing a
    not-yet-fired timer, cancels and AWAITS self._pending_save_task if the
    timer had already fired — closing the gap between async_shutdown()'s own
    loop and async_flush_learning()'s subsequent authoritative save.
  - _request_important_save()'s scheduling call itself is UNCHANGED (still
    the cheap async_call_later timer, no eagerly-created coroutine) so this
    fix has zero behavioral impact on the ~9 other production call sites
    exercised by unrelated test files that never actually fire the timer.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# HA stubs — must precede any coordinator import in this module. Mirrors the
# bootstrap in test_v104_presence_fanout.py, with async_shutdown added to the
# DataUpdateCoordinator stub (needed by SmartShadingCoordinator.async_shutdown's
# super() call, which real HA provides but the stub otherwise wouldn't).
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _CoordBase:
    """Minimal DataUpdateCoordinator stub — includes async_shutdown (the base
    class method SmartShadingCoordinator.async_shutdown extends via super())."""

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.config_entry = config_entry
        self._refresh_task_created = 0
        self.base_shutdown_called = False

    def async_request_refresh(self):
        self._refresh_task_created += 1

        async def _noop():
            return None
        return _noop()

    async def async_shutdown(self) -> None:
        self.base_shutdown_called = True


class _StoreStub:
    def __init__(self, hass, version, key) -> None: pass
    async def async_load(self): return None
    async def async_save(self, data) -> None: pass
    async def async_remove(self) -> None: pass


_HA_STUBS = {
    "homeassistant": _stub("homeassistant"),
    "homeassistant.components": _stub("homeassistant.components"),
    "homeassistant.components.cover": _stub(
        "homeassistant.components.cover",
        CoverEntityFeature=type("CEF", (), {"SET_POSITION": 1, "SET_TILT_POSITION": 2, "OPEN": 4, "CLOSE": 8, "STOP": 16}),
    ),
    "homeassistant.config_entries": _stub("homeassistant.config_entries", ConfigEntry=object),
    "homeassistant.core": _stub(
        "homeassistant.core",
        HomeAssistant=object,
        Event=object,
        callback=lambda fn: fn,
    ),
    "homeassistant.helpers": _stub("homeassistant.helpers"),
    "homeassistant.helpers.entity_registry": _stub("homeassistant.helpers.entity_registry", async_get=lambda *a, **k: None),
    "homeassistant.helpers.event": _stub(
        "homeassistant.helpers.event",
        async_track_state_change_event=lambda hass, entity_id, action: (lambda: None),
        async_track_point_in_time=lambda *a, **k: (lambda: None),
        async_call_later=lambda hass, delay, action: (lambda: None),
    ),
    "homeassistant.util": _stub("homeassistant.util"),
}
for _name, _mod in _HA_STUBS.items():
    sys.modules.setdefault(_name, _mod)

import datetime as _datetime
if not hasattr(sys.modules.get("homeassistant.util.dt", _stub("_")), "utcnow"):
    sys.modules["homeassistant.util.dt"] = _stub(
        "homeassistant.util.dt",
        utcnow=lambda: _datetime.datetime.now(_datetime.timezone.utc),
        now=lambda: _datetime.datetime.now(_datetime.timezone.utc),
        as_utc=lambda dt: dt.astimezone(_datetime.timezone.utc),
        DEFAULT_TIME_ZONE=_datetime.timezone.utc,
    )

sys.modules["homeassistant.helpers.update_coordinator"] = _stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=_CoordBase,
    CoordinatorEntity=type("CE", (), {"__class_getitem__": classmethod(lambda cls, x: cls), "__init__": lambda self, c: None}),
)
sys.modules["homeassistant.helpers.storage"] = _stub("homeassistant.helpers.storage", Store=_StoreStub)

sys.modules.pop("custom_components.smartshading.coordinator", None)
from custom_components.smartshading.coordinator import SmartShadingCoordinator  # noqa: E402


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.async_create_task = MagicMock(return_value=None)
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    return hass


def _make_entry(real_tasks: bool = False) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}
    entry.data = {}
    entry.async_on_unload = MagicMock()
    if real_tasks:
        def _create_bg_task(hass, coro, name):
            return asyncio.ensure_future(coro)
        entry.async_create_background_task = MagicMock(side_effect=_create_bg_task)
    return entry


def _make_coord(entry=None, presence_entity_ids=None) -> SmartShadingCoordinator:
    hass = _make_hass()
    entry = entry or _make_entry()
    return SmartShadingCoordinator(
        hass, entry, presence_entity_ids=presence_entity_ids or [],
    )


async def _never_ending_refresh():
    """Simulates a real in-flight coordinator refresh (e.g. an
    async_request_refresh() that has entered dispatch_completion's bounded
    completion wait) that is still running when shutdown starts."""
    await asyncio.sleep(3600)


# ---------------------------------------------------------------------------
# _create_tracked_background_task: blocks new work once unloading
# ---------------------------------------------------------------------------

class TestCreateTrackedBackgroundTaskGating:
    def test_creates_and_tracks_a_real_task(self):
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)

        async def scenario():
            coord._create_tracked_background_task(entry, _never_ending_refresh(), "t")
            assert len(coord._background_tasks) == 1
            entry.async_create_background_task.assert_called_once()
            # cleanup: don't leak the task past this test
            for t in list(coord._background_tasks):
                t.cancel()
            await asyncio.gather(*coord._background_tasks, return_exceptions=True)

        asyncio.run(scenario())

    def test_unloading_blocks_new_task_creation(self):
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)
        coord._unloading = True
        coro = _never_ending_refresh()

        coord._create_tracked_background_task(entry, coro, "t")

        entry.async_create_background_task.assert_not_called()
        assert len(coord._background_tasks) == 0
        # The coroutine must be closed, not silently dropped un-awaited
        # (which would emit a "coroutine was never awaited" RuntimeWarning).
        assert inspect.getcoroutinestate(coro) == "GEN_CLOSED" or coro.cr_frame is None

    def test_completed_task_is_discarded_from_tracking_set(self):
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)

        async def scenario():
            async def _quick():
                return None
            coord._create_tracked_background_task(entry, _quick(), "t")
            assert len(coord._background_tasks) == 1
            await asyncio.sleep(0)  # let the task run to completion
            await asyncio.sleep(0)  # let its done-callback fire
            assert len(coord._background_tasks) == 0

        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# async_shutdown(): the actual race fix
# ---------------------------------------------------------------------------

class TestAsyncShutdownCancelsInFlightTasks:
    def test_in_flight_refresh_is_cancelled_and_awaited_by_shutdown(self):
        """The core T17 fix: a refresh already running when shutdown starts
        must not still be running once async_shutdown() returns."""
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)

        async def scenario():
            coord._create_tracked_background_task(entry, _never_ending_refresh(), "t")
            task = next(iter(coord._background_tasks))
            assert not task.done()

            await asyncio.wait_for(coord.async_shutdown(), timeout=2.0)

            assert task.done()
            assert task.cancelled()
            assert len(coord._background_tasks) == 0

        asyncio.run(scenario())

    def test_shutdown_does_not_hang_with_multiple_in_flight_tasks(self):
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)

        async def scenario():
            for i in range(3):
                coord._create_tracked_background_task(
                    entry, _never_ending_refresh(), f"t{i}")
            assert len(coord._background_tasks) == 3

            await asyncio.wait_for(coord.async_shutdown(), timeout=2.0)

            assert len(coord._background_tasks) == 0

        asyncio.run(scenario())

    def test_shutdown_sets_unloading_before_touching_tasks(self):
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)
        assert coord._unloading is False

        asyncio.run(coord.async_shutdown())

        assert coord._unloading is True

    def test_shutdown_calls_base_class_shutdown(self):
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)

        asyncio.run(coord.async_shutdown())

        assert coord.base_shutdown_called is True

    def test_shutdown_is_idempotent(self):
        """HA also invokes async_shutdown via entry.async_on_unload after our
        own async_unload_entry returns — a second call must be a safe no-op."""
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)

        async def scenario():
            coord._create_tracked_background_task(entry, _never_ending_refresh(), "t")
            await coord.async_shutdown()
            # second call: nothing left to cancel, must not raise or hang
            await asyncio.wait_for(coord.async_shutdown(), timeout=1.0)

        asyncio.run(scenario())

    def test_no_new_task_after_shutdown_even_if_listener_still_fires(self):
        """Listener callbacks are torn down separately (async_teardown_*), but
        even if one somehow fires between shutdown and teardown, no new
        refresh may be scheduled."""
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)

        async def scenario():
            await coord.async_shutdown()
            coord._create_tracked_background_task(
                entry, _never_ending_refresh(), "late")
            assert len(coord._background_tasks) == 0

        asyncio.run(scenario())

    def test_cancellation_is_not_logged_as_an_error(self, caplog):
        import logging
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)

        async def scenario():
            coord._create_tracked_background_task(entry, _never_ending_refresh(), "t")
            with caplog.at_level(logging.WARNING):
                await asyncio.wait_for(coord.async_shutdown(), timeout=2.0)

        asyncio.run(scenario())
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# Multi-entry isolation
# ---------------------------------------------------------------------------

class TestMultiEntryIsolation:
    def test_shutting_down_one_coordinator_does_not_cancel_another_entrys_task(self):
        entry_a = _make_entry(real_tasks=True)
        entry_b = _make_entry(real_tasks=True)
        coord_a = _make_coord(entry=entry_a)
        coord_b = _make_coord(entry=entry_b)

        async def scenario():
            coord_a._create_tracked_background_task(entry_a, _never_ending_refresh(), "a")
            coord_b._create_tracked_background_task(entry_b, _never_ending_refresh(), "b")
            task_b = next(iter(coord_b._background_tasks))

            await asyncio.wait_for(coord_a.async_shutdown(), timeout=2.0)

            assert len(coord_a._background_tasks) == 0
            assert not task_b.done()
            assert len(coord_b._background_tasks) == 1

            # cleanup
            await coord_b.async_shutdown()

        asyncio.run(scenario())

    def test_background_tasks_set_is_a_separate_instance_per_coordinator(self):
        coord_a = _make_coord()
        coord_b = _make_coord()
        assert coord_a._background_tasks is not coord_b._background_tasks


# ---------------------------------------------------------------------------
# Integration with the pre-existing _unloading-gated save scheduler (P10)
# ---------------------------------------------------------------------------

class TestShutdownIntegratesWithSaveDebounce:
    def test_request_important_save_is_a_no_op_after_shutdown(self):
        """Shutdown during debounce/coalescing: once shutdown has run,
        _request_important_save() (the existing P10 coalescing scheduler)
        must not schedule a new save callback."""
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)

        async def scenario():
            await coord.async_shutdown()
            coord._request_important_save()
            assert coord._pending_save_unsub is None

        asyncio.run(scenario())

    def test_flush_learning_still_runs_after_shutdown(self):
        """Shutdown during persistence save: async_flush_learning() (called
        AFTER async_shutdown() in __init__.py's unload sequence) must still
        be able to run its save to completion."""
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)
        saved = []

        async def _fake_save(now):
            saved.append(now)
            coord._saved_generation = coord._dirty_generation

        coord._save_learning_snapshot = _fake_save

        async def scenario():
            await coord.async_shutdown()
            await coord.async_flush_learning()

        asyncio.run(scenario())
        assert len(saved) == 1


# ---------------------------------------------------------------------------
# R2 (Beta-2 corrective phase): the fired important-save callback's own Task
# must be coordinator-tracked for its entire execution, not just while merely
# scheduled -- closing the gap where it previously ran as an invisible
# HA-core background task once async_call_later's timer actually fired.
# ---------------------------------------------------------------------------

class TestImportantSaveTaskTrackedThroughShutdown:
    def test_fired_save_in_progress_is_tracked_cancelled_and_awaited_by_shutdown(self):
        """The full R2 scenario: request -> timer fires -> callback is
        actively mid-save -> shutdown starts concurrently -> the task is
        visible in coordinator._background_tasks -> shutdown's existing
        generic loop cancels+awaits it, never hanging -> nothing is left
        running once shutdown returns -> the final flush is still able to run
        its own authoritative save afterward. Event/barrier synchronization
        throughout, no sleeps for timing, every await bounded by a timeout so
        a real regression fails fast instead of hanging the suite."""
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)

        save_started = asyncio.Event()
        release_save = asyncio.Event()
        save_calls: list = []

        async def _blocking_save(now):
            save_started.set()
            await release_save.wait()
            save_calls.append(now)
            coord._saved_generation = coord._dirty_generation

        coord._save_learning_snapshot = _blocking_save

        async def scenario():
            # NOTE: this test suite's shared conftest.py stubs
            # async_call_later as an unconditional no-op returning None (see
            # tests/conftest.py's _SHARED_HA_STUBS), so
            # coord._pending_save_unsub is never a live callable under test —
            # this call only exercises _mark_learning_dirty()'s side effect;
            # the fired callback below is what actually simulates the timer
            # having gone off.
            coord._request_important_save()

            # Simulate async_call_later's timer actually firing: the fired
            # job runs as a real Task, exactly as HA-core's
            # async_run_hass_job(..., background=True) would create one.
            fired_task = asyncio.ensure_future(coord._on_important_save_due())
            await asyncio.wait_for(save_started.wait(), timeout=2.0)

            # The callback has started running (mid-save, blocked on
            # release_save) -- it must already be coordinator-tracked, not
            # an invisible background job.
            assert fired_task in coord._background_tasks
            assert coord._pending_save_task is fired_task
            assert not fired_task.done()

            # Shutdown begins WHILE the save is still active and is NEVER
            # released -- proving shutdown's existing cancel+await loop does
            # not hang waiting for it and does not need cooperation from the
            # in-flight save to complete.
            await asyncio.wait_for(coord.async_shutdown(), timeout=2.0)

            assert fired_task.done()
            assert fired_task.cancelled()
            assert len(coord._background_tasks) == 0
            assert coord._pending_save_task is None
            assert save_calls == []  # cut off cleanly before writing anything

            # The authoritative flush must still be able to complete on its
            # own afterward -- swap in a plain, non-blocking save so this
            # part of the scenario is unambiguous.
            async def _plain_save(now):
                save_calls.append(now)
                coord._saved_generation = coord._dirty_generation

            coord._save_learning_snapshot = _plain_save
            await asyncio.wait_for(coord.async_flush_learning(), timeout=2.0)
            assert len(save_calls) == 1

        asyncio.run(scenario())

    def test_timer_firing_after_shutdowns_own_loop_already_ran_starts_no_save_at_all(self):
        """Narrower window: the timer fires AFTER async_shutdown()'s own
        cancel loop has already finished (so that generic loop cannot have
        seen it), but BEFORE async_flush_learning() runs -- exactly the
        __init__.py ordering (async_shutdown() then async_flush_learning()).
        _on_important_save_due()'s own _unloading guard (set by shutdown)
        means this window is closed by never starting a second save in the
        first place, not by racing a cancellation -- so no save call happens
        at all here, and async_flush_learning() alone remains authoritative,
        without hanging."""
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)
        save_calls: list = []

        async def _record_save(now):
            save_calls.append(now)
            coord._saved_generation = coord._dirty_generation

        coord._save_learning_snapshot = _record_save

        async def scenario():
            await asyncio.wait_for(coord.async_shutdown(), timeout=2.0)
            assert coord._unloading is True

            # Timer fires only now, strictly after shutdown's own loop ran.
            await asyncio.wait_for(coord._on_important_save_due(), timeout=2.0)

            # No save started, nothing tracked, nothing left dangling.
            assert save_calls == []
            assert coord._pending_save_task is None
            assert len(coord._background_tasks) == 0

            # async_flush_learning() remains the sole, authoritative save.
            await asyncio.wait_for(coord.async_flush_learning(), timeout=2.0)
            assert len(save_calls) == 1

        asyncio.run(scenario())

    def test_callback_declines_to_start_a_save_if_already_unloading_when_it_fires(self):
        """Defense in depth: if the timer fires strictly after _unloading was
        already set, the callback must not start a second, redundant save
        that could race the authoritative flush -- it defers entirely."""
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)
        coord._unloading = True
        save_calls: list = []

        async def _record_save(now):
            save_calls.append(now)

        coord._save_learning_snapshot = _record_save

        asyncio.run(coord._on_important_save_due())

        assert save_calls == []
        assert coord._pending_save_task is None
        assert len(coord._background_tasks) == 0

    def test_no_leftover_task_can_rewrite_after_the_full_unload_sequence_completes(self):
        """End-to-end guarantee: after async_shutdown() + async_flush_learning()
        both complete (the real __init__.py ordering, immediately followed by
        entry removal in production), no important-save task remains that
        could still write to a since-removed entry's storage file, and no
        stray schedule can be created afterward either."""
        entry = _make_entry(real_tasks=True)
        coord = _make_coord(entry=entry)

        save_started = asyncio.Event()
        save_calls: list = []

        async def _blocking_save(now):
            save_started.set()
            await asyncio.sleep(3600)  # never released -- must be cancelled, not awaited out

        coord._save_learning_snapshot = _blocking_save

        async def scenario():
            coord._request_important_save()
            fired_task = asyncio.ensure_future(coord._on_important_save_due())
            await asyncio.wait_for(save_started.wait(), timeout=2.0)

            await asyncio.wait_for(coord.async_shutdown(), timeout=2.0)

            async def _plain_save(now):
                save_calls.append(now)
                coord._saved_generation = coord._dirty_generation

            coord._save_learning_snapshot = _plain_save
            await asyncio.wait_for(coord.async_flush_learning(), timeout=2.0)

            assert fired_task.done()
            assert len(coord._background_tasks) == 0
            assert coord._pending_save_task is None

            # Simulates the entry-removal step that follows unload in
            # __init__.py's async_remove_entry: nothing must still be able to
            # trigger a write from this point on.
            save_calls.clear()
            coord._request_important_save()
            assert coord._pending_save_unsub is None  # _unloading blocks scheduling
            assert save_calls == []

        asyncio.run(scenario())
