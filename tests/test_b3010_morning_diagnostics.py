"""B3-010 correction — morning-decision diagnostics.

Proves the required morning-decision facts are surfaced through the EXISTING
_record_decision_trace() authority-map structure (lifecycle_authority), not a
new diagnostics structure: effective morning position (HA convention), the
weekday/weekend/same_every_day schedule source, the resolved+clamped
effective trigger time, the active-month check result, and — when Morning
genuinely competed but a stronger candidate won the cycle instead — which
decider actually won (morning_superseded_by).

Values are computed from a REAL decision run through the actual
LifecycleEngine -> build_window_decision_input -> TierOrchestrator chain
(mirroring coordinator.py's own per-cycle sequence, including
self.lifecycle_engine.active_profile()/schedule_diagnostics()), not
hand-picked field values that could silently drift out of sync with what
production actually computes.

Same self-contained real-SmartShadingCoordinator-with-HA-stubs technique as
test_t22_phase5c_safety_preemption.py / test_t22_phase5b_concurrent_cycle_guard.py.
"""
from __future__ import annotations

import sys
import types
from dataclasses import replace
from datetime import datetime, time, timezone
from typing import Any
from unittest.mock import MagicMock


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

    async def async_shutdown(self) -> None:
        pass


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
from custom_components.smartshading.coordinator import (  # noqa: E402
    SmartShadingCoordinator,
    _WindowComputeState,
)
from custom_components.smartshading.cover_control.command_filter import (  # noqa: E402
    CommandFilterResult,
    ExecutionMode,
)
from custom_components.smartshading.cover_control.cover_capabilities import CoverCapability  # noqa: E402
from custom_components.smartshading.cover_control.cover_entity_snapshot import (  # noqa: E402
    build_cover_entity_snapshot,
)
from custom_components.smartshading.cover_control.execution_result import (  # noqa: E402
    build_sent_result,
)
from custom_components.smartshading.cover_control.shading_group_harmonizer import (  # noqa: E402
    HarmonizationResult,
)
from custom_components.smartshading.models.comfort import ComfortConfig  # noqa: E402
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults  # noqa: E402
from custom_components.smartshading.models.decision_provenance import DispatchProvenance  # noqa: E402
from custom_components.smartshading.cover_control.execution_plan import (  # noqa: E402
    CoverIntent, CoverCommandType,
)
from custom_components.smartshading.models.lifecycle import (  # noqa: E402
    LifecycleScheduleMode, LifecycleState, MorningTrigger, NightDayLifecycleConfig, NightTrigger,
)
from custom_components.smartshading.models.window import WindowConfig  # noqa: E402
from custom_components.smartshading.models.window_decision_input import build_window_decision_input  # noqa: E402
from custom_components.smartshading.models.zone import ZoneConfig  # noqa: E402
from custom_components.smartshading.state_machine.states import ShadingState  # noqa: E402
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator  # noqa: E402

_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


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


def _make_coord(lifecycle_config: NightDayLifecycleConfig) -> SmartShadingCoordinator:
    hass = _make_hass()
    entry = _make_entry()
    coord = SmartShadingCoordinator(hass, entry, lifecycle_config=lifecycle_config)
    coord.windows = {}
    coord.zones = {}
    coord.cover_groups = {}
    coord._startup_cycles_remaining = 0
    return coord


def _window(window_id: str, zone_id: str) -> WindowConfig:
    return WindowConfig(id=window_id, name=window_id, zone_id=zone_id, azimuth=180, floor_level=0, cover_group_id=f"cg_{window_id}")


def _harm_for(*states):
    return {
        s.window.id: HarmonizationResult(harmonized=False, final_target_position_ha=None, pre_harmonization_target_position_ha=None)
        for s in states
    }


def _sent_exec_result(target_position_ha: int):
    intent = CoverIntent(
        cover_entity_id="cover.w1", command_type=CoverCommandType.MOVE_TO_POSITION,
        target_position_internal=100 - target_position_ha, target_position_ha=target_position_ha,
        target_tilt=None, is_safety=False, execution_mode=ExecutionMode.AUTOMATIC.value,
        allowed=True, blocked_reason=None, decided_by="TestEvaluator", computed_at=_NOW,
    )
    return build_sent_result(intent, sent_at_utc=_NOW, reason="dispatched")


