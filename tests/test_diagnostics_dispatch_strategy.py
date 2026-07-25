"""dispatch_strategy_summary coverage for T11 (v1.2.0-beta.1) —
engines/diagnostics_builder.py.

Uses the same real-Coordinator-construction technique established in
test_override_policy_e2e_wiring.py / test_diagnostics_manual_override_release_strategy.py.

Coverage:
  DGD-01  Default summary reports mode="spaced" at the backward-compatible
          2.0s interval, empty completion counts.
  DGD-02  A configured non-default mode/zone_batching surfaces correctly.
  DGD-03  completion_method_counts aggregates recorded support-critical
          events by completion method, with zero window-identifying detail.
  DGD-04  completion_timeout_count reflects timed-out completions.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
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
from custom_components.smartshading.engines.diagnostics_builder import build_consolidated_diagnostics  # noqa: E402
from custom_components.smartshading.models.dispatch_config import (  # noqa: E402
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


class TestDefaultSummary:
    def test_default_mode_and_interval(self) -> None:
        coord = _make_coord()
        summary = build_consolidated_diagnostics(coord)["dispatch_strategy_summary"]
        assert summary["mode"] == "spaced"
        assert summary["start_interval_s"] == 2.0
        assert summary["zone_batching"] is False
        assert summary["completion_method_counts"] == {}
        assert summary["completion_timeout_count"] == 0


class TestConfiguredStrategySurfaces:
    def test_sequential_zone_batching_surface(self) -> None:
        coord = _make_coord(
            dispatch_config=DispatchConfig(
                mode=DispatchMode.SEQUENTIAL, max_travel_wait_s=55.0, zone_batching=True,
            )
        )
        summary = build_consolidated_diagnostics(coord)["dispatch_strategy_summary"]
        assert summary["mode"] == "sequential"
        assert summary["max_travel_wait_s"] == 55.0
        assert summary["zone_batching"] is True


class TestCompletionAggregation:
    def test_completion_method_counts_and_timeouts(self) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        coord._support_critical_events = [
            {"completion_method": "position", "completion_timed_out": False},
            {"completion_method": "position", "completion_timed_out": False},
            {"completion_method": "timeout", "completion_timed_out": True},
            {"completion_method": None, "completion_timed_out": False},  # PARALLEL/SPACED event
        ]
        summary = build_consolidated_diagnostics(coord)["dispatch_strategy_summary"]
        assert summary["completion_method_counts"] == {"position": 2, "timeout": 1}
        assert summary["completion_timeout_count"] == 1
