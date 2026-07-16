"""v1.1.1 field fix: support-event trace consistency.

Real-world report: the support export showed a critical event like

    decided_by: TierOrchestrator:fallback
    shading_state: strong_shade
    target_ha: 100

target_ha=100 (fully open) is semantically inconsistent with shading_state=
strong_shade. Root cause: coordinator._record_support_event() read
`resolved_state` from `_WindowComputeState.new_state`, which StateGuard may
still hold at the PRIOR state (STRONG_SHADE) for hysteresis/outcome
bookkeeping (`is_locked()`), while the ACTUAL dispatch that cycle is driven
by `tier_decision.target_position` (here: TierOrchestrator's fallback OPEN,
internal 0 -> HA 100) under the separate, weaker minimum_action_interval
check — so a real "OPEN, fully retracted" dispatch could be logged with a
stale "strong_shade" label.

Fix: when a command was actually sent this cycle, the trace now reports
`tier_decided_state` (the evaluator decision that drove the dispatch)
instead of the possibly-stale `new_state`. When no command was sent,
`new_state` (the guard-held label) is still correct and unchanged.
"""
from __future__ import annotations

import sys
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

UTC = timezone.utc


def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _DUCStub:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.config_entry = config_entry


class _CEStub:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator


class _StoreStub:
    def __init__(self, hass, version, key) -> None:
        self.key = key

    async def async_load(self):
        return None

    async def async_save(self, data) -> None:
        return None

    async def async_remove(self) -> None:
        return None


for _name, _module in {
    "homeassistant": _stub("homeassistant"),
    "homeassistant.components": _stub("homeassistant.components"),
    "homeassistant.components.cover": _stub(
        "homeassistant.components.cover",
        CoverEntityFeature=type("CoverEntityFeature", (), {
            "SET_POSITION": 1, "SET_TILT_POSITION": 2, "OPEN": 4, "CLOSE": 8, "STOP": 16}),
    ),
    "homeassistant.config_entries": _stub("homeassistant.config_entries", ConfigEntry=object),
    "homeassistant.core": _stub("homeassistant.core", HomeAssistant=object, Event=object,
                                callback=lambda fn: fn),
    "homeassistant.helpers": _stub("homeassistant.helpers"),
    "homeassistant.helpers.entity_registry": _stub(
        "homeassistant.helpers.entity_registry", async_get=lambda *a, **k: None),
    "homeassistant.helpers.event": _stub(
        "homeassistant.helpers.event",
        async_track_state_change_event=lambda hass, entity_id, action: (lambda: None),
        async_call_later=lambda hass, delay, action: (lambda: None)),
    "homeassistant.util": _stub("homeassistant.util"),
}.items():
    sys.modules.setdefault(_name, _module)

if sys.modules.get("homeassistant.util.dt") is None or not hasattr(
    sys.modules.get("homeassistant.util.dt"), "utcnow"
):
    import datetime as _dt
    sys.modules["homeassistant.util.dt"] = _stub(
        "homeassistant.util.dt",
        utcnow=lambda: _dt.datetime.now(_dt.timezone.utc),
        DEFAULT_TIME_ZONE=_dt.timezone.utc,
    )

sys.modules["homeassistant.helpers.update_coordinator"] = _stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=_DUCStub, CoordinatorEntity=_CEStub)
sys.modules["homeassistant.helpers.storage"] = _stub(
    "homeassistant.helpers.storage", Store=_StoreStub)

sys.modules.pop("custom_components.smartshading.coordinator", None)

from custom_components.smartshading.coordinator import (  # noqa: E402
    SmartShadingCoordinator,
    _WindowComputeState,
)
from custom_components.smartshading.models.zone import ZoneConfig  # noqa: E402
from custom_components.smartshading.state_machine.states import ShadingState  # noqa: E402
from custom_components.smartshading.cover_control.command_filter import ExecutionMode  # noqa: E402
from custom_components.smartshading.models.decision_provenance import DispatchProvenance  # noqa: E402


class _FakeEntry:
    def __init__(self) -> None:
        self.entry_id = "entry1"
        self.options: dict[str, Any] = {}
        self.data: dict[str, Any] = {}


class _FakeHass:
    def __init__(self) -> None:
        self.states = types.SimpleNamespace(get=lambda *a, **k: None)
        self.config_entries = types.SimpleNamespace(async_update_entry=lambda *a, **k: None)


def _window(wid="w1"):
    return types.SimpleNamespace(id=wid, zone_id="z1", azimuth=270.0, cover_group_id="cg1")


def _coord() -> SmartShadingCoordinator:
    c = SmartShadingCoordinator(
        _FakeHass(), _FakeEntry(), zones={"z1": ZoneConfig(id="z1", name="Zone")},
    )
    c.windows = {"w1": _window("w1")}
    return c


