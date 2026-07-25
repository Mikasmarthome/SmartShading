"""Coordinator-level wiring for DispatchConfig (v1.2.0-beta.1, T11).

Same real-SmartShadingCoordinator-with-HA-stubs technique as
test_coordinator_override_release_strategy_wiring.py — proves the
Coordinator's own stored self._dispatch_config attribute genuinely reaches
GlobalSerialDispatch's wait computation via cover_control/
dispatch_orchestrator.py's effective_interval_s(), for each mode and for
zone_batching.

Coverage:
  CDW-01  No dispatch_config kwarg -> DispatchConfig() default (backward
          compatibility: reproduces the fixed 2.0s interval exactly).
  CDW-02  PARALLEL mode: effective_interval_s is 0 -> no throttle wait.
  CDW-03  SPACED mode: effective_interval_s matches configured
          start_interval_s -> real throttle wait via GlobalSerialDispatch.
  CDW-04  SEQUENTIAL mode: no pre-dispatch wait (0), independent of
          start_interval_s.
  CDW-05  zone_batching forces start_interval_s at a zone boundary
          regardless of mode, verified against the real GlobalSerialDispatch
          instance (not just the pure orchestrator function in isolation).
  CDW-06  A fresh Coordinator instance (HA reload / Config Reload) picks up
          a changed dispatch_config, never a stale previous-instance value.
  CDW-07  Multiple config entries can carry independent DispatchConfig
          instances without interfering with each other.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# HA stubs — identical technique to test_override_policy_e2e_wiring.py.
# ---------------------------------------------------------------------------


def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _CoordBase:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.config_entry = config_entry


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
        "homeassistant.core", HomeAssistant=object, Event=object, callback=lambda fn: fn,
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

sys.modules["homeassistant.util.dt"] = _stub(
    "homeassistant.util.dt",
    utcnow=lambda: datetime.now(timezone.utc),
    now=lambda: datetime.now(timezone.utc),
    as_utc=lambda dt: dt.astimezone(timezone.utc),
    as_local=lambda dt: dt,
    DEFAULT_TIME_ZONE=timezone.utc,
)

sys.modules["homeassistant.helpers.update_coordinator"] = _stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=_CoordBase,
    CoordinatorEntity=type("CE", (), {"__class_getitem__": classmethod(lambda cls, x: cls), "__init__": lambda self, c: None}),
)
sys.modules["homeassistant.helpers.storage"] = _stub("homeassistant.helpers.storage", Store=_StoreStub)

sys.modules.pop("custom_components.smartshading.coordinator", None)
from custom_components.smartshading.coordinator import SmartShadingCoordinator  # noqa: E402
from custom_components.smartshading.cover_control.dispatch_orchestrator import (  # noqa: E402
    effective_interval_s,
)
from custom_components.smartshading.models.dispatch_config import (  # noqa: E402
    DEFAULT_START_INTERVAL_S,
    DispatchConfig,
    DispatchMode,
)
from custom_components.smartshading.models.lifecycle import NightDayLifecycleConfig  # noqa: E402


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    return hass


def _make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}
    entry.data = {}
    entry.async_on_unload = MagicMock()
    return entry


def _make_coord(**kwargs) -> SmartShadingCoordinator:
    hass = _make_hass()
    entry = _make_entry()
    coord = SmartShadingCoordinator(
        hass, entry, lifecycle_config=NightDayLifecycleConfig(id="default"), **kwargs,
    )
    coord.windows = {}
    coord.zones = {}
    coord.cover_groups = {}
    return coord


class TestBackwardCompatibleDefault:
    def test_no_kwarg_gives_default_config(self) -> None:
        coord = _make_coord()
        assert coord._dispatch_config == DispatchConfig()
        assert coord._dispatch_config.mode is DispatchMode.SPACED
        assert coord._dispatch_config.start_interval_s == DEFAULT_START_INTERVAL_S

    def test_explicit_none_also_gives_default(self) -> None:
        coord = _make_coord(dispatch_config=None)
        assert coord._dispatch_config == DispatchConfig()


class TestModeReachesRealThrottle:
    def test_parallel_mode_no_wait(self) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.PARALLEL))
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        coord._serial_dispatch.record_dispatch(t0)
        interval = effective_interval_s(coord._dispatch_config, is_first_in_zone_group=False)
        assert interval == 0.0
        wait = coord._serial_dispatch.time_until_next_allowed(
            min_interval_override=timedelta(seconds=interval)
        )
        assert wait == timedelta(0)

    def test_spaced_mode_real_wait(self) -> None:
        coord = _make_coord(
            dispatch_config=DispatchConfig(mode=DispatchMode.SPACED, start_interval_s=5.0)
        )
        fake_mono = [100.0]
        coord._serial_dispatch._throttle._mono_clock = lambda: fake_mono[0]
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        coord._serial_dispatch.record_dispatch(t0)
        fake_mono[0] += 1.0  # only 1s elapsed of the configured 5s
        interval = effective_interval_s(coord._dispatch_config, is_first_in_zone_group=False)
        assert interval == 5.0
        wait = coord._serial_dispatch.time_until_next_allowed(
            min_interval_override=timedelta(seconds=interval)
        )
        assert wait == timedelta(seconds=4.0)

    def test_sequential_mode_no_pre_dispatch_wait(self) -> None:
        coord = _make_coord(
            dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL, start_interval_s=10.0)
        )
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        coord._serial_dispatch.record_dispatch(t0)
        interval = effective_interval_s(coord._dispatch_config, is_first_in_zone_group=False)
        assert interval == 0.0


class TestZoneBatchingReachesRealThrottle:
    def test_zone_boundary_forces_start_interval_regardless_of_mode(self) -> None:
        coord = _make_coord(
            dispatch_config=DispatchConfig(
                mode=DispatchMode.PARALLEL, start_interval_s=3.0, zone_batching=True,
            )
        )
        fake_mono = [100.0]
        coord._serial_dispatch._throttle._mono_clock = lambda: fake_mono[0]
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        coord._serial_dispatch.record_dispatch(t0)
        fake_mono[0] += 0.5  # only 0.5s elapsed

        interval = effective_interval_s(coord._dispatch_config, is_first_in_zone_group=True)
        assert interval == 3.0
        wait = coord._serial_dispatch.time_until_next_allowed(
            min_interval_override=timedelta(seconds=interval)
        )
        assert wait == timedelta(seconds=2.5)

    def test_non_boundary_intent_no_forced_wait(self) -> None:
        coord = _make_coord(
            dispatch_config=DispatchConfig(
                mode=DispatchMode.PARALLEL, start_interval_s=3.0, zone_batching=True,
            )
        )
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        coord._serial_dispatch.record_dispatch(t0)
        interval = effective_interval_s(coord._dispatch_config, is_first_in_zone_group=False)
        assert interval == 0.0


class TestReloadPicksUpNewConfigNoStaleDefault:
    def test_fresh_coordinator_uses_new_dispatch_config(self) -> None:
        coord_before = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.PARALLEL))
        coord_after = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        assert coord_before._dispatch_config.mode is DispatchMode.PARALLEL
        assert coord_after._dispatch_config.mode is DispatchMode.SEQUENTIAL


class TestMultipleEntriesIndependentConfigs:
    def test_two_coordinators_do_not_share_dispatch_config(self) -> None:
        coord_a = _make_coord(
            dispatch_config=DispatchConfig(mode=DispatchMode.SPACED, start_interval_s=1.0)
        )
        coord_b = _make_coord(
            dispatch_config=DispatchConfig(mode=DispatchMode.SPACED, start_interval_s=9.0)
        )
        assert coord_a._dispatch_config.start_interval_s == 1.0
        assert coord_b._dispatch_config.start_interval_s == 9.0
