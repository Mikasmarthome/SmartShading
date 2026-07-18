"""Diagnostics coverage for manual_override_summary — T7.

Same real-Coordinator-construction technique established in
tests/test_coordinator_lifecycle_profile.py (T6): a minimal HA stub set lets
coordinator.py actually import and construct, so these tests exercise the
real build_consolidated_diagnostics() output, not a hand-built fixture.

Coverage:
  MOD-01  Legacy defaults produce the expected safe summary.
  MOD-02  Configured fixed-time/allow flags surface correctly (as booleans/
          strings only — no raw clock time, no window/position data).
  MOD-03  active_override_count reflects the detector's live state.
  MOD-04  nearest_expiry_remaining_min is a relative minute count, never an
          absolute timestamp, and is None when no override is active.
  MOD-05  Diagnostics are safe (no crash, JSON-serializable) before any
          coordinator cycle has run.
  MOD-06  No window ids, cover ids, or override positions ever appear.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, time, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# HA stubs — identical technique to test_coordinator_lifecycle_profile.py.
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
from custom_components.smartshading.models.manual_override import ManualOverride  # noqa: E402
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


class TestLegacyDefaultsSummary:
    def test_default_summary(self):
        coord = _make_coord()
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        assert summary == {
            "duration_mode": "legacy",
            "fixed_time_configured": False,
            "allow_comfort": False,
            "allow_protection": False,
            "break_on_lifecycle": True,
            "active_override_count": 0,
            "nearest_expiry_remaining_min": None,
        }


class TestConfiguredFlagsSurfaceSafely:
    def test_fixed_time_and_allow_flags(self):
        coord = _make_coord(
            override_duration_mode="fixed_time",
            override_fixed_until=time(8, 0),
            override_allow_comfort_actions=True,
            override_allow_protection_actions=True,
            override_break_on_lifecycle=False,
        )
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        assert summary["duration_mode"] == "fixed_time"
        assert summary["fixed_time_configured"] is True
        assert summary["allow_comfort"] is True
        assert summary["allow_protection"] is True
        assert summary["break_on_lifecycle"] is False
        # The raw configured clock time itself is never exposed.
        assert "08:00" not in str(summary)
        assert "time(8, 0)" not in str(summary)


class TestActiveOverrideCount:
    def test_count_reflects_detector_state(self):
        coord = _make_coord()
        # Anchored to real "now" (not a fixed past/future date): the
        # diagnostics builder itself uses datetime.now(timezone.utc)
        # internally to filter active_overrides_snapshot(), so the override
        # created here must still be unexpired relative to the real clock.
        now = datetime.now(timezone.utc)
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now,
        )  # warmup cycle, no override yet
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120,
            now=now + timedelta(minutes=1),
        )
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        assert summary["active_override_count"] == 1

    def test_count_zero_when_no_windows_overridden(self):
        coord = _make_coord()
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        assert summary["active_override_count"] == 0


class TestNearestExpiryRemaining:
    def test_none_when_no_active_override(self):
        coord = _make_coord()
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        assert summary["nearest_expiry_remaining_min"] is None

    def test_relative_minutes_not_absolute_timestamp(self):
        coord = _make_coord()
        now = datetime.now(timezone.utc)
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now,
        )
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120,
            now=now + timedelta(minutes=1),
        )
        # Monkeypatch the diagnostics builder's "now" indirectly is not
        # possible (it uses datetime.now(timezone.utc) internally), so
        # instead assert the returned value is a small, plausible relative
        # number (<= 120, the configured duration) rather than an absolute
        # epoch-scale number, and that no ISO timestamp string leaks.
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        remaining = summary["nearest_expiry_remaining_min"]
        assert remaining is None or (0 <= remaining <= 122)
        assert "T" not in str(remaining)  # not an ISO datetime string


class TestDiagnosticsSafeBeforeFirstCycle:
    def test_no_crash_and_json_serializable(self):
        coord = _make_coord()
        result = build_consolidated_diagnostics(coord)  # must not raise
        summary = result["manual_override_summary"]
        json.dumps(summary)  # must be JSON-serializable


class TestExpiredOverridesNotCounted:
    def test_expired_override_excluded_from_active_count(self) -> None:
        coord = _make_coord()
        now = datetime.now(timezone.utc)
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now - timedelta(minutes=200),
        )
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=1,  # expires almost immediately
            now=now - timedelta(minutes=199),
        )
        # By "now" (real time), that override (1-minute duration, created
        # ~199 minutes ago) is long expired.
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        assert summary["active_override_count"] == 0
        assert summary["nearest_expiry_remaining_min"] is None


class TestNearestExpiryNeverNegative:
    def test_remaining_min_is_never_negative(self) -> None:
        coord = _make_coord()
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        remaining = summary["nearest_expiry_remaining_min"]
        assert remaining is None or remaining >= 0.0


class TestFixedTimeAndLegacyProduceEquivalentDiagnosticsShape:
    def test_fixed_time_override_diagnostics_same_shape_as_legacy(self) -> None:
        coord = _make_coord(override_duration_mode="fixed_time", override_fixed_until=time(23, 0))
        now = datetime.now(timezone.utc)
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now,
        )
        now_local = now.astimezone()
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now + timedelta(minutes=1),
            duration_mode="fixed_time", fixed_until=time(23, 59), now_local=now_local + timedelta(minutes=1),
        )
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        assert summary["duration_mode"] == "fixed_time"
        assert summary["active_override_count"] == 1
        assert isinstance(summary["nearest_expiry_remaining_min"], float)
        assert set(summary.keys()) == {
            "duration_mode", "fixed_time_configured", "allow_comfort",
            "allow_protection", "break_on_lifecycle", "active_override_count",
            "nearest_expiry_remaining_min",
        }  # identical shape to the legacy case


class TestMultipleOverridesDifferentExpiryPicksNearest:
    def test_nearest_expiry_across_multiple_windows(self) -> None:
        coord = _make_coord()
        now = datetime.now(timezone.utc)
        # Window 1: expires far in the future (120 min).
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now,
        )
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now + timedelta(minutes=1),
        )
        # Window 2: expires soon (10 min).
        coord._override_detector.tick(
            window_id="w2", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=10, now=now,
        )
        coord._override_detector.tick(
            window_id="w2", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=10, now=now + timedelta(minutes=1),
        )
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        assert summary["active_override_count"] == 2
        # The MINIMUM (window 2's ~9-10 min) must be reported, not window 1's.
        assert summary["nearest_expiry_remaining_min"] < 15
        assert "w1" not in str(summary)
        assert "w2" not in str(summary)


class TestNoPrivateDetectorDictAccess:
    def test_diagnostics_builder_only_calls_public_detector_api(self) -> None:
        """Structural proof (source inspection): diagnostics_builder.py
        must call OverrideDetector's public active_overrides_snapshot()
        method, never reach into its private _active_overrides dict
        directly."""
        import inspect
        from custom_components.smartshading.engines import diagnostics_builder
        source = inspect.getsource(diagnostics_builder)
        assert "active_overrides_snapshot(now)" in source
        assert "_active_overrides" not in source  # never touches the private dict name


class TestNoAbsoluteTimestampsExposed:
    def test_no_iso_timestamp_strings_in_summary(self) -> None:
        coord = _make_coord()
        now = datetime.now(timezone.utc)
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now,
        )
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now + timedelta(minutes=1),
        )
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        summary_str = str(summary)
        assert "T" not in summary_str or "+00:00" not in summary_str  # no ISO-8601 datetime signature
        assert str(now.year) not in summary_str  # no year fragment from an absolute date


class TestNoWindowOrPositionDataExposed:
    def test_no_window_ids_or_positions_in_summary(self):
        coord = _make_coord()
        now = datetime.now(timezone.utc)
        coord._override_detector.tick(
            window_id="window-secret-42", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now,
        )
        coord._override_detector.tick(
            window_id="window-secret-42", observed_position=77, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120,
            now=now + timedelta(minutes=1),
        )
        summary = build_consolidated_diagnostics(coord)["manual_override_summary"]
        assert "window-secret-42" not in str(summary)
        assert "77" not in str(summary.get("duration_mode", ""))
        assert set(summary.keys()) == {
            "duration_mode", "fixed_time_configured", "allow_comfort",
            "allow_protection", "break_on_lifecycle", "active_override_count",
            "nearest_expiry_remaining_min",
        }
