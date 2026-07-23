"""End-to-end wiring proof: previously-hidden override values (legacy
duration, night duration, detection tolerance, break-on-lifecycle) flow
from UI/storage all the way to the real OverrideDetector's behavior —
T7 pre-push review point 14.

Two-part proof, matching the two halves of the wiring chain:

  Part A (storage -> Coordinator constructor kwargs): source-level proof
  already exists in test_override_duration_defaults_regression.py
  (TestEffectiveRuntimeChainUsesOverridePolicyConfigNotBehaviorConfigDefault) —
  confirms __init__.py passes
  override_duration_min=entry_data.override_policy.duration_min (etc.) into
  SmartShadingCoordinator's constructor, never a BehaviorConfig instance.

  Part B (Coordinator attribute -> real Detector behavior): THIS file.
  Constructs a real SmartShadingCoordinator with specific override_policy-
  derived kwargs (exactly as __init__.py would), then drives its OWN
  OverrideDetector instance using the Coordinator's OWN stored attributes
  (self._override_duration_min, self._override_night_duration_min,
  self._override_detection_tolerance, self._override_release_strategy) —
  proving a non-default configured value genuinely changes the detector's
  observable output (expiry timing, detection threshold), not just that a
  constructor parameter exists.

  T10: the old boolean override_break_on_lifecycle constructor kwarg/
  attribute became the OverrideReleaseStrategy-valued
  override_release_strategy — LIFECYCLE reproduces the old True, any other
  strategy reproduces the old False (see coordinator.py, which derives the
  break_enabled bool passed to lifecycle_should_break_override() as
  `self._override_release_strategy is OverrideReleaseStrategy.LIFECYCLE`).
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

from custom_components.smartshading.engines.lifecycle_guard import lifecycle_should_break_override
from custom_components.smartshading.models.lifecycle import LifecycleState
from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy

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
from custom_components.smartshading.coordinator import SmartShadingCoordinator  # noqa: E402
from custom_components.smartshading.models.lifecycle import NightDayLifecycleConfig  # noqa: E402
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


class TestLegacyDurationWiredToDetector:
    def test_custom_duration_min_actually_changes_expiry(self) -> None:
        coord = _make_coord(override_duration_min=45)  # non-default value
        assert coord._override_duration_min == 45

        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10,
            duration_min=coord._override_duration_min, now=t0,
        )
        t1 = t0 + timedelta(minutes=1)
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10,
            duration_min=coord._override_duration_min, now=t1,
        )
        override = coord._override_detector.get("w1", t1)
        assert override is not None
        assert override.expires_at == t1 + timedelta(minutes=45)  # NOT the 120-min built-in default


class TestNightDurationWiredToDetector:
    def test_custom_night_duration_min_used_when_lifecycle_is_night(self) -> None:
        coord = _make_coord(override_night_duration_min=999)
        assert coord._override_night_duration_min == 999

        # Mirrors coordinator.py's scope selection: night lifecycle -> the
        # night-scoped duration attribute.
        scope = "night"
        duration_for_scope = coord._override_night_duration_min if scope == "night" else coord._override_duration_min
        assert duration_for_scope == 999

        t0 = datetime(2026, 6, 15, 22, 0, tzinfo=timezone.utc)
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=duration_for_scope, now=t0, scope=scope,
        )
        t1 = t0 + timedelta(minutes=1)
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=duration_for_scope, now=t1, scope=scope,
        )
        override = coord._override_detector.get("w1", t1)
        assert override.expires_at == t1 + timedelta(minutes=999)
        assert override.scope == "night"

    def test_day_uses_the_legacy_day_duration_not_night(self) -> None:
        coord = _make_coord(override_duration_min=45, override_night_duration_min=999)
        scope = "daytime"
        duration_for_scope = coord._override_night_duration_min if scope == "night" else coord._override_duration_min
        assert duration_for_scope == 45


class TestDetectionToleranceWiredToDetectorAtStartAndRenewal:
    def test_custom_tolerance_affects_initial_detection(self) -> None:
        coord = _make_coord(override_detection_tolerance=25)
        assert coord._override_detection_tolerance == 25

        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=coord._override_detection_tolerance,
            duration_min=120, now=t0,
        )
        # A 20-point deviation is WITHIN the custom 25-point tolerance -> no
        # override, unlike the built-in default tolerance of 10.
        t1 = t0 + timedelta(minutes=1)
        coord._override_detector.tick(
            window_id="w1", observed_position=20, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=coord._override_detection_tolerance,
            duration_min=120, now=t1,
        )
        assert coord._override_detector.get("w1", t1) is None

        # A 30-point deviation exceeds even the custom tolerance -> detected.
        t2 = t1 + timedelta(minutes=1)
        coord._override_detector.tick(
            window_id="w1", observed_position=30, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=coord._override_detection_tolerance,
            duration_min=120, now=t2,
        )
        assert coord._override_detector.get("w1", t2) is not None

    def test_custom_tolerance_affects_renewal_too(self) -> None:
        coord = _make_coord(override_detection_tolerance=25)
        t0 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
        coord._override_detector.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=coord._override_detection_tolerance, duration_min=120, now=t0,
        )
        t1 = t0 + timedelta(minutes=1)
        coord._override_detector.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=coord._override_detection_tolerance, duration_min=120, now=t1,
        )
        original = coord._override_detector.get("w1", t1)
        # A 20-point move from 40 to 55 is within the custom 25-point
        # tolerance -> no renewal.
        t2 = t1 + timedelta(minutes=1)
        coord._override_detector.tick(
            window_id="w1", observed_position=55, smartshading_target=0,
            prev_state=ShadingState.MANUAL_OVERRIDE, tolerance=coord._override_detection_tolerance, duration_min=120, now=t2,
        )
        after = coord._override_detector.get("w1", t2)
        assert after.started_at == original.started_at  # not renewed


class TestBreakOnLifecycleWired:
    def test_non_lifecycle_release_strategy_disables_the_break(self) -> None:
        coord = _make_coord(override_release_strategy=OverrideReleaseStrategy.DURATION)
        assert coord._override_release_strategy is OverrideReleaseStrategy.DURATION
        break_enabled = coord._override_release_strategy is OverrideReleaseStrategy.LIFECYCLE
        should_break = lifecycle_should_break_override(
            prev=LifecycleState.DAY, new=LifecycleState.NIGHT,
            break_enabled=break_enabled,
        )
        assert should_break is False

    def test_default_lifecycle_release_strategy_enables_the_break(self) -> None:
        coord = _make_coord()
        assert coord._override_release_strategy is OverrideReleaseStrategy.LIFECYCLE
        break_enabled = coord._override_release_strategy is OverrideReleaseStrategy.LIFECYCLE
        should_break = lifecycle_should_break_override(
            prev=LifecycleState.DAY, new=LifecycleState.NIGHT,
            break_enabled=break_enabled,
        )
        assert should_break is True


class TestReloadPicksUpNewValueNoStaleDefault:
    def test_fresh_coordinator_with_new_value_does_not_use_old_default(self) -> None:
        """Simulates a config-change reload: a fresh Coordinator instance
        (exactly what HA's reload-on-options-change produces) constructed
        with an updated duration_min must use THAT value, never a stale
        120-default left over from a previous instance."""
        coord_before = _make_coord(override_duration_min=120)
        coord_after_reload = _make_coord(override_duration_min=200)
        assert coord_before._override_duration_min == 120
        assert coord_after_reload._override_duration_min == 200  # not overwritten by the old default