def _run_real_cycle_and_record(
    coord: SmartShadingCoordinator, now: datetime, window: WindowConfig, zone: ZoneConfig,
    *, previous_lifecycle_state: LifecycleState,
    outdoor_temp_c: float | None = None, is_in_solar_sector: bool = False,
    comfort_config: ComfortConfig = ComfortConfig(
        heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False,
    ),
) -> dict:
    """Runs coordinator.py's exact real per-cycle diagnostics-field
    computation (LifecycleEngine.get_lifecycle_state ->
    LifecycleEngine.active_profile/schedule_diagnostics ->
    build_window_decision_input -> TierOrchestrator -> the same
    _morning_applies_this_cycle gating coordinator.py itself uses), then
    feeds the resulting REAL _WindowComputeState into the REAL
    _record_decision_trace() and returns the recorded lifecycle_authority
    block."""
    state = coord.lifecycle_engine.get_lifecycle_state(
        now, sun_elevation_deg=None, config=coord._lifecycle_config,
        previous_lifecycle_state=previous_lifecycle_state,
    )
    profile = coord.lifecycle_engine.active_profile(now, coord._lifecycle_config)
    schedule_diag = coord.lifecycle_engine.schedule_diagnostics(now, coord._lifecycle_config)
    effective_config = replace(
        coord._lifecycle_config,
        night_position=profile.night_position, morning_position=profile.morning_position,
    )
    wdi = build_window_decision_input(
        window=window, zone=zone, global_defaults=GlobalDefaults(),
        shade_position_defaults=ShadePositionDefaults(), lifecycle_config=effective_config,
        lifecycle_state=state, absence_active=False, current_shading_state=ShadingState.NIGHT_CLOSED,
        outdoor_temp_c=outdoor_temp_c, indoor_temp_c=(35.0 if outdoor_temp_c else None), exposure=None,
        is_in_solar_sector=is_in_solar_sector, comfort_config=comfort_config,
    )
    decision = TierOrchestrator().evaluate_window(wdi)

    # Exactly coordinator.py's own gating condition (coordinator.py ~5456-5468).
    morning_applies_this_cycle = (
        wdi.lifecycle_state is LifecycleState.MORNING
        and wdi.effective_behavior.morning_position is not None
    )
    from custom_components.smartshading.cover_control.position_semantics import to_ha_position
    morning_effective_position_ha = (
        to_ha_position(wdi.effective_behavior.morning_position) if morning_applies_this_cycle else None
    )
    morning_superseded_by = (
        decision.decided_by if morning_applies_this_cycle and decision.decided_by != "MorningEvaluator" else None
    )

    entity_id = "cover.w1"
    target_ha = 100 - decision.target_position if decision.target_position is not None else 100
    s = _WindowComputeState(
        window=window, zone=zone, obs_enabled=False, active_control_enabled=True,
        new_state=decision.shading_state, exec_entity_id=entity_id,
        exec_cap=CoverCapability(entity_id=entity_id, supports_position=True, supports_tilt=False, supports_open_close_only=False),
        exec_snapshot=build_cover_entity_snapshot(entity_id=entity_id, state="open", attributes={"current_position": 0}),
        exec_mode=ExecutionMode.AUTOMATIC, is_safety=False,
        exec_target_internal=decision.target_position,
        exec_filter_result=CommandFilterResult(
            allowed=True, blocked_reason=None,
            target_position_internal=decision.target_position, target_position_ha=target_ha,
            execution_mode=ExecutionMode.AUTOMATIC.value, is_safety=False,
        ),
        tier_decided_by=decision.decided_by, is_override_active=False, cover_available=True,
        decision_id="d1",
        morning_effective_position_ha=morning_effective_position_ha,
        morning_schedule_source=(schedule_diag.source if morning_applies_this_cycle else None),
        morning_month_active=(schedule_diag.month_active if morning_applies_this_cycle else None),
        morning_effective_trigger_time=(
            profile.morning_fixed_time.isoformat()
            if morning_applies_this_cycle and profile.morning_fixed_time is not None else None
        ),
        morning_superseded_by=morning_superseded_by,
    )

    exec_result = _sent_exec_result(target_ha)
    dispatch_prov = DispatchProvenance(dispatch_allowed=True, dispatch_filter_reason=None, dispatch_attempted=True, dispatch_succeeded=True)
    harm = _harm_for(s)[window.id]
    coord._record_decision_trace(window.id, s, harm, dispatch_prov, exec_result, now)
    snap = coord.decision_trace_snapshot()
    return snap[zone.id]["records"][-1]["authorities"]["lifecycle_authority"]


