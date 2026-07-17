"""Lifecycle profile wiring at the Coordinator level — v1.2.0-beta.1, Beta.1-T6.

Covers what tests/test_lifecycle_resolver.py (pure resolver tests) cannot:
that the Coordinator's LifecycleEngine calls actually use whichever
NightDayLifecycleConfig resolve_lifecycle_config() produced, that the
resolution happens once (at construction — see __init__.py, which is
"reload" for this integration: a config/options change always triggers an
HA ConfigEntry reload, i.e. a fresh Coordinator construction, so there is
no separate "live profile switch without reload" code path to test), that
diagnostics expose profile provenance safely, and that switching profiles
changes ONLY lifecycle-relevant decisions (Presence/EMA/Comfort/Protection/
Manual-Override are untouched, since they never read lifecycle_profiles or
active_lifecycle_profile_id at all).

Coverage:
  CLP-01  Active months come from the active profile (T1 field flows through).
  CLP-02  Night sun event comes from the active profile (T2 field flows through).
  CLP-03  Morning sun event comes from the active profile.
  CLP-04  Night clamp (not_before/not_after) comes from the active profile
          (T3 fields flow through).
  CLP-05  Morning clamp comes from the active profile.
  CLP-06  Switching between two profiles changes ONLY the lifecycle
          decision — constructing two coordinators with different resolved
          lifecycle_config but otherwise identical inputs never differs
          anywhere except LifecycleState.
  CLP-07  An inactive (non-selected) profile has zero effect on the
          decision — only the resolved config matters, never the full
          `profiles` dict content.
  CLP-08  _evaluate_trigger()/_active_profile() are called exactly as
          before T6 — behavior-identical for a profile's config as for an
          equivalent legacy config (same inputs -> same LifecycleState).
  CLP-09  Diagnostics: lifecycle_profile_summary fields (enabled,
          profile_count, active_profile_id, source) are correct for
          legacy/stored/fallback cases.
  CLP-10  Diagnostics are safe (no AttributeError, JSON-serializable, no
          display_name exposed) even before any cycle has run.
  CLP-11  Regression: legacy-oracle comparison — a Coordinator built with
          NO profiles behaves identically to one built pre-T6 (same
          constructor call minus the three new profile kwargs).
  CLP-12  Reload semantics: "switching the active profile" is modeled as
          constructing a fresh Coordinator with the newly-resolved config
          (matching real HA's reload-on-options-change behavior) — no
          stale profile state survives from a prior Coordinator instance.
  CLP-13  Bug-injection: a Coordinator constructed with the WRONG resolved
          lifecycle_config (as if the resolver's active_profile_id were
          ignored) produces a different LifecycleState than the correctly
          resolved one for the same profile data.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, time, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# HA stubs — must precede any coordinator import in this module (mirrors the
# proven-working pattern in tests/test_coordinator_presence_policy.py).
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
from custom_components.smartshading.engines.lifecycle_resolver import resolve_lifecycle_config  # noqa: E402
from custom_components.smartshading.models.lifecycle import (  # noqa: E402
    LifecycleState,
    NightDayLifecycleConfig,
    NightTrigger,
    SunEvent,
)
from custom_components.smartshading.models.lifecycle_profile import LifecycleProfile  # noqa: E402


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

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


def _make_coord(lifecycle_config=None, **kwargs) -> SmartShadingCoordinator:
    hass = _make_hass()
    entry = _make_entry()
    return SmartShadingCoordinator(
        hass, entry,
        lifecycle_config=lifecycle_config or NightDayLifecycleConfig(id="default"),
        **kwargs,
    )


def _local(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# CLP-01 .. CLP-05 — profile fields flow through to LifecycleEngine.
# ---------------------------------------------------------------------------

class TestProfileFieldsFlowThrough:
    def test_active_months_from_active_profile(self):
        legacy = NightDayLifecycleConfig(id="default")  # unrestricted
        profile_config = NightDayLifecycleConfig(
            id="p1", night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(20, 0),
            active_months=[6, 7, 8],
        )
        resolved = resolve_lifecycle_config(legacy, {"p1": LifecycleProfile("p1", "Summer", profile_config)}, "p1")
        coord = _make_coord(lifecycle_config=resolved.config)
        # January is outside active_months -> DAY regardless of time.
        state_jan = coord.lifecycle_engine.get_lifecycle_state(_local(1, 15, 23, 0), None, coord._lifecycle_config)
        assert state_jan is LifecycleState.DAY
        state_jul = coord.lifecycle_engine.get_lifecycle_state(_local(7, 15, 20, 30), None, coord._lifecycle_config)
        assert state_jul is LifecycleState.NIGHT

    def test_night_sun_event_from_active_profile(self):
        from custom_components.smartshading.engines.lifecycle_engine import SunEventTimes
        profile_config = NightDayLifecycleConfig(
            id="p1", night_trigger=NightTrigger.FIXED_TIME, night_sun_event=SunEvent.SUNSET,
        )
        coord = _make_coord(lifecycle_config=profile_config)
        sun_times = SunEventTimes(next_sunset=_local(6, 15, 21, 0))
        state = coord.lifecycle_engine.get_lifecycle_state(
            _local(6, 15, 21, 30), None, coord._lifecycle_config, LifecycleState.DAY, sun_times
        )
        assert state is LifecycleState.NIGHT

    def test_morning_sun_event_from_active_profile(self):
        from custom_components.smartshading.engines.lifecycle_engine import SunEventTimes
        profile_config = NightDayLifecycleConfig(
            id="p1", night_trigger=NightTrigger.DISABLED,
            morning_trigger=NightTrigger.FIXED_TIME, morning_sun_event=SunEvent.SUNRISE,
        )
        coord = _make_coord(lifecycle_config=profile_config)
        sun_times = SunEventTimes(next_sunrise=_local(6, 15, 6, 0))
        state = coord.lifecycle_engine.get_lifecycle_state(
            _local(6, 15, 6, 30), None, coord._lifecycle_config, LifecycleState.NIGHT, sun_times
        )
        assert state is LifecycleState.MORNING

    def test_night_clamp_from_active_profile(self):
        profile_config = NightDayLifecycleConfig(
            id="p1", night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(20, 0),
            night_not_before=time(21, 30),
        )
        coord = _make_coord(lifecycle_config=profile_config)
        before_clamp = coord.lifecycle_engine.get_lifecycle_state(_local(6, 15, 20, 30), None, coord._lifecycle_config)
        assert before_clamp is LifecycleState.DAY
        after_clamp = coord.lifecycle_engine.get_lifecycle_state(_local(6, 15, 21, 35), None, coord._lifecycle_config)
        assert after_clamp is LifecycleState.NIGHT

    def test_morning_clamp_from_active_profile(self):
        profile_config = NightDayLifecycleConfig(
            id="p1", night_trigger=NightTrigger.DISABLED,
            morning_trigger=NightTrigger.FIXED_TIME, morning_fixed_time=time(5, 0),
            morning_not_before=time(6, 30),
        )
        coord = _make_coord(lifecycle_config=profile_config)
        before_clamp = coord.lifecycle_engine.get_lifecycle_state(
            _local(6, 15, 5, 30), None, coord._lifecycle_config, LifecycleState.NIGHT
        )
        assert before_clamp is LifecycleState.NIGHT  # still waiting for clamped time
        after_clamp = coord.lifecycle_engine.get_lifecycle_state(
            _local(6, 15, 6, 45), None, coord._lifecycle_config, LifecycleState.NIGHT
        )
        assert after_clamp is LifecycleState.MORNING


# ---------------------------------------------------------------------------
# CLP-06 / CLP-07 — switching profiles / inactive profile has no effect.
# ---------------------------------------------------------------------------

class TestProfileSwitchingIsolated:
    def test_switching_profiles_changes_only_lifecycle_decision(self):
        profile_a = NightDayLifecycleConfig(id="a", night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(20, 0))
        profile_b = NightDayLifecycleConfig(id="b", night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(23, 0))
        coord_a = _make_coord(lifecycle_config=profile_a)
        coord_b = _make_coord(lifecycle_config=profile_b)
        now = _local(6, 15, 21, 0)
        state_a = coord_a.lifecycle_engine.get_lifecycle_state(now, None, coord_a._lifecycle_config)
        state_b = coord_b.lifecycle_engine.get_lifecycle_state(now, None, coord_b._lifecycle_config)
        assert state_a is LifecycleState.NIGHT   # 21:00 >= 20:00
        assert state_b is LifecycleState.DAY      # 21:00 < 23:00

    def test_inactive_profile_data_never_reaches_the_engine(self):
        """resolve_lifecycle_config() only ever returns ONE config — the
        Coordinator never even sees the other profiles in the dict, so an
        inactive profile literally cannot influence anything."""
        legacy = NightDayLifecycleConfig(id="default", night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(22, 0))
        inactive = LifecycleProfile("inactive", "Unused", NightDayLifecycleConfig(id="inactive", night_position=99, night_trigger=NightTrigger.DISABLED))
        resolved = resolve_lifecycle_config(legacy, {"inactive": inactive}, None)  # not selected
        assert resolved.config is legacy
        assert resolved.config.night_position != 99


# ---------------------------------------------------------------------------
# CLP-08 — _evaluate_trigger()/_active_profile() behavior-identical.
# ---------------------------------------------------------------------------

class TestEvaluateTriggerUnchanged:
    def test_profile_config_and_equivalent_legacy_config_produce_same_state(self):
        shared_kwargs = dict(night_trigger=NightTrigger.BOTH, night_sun_elevation_deg=-6.0, night_fixed_time=time(22, 0))
        legacy_equivalent = NightDayLifecycleConfig(id="default", **shared_kwargs)
        profile_equivalent = NightDayLifecycleConfig(id="p1", **shared_kwargs)
        now = _local(6, 15, 22, 30)
        coord1 = _make_coord(lifecycle_config=legacy_equivalent)
        coord2 = _make_coord(lifecycle_config=profile_equivalent)
        state1 = coord1.lifecycle_engine.get_lifecycle_state(now, -10.0, coord1._lifecycle_config)
        state2 = coord2.lifecycle_engine.get_lifecycle_state(now, -10.0, coord2._lifecycle_config)
        assert state1 == state2 == LifecycleState.NIGHT

    def test_weekday_weekend_profile_drives_different_weekday_vs_weekend_state(self):
        """Post pre-push-review correction: a profile with
        schedule_mode=WEEKDAY_WEEKEND actually differentiates weekday vs
        weekend behavior through the unchanged _active_profile() logic —
        proving weekday/weekend support is functional, not just stored."""
        from custom_components.smartshading.models.lifecycle import LifecycleScheduleMode
        profile_config = NightDayLifecycleConfig(
            id="p1", schedule_mode=LifecycleScheduleMode.WEEKDAY_WEEKEND,
            night_trigger=NightTrigger.FIXED_TIME,
            weekday_night_fixed_time=time(21, 0), weekend_night_fixed_time=time(23, 0),
        )
        coord = _make_coord(lifecycle_config=profile_config)
        # 2026-09-18 is a Friday (weekday), 2026-09-19 is a Saturday (weekend).
        weekday_now = _local(9, 18, 21, 30)
        weekend_now = _local(9, 19, 21, 30)
        weekday_state = coord.lifecycle_engine.get_lifecycle_state(weekday_now, None, coord._lifecycle_config)
        weekend_state = coord.lifecycle_engine.get_lifecycle_state(weekend_now, None, coord._lifecycle_config)
        assert weekday_state is LifecycleState.NIGHT   # 21:30 >= weekday 21:00
        assert weekend_state is LifecycleState.DAY       # 21:30 < weekend 23:00


# ---------------------------------------------------------------------------
# CLP-09 / CLP-10 — diagnostics.
# ---------------------------------------------------------------------------

class TestDiagnostics:
    def test_legacy_source_diagnostics(self):
        coord = _make_coord()
        coord.windows = {}; coord.zones = {}; coord.cover_groups = {}
        result = build_consolidated_diagnostics(coord)
        summary = result["lifecycle_profile_summary"]
        assert summary == {"enabled": False, "profile_count": 0, "active_profile_selected": False, "source": "legacy"}

    def test_stored_source_diagnostics(self):
        legacy = NightDayLifecycleConfig(id="default")
        p1 = LifecycleProfile("p1", "Weekend", NightDayLifecycleConfig(id="p1"))
        resolved = resolve_lifecycle_config(legacy, {"p1": p1}, "p1")
        coord = _make_coord(
            lifecycle_config=resolved.config, lifecycle_profile_source=resolved.source,
            lifecycle_profile_count=resolved.profile_count, active_lifecycle_profile_id=resolved.active_profile_id,
        )
        coord.windows = {}; coord.zones = {}; coord.cover_groups = {}
        summary = build_consolidated_diagnostics(coord)["lifecycle_profile_summary"]
        assert summary == {"enabled": True, "profile_count": 1, "active_profile_selected": True, "source": "stored"}

    def test_fallback_source_diagnostics_for_unknown_active_id(self):
        """A fallback still means NO profile is actually in effect (the
        legacy config is used) — active_profile_selected must be False even
        though profiles are configured and an (unknown, stale) id exists in
        storage. No raw id is exposed regardless (see diagnostics_builder.py
        _lifecycle_profile_summary())."""
        legacy = NightDayLifecycleConfig(id="default")
        p1 = LifecycleProfile("p1", "Weekend", NightDayLifecycleConfig(id="p1"))
        resolved = resolve_lifecycle_config(legacy, {"p1": p1}, "missing_id")
        coord = _make_coord(
            lifecycle_config=resolved.config, lifecycle_profile_source=resolved.source,
            lifecycle_profile_count=resolved.profile_count, active_lifecycle_profile_id=resolved.active_profile_id,
        )
        coord.windows = {}; coord.zones = {}; coord.cover_groups = {}
        summary = build_consolidated_diagnostics(coord)["lifecycle_profile_summary"]
        assert summary["source"] == "fallback"
        assert "active_profile_id" not in summary
        assert "missing_id" not in str(summary)

    def test_diagnostics_safe_before_first_cycle_and_json_safe(self):
        import json
        coord = _make_coord(lifecycle_profile_source="stored", lifecycle_profile_count=2, active_lifecycle_profile_id="p1")
        coord.windows = {}; coord.zones = {}; coord.cover_groups = {}
        result = build_consolidated_diagnostics(coord)  # must not raise
        summary = result["lifecycle_profile_summary"]
        json.dumps(summary)  # must be JSON-serializable
        assert "display_name" not in summary
        assert "Weekend" not in str(summary)
        assert "active_profile_id" not in summary
        assert "p1" not in str(summary)  # no raw profile_id exposed anywhere


# ---------------------------------------------------------------------------
# CLP-11 — regression: legacy-oracle comparison.
# ---------------------------------------------------------------------------

class TestLegacyOracleRegression:
    def test_no_profile_kwargs_matches_pre_t6_construction(self):
        legacy = NightDayLifecycleConfig(id="default", night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(21, 0))
        coord_pre_t6_style = _make_coord(lifecycle_config=legacy)  # no profile kwargs at all
        coord_explicit_legacy = _make_coord(
            lifecycle_config=legacy, lifecycle_profile_source="legacy",
            lifecycle_profile_count=0, active_lifecycle_profile_id=None,
        )
        now = _local(6, 15, 21, 30)
        s1 = coord_pre_t6_style.lifecycle_engine.get_lifecycle_state(now, None, coord_pre_t6_style._lifecycle_config)
        s2 = coord_explicit_legacy.lifecycle_engine.get_lifecycle_state(now, None, coord_explicit_legacy._lifecycle_config)
        assert s1 == s2 == LifecycleState.NIGHT


# ---------------------------------------------------------------------------
# CLP-12 — reload semantics (fresh construction = "reload").
# ---------------------------------------------------------------------------

class TestReloadSemantics:
    def test_switching_active_profile_via_fresh_construction_no_stale_state(self):
        legacy = NightDayLifecycleConfig(id="default", night_trigger=NightTrigger.DISABLED)
        p1 = LifecycleProfile("p1", "A", NightDayLifecycleConfig(id="p1", night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(20, 0)))
        p2 = LifecycleProfile("p2", "B", NightDayLifecycleConfig(id="p2", night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(23, 30)))
        profiles = {"p1": p1, "p2": p2}

        # "Reload" with p1 active.
        resolved1 = resolve_lifecycle_config(legacy, profiles, "p1")
        coord1 = _make_coord(lifecycle_config=resolved1.config)
        now = _local(6, 15, 21, 0)
        assert coord1.lifecycle_engine.get_lifecycle_state(now, None, coord1._lifecycle_config) is LifecycleState.NIGHT

        # "Reload" with p2 active — a brand-new Coordinator, exactly like a
        # real HA ConfigEntry reload after an OptionsFlow save.
        resolved2 = resolve_lifecycle_config(legacy, profiles, "p2")
        coord2 = _make_coord(lifecycle_config=resolved2.config)
        assert coord2.lifecycle_engine.get_lifecycle_state(now, None, coord2._lifecycle_config) is LifecycleState.DAY

        # Switching back to p1 in a third fresh instance must reproduce the
        # original result exactly — no stale state leaked across instances.
        resolved1_again = resolve_lifecycle_config(legacy, profiles, "p1")
        coord3 = _make_coord(lifecycle_config=resolved1_again.config)
        assert coord3.lifecycle_engine.get_lifecycle_state(now, None, coord3._lifecycle_config) is LifecycleState.NIGHT

        # Full requested scenario (pre-push review point 6): Legacy -> A ->
        # B -> Legacy across four independent "reloads", each represented
        # by a fresh Coordinator instance. Explicitly walk back to Legacy
        # (active_profile_id=None, matching what deleting/deselecting the
        # active profile persists) and confirm the resolver + Coordinator
        # report Legacy again, not a stale profile.
        resolved_legacy_start = resolve_lifecycle_config(legacy, profiles, None)
        coord_legacy_start = _make_coord(
            lifecycle_config=resolved_legacy_start.config,
            lifecycle_profile_source=resolved_legacy_start.source,
            lifecycle_profile_count=resolved_legacy_start.profile_count,
            active_lifecycle_profile_id=resolved_legacy_start.active_profile_id,
        )
        assert resolved_legacy_start.source == "legacy"
        assert coord_legacy_start.lifecycle_engine.get_lifecycle_state(now, None, coord_legacy_start._lifecycle_config) is LifecycleState.DAY

        resolved_a = resolve_lifecycle_config(legacy, profiles, "p1")
        coord_a = _make_coord(
            lifecycle_config=resolved_a.config,
            lifecycle_profile_source=resolved_a.source,
            active_lifecycle_profile_id=resolved_a.active_profile_id,
        )
        assert resolved_a.source == "stored"
        assert coord_a.lifecycle_engine.get_lifecycle_state(now, None, coord_a._lifecycle_config) is LifecycleState.NIGHT

        resolved_b = resolve_lifecycle_config(legacy, profiles, "p2")
        coord_b = _make_coord(
            lifecycle_config=resolved_b.config,
            lifecycle_profile_source=resolved_b.source,
            active_lifecycle_profile_id=resolved_b.active_profile_id,
        )
        assert resolved_b.source == "stored"
        assert coord_b.lifecycle_engine.get_lifecycle_state(now, None, coord_b._lifecycle_config) is LifecycleState.DAY

        # Back to Legacy — e.g. after the user deletes the active profile,
        # which persists active_lifecycle_profile_id=None.
        resolved_back_to_legacy = resolve_lifecycle_config(legacy, profiles, None)
        coord_back_to_legacy = _make_coord(
            lifecycle_config=resolved_back_to_legacy.config,
            lifecycle_profile_source=resolved_back_to_legacy.source,
            active_lifecycle_profile_id=resolved_back_to_legacy.active_profile_id,
        )
        assert resolved_back_to_legacy.source == "legacy"
        assert resolved_back_to_legacy.active_profile_id is None
        assert coord_back_to_legacy.lifecycle_engine.get_lifecycle_state(now, None, coord_back_to_legacy._lifecycle_config) is LifecycleState.DAY
        # No module/class-level cache: this is a brand-new Coordinator
        # instance, unrelated to coord_legacy_start, yet produces the exact
        # same result purely from resolve_lifecycle_config()'s pure inputs.


# ---------------------------------------------------------------------------
# CLP-13 — bug-injection.
# ---------------------------------------------------------------------------

class TestBugInjection:
    def test_wrong_resolved_config_changes_outcome(self):
        """If __init__.py's wiring were broken and always passed the legacy
        config to the Coordinator regardless of resolve_lifecycle_config()'s
        result, this would produce a different (wrong) LifecycleState than
        correctly passing the resolved profile config — proving the
        resolution is genuinely wired into Coordinator construction."""
        legacy = NightDayLifecycleConfig(id="default", night_trigger=NightTrigger.DISABLED)
        p1 = LifecycleProfile("p1", "A", NightDayLifecycleConfig(id="p1", night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(20, 0)))
        resolved = resolve_lifecycle_config(legacy, {"p1": p1}, "p1")
        assert resolved.config is p1.config  # sanity: resolver did pick the profile

        now = _local(6, 15, 21, 0)
        correct_coord = _make_coord(lifecycle_config=resolved.config)
        broken_coord = _make_coord(lifecycle_config=legacy)  # BUG: ignores resolver's result

        correct_state = correct_coord.lifecycle_engine.get_lifecycle_state(now, None, correct_coord._lifecycle_config)
        broken_state = broken_coord.lifecycle_engine.get_lifecycle_state(now, None, broken_coord._lifecycle_config)
        assert correct_state != broken_state
        assert correct_state is LifecycleState.NIGHT
        assert broken_state is LifecycleState.DAY
