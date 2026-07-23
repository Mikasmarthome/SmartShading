"""manual_override_summary coverage for the T10 release-strategy fields
(v1.2.0-beta.1) — engines/diagnostics_builder.py.

Uses the same real-Coordinator-construction technique established in
test_override_policy_e2e_wiring.py (a minimal HA stub set lets coordinator.py
actually import and construct), so this exercises the real
build_consolidated_diagnostics() output.

Coverage:
  DGS-01  Default summary reports release_strategy="lifecycle" and
          safety_timeout_enabled=True (T7-legacy-equivalent defaults), no
          duration_mode/break_on_lifecycle keys remain.
  DGS-02  A configured non-default release_strategy/safety_timeout_enabled
          surfaces correctly.
  DGS-03  active_override_strategy_counts aggregates active overrides by
          strategy, still with zero per-window identifying information.
  DGS-04  Count dict is empty when no overrides are active.
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
from custom_components.smartshading.engines.diagnostics_builder import build_consolidated_diagnostics  # noqa: E402
from custom_components.smartshading.models.lifecycle import NightDayLifecycleConfig  # noqa: E402
from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy  # noqa: E402
from custom_components.smartshading.state_machine.states import ShadingState  # noqa: E402


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
        hass, entry,
        lifecycle_config=NightDayLifecycleConfig(id="default"),
        **kwargs,
    )
    coord.windows = {}
    coord.zones = {}
    coord.cover_groups = {}
    return coord


class TestDefaultSummaryReflectsLifecycleDefault:
    def test_default_summary(self) -> None:
        coord = _make_coord()
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        assert summary["release_strategy"] == "lifecycle"
        assert summary["safety_timeout_enabled"] is True
        assert "duration_mode" not in summary
        assert "break_on_lifecycle" not in summary
        assert summary["active_override_strategy_counts"] == {}


class TestConfiguredStrategySurfaces:
    def test_configured_strategy_and_safety_timeout(self) -> None:
        coord = _make_coord(
            override_release_strategy=OverrideReleaseStrategy.FIRST_ANY_DECISION,
            override_safety_timeout_enabled=False,
        )
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        assert summary["release_strategy"] == "first_any_decision"
        assert summary["safety_timeout_enabled"] is False


class TestActiveOverrideStrategyCounts:
    def test_counts_reflect_detector_state(self) -> None:
        coord = _make_coord(override_release_strategy=OverrideReleaseStrategy.FIRST_PROTECTION)
        now = datetime.now(timezone.utc)
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now,
        )  # warmup
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120,
            now=now + timedelta(minutes=1),
            release_strategy=OverrideReleaseStrategy.FIRST_PROTECTION,
        )
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        assert summary["active_override_count"] == 1
        assert summary["active_override_strategy_counts"] == {"first_protection": 1}

    def test_empty_when_no_overrides_active(self) -> None:
        coord = _make_coord()
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        assert summary["active_override_count"] == 0
        assert summary["active_override_strategy_counts"] == {}
