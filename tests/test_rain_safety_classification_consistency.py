"""Classification consistency for RAIN_SAFE — T8 review items 25-27.

Proves `_classify_movement_cause` recognizes RAIN_SAFE as MOVE_CAUSE_SAFETY,
and that SAFETY_SHADING_STATES (the shared constant introduced by T8) is
used consistently — no remaining hardcoded two-state safety tuple in the
classification/eligibility call sites T8 was asked to fix.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# HA stubs — identical technique to test_manual_override_diagnostics.py.
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
from custom_components.smartshading.coordinator import SmartShadingCoordinator, MOVE_CAUSE_SAFETY  # noqa: E402
from custom_components.smartshading.state_machine.states import SAFETY_SHADING_STATES, ShadingState  # noqa: E402


class TestClassifyMovementCauseRecognizesRain:
    def test_rain_safe_classified_as_safety(self) -> None:
        assert SmartShadingCoordinator._classify_movement_cause(ShadingState.RAIN_SAFE) == MOVE_CAUSE_SAFETY

    def test_storm_and_wind_still_classified_as_safety(self) -> None:
        assert SmartShadingCoordinator._classify_movement_cause(ShadingState.STORM_SAFE) == MOVE_CAUSE_SAFETY
        assert SmartShadingCoordinator._classify_movement_cause(ShadingState.WIND_SAFE) == MOVE_CAUSE_SAFETY

    def test_non_safety_states_unaffected(self) -> None:
        from custom_components.smartshading.coordinator import MOVE_CAUSE_COMFORT, MOVE_CAUSE_LIFECYCLE, MOVE_CAUSE_ABSENCE, MOVE_CAUSE_MANUAL
        assert SmartShadingCoordinator._classify_movement_cause(ShadingState.NIGHT_CLOSED) == MOVE_CAUSE_LIFECYCLE
        assert SmartShadingCoordinator._classify_movement_cause(ShadingState.ABSENCE_CLOSED) == MOVE_CAUSE_ABSENCE
        assert SmartShadingCoordinator._classify_movement_cause(ShadingState.MANUAL_OVERRIDE) == MOVE_CAUSE_MANUAL
        assert SmartShadingCoordinator._classify_movement_cause(ShadingState.OPEN) == MOVE_CAUSE_COMFORT


class TestSafetyShadingStatesConstantExported:
    def test_all_three_present(self) -> None:
        assert ShadingState.STORM_SAFE in SAFETY_SHADING_STATES
        assert ShadingState.WIND_SAFE in SAFETY_SHADING_STATES
        assert ShadingState.RAIN_SAFE in SAFETY_SHADING_STATES
        assert len(SAFETY_SHADING_STATES) == 3

    def test_non_safety_states_absent(self) -> None:
        for state in ShadingState:
            if state in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE, ShadingState.RAIN_SAFE):
                continue
            assert state not in SAFETY_SHADING_STATES