def _compute_state(
    *, new_state: ShadingState, tier_decided_state: ShadingState, tier_decided_by: str,
    exec_ha: int,
) -> _WindowComputeState:
    """A window state where StateGuard held new_state at a PRIOR state while
    the actual tier decision (and dispatch) for this cycle is different —
    exactly the guard-held vs. tier-decided split that caused the mismatch."""
    return _WindowComputeState(
        window=_window("w1"),
        zone=ZoneConfig(id="z1", name="Zone"),
        obs_enabled=False,
        active_control_enabled=True,
        new_state=new_state,
        exec_entity_id="cover.w1",
        exec_cap=types.SimpleNamespace(invert_position=False),
        exec_snapshot=None,
        exec_mode=ExecutionMode.AUTOMATIC,
        is_safety=False,
        exec_target_internal=100 - exec_ha,
        exec_filter_result=types.SimpleNamespace(
            target_position_ha=exec_ha, allowed=True, blocked_reason=None),
        tier_decided_by=tier_decided_by,
        tier_decided_state=tier_decided_state,
        is_override_active=False,
        cover_available=True,
    )


def _dispatch_sent() -> DispatchProvenance:
    return DispatchProvenance(
        dispatch_allowed=True, dispatch_filter_reason=None,
        dispatch_attempted=True, dispatch_succeeded=True,
        dispatch_status="SENT", requested_target_ha=100,
        transport_inversion_applied=False,
    )


def _dispatch_not_sent() -> DispatchProvenance:
    return DispatchProvenance(
        dispatch_allowed=True, dispatch_filter_reason="command_filter_suppressed",
        dispatch_attempted=True, dispatch_succeeded=False,
        dispatch_status="BLOCKED", requested_target_ha=100,
        transport_inversion_applied=False,
    )


_NOW = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)


class TestSupportEventTraceConsistency:
    def test_dispatched_fallback_open_reports_open_not_stale_strong(self):
        # Guard held new_state at STRONG_SHADE (hysteresis bookkeeping), but
        # TierOrchestrator's fallback OPEN actually dispatched this cycle
        # (target_ha=100). The trace must report "open", not "strong_shade".
        c = _coord()
        s = _compute_state(
            new_state=ShadingState.STRONG_SHADE,
            tier_decided_state=ShadingState.OPEN,
            tier_decided_by="TierOrchestrator:fallback",
            exec_ha=100,
        )
        c._record_support_event("w1", s, _dispatch_sent(), _NOW)
        evt = c._support_critical_events[-1]
        assert evt["target_ha"] == 100
        assert evt["resolved_state"] == "open"
        assert evt["resolved_state"] != "strong_shade"

    def test_no_dispatch_still_reports_guard_held_new_state(self):
        # No command was sent (e.g. command_filter blocked it) — the trace
        # must still report the guard-held new_state, unchanged from before.
        c = _coord()
        s = _compute_state(
            new_state=ShadingState.STRONG_SHADE,
            tier_decided_state=ShadingState.OPEN,
            tier_decided_by="TierOrchestrator:fallback",
            exec_ha=100,
        )
        c._record_support_event("w1", s, _dispatch_not_sent(), _NOW)
        evt = c._support_critical_events[-1]
        assert evt["resolved_state"] == "strong_shade"

    def test_dispatched_state_matches_new_state_when_consistent(self):
        # Ordinary case: guard did not hold anything back, tier decision and
        # new_state agree — behavior is unchanged either way.
        c = _coord()
        s = _compute_state(
            new_state=ShadingState.NORMAL_SHADE,
            tier_decided_state=ShadingState.NORMAL_SHADE,
            tier_decided_by="GlareEvaluator",
            exec_ha=30,
        )
        c._record_support_event("w1", s, _dispatch_sent(), _NOW)
        evt = c._support_critical_events[-1]
        assert evt["resolved_state"] == "normal_shade"
        assert evt["target_ha"] == 30


# ===========================================================================
# F31a field fix — target_ha must always use the real resolved/held target
# (CommandFilterResult.target_position_ha), never the static configured
# "normal shade" baseline (normal_cfg_ha_for_prov), regardless of
# command_sent. Real-world report: a manual_override-blocked event showed
# target_ha=30 (the static baseline) while the Current Snapshot correctly
# showed actual/target=100 (the real held override position).
# ===========================================================================

