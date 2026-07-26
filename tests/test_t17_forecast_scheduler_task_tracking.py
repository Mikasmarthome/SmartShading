"""T17 — forecast_scheduler.py startup-task tracking.

Prior to T17, async_setup_forecast_learning() fired its two immediate
first-run callbacks via a bare hass.async_create_task(...) — untracked by
anything: not entry-scoped (so HA's own entry-unload machinery never saw
it), and not coordinator-tracked either. It could keep running past
unload/removal.

T17 fix: the two startup tasks are now created via a background-task
factory tied to the config entry (entry.async_create_background_task in
production, injectable via _create_background_task for tests), and the two
returned cancel callables also cancel the corresponding startup task.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.smartshading.engines import forecast_scheduler
from custom_components.smartshading.models.forecast_store import ForecastLearningStore


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    return hass


class TestStartupTasksAreEntryTrackedNotBareHassTask:
    def test_startup_tasks_use_injected_background_task_factory_not_bare_hass_task(self):
        hass = _make_hass()
        entry = MagicMock()
        store = ForecastLearningStore.empty()
        adapter = MagicMock()
        created = []

        def _fake_create_bg_task(coro, name):
            created.append(name)
            return asyncio.ensure_future(coro)

        async def scenario():
            def _fake_track_interval(hass, cb, interval):
                return lambda: None

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(forecast_scheduler, "async_collect_forecast_cycle", AsyncMock())
                mp.setattr(forecast_scheduler, "async_collect_reality_cycle", AsyncMock())
                result = await forecast_scheduler.async_setup_forecast_learning(
                    hass, entry, store, adapter,
                    forecast_entity_id="weather.home",
                    temp_entity_id=None, cloud_entity_id=None, solar_entity_id=None,
                    _track_interval=_fake_track_interval,
                    _create_background_task=_fake_create_bg_task,
                )
            # hass.async_create_task must NEVER be called directly by this module.
            hass.async_create_task.assert_not_called()
            assert created == [
                "smartshading_forecast_startup", "smartshading_forecast_reality_startup",
            ]
            return result

        result = asyncio.run(scenario())
        assert result is not None

    def test_production_default_ties_startup_tasks_to_the_entry(self):
        """Without injection (production path), the factory must go through
        entry.async_create_background_task — not a bare hass call."""
        hass = _make_hass()
        entry = MagicMock()
        entry.async_create_background_task = MagicMock(
            side_effect=lambda hass, coro, name: asyncio.ensure_future(coro))
        store = ForecastLearningStore.empty()
        adapter = MagicMock()

        async def scenario():
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(forecast_scheduler, "async_collect_forecast_cycle", AsyncMock())
                mp.setattr(forecast_scheduler, "async_collect_reality_cycle", AsyncMock())
                def _fake_track_interval(hass, cb, interval):
                    return lambda: None
                await forecast_scheduler.async_setup_forecast_learning(
                    hass, entry, store, adapter,
                    forecast_entity_id="weather.home",
                    temp_entity_id=None, cloud_entity_id=None, solar_entity_id=None,
                    _track_interval=_fake_track_interval,
                )
            assert entry.async_create_background_task.call_count == 2
            hass.async_create_task.assert_not_called()

        asyncio.run(scenario())

    def test_cancel_callables_also_cancel_the_startup_task(self):
        hass = _make_hass()
        entry = MagicMock()
        store = ForecastLearningStore.empty()
        adapter = MagicMock()
        tasks = []

        def _fake_create_bg_task(coro, name):
            t = asyncio.ensure_future(coro)
            tasks.append(t)
            return t

        async def scenario():
            def _fake_track_interval(hass, cb, interval):
                return lambda: None

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(forecast_scheduler, "async_collect_forecast_cycle", AsyncMock())
                mp.setattr(forecast_scheduler, "async_collect_reality_cycle", AsyncMock())
                cancel_forecast, cancel_reality = await forecast_scheduler.async_setup_forecast_learning(
                    hass, entry, store, adapter,
                    forecast_entity_id="weather.home",
                    temp_entity_id=None, cloud_entity_id=None, solar_entity_id=None,
                    _track_interval=_fake_track_interval,
                    _create_background_task=_fake_create_bg_task,
                )
            assert len(tasks) == 2
            cancel_forecast()
            cancel_reality()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert all(t.cancelled() or t.done() for t in tasks)

        asyncio.run(scenario())

    def test_inactive_forecast_learning_creates_no_tasks(self):
        hass = _make_hass()
        entry = MagicMock()
        store = ForecastLearningStore.empty()
        adapter = MagicMock()

        result = asyncio.run(forecast_scheduler.async_setup_forecast_learning(
            hass, entry, store, adapter,
            forecast_entity_id=None,  # inactive
            temp_entity_id=None, cloud_entity_id=None, solar_entity_id=None,
        ))

        assert result is None
        hass.async_create_task.assert_not_called()
        entry.async_create_background_task.assert_not_called()