def _config(
    *, morning_position_ha: int = 70, morning_enabled: bool = True,
    schedule_mode=LifecycleScheduleMode.SAME_EVERY_DAY, active_months=None,
) -> NightDayLifecycleConfig:
    return NightDayLifecycleConfig(
        id="default", schedule_mode=schedule_mode,
        night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(22, 0), night_position=0,
        morning_trigger=MorningTrigger.FIXED_TIME, morning_fixed_time=time(6, 30),
        morning_position=morning_position_ha, morning_enabled=morning_enabled,
        # WEEKDAY_WEEKEND selects weekday_morning_position/weekend_morning_position
        # instead of the shared morning_position field -- keep them consistent
        # with morning_position_ha so a WEEKDAY_WEEKEND test still exercises
        # the SAME configured HA value.
        weekday_morning_position=morning_position_ha, weekend_morning_position=morning_position_ha,
        active_months=active_months,
    )


class TestMorningDiagnosticsFromARealDecision:
    def test_morning_transition_winner_carries_its_own_facts(self) -> None:
        coord = _make_coord(_config(morning_position_ha=70, schedule_mode=LifecycleScheduleMode.WEEKDAY_WEEKEND))
        window, zone = _window("w1", "z1"), ZoneConfig(id="z1", name="Zone")
        now = datetime(2026, 6, 15, 6, 30, 0, tzinfo=timezone.utc)  # Monday, at the trigger
        lc = _run_real_cycle_and_record(coord, now, window, zone, previous_lifecycle_state=LifecycleState.NIGHT)
        assert lc["morning_effective_position_ha"] == 70  # round-trips through internal back to the configured HA value
        assert lc["morning_schedule_source"] == "weekday"
        assert lc["morning_month_active"] is True
        assert lc["morning_effective_trigger_time"] == "06:30:00"
        assert lc["morning_superseded_by"] is None

    def test_stronger_heat_candidate_records_who_superseded_morning(self) -> None:
        coord = _make_coord(_config(morning_position_ha=70))
        window, zone = _window("w1", "z1"), ZoneConfig(id="z1", name="Zone")
        now = datetime(2026, 6, 15, 6, 30, 0, tzinfo=timezone.utc)
        hot = ComfortConfig(
            heat_protection_enabled=True, heat_protection_outdoor_temp_c=25.0,
            heat_protection_indoor_temp_c=24.0, heat_protection_hysteresis_c=1.0,
        )
        lc = _run_real_cycle_and_record(
            coord, now, window, zone, previous_lifecycle_state=LifecycleState.NIGHT,
            outdoor_temp_c=30.0, is_in_solar_sector=True, comfort_config=hot,
        )
        assert lc["morning_effective_position_ha"] == 70, (
            "the morning candidate this cycle stays visible in diagnostics "
            "even though it did not win, so the arbitration outcome is provable"
        )
        assert lc["morning_superseded_by"] == "HeatEvaluator", (
            "must name the actual winning decider — proves *why* a stronger "
            "sun/heat/safety candidate won instead of the configured morning position"
        )

    def test_day_cycle_has_no_morning_candidate_all_fields_none(self) -> None:
        """B3-010 correction: MorningEvaluator never fires for DAY, so a
        DAY cycle's diagnostics fields are None regardless of whether
        morning_position is configured — never merely "morning_position is
        configured" (the exact contradiction the correction ticket flagged
        in the previous version of this test)."""
        coord = _make_coord(_config(morning_position_ha=70))
        window, zone = _window("w1", "z1"), ZoneConfig(id="z1", name="Zone")
        now = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)  # well past the trigger
        lc = _run_real_cycle_and_record(coord, now, window, zone, previous_lifecycle_state=LifecycleState.DAY)
        assert lc["morning_effective_position_ha"] is None
        assert lc["morning_schedule_source"] is None
        assert lc["morning_month_active"] is None
        assert lc["morning_effective_trigger_time"] is None
        assert lc["morning_superseded_by"] is None

    def test_month_inactive_never_reaches_morning_fields_are_none(self) -> None:
        """Outside active_months, LifecycleEngine never even returns MORNING
        (proven directly by test_morning_position_resolution.py's
        test_get_lifecycle_state_never_reaches_morning_outside_active_months)
        — so this cycle's diagnostics fields are None here too, never a
        contradictory "Morning won AND month_active=False"."""
        coord = _make_coord(_config(morning_position_ha=70, active_months=(12, 1, 2)))
        window, zone = _window("w1", "z1"), ZoneConfig(id="z1", name="Zone")
        now = datetime(2026, 6, 15, 6, 30, 0, tzinfo=timezone.utc)  # June, outside winter-only months
        lc = _run_real_cycle_and_record(coord, now, window, zone, previous_lifecycle_state=LifecycleState.NIGHT)
        assert lc["morning_effective_position_ha"] is None
        assert lc["morning_month_active"] is None
        assert lc["morning_superseded_by"] is None

    def test_no_morning_position_configured_all_fields_none(self) -> None:
        coord = _make_coord(_config(morning_enabled=False))
        window, zone = _window("w1", "z1"), ZoneConfig(id="z1", name="Zone")
        now = datetime(2026, 6, 15, 6, 30, 0, tzinfo=timezone.utc)
        lc = _run_real_cycle_and_record(coord, now, window, zone, previous_lifecycle_state=LifecycleState.NIGHT)
        assert lc["morning_effective_position_ha"] is None
        assert lc["morning_superseded_by"] is None


