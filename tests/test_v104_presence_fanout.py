"""v1.0.4 — Presence fan-out, absence-release semantics, dispatch spacing.

Covers:
  TC-PR1–PR10  Presence listener registration and callback logic
  TC-AR1–AR7   Absence-release semantics per window behavior mode
  TC-DS1–DS8   Dispatch spacing 1.0 s

Root cause confirmed by TC-PR1:
  Before v1.0.4 the coordinator polled presence once per 5-minute cycle.
  Zone entries had independent cycle phases, causing 1–5 minute variance
  when multiple zones used the same presence sensor.

Fix:  async_setup_presence_listeners() registers async_track_state_change_event
      for each presence entity.  Any state change triggers async_request_refresh()
      immediately — within the same HA event-loop tick — for every affected zone.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# HA stubs — must precede any coordinator import in this module
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _CoordBase:
    """Minimal DataUpdateCoordinator stub for presence tests."""

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.config_entry = config_entry
        self._refresh_task_created = 0

    def async_request_refresh(self) -> None:
        self._refresh_task_created += 1


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
import custom_components.smartshading.coordinator as _coordinator_module  # noqa: E402
# Keep a direct reference to the module so patches target the correct namespace
# regardless of later sys.modules changes by other test files.
_coord_module_ref = _coordinator_module


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


def _make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}
    entry.data = {}
    entry.async_on_unload = MagicMock()
    return entry


def _make_coord(presence_entity_ids: list[str] | None = None) -> SmartShadingCoordinator:
    hass = _make_hass()
    entry = _make_entry()
    return SmartShadingCoordinator(
        hass, entry,
        presence_entity_ids=presence_entity_ids or [],
    )


def _mock_event(old_state_str: str | None, new_state_str: str | None) -> MagicMock:
    event = MagicMock()
    old = MagicMock() if old_state_str is not None else None
    new = MagicMock() if new_state_str is not None else None
    if old is not None:
        old.state = old_state_str
    if new is not None:
        new.state = new_state_str
    event.data = {"old_state": old, "new_state": new}
    return event


# ---------------------------------------------------------------------------
# TC-PR: Presence listener registration and callback logic
# ---------------------------------------------------------------------------

class TestPresenceListenerRegistration:
    """TC-PR1–PR5: listener is registered correctly per entity."""

    def test_tc_pr1_registers_one_listener_per_entity(self):
        """TC-PR1: two presence entities → two async_track_state_change_event calls.
        Root cause of zone delay: without this, only polling (every 5 min) fired.
        """
        coord = _make_coord(presence_entity_ids=["person.alice", "person.bob"])
        entry = _make_entry()
        unsub1 = MagicMock()
        unsub2 = MagicMock()

        with patch.object(
            _coord_module_ref, "async_track_state_change_event",
            side_effect=[unsub1, unsub2],
        ) as mock_track:
            coord.async_setup_presence_listeners(entry)

        assert mock_track.call_count == 2
        called_with_entities = {c.args[1] for c in mock_track.call_args_list}
        assert "person.alice" in called_with_entities
        assert "person.bob" in called_with_entities

    def test_tc_pr2_no_listener_when_no_presence_entities(self):
        """TC-PR2: no presence entities configured → no listener registered."""
        coord = _make_coord(presence_entity_ids=[])
        entry = _make_entry()

        with patch.object(
            _coord_module_ref, "async_track_state_change_event"
        ) as mock_track:
            coord.async_setup_presence_listeners(entry)

        mock_track.assert_not_called()

    def test_tc_pr3_unsub_stored_for_teardown(self):
        """TC-PR3: unsub callables are stored so teardown can cancel them."""
        coord = _make_coord(presence_entity_ids=["person.a"])
        entry = _make_entry()
        unsub = MagicMock()

        with patch.object(
            _coord_module_ref, "async_track_state_change_event",
            return_value=unsub,
        ):
            coord.async_setup_presence_listeners(entry)

        assert len(coord._unsub_presence_listeners) == 1

    def test_tc_pr4_registers_async_on_unload(self):
        """TC-PR4: entry.async_on_unload registered for automatic cleanup."""
        coord = _make_coord(presence_entity_ids=["person.a"])
        entry = _make_entry()

        with patch.object(
            _coord_module_ref, "async_track_state_change_event",
            return_value=MagicMock(),
        ):
            coord.async_setup_presence_listeners(entry)

        entry.async_on_unload.assert_called_once_with(coord.async_teardown_presence_listeners)

    def test_tc_pr5_no_async_on_unload_when_no_entities(self):
        """TC-PR5: no entities → no async_on_unload call."""
        coord = _make_coord(presence_entity_ids=[])
        entry = _make_entry()

        with patch.object(
            _coord_module_ref, "async_track_state_change_event",
        ):
            coord.async_setup_presence_listeners(entry)

        entry.async_on_unload.assert_not_called()


class TestPresenceCallbackBehavior:
    """TC-PR6–PR10: callback logic — what triggers refresh and what doesn't."""

    def _setup_and_capture_callback(self, coord, entry):
        captured_callbacks = []

        def _track(hass, entity_id, action):
            captured_callbacks.append(action)
            return MagicMock()

        with patch.object(
            _coord_module_ref, "async_track_state_change_event",
            side_effect=_track,
        ):
            coord.async_setup_presence_listeners(entry)

        return captured_callbacks

    def test_tc_pr6_away_to_home_triggers_refresh(self):
        """TC-PR6: away→home state change triggers async_create_task(async_request_refresh)."""
        coord = _make_coord(presence_entity_ids=["person.alice"])
        entry = _make_entry()
        callbacks = self._setup_and_capture_callback(coord, entry)
        assert callbacks  # listener was registered

        event = _mock_event(old_state_str="not_home", new_state_str="home")
        callbacks[0](event)

        coord.hass.async_create_task.assert_called_once()

    def test_tc_pr7_home_to_away_triggers_refresh(self):
        """TC-PR7: home→not_home also triggers refresh (departure is also a presence change)."""
        coord = _make_coord(presence_entity_ids=["person.alice"])
        entry = _make_entry()
        callbacks = self._setup_and_capture_callback(coord, entry)

        event = _mock_event(old_state_str="home", new_state_str="not_home")
        callbacks[0](event)

        coord.hass.async_create_task.assert_called_once()

    def test_tc_pr8_same_state_deduplicated_no_refresh(self):
        """TC-PR8: same old/new state → deduplication → no refresh."""
        coord = _make_coord(presence_entity_ids=["person.alice"])
        entry = _make_entry()
        callbacks = self._setup_and_capture_callback(coord, entry)

        event = _mock_event(old_state_str="home", new_state_str="home")
        callbacks[0](event)

        coord.hass.async_create_task.assert_not_called()

    def test_tc_pr9_none_new_state_no_refresh(self):
        """TC-PR9: entity removed (new_state=None) → no refresh."""
        coord = _make_coord(presence_entity_ids=["person.alice"])
        entry = _make_entry()
        callbacks = self._setup_and_capture_callback(coord, entry)

        event = _mock_event(old_state_str="home", new_state_str=None)
        callbacks[0](event)

        coord.hass.async_create_task.assert_not_called()

    def test_tc_pr10_none_old_state_no_refresh(self):
        """TC-PR10: entity added (old_state=None) → no refresh."""
        coord = _make_coord(presence_entity_ids=["person.alice"])
        entry = _make_entry()
        callbacks = self._setup_and_capture_callback(coord, entry)

        event = _mock_event(old_state_str=None, new_state_str="home")
        callbacks[0](event)

        coord.hass.async_create_task.assert_not_called()


