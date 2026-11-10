"""Characterization of the Manual Override policy runtime (Phase 5).

Pins the behavior that the Manual Override options UI must NOT change while it
is simplified: the seven release strategies, system/zone policy resolution,
day/night duration semantics, the safety timeout, comfort/protection
passthrough, fixed-time release, the hidden dependency of manual-stop hold
durations on duration_min / night_duration_min, the Night-Contact paths and
the interplay of Safety and the command filter with an active override.

Every assertion here was first run against the implementation BEFORE the UI
change; none of them touches the Options Flow.
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



import re
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.smartshading.config_entry_data import (  # noqa: E402
    _override_policy_from_storage,
    resolve_zone_override_policy,
)
from custom_components.smartshading.cover_control.command_filter import (  # noqa: E402
    BLOCKED_MANUAL_OVERRIDE,
    CommandFilter,
)
from custom_components.smartshading.engines.manual_override_policy import (  # noqa: E402
    evaluate_manual_override_policy,
)
from custom_components.smartshading.engines.night_contact_hold import (  # noqa: E402
    NightContactAction,
    NightContactHold,
)
from custom_components.smartshading.engines.override_detector import OverrideDetector  # noqa: E402
from custom_components.smartshading.engines.override_fixed_time import (  # noqa: E402
    compute_fixed_time_expiry,
)
from custom_components.smartshading.engines.override_release import (  # noqa: E402
    compute_expiry,
    extends_on_renewal,
    resolve_candidate_release,
)
from custom_components.smartshading.models.cover_group import CoverGroup  # noqa: E402
from custom_components.smartshading.models.lifecycle import LifecycleState  # noqa: E402
from custom_components.smartshading.models.manual_override import ManualOverride  # noqa: E402
from custom_components.smartshading.models.window_decision import WindowDecision  # noqa: E402
from custom_components.smartshading.state_machine.states import DecisionCategory  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
_COORD_SRC = (_REPO / "custom_components" / "smartshading" / "coordinator.py").read_text(encoding="utf-8")

S = OverrideReleaseStrategy
T0 = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
ALL_STRATEGIES = list(S)
OPEN_ENDED = [S.LIFECYCLE, S.FIRST_COMFORT, S.FIRST_PROTECTION, S.FIRST_ANY_DECISION, S.MANUAL]


def _candidate(category, decided_by="X", state=ShadingState.LIGHT_SHADE):
    return WindowDecision(
        window_id="w1", shading_state=state, target_position=50,
        decided_by=decided_by, category=category,
    )


def _override(strategy=S.LIFECYCLE):
    return ManualOverride(
        window_id="w1", override_position=30, started_at=T0, expires_at=T0 + timedelta(hours=2),
        source="position_delta", overridden_state=ShadingState.OPEN, overridden_position=0,
        release_strategy=strategy.value,
    )


class TestSevenStrategies:
    def test_there_are_exactly_seven_strategies_with_stable_values(self) -> None:
        assert [s.value for s in S] == [
            "duration", "fixed_time", "lifecycle", "first_comfort",
            "first_protection", "first_any_decision", "manual",
        ]

    @pytest.mark.parametrize("strategy", ALL_STRATEGIES)
    def test_expiry_per_strategy(self, strategy) -> None:
        kw = dict(strategy=strategy, now=T0, now_local=T0, duration_min=45,
                  fixed_until=time(18, 0))
        if strategy is S.DURATION:
            assert compute_expiry(**kw, safety_timeout_enabled=True) == T0 + timedelta(minutes=45)
            assert compute_expiry(**kw, safety_timeout_enabled=False) == T0 + timedelta(minutes=45)
        elif strategy is S.FIXED_TIME:
            expected = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)
            assert compute_expiry(**kw, safety_timeout_enabled=True) == expected
            assert compute_expiry(**kw, safety_timeout_enabled=False) == expected
        else:
            assert compute_expiry(**kw, safety_timeout_enabled=True) == T0 + timedelta(minutes=45)
            assert compute_expiry(**kw, safety_timeout_enabled=False) == NO_SAFETY_TIMEOUT

    def test_only_duration_extends_on_renewal(self) -> None:
        assert [s for s in S if extends_on_renewal(s)] == [S.DURATION]

    @pytest.mark.parametrize("strategy", ALL_STRATEGIES)
    def test_candidate_release_matrix(self, strategy) -> None:
        expected = {
            S.FIRST_COMFORT: {DecisionCategory.COMFORT},
            S.FIRST_PROTECTION: {DecisionCategory.PROTECTION},
            S.FIRST_ANY_DECISION: {DecisionCategory.COMFORT, DecisionCategory.PROTECTION},
        }.get(strategy, set())
        for cat in (DecisionCategory.COMFORT, DecisionCategory.PROTECTION,
                    DecisionCategory.LIFECYCLE, DecisionCategory.SAFETY, DecisionCategory.HOLD):
            assert resolve_candidate_release(strategy=strategy, category=cat) is (cat in expected)

    def test_lifecycle_break_is_tied_to_the_lifecycle_strategy_only(self) -> None:
        assert re.search(
            r"break_enabled=\(\s*self\._override_release_strategy is OverrideReleaseStrategy\.LIFECYCLE\s*\)",
            _COORD_SRC)


class TestPassthroughAndPolicyGate:
    @pytest.mark.parametrize("strategy", ALL_STRATEGIES)
    def test_blocked_without_flags_and_for_lifecycle_candidates(self, strategy) -> None:
        for cat in (DecisionCategory.LIFECYCLE, DecisionCategory.HOLD):
            out = evaluate_manual_override_policy(
                active_override=_override(strategy), candidate=_candidate(cat),
                allow_comfort=True, allow_protection=True, release_strategy=strategy)
            assert out.shading_state is ShadingState.MANUAL_OVERRIDE
            assert out.target_position == 30
        for cat, flag in ((DecisionCategory.COMFORT, "allow_comfort"),
                          (DecisionCategory.PROTECTION, "allow_protection")):
            cand = _candidate(cat)
            releasing = resolve_candidate_release(strategy=strategy, category=cat)
            out = evaluate_manual_override_policy(
                active_override=_override(strategy), candidate=cand,
                allow_comfort=False, allow_protection=False, release_strategy=strategy)
            assert (out.shading_state is not ShadingState.MANUAL_OVERRIDE) is releasing
            assert out.release_override is releasing
            out2 = evaluate_manual_override_policy(
                active_override=_override(strategy), candidate=cand,
                allow_comfort=(flag == "allow_comfort"), allow_protection=(flag == "allow_protection"),
                release_strategy=strategy)
            assert out2.shading_state is cand.shading_state
            assert out2.release_override is releasing

    def test_safety_always_passes(self) -> None:
        cand = _candidate(DecisionCategory.SAFETY, state=ShadingState.STORM_SAFE)
        assert evaluate_manual_override_policy(
            active_override=_override(), candidate=cand, allow_comfort=False, allow_protection=False) is cand

    def test_absence_is_a_protection_candidate_blocked_by_default(self) -> None:
        cand = _candidate(DecisionCategory.PROTECTION, "AbsenceEvaluator", ShadingState.ABSENCE_CLOSED)
        out = evaluate_manual_override_policy(
            active_override=_override(), candidate=cand, allow_comfort=False, allow_protection=False)
        assert out.shading_state is ShadingState.MANUAL_OVERRIDE
        out = evaluate_manual_override_policy(
            active_override=_override(), candidate=cand, allow_comfort=False, allow_protection=True)
        assert out is cand


class TestStoredPolicyAndInheritance:
    def test_empty_and_missing_give_defaults(self) -> None:
        for raw in (None, {}):
            cfg = _override_policy_from_storage(raw)
            assert (cfg.release_strategy, cfg.duration_min, cfg.night_duration_min,
                    cfg.detection_tolerance, cfg.safety_timeout_enabled,
                    cfg.allow_comfort_actions, cfg.allow_protection_actions, cfg.fixed_until) == (
                S.LIFECYCLE, 120, 720, 10, True, False, False, None)

    @pytest.mark.parametrize("strategy", ALL_STRATEGIES)
    def test_every_strategy_value_round_trips(self, strategy) -> None:
        raw = {"release_strategy": strategy.value, "fixed_until": "07:30:00"}
        assert _override_policy_from_storage(raw).release_strategy is strategy

    def test_fixed_time_without_time_falls_back_to_duration(self) -> None:
        assert _override_policy_from_storage({"release_strategy": "fixed_time"}).release_strategy is S.DURATION
        assert _override_policy_from_storage(
            {"release_strategy": "fixed_time", "fixed_until": "garbage"}).release_strategy is S.DURATION

    def test_unknown_strategy_falls_back_to_lifecycle(self) -> None:
        assert _override_policy_from_storage({"release_strategy": "bogus"}).release_strategy is S.LIFECYCLE

    def test_pre_t10_keys(self) -> None:
        assert _override_policy_from_storage({"break_on_lifecycle": True, "duration_mode": "fixed_time"}
                                             ).release_strategy is S.LIFECYCLE
        assert _override_policy_from_storage({"break_on_lifecycle": False}).release_strategy is S.DURATION
        assert _override_policy_from_storage(
            {"break_on_lifecycle": False, "duration_mode": "fixed_time", "fixed_until": "06:00:00"}
        ).release_strategy is S.FIXED_TIME

    @pytest.mark.parametrize("bad", [None, "x", [], {}, True])
    def test_malformed_numbers_fall_back_safely(self, bad) -> None:
        cfg = _override_policy_from_storage({
            "release_strategy": "duration", "duration_min": bad,
            "night_duration_min": bad, "detection_tolerance": bad})
        assert (cfg.duration_min, cfg.night_duration_min, cfg.detection_tolerance) == (120, 720, 10)

    def test_flags_and_values_are_read(self) -> None:
        cfg = _override_policy_from_storage({
            "release_strategy": "manual", "allow_comfort_actions": True, "allow_protection_actions": True,
            "safety_timeout_enabled": False, "duration_min": 90, "night_duration_min": 600,
            "detection_tolerance": 15})
        assert (cfg.allow_comfort_actions, cfg.allow_protection_actions, cfg.safety_timeout_enabled,
                cfg.duration_min, cfg.night_duration_min, cfg.detection_tolerance) == (
            True, True, False, 90, 600, 15)

    def test_zone_without_policy_defers_to_system_but_keeps_its_own_tolerance(self) -> None:
        system = {"system_override_policy": {"release_strategy": "manual", "duration_min": 33,
                                              "safety_timeout_enabled": False}}
        zone = {"override_policy": {"detection_tolerance": 25}}
        cfg = resolve_zone_override_policy(zone, system)
        assert (cfg.release_strategy, cfg.duration_min, cfg.safety_timeout_enabled,
                cfg.detection_tolerance) == (S.MANUAL, 33, False, 25)
        assert resolve_zone_override_policy({}, system).release_strategy is S.MANUAL

    def test_zone_with_explicit_strategy_and_no_flag_is_isolated_from_system(self) -> None:
        system = {"system_override_policy": {"release_strategy": "manual"}}
        zone = {"override_policy": {"release_strategy": "duration", "duration_min": 77, "detection_tolerance": 20}}
        cfg = resolve_zone_override_policy(zone, system)
        assert (cfg.release_strategy, cfg.duration_min, cfg.detection_tolerance) == (S.DURATION, 77, 20)

    def test_explicit_use_system_default_flag_wins_even_with_stored_custom_values(self) -> None:
        system = {"system_override_policy": {"release_strategy": "manual"}}
        zone = {"override_policy": {"use_system_default": True, "release_strategy": "duration",
                                     "duration_min": 77, "detection_tolerance": 20}}
        cfg = resolve_zone_override_policy(zone, system)
        assert (cfg.release_strategy, cfg.duration_min, cfg.detection_tolerance) == (S.MANUAL, 120, 20)
        zone["override_policy"]["use_system_default"] = False
        assert resolve_zone_override_policy(zone, system).release_strategy is S.DURATION


class TestDayAndNightDurationSemantics:
    def test_coordinator_picks_night_duration_for_the_night_scope(self) -> None:
        assert re.search(
            r"self\._override_night_duration_min\s*\n\s*if _override_scope == \"night\"\s*\n\s*else self\._override_duration_min",
            _COORD_SRC)

    def test_duration_override_uses_the_duration_it_is_handed(self) -> None:
        det = OverrideDetector()
        kw = dict(window_id="w", smartshading_target=0, prev_state=ShadingState.OPEN,
                  tolerance=10, duration_min=720, now=T0, scope="night", release_strategy=S.DURATION)
        det.tick(observed_position=0, **kw)
        det.tick(observed_position=60, **kw)
        ov = det.get("w", T0)
        assert ov.scope == "night" and ov.expires_at == T0 + timedelta(minutes=720)

    def test_duration_renewal_extends_and_other_strategies_keep_their_boundary(self) -> None:
        for strategy, extends in ((S.DURATION, True), (S.MANUAL, False)):
            det = OverrideDetector()
            kw = dict(window_id="w", smartshading_target=0, prev_state=ShadingState.OPEN, tolerance=10,
                      duration_min=60, release_strategy=strategy, safety_timeout_enabled=True)
            det.tick(observed_position=0, now=T0, **kw)
            det.tick(observed_position=50, now=T0, **kw)
            first = det.get("w", T0).expires_at
            later = T0 + timedelta(minutes=10)
            det.tick(observed_position=90, now=later, **kw)
            assert (det.get("w", later).expires_at != first) is extends

    def test_safety_timeout_disabled_yields_no_expiry_for_open_ended_strategies(self) -> None:
        for strategy in OPEN_ENDED:
            det = OverrideDetector()
            kw = dict(window_id="w", smartshading_target=0, prev_state=ShadingState.OPEN, tolerance=10,
                      duration_min=60, release_strategy=strategy, safety_timeout_enabled=False)
            det.tick(observed_position=0, now=T0, **kw)
            det.tick(observed_position=50, now=T0, **kw)
            assert det.get("w", T0 + timedelta(days=3650)).expires_at == NO_SAFETY_TIMEOUT


class TestFixedTimeRelease:
    def test_today_if_future_else_tomorrow(self) -> None:
        berlin = ZoneInfo("Europe/Berlin")
        now = datetime(2026, 6, 15, 7, 0, tzinfo=berlin)
        assert compute_fixed_time_expiry(now=now, fixed_until=time(8, 0)).day == 15
        assert compute_fixed_time_expiry(now=now, fixed_until=time(7, 0)).day == 16
        assert compute_fixed_time_expiry(now=now, fixed_until=time(6, 0)).day == 16

    def test_dst_gap_resolves_without_raising_and_is_in_the_future(self) -> None:
        berlin = ZoneInfo("Europe/Berlin")
        now = datetime(2026, 3, 28, 23, 0, tzinfo=berlin)
        assert compute_fixed_time_expiry(now=now, fixed_until=time(2, 30)) > now


class TestRestartPersistence:
    def test_snapshot_restore_keeps_strategy_and_deadline_and_drops_stale(self) -> None:
        det = OverrideDetector()
        fresh = ManualOverride("a", 30, T0, T0 + timedelta(hours=2), "position_delta",
                               ShadingState.OPEN, 0, "night", S.FIRST_COMFORT.value)
        stale = ManualOverride("b", 30, T0 - timedelta(hours=5), T0 - timedelta(hours=3),
                               "position_delta", ShadingState.OPEN, 0)
        restored = det.restore_active_overrides([fresh.to_dict(), stale.to_dict(), {"bad": 1}], T0)
        assert [o.window_id for o in restored] == ["a"]
        got = det.get("a", T0)
        assert (got.release_strategy, got.scope, got.expires_at) == (
            S.FIRST_COMFORT.value, "night", fresh.expires_at)
        assert det.get("b", T0) is None

    def test_pre_t10_persisted_entry_is_mapped(self) -> None:
        d = _override().to_dict()
        d.pop("release_strategy")
        d["duration_mode"] = "fixed_time"
        assert ManualOverride.from_dict(d).release_strategy == "fixed_time"
        d.pop("duration_mode")
        assert ManualOverride.from_dict(d).release_strategy == "duration"


def _make_hass():
    hass = MagicMock()
    hass.data = {}
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    return hass


def _make_entry():
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}
    entry.data = {}
    entry.async_on_unload = MagicMock()
    return entry


def _make_coord(**kwargs):
    from custom_components.smartshading.models.window import WindowConfig

    coord = SmartShadingCoordinator(
        _make_hass(), _make_entry(), lifecycle_config=NightDayLifecycleConfig(id="default"), **kwargs)
    coord.windows = {"w1": WindowConfig(id="w1", name="W", zone_id="z1", azimuth=180, floor_level=0,
                                         cover_group_id="cg1")}
    coord.zones = {}
    coord.cover_groups = {"cg1": CoverGroup(id="cg1", window_id="w1", cover_ids=["cover.first", "cover.second"])}
    return coord


class _Snap:
    current_position_internal = 40


class TestManualStopHoldDurationDependency:
    """The hold of a stopped cover other than the window's first one takes its
    duration from duration_min / night_duration_min for EVERY strategy except
    fixed_time - independent of safety_timeout_enabled."""

    def _coord(self, monkeypatch, *, strategy, night, safety_timeout=True, duration=45,
               night_duration=300, fixed_until=None):
        coord = _make_coord(
            override_release_strategy=strategy, override_duration_min=duration,
            override_night_duration_min=night_duration, override_safety_timeout_enabled=safety_timeout,
            override_fixed_until=fixed_until)
        coord._lifecycle_state = LifecycleState.NIGHT if night else LifecycleState.DAY
        glb = coord._hold_stopped_cover.__func__.__globals__
        monkeypatch.setattr(glb["dt_util"], "utcnow", lambda: T0)
        return coord

    @pytest.mark.parametrize("strategy", [s for s in S if s is not S.FIXED_TIME])
    @pytest.mark.parametrize("safety_timeout", [True, False])
    def test_hold_lasts_the_scope_duration(self, monkeypatch, strategy, safety_timeout) -> None:
        day = self._coord(monkeypatch, strategy=strategy, night=False, safety_timeout=safety_timeout)
        day._hold_stopped_cover("w1", "cover.second", _Snap())
        assert day._cover_stop_holds["cover.second"].expires_at == T0 + timedelta(minutes=45)
        night = self._coord(monkeypatch, strategy=strategy, night=True, safety_timeout=safety_timeout)
        night._hold_stopped_cover("w1", "cover.second", _Snap())
        assert night._cover_stop_holds["cover.second"].expires_at == T0 + timedelta(minutes=300)

    def test_fixed_time_hold_ends_at_the_clock_time_not_the_duration(self, monkeypatch) -> None:
        coord = self._coord(monkeypatch, strategy=S.FIXED_TIME, night=False, fixed_until=time(18, 0))
        coord._hold_stopped_cover("w1", "cover.second", _Snap())
        assert coord._cover_stop_holds["cover.second"].expires_at == datetime(
            2026, 6, 15, 18, 0, tzinfo=timezone.utc)

    def test_the_first_cover_gets_no_hold(self, monkeypatch) -> None:
        coord = self._coord(monkeypatch, strategy=S.LIFECYCLE, night=False)
        coord._hold_stopped_cover("w1", "cover.first", _Snap())
        assert coord._cover_stop_holds == {}

    @pytest.mark.parametrize("strategy,category,released", [
        (S.FIRST_COMFORT, DecisionCategory.COMFORT, True),
        (S.FIRST_COMFORT, DecisionCategory.PROTECTION, False),
        (S.FIRST_PROTECTION, DecisionCategory.PROTECTION, True),
        (S.FIRST_ANY_DECISION, DecisionCategory.COMFORT, True),
        (S.LIFECYCLE, DecisionCategory.COMFORT, False),
        (S.MANUAL, DecisionCategory.PROTECTION, False),
        (S.DURATION, DecisionCategory.COMFORT, False),
    ])
    def test_holds_are_released_by_policy_only_for_matching_categories(
            self, monkeypatch, strategy, category, released) -> None:
        coord = self._coord(monkeypatch, strategy=strategy, night=False)
        coord._hold_stopped_cover("w1", "cover.second", _Snap())
        coord._release_stop_holds_by_policy("w1", category, T0 + timedelta(minutes=1))
        assert (len(coord._cover_stop_holds) == 0) is released

    def test_explicit_release_clears_holds_of_the_window(self, monkeypatch) -> None:
        coord = self._coord(monkeypatch, strategy=S.MANUAL, night=False)
        coord._hold_stopped_cover("w1", "cover.second", _Snap())
        assert coord._release_cover_stop_holds("w1") == 1
        assert coord._cover_stop_holds == {}


def _filter(**kw):
    from custom_components.smartshading.cover_control.command_filter import ExecutionCapability, ExecutionMode

    return CommandFilter().evaluate(
        target_position_internal=50, current_position_internal=0,
        execution_mode=ExecutionMode.AUTOMATIC, is_cover_available=True, state_guard_allowed=True,
        execution_capability=ExecutionCapability(), **kw)


class TestSafetyAndContactInteraction:
    def test_safety_state_branch_clears_the_override_before_the_command_filter_sees_it(self) -> None:
        branch = _COORD_SRC.index("if tier_decision.shading_state in SAFETY_SHADING_STATES:")
        clear = _COORD_SRC.index("self._override_detector.clear(window_id)", branch)
        later_read = _COORD_SRC.index("current_override = self._override_detector.get(window_id, now)", branch)
        assert branch < clear < later_read

    def test_command_filter_blocks_while_an_override_is_held(self) -> None:
        for is_safety in (False, True):
            res = _filter(is_safety=is_safety, is_manual_override=True)
            assert not res.allowed and res.blocked_reason == BLOCKED_MANUAL_OVERRIDE

    def test_without_an_override_safety_is_not_blocked(self) -> None:
        assert _filter(is_safety=True, is_manual_override=False).allowed

    def test_night_candidates_are_lifecycle_and_blocked_by_the_policy_whatever_the_flags(self) -> None:
        for by, state in (("NightEvaluator", ShadingState.NIGHT_CLOSED),
                          ("NightContactVent", ShadingState.NIGHT_VENT),
                          ("NightContactCatchUp", ShadingState.NIGHT_CLOSED),
                          ("MorningEvaluator", ShadingState.OPEN)):
            out = evaluate_manual_override_policy(
                active_override=_override(), candidate=_candidate(DecisionCategory.LIFECYCLE, by, state),
                allow_comfort=True, allow_protection=True, release_strategy=S.FIRST_ANY_DECISION)
            assert out.shading_state is ShadingState.MANUAL_OVERRIDE

    def test_night_contact_hold_state_machine_has_no_override_input(self) -> None:
        import inspect
        assert not any("override" in p for p in inspect.signature(NightContactHold.evaluate).parameters)

    def test_night_contact_option_a_blocks_then_catches_up_on_close(self) -> None:
        hold = NightContactHold()
        kw = dict(contact_unknown=False, night_active=True, night_block_enabled=True,
                  night_lift_enabled=False, night_decision_pending=True)
        assert hold.evaluate(contact_open=True, **kw) is NightContactAction.BLOCK
        assert hold.evaluate(contact_open=False, **kw) is NightContactAction.CATCH_UP

    def test_night_contact_option_b_vents_and_returns(self) -> None:
        hold = NightContactHold()
        kw = dict(contact_unknown=False, night_active=True, night_block_enabled=False, night_lift_enabled=True)
        assert hold.evaluate(contact_open=False, night_decision_pending=True, **kw) is NightContactAction.PASS_THROUGH
        assert hold.evaluate(contact_open=True, night_decision_pending=False, **kw) is NightContactAction.HOLD_NIGHT_VENT
        assert hold.evaluate(contact_open=False, night_decision_pending=False, **kw) is NightContactAction.RETURN_TO_NIGHT

    def test_the_filter_gate_uses_the_detectors_override_not_the_decision_state(self) -> None:
        assert "is_manual_override=current_override is not None" in _COORD_SRC