class TestScheduleDiagnosticsPureFunction:
    """LifecycleEngine.schedule_diagnostics() — the source of the source/
    month_active facts asserted above, tested directly against the real
    schedule-selection rules it mirrors (_active_profile())."""

    def test_same_every_day_source(self) -> None:
        from custom_components.smartshading.engines.lifecycle_engine import LifecycleEngine
        cfg = NightDayLifecycleConfig(id="default", schedule_mode=LifecycleScheduleMode.SAME_EVERY_DAY)
        diag = LifecycleEngine().schedule_diagnostics(datetime(2026, 6, 15, 7, 0), cfg)
        assert diag.source == "same_every_day"
        assert diag.month_active is True

    def test_weekday_weekend_selects_by_local_weekday(self) -> None:
        from custom_components.smartshading.engines.lifecycle_engine import LifecycleEngine
        cfg = NightDayLifecycleConfig(id="default", schedule_mode=LifecycleScheduleMode.WEEKDAY_WEEKEND)
        # 2026-06-15 is a Monday.
        weekday_diag = LifecycleEngine().schedule_diagnostics(datetime(2026, 6, 15, 7, 0), cfg)
        assert weekday_diag.source == "weekday"
        # 2026-06-20 is a Saturday.
        weekend_diag = LifecycleEngine().schedule_diagnostics(datetime(2026, 6, 20, 7, 0), cfg)
        assert weekend_diag.source == "weekend"

    def test_active_months_gate_is_reflected(self) -> None:
        from custom_components.smartshading.engines.lifecycle_engine import LifecycleEngine
        cfg = NightDayLifecycleConfig(id="default", active_months=frozenset({12, 1, 2}))
        diag = LifecycleEngine().schedule_diagnostics(datetime(2026, 6, 15, 7, 0), cfg)
        assert diag.month_active is False
        diag_dec = LifecycleEngine().schedule_diagnostics(datetime(2026, 12, 15, 7, 0), cfg)
        assert diag_dec.month_active is True


class TestSunEventTimeThreadedIntoDiagnostics:
    """B3-010 correction: coordinator.py's active_profile() call for the
    diagnostics trigger-time field must resolve the SAME sun-event time the
    real cycle's night/morning decision actually used — proven here by
    confirming the coordinator's own call site now passes sun_event_times
    through (structural check on the exact call, matching this file's other
    tests' "prove it against production, not a re-implementation" approach)."""

    def test_active_profile_call_for_diagnostics_passes_sun_event_times(self) -> None:
        import inspect
        from custom_components.smartshading import coordinator as _coord_mod
        source = inspect.getsource(_coord_mod)
        assert (
            "_active_lc_profile = self.lifecycle_engine.active_profile(\n"
            "                local_now, self._lifecycle_config, _sun_event_times\n"
            "            )"
        ) in source, (
            "the diagnostics-facing active_profile() call must thread the "
            "same _sun_event_times get_lifecycle_state() used this cycle, "
            "or a configured sun-event morning trigger would display the "
            "wrong effective time"
        )
