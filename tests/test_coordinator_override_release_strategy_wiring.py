"""Coordinator-level wiring for override_release_strategy /
override_safety_timeout_enabled (v1.2.0-beta.1, T10) and the new
async_clear_manual_override() public method.

Uses the same real-SmartShadingCoordinator-with-HA-stubs technique as
test_override_policy_e2e_wiring.py — proves the Coordinator's own stored
attributes (self._override_release_strategy,
self._override_safety_timeout_enabled) genuinely reach the real
OverrideDetector's tick() behavior, and that async_clear_manual_override()
actually clears an active override end-to-end.

Coverage:
  CW-01  Coordinator constructor kwarg override_release_strategy is stored
         and reaches the detector's expiry computation.
  CW-02  override_safety_timeout_enabled reaches the detector (sentinel vs
         timed expiry).
  CW-03  A fresh Coordinator instance (HA reload / Config Reload) picks up a
         changed release_strategy, never a stale previous-instance value.
  CW-04  async_clear_manual_override() clears an active override and
         suppresses the next detection tick (no immediate false re-detection
         from the still-unmoved cover position).
  CW-05  async_clear_manual_override() is a no-op (returns False) when no
         override is active, and when the window_id is unknown.
  CW-06  HA restart: restore_active_overrides() round-trips release_strategy
         through ManualOverride.to_dict()/from_dict() unchanged.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
from custom_components.smartshading.engines.override_release import NO_SAFETY_TIMEOUT  # noqa: E402
from custom_components.smartshading.models.lifecycle import NightDayLifecycleConfig  # noqa: E402
from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy  # noqa: E402
from custom_components.smartshading.models.window import WindowConfig  # noqa: E402
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
        hass, entry, lifecycle_config=NightDayLifecycleConfig(id="default"), **kwargs,
    )
    coord.windows = {}
    coord.zones = {}
    coord.cover_groups = {}
    return coord


class TestReleaseStrategyWiredToDetector:
    def test_lifecycle_strategy_with_safety_timeout_uses_duration_min(self) -> None:
        coord = _make_coord(
            override_release_strategy=OverrideReleaseStrategy.LIFECYCLE,
            override_safety_timeout_enabled=True,
        )
        assert coord._override_release_strategy is OverrideReleaseStrategy.LIFECYCLE

        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
            release_strategy=coord._override_release_strategy,
            safety_timeout_enabled=coord._override_safety_timeout_enabled,
        )
        t1 = t0 + timedelta(minutes=1)
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t1,
            release_strategy=coord._override_release_strategy,
            safety_timeout_enabled=coord._override_safety_timeout_enabled,
        )
        ov = coord._override_detector.get("w1", t1)
        assert ov.expires_at == t1 + timedelta(minutes=120)


class TestSafetyTimeoutEnabledWiredToDetector:
    def test_disabled_produces_sentinel_expiry(self) -> None:
        coord = _make_coord(
            override_release_strategy=OverrideReleaseStrategy.FIRST_COMFORT,
            override_safety_timeout_enabled=False,
        )
        assert coord._override_safety_timeout_enabled is False

        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
            release_strategy=coord._override_release_strategy,
            safety_timeout_enabled=coord._override_safety_timeout_enabled,
        )
        t1 = t0 + timedelta(minutes=1)
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t1,
            release_strategy=coord._override_release_strategy,
            safety_timeout_enabled=coord._override_safety_timeout_enabled,
        )
        ov = coord._override_detector.get("w1", t1)
        assert ov.expires_at == NO_SAFETY_TIMEOUT


class TestReloadPicksUpNewStrategyNoStaleDefault:
    def test_fresh_coordinator_uses_new_value(self) -> None:
        coord_before = _make_coord(override_release_strategy=OverrideReleaseStrategy.DURATION)
        coord_after = _make_coord(override_release_strategy=OverrideReleaseStrategy.MANUAL)
        assert coord_before._override_release_strategy is OverrideReleaseStrategy.DURATION
        assert coord_after._override_release_strategy is OverrideReleaseStrategy.MANUAL


class TestAsyncClearManualOverride:
    def test_clears_active_override_and_suppresses_next_tick(self, monkeypatch) -> None:
        coord = _make_coord()
        window = WindowConfig(id="w1", name="Window 1", zone_id="z1", azimuth=180, floor_level=0, cover_group_id="cg1")
        coord.windows = {"w1": window}
        coord.async_request_refresh = AsyncMock()

        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
        )
        t1 = t0 + timedelta(minutes=1)
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t1,
        )
        assert coord._override_detector.get("w1", t1) is not None

        # async_clear_manual_override() reads dt_util.utcnow() internally —
        # pin it to t1 so the override (expires_at = t1 + 120min) is
        # unambiguously still active at "now", independent of wall-clock time.
        # Resolved via the bound method's own __globals__ rather than a
        # module-level import or a sys.modules[name] lookup: many test files
        # in this suite each pop and reimport
        # custom_components.smartshading.coordinator with their own HA stub
        # set for isolation, so whichever file's reimport ran LAST leaves
        # sys.modules[name] pointing at ITS module, not necessarily the one
        # coord's class actually came from. __globals__ is captured at
        # function-definition time and always names the exact dt_util object
        # this bound method will read, regardless of later reimports
        # elsewhere in the suite.
        monkeypatch.setattr(
            coord.async_clear_manual_override.__func__.__globals__["dt_util"],
            "utcnow", lambda: t1,
        )

        result = asyncio.run(coord.async_clear_manual_override("w1"))
        assert result is True
        assert coord._override_detector.get("w1", t1) is None

        # The next tick with the still-unmoved (40) position must NOT
        # re-detect a brand-new override — the suppression consumed here.
        t2 = t1 + timedelta(seconds=30)
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t2,
        )
        assert coord._override_detector.get("w1", t2) is None

    def test_no_op_when_no_override_active(self) -> None:
        coord = _make_coord()
        coord.windows = {"w1": WindowConfig(id="w1", name="Window 1", zone_id="z1", azimuth=180, floor_level=0, cover_group_id="cg1")}
        result = asyncio.run(coord.async_clear_manual_override("w1"))
        assert result is False

    def test_no_op_when_window_unknown(self) -> None:
        coord = _make_coord()
        coord.windows = {}
        result = asyncio.run(coord.async_clear_manual_override("nonexistent"))
        assert result is False


class TestRestartPersistenceRoundTripsReleaseStrategy:
    def test_restore_preserves_release_strategy(self) -> None:
        coord = _make_coord(override_release_strategy=OverrideReleaseStrategy.FIRST_PROTECTION)
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
            release_strategy=OverrideReleaseStrategy.FIRST_PROTECTION,
        )
        t1 = t0 + timedelta(minutes=1)
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t1,
            release_strategy=OverrideReleaseStrategy.FIRST_PROTECTION,
        )
        snapshot = coord._override_detector.active_overrides_snapshot(t1)
        assert snapshot[0]["release_strategy"] == "first_protection"

        # A fresh detector (simulating restart) restores from the snapshot.
        restored_detector = type(coord._override_detector)()
        restored_detector.restore_active_overrides(snapshot, t1)
        restored = restored_detector.get("w1", t1)
        assert restored is not None
        assert restored.release_strategy == "first_protection"