class TestPresenceTeardown:
    """Teardown: listeners cancelled, idempotent, clean after reload."""

    def test_teardown_calls_all_unsub_callbacks(self):
        """All registered unsub callables are called on teardown."""
        coord = _make_coord(presence_entity_ids=["person.a", "person.b"])
        entry = _make_entry()
        unsub1 = MagicMock()
        unsub2 = MagicMock()

        with patch.object(
            _coord_module_ref, "async_track_state_change_event",
            side_effect=[unsub1, unsub2],
        ):
            coord.async_setup_presence_listeners(entry)

        coord.async_teardown_presence_listeners()

        unsub1.assert_called_once()
        unsub2.assert_called_once()

    def test_teardown_clears_list(self):
        """After teardown, the unsub list is empty."""
        coord = _make_coord(presence_entity_ids=["person.a"])
        entry = _make_entry()

        with patch.object(
            _coord_module_ref, "async_track_state_change_event",
            return_value=MagicMock(),
        ):
            coord.async_setup_presence_listeners(entry)

        coord.async_teardown_presence_listeners()
        assert coord._unsub_presence_listeners == []

    def test_teardown_idempotent(self):
        """Calling teardown twice does not raise and does not double-cancel."""
        coord = _make_coord(presence_entity_ids=["person.a"])
        entry = _make_entry()
        unsub = MagicMock()

        with patch.object(
            _coord_module_ref, "async_track_state_change_event",
            return_value=unsub,
        ):
            coord.async_setup_presence_listeners(entry)

        coord.async_teardown_presence_listeners()
        coord.async_teardown_presence_listeners()  # second call — must not raise

        assert unsub.call_count == 1  # only cancelled once


