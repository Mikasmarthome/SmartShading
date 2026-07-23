"""prov["manual_override"] block coverage for the T10 release-strategy
fields (v1.2.0-beta.1) — engines/support_export.py build_support_export_v3().

Uses a minimal duck-typed coordinator (same technique as
test_v11_phase4c_persistent_stores.py's `_Coord`) rather than the full HA-
stubbed SmartShadingCoordinator — build_input_provenance()/_call() are all
getattr-guarded and fail open, so a light fixture is sufficient to exercise
just the manual_override block.

Coverage:
  SUP-01  active=False / diag=None -> all new fields present and None/False.
  SUP-02  An active override surfaces release_strategy, waiting_on (derived
          plain-language text), started_at, and expires_at together.
  SUP-03  waiting_on is None for an unrecognized/legacy release_strategy
          value (defensive: never raises on unknown input).
  SUP-04  release_reason ("timeout"/"lifecycle_transition"/"safety") is
          still surfaced unchanged alongside the new fields (regression:
          T10 must not have dropped the T7-era field).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.smartshading.engines.support_export import build_support_export_v3
from custom_components.smartshading.models.execution_diagnostics import WindowExecutionDiagnostics
from custom_components.smartshading.models.window import WindowConfig

_NOW = datetime(2026, 6, 15, 14, 0, 0, tzinfo=timezone.utc)


class _Coord:
    """Minimal duck-typed coordinator — see test_v11_phase4c_persistent_stores.py."""

    def __init__(self, *, diag=None):
        self.zones = {"z1": object()}
        self.windows = {"w1": WindowConfig(
            id="w1", name="Window 1", zone_id="z1", azimuth=180, floor_level=0, cover_group_id="cg1",
        )}
        self.cover_groups = {}
        self.config_entry = type("CE", (), {"entry_id": "eid1"})()
        self._adoption_history = []
        self._strategy_adoption_history = []
        self._adoptions_active = {}
        self._strategy_adoptions_active = {}
        self._pending_outcomes = type("PO", (), {"all_pending": lambda s: []})()
        self._support_critical_events = []
        self._research_daily_buckets = {}
        self._ring_records = []
        self.data = type("Data", (), {"execution_diagnostics": {"w1": diag} if diag is not None else {}})()

    def decision_trace_snapshot(self):
        return {}

    def get_decisions(self, _wid):
        return []

    def get_transitions(self, _wid):
        return []

    def get_overrides(self, _wid):
        return []

    def get_snapshots(self, _wid):
        return []

    def get_outcomes(self, _wid):
        return []

    def storage_diagnostics(self):
        return {}


def _diag(**kwargs) -> WindowExecutionDiagnostics:
    """Build a WindowExecutionDiagnostics with sensible defaults (same
    baseline as test_phase_9g8_global_dispatch_throttle.py's _diag())."""
    defaults = dict(
        learning_enabled=True,
        active_control_enabled=True,
        execution_mode="automatic",
        cover_entity_id="cover.test",
        cover_available=True,
        actual_position_ha=20,
        actual_position_internal=80,
        assumed_position_internal=0,
        has_position_feedback=True,
        tier_decided_by="SolarEvaluator",
        target_position_internal=80,
        target_position_ha=20,
        is_safety=False,
        command_allowed=True,
        command_blocked_reason=None,
        last_command_status="sent",
        last_command_sent_at=_NOW,
        service_call_sent=True,
        service_call_failed=False,
        execution_error=None,
        safety_result_failed=False,
        dispatch_suppressed_reason=None,
        dispatch_throttled=False,
        throttle_wait_ms=None,
    )
    defaults.update(kwargs)
    return WindowExecutionDiagnostics(**defaults)


def _mo_block(diag=None) -> dict:
    coord = _Coord(diag=diag)
    export = build_support_export_v3(coord, now=_NOW)
    wref = next(iter(export["inputs"].keys()))
    return export["inputs"][wref]["manual_override"]


class TestNoActiveOverride:
    def test_defaults_when_diag_is_none(self) -> None:
        block = _mo_block(diag=None)
        assert block == {
            "active": False, "scope": None, "started_at": None, "expires_at": None,
            "remaining_min": None, "release_strategy": None, "waiting_on": None,
            "release_reason": None, "override_position_ha": None,
        }

    def test_defaults_when_diag_present_but_inactive(self) -> None:
        block = _mo_block(diag=_diag(manual_override_active=False))
        assert block["active"] is False
        assert block["release_strategy"] is None
        assert block["waiting_on"] is None
        assert block["started_at"] is None


class TestActiveOverrideSurfacesStrategyFields:
    def test_first_comfort_strategy(self) -> None:
        started = _NOW - timedelta(minutes=10)
        block = _mo_block(diag=_diag(
            manual_override_active=True,
            manual_override_scope="daytime",
            manual_override_started_at=started,
            manual_override_expires_at=_NOW + timedelta(minutes=50),
            manual_override_remaining_min=50.0,
            manual_override_release_strategy="first_comfort",
            manual_override_position=40,
        ))
        assert block["active"] is True
        assert block["release_strategy"] == "first_comfort"
        assert block["waiting_on"] == "next automatic Comfort decision"
        assert block["started_at"] is not None
        assert block["expires_at"] is not None
        assert block["remaining_min"] == 50.0
        assert block["override_position_ha"] == 40

    def test_manual_strategy_waiting_on_text(self) -> None:
        block = _mo_block(diag=_diag(
            manual_override_active=True,
            manual_override_release_strategy="manual",
        ))
        assert block["waiting_on"] == "explicit manual clear"

    def test_duration_strategy_waiting_on_text(self) -> None:
        block = _mo_block(diag=_diag(
            manual_override_active=True,
            manual_override_release_strategy="duration",
        ))
        assert block["waiting_on"] == "configured duration elapses"


class TestUnknownStrategyNeverRaises:
    def test_unrecognized_value_yields_none_waiting_on(self) -> None:
        block = _mo_block(diag=_diag(
            manual_override_active=True,
            manual_override_release_strategy="not_a_real_strategy",
        ))
        assert block["release_strategy"] == "not_a_real_strategy"
        assert block["waiting_on"] is None


class TestReleaseReasonStillSurfaced:
    def test_release_reason_regression(self) -> None:
        block = _mo_block(diag=_diag(
            manual_override_active=False,
            manual_override_release_reason="timeout",
        ))
        assert block["release_reason"] == "timeout"