class TestF31aTargetHaUsesResolvedFilterTarget:
    def test_blocked_manual_override_uses_filter_target_not_static_baseline(self):
        c = _coord()
        s = _compute_state(
            new_state=ShadingState.MANUAL_OVERRIDE,
            tier_decided_state=ShadingState.MANUAL_OVERRIDE,
            tier_decided_by="ManualOverrideEvaluator",
            exec_ha=100,  # the real held override target
        )
        s = replace(s, normal_cfg_ha_for_prov=30)  # unrelated static config baseline
        c._record_support_event("w1", s, _dispatch_not_sent(), _NOW)
        evt = c._support_critical_events[-1]
        assert evt["target_ha"] == 100
        assert evt["target_ha"] != 30

    def test_blocked_comfort_position_hold_uses_filter_target_not_static_baseline(self):
        c = _coord()
        s = _compute_state(
            new_state=ShadingState.LIGHT_SHADE,
            tier_decided_state=ShadingState.LIGHT_SHADE,
            tier_decided_by="GlareEvaluator",
            exec_ha=50,
        )
        s = replace(s, normal_cfg_ha_for_prov=30)
        dp = types.SimpleNamespace(
            dispatch_allowed=False, dispatch_filter_reason="comfort_position_hold",
            dispatch_attempted=True, dispatch_succeeded=False,
            dispatch_status="BLOCKED", requested_target_ha=50,
            transport_inversion_applied=False,
        )
        c._record_support_event("w1", s, dp, _NOW)
        evt = c._support_critical_events[-1]
        assert evt["target_ha"] == 50
        assert evt["target_ha"] != 30

    def test_dispatched_event_unaffected_by_fix(self):
        # Regression: the command_sent path must remain exactly as before.
        c = _coord()
        s = _compute_state(
            new_state=ShadingState.NORMAL_SHADE,
            tier_decided_state=ShadingState.NORMAL_SHADE,
            tier_decided_by="GlareEvaluator",
            exec_ha=30,
        )
        s = replace(s, normal_cfg_ha_for_prov=80)
        c._record_support_event("w1", s, _dispatch_sent(), _NOW)
        evt = c._support_critical_events[-1]
        assert evt["target_ha"] == 30


class TestF31aRealDispatchTimestamp:
    def test_dispatch_sent_at_utc_recorded_and_distinct_from_cycle_ts(self):
        c = _coord()
        s = _compute_state(
            new_state=ShadingState.NORMAL_SHADE,
            tier_decided_state=ShadingState.NORMAL_SHADE,
            tier_decided_by="GlareEvaluator",
            exec_ha=30,
        )
        # Real post-throttle timestamp, several seconds after the shared
        # per-cycle decision "now" — as would happen after a global-dispatch
        # throttle wait (F32).
        _sent_at = _NOW + timedelta(seconds=6)
        last_exec_result = types.SimpleNamespace(sent_at_utc=_sent_at)
        c._record_support_event(
            "w1", s, _dispatch_sent(), _NOW, last_exec_result=last_exec_result)
        evt = c._support_critical_events[-1]
        assert evt["dispatch_sent_at_utc"] == _sent_at.isoformat()
        assert evt["dispatch_sent_at_utc"] != evt["ts"]

    def test_dispatch_sent_at_utc_none_when_not_dispatched(self):
        c = _coord()
        s = _compute_state(
            new_state=ShadingState.MANUAL_OVERRIDE,
            tier_decided_state=ShadingState.MANUAL_OVERRIDE,
            tier_decided_by="ManualOverrideEvaluator",
            exec_ha=100,
        )
        last_exec_result = types.SimpleNamespace(
            sent_at_utc=_NOW + timedelta(seconds=6))
        c._record_support_event(
            "w1", s, _dispatch_not_sent(), _NOW, last_exec_result=last_exec_result)
        evt = c._support_critical_events[-1]
        assert evt["dispatch_sent_at_utc"] is None

    def test_no_last_exec_result_does_not_crash(self):
        c = _coord()
        s = _compute_state(
            new_state=ShadingState.NORMAL_SHADE,
            tier_decided_state=ShadingState.NORMAL_SHADE,
            tier_decided_by="GlareEvaluator",
            exec_ha=30,
        )
        c._record_support_event("w1", s, _dispatch_sent(), _NOW)  # no kwargs
        evt = c._support_critical_events[-1]
        assert evt["dispatch_sent_at_utc"] is None


class TestF31aThrottleContextInSupportEvent:
    def test_throttle_context_fields_recorded(self):
        c = _coord()
        s = _compute_state(
            new_state=ShadingState.NORMAL_SHADE,
            tier_decided_state=ShadingState.NORMAL_SHADE,
            tier_decided_by="GlareEvaluator",
            exec_ha=30,
        )
        disp_ctx = {
            "global_wait_required": True,
            "planned_global_interval_wait_ms": 2000,
            "actual_global_interval_wait_ms": 2010,
            "global_wait_overrun_ms": 10,
            "required_global_interval_ms": 2000,
        }
        c._record_support_event("w1", s, _dispatch_sent(), _NOW, disp_ctx=disp_ctx)
        evt = c._support_critical_events[-1]
        assert evt["global_wait_required"] is True
        assert evt["planned_global_interval_wait_ms"] == 2000
        assert evt["actual_global_interval_wait_ms"] == 2010
        assert evt["global_wait_overrun_ms"] == 10
        assert evt["required_global_interval_ms"] == 2000

    def test_no_disp_ctx_yields_none_fields_not_error(self):
        c = _coord()
        s = _compute_state(
            new_state=ShadingState.NORMAL_SHADE,
            tier_decided_state=ShadingState.NORMAL_SHADE,
            tier_decided_by="GlareEvaluator",
            exec_ha=30,
        )
        c._record_support_event("w1", s, _dispatch_sent(), _NOW)  # no disp_ctx
        evt = c._support_critical_events[-1]
        assert evt["global_wait_required"] is None
        assert evt["planned_global_interval_wait_ms"] is None
        assert evt["actual_global_interval_wait_ms"] is None
        assert evt["global_wait_overrun_ms"] is None
        assert evt["required_global_interval_ms"] is None