# ---------------------------------------------------------------------------
# TC-AR: Absence-release semantics per window behavior mode
# ---------------------------------------------------------------------------

from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.models.behavior_config import BehaviorConfig
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.window import WindowBehaviorMode, WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import ShadingState


_ORCHESTRATOR = TierOrchestrator()
_LC = NightDayLifecycleConfig(id="default")
_NO_COMFORT = ComfortConfig(
    heat_protection_enabled=False,
    glare_protection_enabled=False,
    solar_gain_enabled=False,
)


def _wdi_absence(
    *,
    behavior_mode: WindowBehaviorMode = WindowBehaviorMode.FULLY_AUTOMATIC,
    absence_active: bool = False,
    current_state: ShadingState = ShadingState.ABSENCE_CLOSED,
):
    window = WindowConfig(
        id="w1", name="W", zone_id="z1",
        azimuth=180.0, floor_level=0, cover_group_id="cg1",
        behavior_mode=behavior_mode,
    )
    zone = ZoneConfig(id="z1", name="Zone")
    return build_window_decision_input(
        window=window, zone=zone,
        global_defaults=GlobalDefaults(),
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=_LC,
        lifecycle_state=LifecycleState.DAY,
        absence_active=absence_active,
        current_shading_state=current_state,
        outdoor_temp_c=None,
        indoor_temp_c=None,
        exposure=None,
        is_in_solar_sector=False,
        comfort_config=_NO_COMFORT,
    )


