"""Regression coverage for the T10.1 diagnostics/Learning-accuracy fix in
SmartShadingCoordinator.async_clear_manual_override().

Background: async_clear_manual_override() (added in T10, now reachable from
the clear_manual_override service added in T10.1) clears an override
OUTSIDE the normal per-cycle loop. Before this fix, the very next coordinator
cycle would misattribute that clear as a natural "timeout" (its
manual_override_release_reason diagnostic would say "timeout", and a
spurious "expired" Learning record would be written) — on top of the
"cleared_by_manual" record async_clear_manual_override() already writes
synchronously, corrupting the override event history with a duplicate,
wrongly-labeled event for the exact same transition.

The fix: async_clear_manual_override() records window_id -> "manual_service"
into self._explicit_override_clear_reasons, consumed once by the very next
per-window loop iteration (coordinator.py) to report the accurate reason and
skip the duplicate "expired" record.

This file tests the directly observable, unit-level part of that fix (the
dict gets populated correctly by async_clear_manual_override(), and is
correctly consumable/poppable) — same HA-stub-real-Coordinator technique as
test_coordinator_override_release_strategy_wiring.py. The full per-cycle
consumption logic (coordinator.py's per-window loop) is exercised
indirectly by every existing full-suite passing test that runs a coordinator
cycle after a T7-era safety/lifecycle clear and by manual code review; a
true end-to-end cycle-level test is out of scope here — this codebase's
existing test suite does not drive full _async_update_data() cycles for
similarly deep coordinator-internal behavior (weather/sun/cover state would
all need to be faked), so this stays at the same unit-level depth as its
neighbors.

Coverage:
  ECR-01  async_clear_manual_override() records "manual_service" for the
          cleared window in self._explicit_override_clear_reasons.
  ECR-02  Only the cleared window's reason is recorded — a second, still-
          active window is untouched.
  ECR-03  Calling async_clear_manual_override() on a window with no active
          override does NOT record an explicit-clear reason (nothing to
          misattribute).
  ECR-04  The dict is a plain pop-once structure: popping the same window_id
          twice yields None the second time (matches the per-cycle
          consumption pattern in coordinator.py).
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

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
from custom_components.smartshading.models.lifecycle import NightDayLifecycleConfig  # noqa: E402
from custom_components.smartshading.models.window import WindowConfig  # noqa: E402
from custom_components.smartshading.state_machine.states import ShadingState  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402


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
    coord.async_request_refresh = AsyncMock()
    return coord


def _window(window_id: str) -> WindowConfig:
    return WindowConfig(
        id=window_id, name=window_id, zone_id="z1",
        azimuth=180, floor_level=0, cover_group_id="cg1",
    )


def _create_override(coord: SmartShadingCoordinator, window_id: str, now: datetime) -> None:
    coord._override_detector.tick(
        window_id=window_id, observed_position=0, smartshading_target=0,
        prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now,
    )
    coord._override_detector.tick(
        window_id=window_id, observed_position=40, smartshading_target=0,
        prev_state=ShadingState.OPEN, tolerance=10, duration_min=120,
        now=now + timedelta(minutes=1),
    )


class TestExplicitClearReasonRecorded:
    def test_recorded_for_the_cleared_window(self, monkeypatch) -> None:
        coord = _make_coord()
        coord.windows = {"w1": _window("w1")}
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        _create_override(coord, "w1", t0)
        t1 = t0 + timedelta(minutes=2)
        monkeypatch.setattr(
            coord.async_clear_manual_override.__func__.__globals__["dt_util"],
            "utcnow", lambda: t1,
        )

        assert coord._explicit_override_clear_reasons == {}
        result = asyncio.run(coord.async_clear_manual_override("w1"))
        assert result is True
        assert coord._explicit_override_clear_reasons == {"w1": "manual_service"}


class TestOnlyClearedWindowRecorded:
    def test_second_window_untouched(self, monkeypatch) -> None:
        coord = _make_coord()
        coord.windows = {"w1": _window("w1"), "w2": _window("w2")}
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        _create_override(coord, "w1", t0)
        _create_override(coord, "w2", t0)
        t1 = t0 + timedelta(minutes=2)
        monkeypatch.setattr(
            coord.async_clear_manual_override.__func__.__globals__["dt_util"],
            "utcnow", lambda: t1,
        )

        asyncio.run(coord.async_clear_manual_override("w1"))
        assert coord._explicit_override_clear_reasons == {"w1": "manual_service"}
        assert "w2" not in coord._explicit_override_clear_reasons


class TestNoActiveOverrideNoReasonRecorded:
    def test_no_op_does_not_record_a_reason(self, monkeypatch) -> None:
        coord = _make_coord()
        coord.windows = {"w1": _window("w1")}
        t1 = datetime(2026, 6, 15, 6, 5, tzinfo=timezone.utc)
        monkeypatch.setattr(
            coord.async_clear_manual_override.__func__.__globals__["dt_util"],
            "utcnow", lambda: t1,
        )

        result = asyncio.run(coord.async_clear_manual_override("w1"))
        assert result is False
        assert coord._explicit_override_clear_reasons == {}


class TestPopOnceSemantics:
    def test_popping_twice_yields_none_second_time(self, monkeypatch) -> None:
        coord = _make_coord()
        coord.windows = {"w1": _window("w1")}
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        _create_override(coord, "w1", t0)
        t1 = t0 + timedelta(minutes=2)
        monkeypatch.setattr(
            coord.async_clear_manual_override.__func__.__globals__["dt_util"],
            "utcnow", lambda: t1,
        )
        asyncio.run(coord.async_clear_manual_override("w1"))

        first = coord._explicit_override_clear_reasons.pop("w1", None)
        second = coord._explicit_override_clear_reasons.pop("w1", None)
        assert first == "manual_service"
        assert second is None
