"""Tests for coordinator.py's pure dispatch-ordering helpers
(_dispatch_order_key / _build_zone_dispatch_order — F18, unaffected by T11
but now load-bearing for DispatchConfig.zone_batching's "first in zone
group" detection, so explicit direct coverage is added here).

Coverage:
  ORD-01  Safety always sorts first, regardless of zone/window order.
  ORD-02  Non-safety intents sort by stable zone order, then window-within-
          zone order — "all of zone A, then all of zone B."
  ORD-03  Zone order matches zones dict insertion order (config_flow order),
          not alphabetical or any other reordering.
  ORD-04  Window order within a zone matches windows dict insertion order,
          scoped per zone (each zone's window index starts at 0).
  ORD-05  A window/zone not present in the order maps falls back to a
          stable "sorts last" position rather than raising.
  ORD-06  Full sort() over a mixed list of safety + non-safety items
          reproduces "all safety first, then zone-grouped."
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# HA stubs — identical technique to test_override_policy_e2e_wiring.py.
# Only _dispatch_order_key/_build_zone_dispatch_order (pure, module-level
# functions) are used here, but importing coordinator.py at all requires
# these to be present.
# ---------------------------------------------------------------------------


def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


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

sys.modules.setdefault("homeassistant.util.dt", _stub(
    "homeassistant.util.dt",
    utcnow=lambda: datetime.now(timezone.utc),
    now=lambda: datetime.now(timezone.utc),
    as_utc=lambda dt: dt.astimezone(timezone.utc),
    as_local=lambda dt: dt,
    DEFAULT_TIME_ZONE=timezone.utc,
))

sys.modules.setdefault("homeassistant.helpers.update_coordinator", _stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=type("DUC", (), {
        "__class_getitem__": classmethod(lambda cls, x: cls),
        "__init__": lambda self, hass, logger, *, config_entry=None, name=None, update_interval=None: None,
    }),
    CoordinatorEntity=type("CE", (), {"__class_getitem__": classmethod(lambda cls, x: cls), "__init__": lambda self, c: None}),
))
sys.modules.setdefault("homeassistant.helpers.storage", _stub(
    "homeassistant.helpers.storage",
    Store=type("Store", (object,), {
        "__init__": lambda self, *a, **k: None,
        "async_load": lambda self: None,
        "async_save": lambda self, data: None,
    }),
))

from custom_components.smartshading.coordinator import (  # noqa: E402
    _build_zone_dispatch_order,
    _dispatch_order_key,
)
from custom_components.smartshading.models.window import WindowConfig  # noqa: E402


def _window(window_id: str, zone_id: str) -> WindowConfig:
    return WindowConfig(
        id=window_id, name=window_id, zone_id=zone_id,
        azimuth=180, floor_level=0, cover_group_id="cg1",
    )


class TestSafetyAlwaysFirst:
    def test_safety_key_is_always_zero_tuple(self) -> None:
        key = _dispatch_order_key(
            is_safety=True, zone_id="z9", window_id="w9",
            zone_order={}, window_order_in_zone={},
        )
        assert key == (0, 0, 0)

    def test_safety_sorts_before_any_non_safety(self) -> None:
        zone_order = {"z1": 0, "z2": 1}
        window_order = {"w1": 0, "w2": 0}
        safety_key = _dispatch_order_key(
            is_safety=True, zone_id="z2", window_id="w2",
            zone_order=zone_order, window_order_in_zone=window_order,
        )
        non_safety_key = _dispatch_order_key(
            is_safety=False, zone_id="z1", window_id="w1",
            zone_order=zone_order, window_order_in_zone=window_order,
        )
        assert safety_key < non_safety_key


class TestZoneGrouping:
    def test_non_safety_groups_by_zone_then_window(self) -> None:
        zones = {"z1": object(), "z2": object()}
        windows = {"w1": _window("w1", "z1"), "w2": _window("w2", "z1"), "w3": _window("w3", "z2")}
        zone_order, window_order = _build_zone_dispatch_order(zones, windows)

        key_w1 = _dispatch_order_key(
            is_safety=False, zone_id="z1", window_id="w1",
            zone_order=zone_order, window_order_in_zone=window_order,
        )
        key_w2 = _dispatch_order_key(
            is_safety=False, zone_id="z1", window_id="w2",
            zone_order=zone_order, window_order_in_zone=window_order,
        )
        key_w3 = _dispatch_order_key(
            is_safety=False, zone_id="z2", window_id="w3",
            zone_order=zone_order, window_order_in_zone=window_order,
        )
        assert key_w1 < key_w2 < key_w3

    def test_zone_order_matches_dict_insertion_order(self) -> None:
        zones = {"south": object(), "north": object(), "east": object()}
        windows = {}
        zone_order, _ = _build_zone_dispatch_order(zones, windows)
        assert zone_order == {"south": 0, "north": 1, "east": 2}

    def test_window_order_scoped_per_zone(self) -> None:
        zones = {"z1": object(), "z2": object()}
        windows = {
            "a": _window("a", "z1"), "b": _window("b", "z2"),
            "c": _window("c", "z1"), "d": _window("d", "z2"),
        }
        _, window_order = _build_zone_dispatch_order(zones, windows)
        # Each zone's own window index starts at 0 independently.
        assert window_order["a"] == 0
        assert window_order["c"] == 1
        assert window_order["b"] == 0
        assert window_order["d"] == 1


class TestMissingFromOrderMaps:
    def test_unknown_zone_sorts_after_known_zones(self) -> None:
        zone_order = {"z1": 0, "z2": 1}
        key_known = _dispatch_order_key(
            is_safety=False, zone_id="z2", window_id="w1",
            zone_order=zone_order, window_order_in_zone={},
        )
        key_unknown = _dispatch_order_key(
            is_safety=False, zone_id="z_removed", window_id="w1",
            zone_order=zone_order, window_order_in_zone={},
        )
        assert key_known < key_unknown

    def test_unknown_window_never_raises(self) -> None:
        key = _dispatch_order_key(
            is_safety=False, zone_id="z1", window_id="w_removed",
            zone_order={"z1": 0}, window_order_in_zone={},
        )
        assert key == (1, 0, 0)


class TestFullSortReproducesExpectedOrder:
    def test_mixed_list_sorts_safety_first_then_zone_grouped(self) -> None:
        zone_order = {"z1": 0, "z2": 1}
        window_order = {"w1": 0, "w2": 0, "w3": 0}
        items = [
            ("w3", {"is_safety": False, "zone_id": "z2"}),
            ("w1", {"is_safety": False, "zone_id": "z1"}),
            ("w2", {"is_safety": True, "zone_id": "z1"}),
        ]
        ordered = sorted(
            items,
            key=lambda item: _dispatch_order_key(
                is_safety=item[1]["is_safety"], zone_id=item[1]["zone_id"],
                window_id=item[0], zone_order=zone_order,
                window_order_in_zone=window_order,
            ),
        )
        assert [wid for wid, _ in ordered] == ["w2", "w1", "w3"]