class TestAbsenceReleaseSemantics:
    """TC-AR1–AR7: correct behavior per WindowBehaviorMode when returning home."""

    def test_tc_ar1_fully_automatic_returns_to_open(self):
        """TC-AR1: FULLY_AUTOMATIC, no protection active, absence=False → OPEN."""
        wdi = _wdi_absence(
            behavior_mode=WindowBehaviorMode.FULLY_AUTOMATIC,
            absence_active=False,
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        assert decision.shading_state is ShadingState.OPEN, (
            f"Expected OPEN, got {decision.shading_state}"
        )

    def test_tc_ar2_fully_automatic_keeps_absence_closed_while_absent(self):
        """TC-AR2: FULLY_AUTOMATIC, absence=True → ABSENCE_CLOSED (floor active)."""
        wdi = _wdi_absence(
            behavior_mode=WindowBehaviorMode.FULLY_AUTOMATIC,
            absence_active=True,
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        assert decision.shading_state is ShadingState.ABSENCE_CLOSED

    def test_tc_ar3_absence_and_schedule_returns_to_open_on_presence(self):
        """TC-AR3: ABSENCE_AND_SCHEDULE, absence=False, no protection → OPEN.
        No Solar/Heat/Glare autonomous daytime shading in this mode."""
        wdi = _wdi_absence(
            behavior_mode=WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            absence_active=False,
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        # Should open (no protection active, absence released)
        # Mode allows absence close/release but suppresses autonomous daytime shading
        assert decision.shading_state is ShadingState.OPEN, (
            f"Expected OPEN, got {decision.shading_state}"
        )

    def test_tc_ar4_absence_only_returns_to_open_on_presence(self):
        """TC-AR4: ABSENCE_ONLY, absence=False, no protection → OPEN."""
        wdi = _wdi_absence(
            behavior_mode=WindowBehaviorMode.ABSENCE_ONLY,
            absence_active=False,
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        assert decision.shading_state is ShadingState.OPEN

    def test_tc_ar5_absence_only_keeps_absence_closed_while_absent(self):
        """TC-AR5: ABSENCE_ONLY, absence=True → ABSENCE_CLOSED."""
        wdi = _wdi_absence(
            behavior_mode=WindowBehaviorMode.ABSENCE_ONLY,
            absence_active=True,
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        assert decision.shading_state is ShadingState.ABSENCE_CLOSED

    def test_tc_ar6_disabled_automatic_no_absence_movement(self):
        """TC-AR6: DISABLED_AUTOMATIC → coordinator sets absence_position=None.

        The coordinator overrides the WDI for DISABLED_AUTOMATIC windows by
        setting effective_behavior.absence_position=None.  AbsenceEvaluator
        returns None when absence_position=None → no ABSENCE_CLOSED, stays OPEN.

        Test simulates coordinator's mode-filtering via dataclasses.replace.
        """
        from dataclasses import replace as _replace
        wdi_base = _wdi_absence(
            behavior_mode=WindowBehaviorMode.DISABLED_AUTOMATIC,
            absence_active=True,
            current_state=ShadingState.OPEN,
        )
        # Simulate coordinator override for DISABLED_AUTOMATIC
        wdi = _replace(
            wdi_base,
            effective_behavior=_replace(wdi_base.effective_behavior, absence_position=None),
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        assert decision.shading_state is ShadingState.OPEN, (
            f"DISABLED_AUTOMATIC (absence_position=None) should not close for absence, "
            f"got {decision.shading_state}"
        )

    def test_tc_ar7_absence_release_only_from_absence_closed_state(self):
        """TC-AR7: FULLY_AUTOMATIC, returning home but previous state was OPEN
        (not ABSENCE_CLOSED) → fallback to OPEN without spurious state change."""
        wdi = _wdi_absence(
            behavior_mode=WindowBehaviorMode.FULLY_AUTOMATIC,
            absence_active=False,
            current_state=ShadingState.OPEN,  # was already open
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        assert decision.shading_state is ShadingState.OPEN


# ---------------------------------------------------------------------------
# TC-DS: Dispatch spacing 2.0 s
# ---------------------------------------------------------------------------

from custom_components.smartshading.cover_control.global_dispatch_throttle import (
    DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS,
    GlobalDispatchThrottle,
    GlobalSerialDispatch,
)


_UTC = timezone.utc
_T0 = datetime(2026, 6, 22, 10, 0, 0, tzinfo=_UTC)


class _FakeMono:
    def __init__(self, t: float = 0.0) -> None:
        self._t = t
    def __call__(self) -> float:
        return self._t
    def advance(self, s: float) -> None:
        self._t += s


class TestDispatchSpacing:
    """TC-DS1-DS8: 2.0 s canonical constant and throttle behavior.

    F32 field fix: raised from 1.5s to 2.0s after a same-second RF collision
    report (ESP Somfy / RTS bridge).
    """

    def test_tc_ds1_default_interval_is_2_0_seconds(self):
        """TC-DS1: canonical constant is 2.0 — single source of truth."""
        assert DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS == 2.0

    def test_tc_ds2_default_throttle_applies_2_0_interval(self):
        """TC-DS2: GlobalDispatchThrottle created without args uses 2.0 s."""
        t = GlobalDispatchThrottle()
        assert t.min_interval == timedelta(seconds=2.0)

    def test_tc_ds3_no_wait_on_first_dispatch(self):
        """TC-DS3: first dispatch never waits (throttle unarmed)."""
        t = GlobalDispatchThrottle()
        assert t.time_until_next_allowed() == timedelta(0)

    def test_tc_ds4_full_wait_after_dispatch_at_same_time(self):
        """TC-DS4: second dispatch immediately after first must wait 2.0 s."""
        mono = _FakeMono(0.0)
        t = GlobalDispatchThrottle(mono_clock=mono)
        t.record_dispatch(_T0)
        wait = t.time_until_next_allowed()  # mono still 0.0
        assert wait == timedelta(seconds=2.0)

    def test_tc_ds5_no_wait_after_2_0_seconds_elapsed(self):
        """TC-DS5: at T+2.0s the throttle allows immediately (no more wait)."""
        mono = _FakeMono(0.0)
        t = GlobalDispatchThrottle(mono_clock=mono)
        t.record_dispatch(_T0)
        mono.advance(2.0)
        wait = t.time_until_next_allowed()
        assert wait == timedelta(0)

    def test_tc_ds6_partial_wait_at_0_5_seconds(self):
        """TC-DS6: at T+0.5s, remaining wait is 1.5 s (2.0s interval - 0.5s elapsed)."""
        mono = _FakeMono(0.0)
        t = GlobalDispatchThrottle(mono_clock=mono)
        t.record_dispatch(_T0)
        mono.advance(0.5)
        wait = t.time_until_next_allowed()
        assert wait == timedelta(seconds=1.5)

    def test_tc_ds7_global_serial_dispatch_uses_2_0(self):
        """TC-DS7: GlobalSerialDispatch default also uses 2.0 s."""
        mono = _FakeMono(0.0)
        gsd = GlobalSerialDispatch(mono_clock=mono)
        gsd.record_dispatch(_T0)
        wait = gsd.time_until_next_allowed()  # mono still 0.0
        assert wait == timedelta(seconds=2.0)

    def test_tc_ds8_custom_interval_overrides_default(self):
        """TC-DS8: explicitly passing a custom interval is still respected."""
        mono = _FakeMono(0.0)
        t = GlobalDispatchThrottle(min_interval=timedelta(seconds=0.5), mono_clock=mono)
        t.record_dispatch(_T0)
        mono.advance(0.3)
        wait = t.time_until_next_allowed()
        assert wait == timedelta(milliseconds=200)


# ---------------------------------------------------------------------------
# v1.1.0-beta.7 — window-contact immediate-refresh listeners (Option A/B reactivity)
# ---------------------------------------------------------------------------

from custom_components.smartshading.models.window import WindowConfig  # noqa: E402


def _win(wid: str, contacts=None, legacy=None) -> WindowConfig:
    return WindowConfig(
        id=wid, name=wid, zone_id="z1", azimuth=180.0, floor_level=0,
        cover_group_id="cg1",
        contact_sensor_entity_ids=contacts,
        contact_sensor_entity_id=legacy,
    )


def _make_coord_contacts(windows):
    hass = _make_hass()
    entry = _make_entry()
    coord = SmartShadingCoordinator(hass, entry, windows={w.id: w for w in windows})
    return coord, entry


def _capture_contact_cb(coord, entry):
    captured = []

    def _track(hass, entity_id, action):
        captured.append(action)
        return MagicMock()

    with patch.object(_coord_module_ref, "async_track_state_change_event",
                      side_effect=_track):
        coord.async_setup_contact_listeners(entry)
    return captured


class TestContactListenerRegistration:
    def test_one_listener_per_unique_contact(self):
        coord, entry = _make_coord_contacts([
            _win("w1", contacts=["binary_sensor.a", "binary_sensor.b"]),
            _win("w2", contacts=["binary_sensor.b"]),  # duplicate b across windows
        ])
        with patch.object(_coord_module_ref, "async_track_state_change_event",
                          return_value=MagicMock()) as m:
            coord.async_setup_contact_listeners(entry)
        assert m.call_count == 2  # a, b (deduplicated)
        assert len(coord._unsub_contact_listeners) == 2

    def test_no_listener_without_contacts(self):
        coord, entry = _make_coord_contacts([_win("w1")])
        with patch.object(_coord_module_ref, "async_track_state_change_event") as m:
            coord.async_setup_contact_listeners(entry)
        assert m.call_count == 0
        entry.async_on_unload.assert_not_called()

    def test_legacy_single_contact_tracked(self):
        coord, entry = _make_coord_contacts([_win("w1", legacy="binary_sensor.legacy")])
        with patch.object(_coord_module_ref, "async_track_state_change_event",
                          return_value=MagicMock()) as m:
            coord.async_setup_contact_listeners(entry)
        assert m.call_count == 1

    def test_async_on_unload_registered(self):
        coord, entry = _make_coord_contacts([_win("w1", contacts=["binary_sensor.a"])])
        with patch.object(_coord_module_ref, "async_track_state_change_event",
                          return_value=MagicMock()):
            coord.async_setup_contact_listeners(entry)
        entry.async_on_unload.assert_called_once()

    def test_teardown_clears(self):
        coord, entry = _make_coord_contacts([_win("w1", contacts=["binary_sensor.a"])])
        with patch.object(_coord_module_ref, "async_track_state_change_event",
                          return_value=MagicMock()):
            coord.async_setup_contact_listeners(entry)
        coord.async_teardown_contact_listeners()
        assert coord._unsub_contact_listeners == []


class TestContactCallbackBehavior:
    def test_closed_to_open_triggers_refresh(self):
        coord, entry = _make_coord_contacts([_win("w1", contacts=["binary_sensor.a"])])
        cbs = _capture_contact_cb(coord, entry)
        assert cbs
        cbs[0](_mock_event(old_state_str="off", new_state_str="on"))
        coord.hass.async_create_task.assert_called_once()

    def test_open_to_closed_triggers_refresh(self):
        coord, entry = _make_coord_contacts([_win("w1", contacts=["binary_sensor.a"])])
        cbs = _capture_contact_cb(coord, entry)
        cbs[0](_mock_event(old_state_str="on", new_state_str="off"))
        coord.hass.async_create_task.assert_called_once()

    def test_unavailable_to_closed_triggers_refresh(self):
        # Sensor recovering after restart → re-evaluate (may now reach Phase 2).
        coord, entry = _make_coord_contacts([_win("w1", contacts=["binary_sensor.a"])])
        cbs = _capture_contact_cb(coord, entry)
        cbs[0](_mock_event(old_state_str="unavailable", new_state_str="off"))
        coord.hass.async_create_task.assert_called_once()

    def test_same_state_deduplicated(self):
        coord, entry = _make_coord_contacts([_win("w1", contacts=["binary_sensor.a"])])
        cbs = _capture_contact_cb(coord, entry)
        cbs[0](_mock_event(old_state_str="on", new_state_str="on"))
        coord.hass.async_create_task.assert_not_called()

    def test_none_states_no_refresh(self):
        coord, entry = _make_coord_contacts([_win("w1", contacts=["binary_sensor.a"])])
        cbs = _capture_contact_cb(coord, entry)
        cbs[0](_mock_event(old_state_str="off", new_state_str=None))
        cbs[0](_mock_event(old_state_str=None, new_state_str="on"))
        coord.hass.async_create_task.assert_not_called()

    def test_multi_contact_one_open_triggers_refresh(self):
        coord, entry = _make_coord_contacts([
            _win("w1", contacts=["binary_sensor.a", "binary_sensor.b"])])
        cbs = _capture_contact_cb(coord, entry)
        assert len(cbs) == 2
        cbs[1](_mock_event(old_state_str="off", new_state_str="on"))
        coord.hass.async_create_task.assert_called_once()


# ---------------------------------------------------------------------------
# v1.1.0-beta.7 — lifecycle schedule-boundary immediate refresh
# ---------------------------------------------------------------------------

from datetime import time as _time, timedelta as _td  # noqa: E402
from custom_components.smartshading.models.lifecycle import (  # noqa: E402
    MorningTrigger,
    NightDayLifecycleConfig,
    NightTrigger,
)

_TZ = _datetime.timezone.utc
_hass_event = sys.modules["homeassistant.helpers.event"]

# The boundary scheduler calls dt_util.now()/as_utc(); complete whichever util.dt
# stub the coordinator bound (a sibling may have registered one with only utcnow),
# so these tests are robust to collection order.
_dt_mod = sys.modules["homeassistant.util.dt"]
if not hasattr(_dt_mod, "now"):
    _dt_mod.now = lambda: _datetime.datetime.now(_TZ)
if not hasattr(_dt_mod, "as_utc"):
    _dt_mod.as_utc = lambda dt: dt.astimezone(_TZ)


def _lc(**kw) -> NightDayLifecycleConfig:
    base = dict(
        id="default", night_enabled=True, morning_enabled=True,
        night_trigger=NightTrigger.FIXED_TIME, morning_trigger=MorningTrigger.FIXED_TIME,
        night_fixed_time=_time(21, 0), morning_fixed_time=_time(6, 35),
    )
    base.update(kw)
    return NightDayLifecycleConfig(**base)


def _make_coord_lc(lc):
    coord = SmartShadingCoordinator(_make_hass(), _make_entry(), lifecycle_config=lc)
    return coord, _make_entry()


def _local(y, mo, d, h, mi):
    return _datetime.datetime(y, mo, d, h, mi, tzinfo=_TZ)


class TestLifecycleBoundaryComputation:
    def test_morning_is_next_before_morning(self):
        coord, _ = _make_coord_lc(_lc())
        when, reason = coord._next_lifecycle_boundary(_local(2026, 6, 30, 5, 0))
        assert reason == "morning_release"
        assert (when.hour, when.minute) == (6, 35)
        assert when.date() == _local(2026, 6, 30, 0, 0).date()

    def test_night_is_next_between(self):
        coord, _ = _make_coord_lc(_lc())
        when, reason = coord._next_lifecycle_boundary(_local(2026, 6, 30, 20, 0))
        assert reason == "night_start" and (when.hour, when.minute) == (21, 0)

    def test_tomorrow_morning_after_night(self):
        coord, _ = _make_coord_lc(_lc())
        when, reason = coord._next_lifecycle_boundary(_local(2026, 6, 30, 22, 0))
        assert reason == "morning_release"
        assert when.date() == (_local(2026, 6, 30, 0, 0) + _td(days=1)).date()

    def test_sun_elevation_only_has_no_fixed_boundary(self):
        coord, _ = _make_coord_lc(_lc(night_trigger=NightTrigger.SUN_ELEVATION,
                                      morning_trigger=MorningTrigger.SUN_ELEVATION))
        assert coord._next_lifecycle_boundary(_local(2026, 6, 30, 12, 0)) is None

    def test_both_trigger_uses_fixed_time(self):
        coord, _ = _make_coord_lc(_lc(night_trigger=NightTrigger.BOTH,
                                      morning_trigger=MorningTrigger.BOTH))
        assert coord._next_lifecycle_boundary(_local(2026, 6, 30, 5, 0)) is not None

    def test_disabled_has_no_boundary(self):
        coord, _ = _make_coord_lc(_lc(night_enabled=False, morning_enabled=False))
        assert coord._next_lifecycle_boundary(_local(2026, 6, 30, 5, 0)) is None


class TestLifecycleBoundaryTimer:
    def _patches(self, now_local):
        return (
            patch.object(sys.modules["homeassistant.helpers.event"], "async_track_point_in_time", create=True),
            patch.object(_coord_module_ref.dt_util, "now", return_value=now_local),
        )

    def test_setup_schedules_point_in_time_at_morning(self):
        coord, entry = _make_coord_lc(_lc())
        with patch.object(sys.modules["homeassistant.helpers.event"], "async_track_point_in_time",
                          create=True, return_value=(lambda: None)) as m, \
             patch.object(_coord_module_ref.dt_util, "now",
                          return_value=_local(2026, 6, 30, 5, 0)):
            coord.async_setup_lifecycle_boundary_timer(entry)
        m.assert_called_once()
        when_arg = m.call_args[0][2]
        assert (when_arg.hour, when_arg.minute) == (6, 35)
        entry.async_on_unload.assert_called_once()
        assert coord._next_lifecycle_boundary_reason == "morning_release"

    def test_fire_requests_refresh_and_reschedules(self):
        coord, entry = _make_coord_lc(_lc())
        captured = {}

        def _track(hass, action, when):
            captured["cb"] = action
            return (lambda: None)

        with patch.object(sys.modules["homeassistant.helpers.event"], "async_track_point_in_time", create=True, side_effect=_track), \
             patch.object(_coord_module_ref.dt_util, "now", return_value=_local(2026, 6, 30, 5, 0)):
            coord.async_setup_lifecycle_boundary_timer(entry)
            before = coord.hass.async_create_task.call_count
            captured["cb"](_local(2026, 6, 30, 6, 35))  # boundary fires
        assert coord.hass.async_create_task.call_count == before + 1
        assert coord._last_lifecycle_boundary_refresh_utc is not None

    def test_teardown_cancels_timer(self):
        coord, entry = _make_coord_lc(_lc())
        calls = {"unsub": 0}

        def _track(*a, **k):
            def _u():
                calls["unsub"] += 1
            return _u

        with patch.object(sys.modules["homeassistant.helpers.event"], "async_track_point_in_time", create=True, side_effect=_track), \
             patch.object(_coord_module_ref.dt_util, "now", return_value=_local(2026, 6, 30, 5, 0)):
            coord.async_setup_lifecycle_boundary_timer(entry)
            coord.async_teardown_lifecycle_boundary_timer()
        assert calls["unsub"] == 1
        assert coord._unsub_lifecycle_boundary is None

    def test_no_timer_without_fixed_boundary(self):
        coord, entry = _make_coord_lc(_lc(night_trigger=NightTrigger.SUN_ELEVATION,
                                          morning_trigger=MorningTrigger.SUN_ELEVATION))
        with patch.object(sys.modules["homeassistant.helpers.event"], "async_track_point_in_time", create=True) as m, \
             patch.object(_coord_module_ref.dt_util, "now",
                          return_value=_local(2026, 6, 30, 5, 0)):
            coord.async_setup_lifecycle_boundary_timer(entry)
        m.assert_not_called()
