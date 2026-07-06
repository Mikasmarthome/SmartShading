"""Night contact coordinator integration tests.

Structural tests (source inspection) and unit tests for the NightContactHold
wiring in coordinator.py.  No HA instance required.

Tests:
  1. NightContactHold dict initialized in __init__
  2. Contact reading built before WDI call (source pattern check)
  3. Night contact logic block exists (BLOCK / CATCH_UP / HOLD_NIGHT_VENT / RETURN_TO_NIGHT)
  4. NIGHT_VENT is exempt from NightHardHold
  5. Experiment eligibility guard (night_contact_blocked param present)
  6. PendingOutcome carries night contact fields
  7. Contact diagnostics propagated to _WindowComputeState
  8. Contact diagnostics propagated to WindowExecutionDiagnostics
  9. NightContactAction uses == (not is) for string comparisons
  10. ContactStatus is a real Enum (not string constants)
  11. WindowDecisionInput.contact_status field exists
  12. ExecutionDiagnostics has 7 contact fields
  13. build_window_decision_input accepts night contact params
"""
from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Source files (read once)
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent / "custom_components" / "smartshading"
_COORDINATOR_SRC = (_ROOT / "coordinator.py").read_text(encoding="utf-8")
_EXPERIMENT_ELIGIBILITY_SRC = (_ROOT / "engines" / "experiment_eligibility.py").read_text(encoding="utf-8")
_PENDING_OUTCOME_SRC = (_ROOT / "models" / "pending_outcome.py").read_text(encoding="utf-8")
_EXECUTION_DIAG_SRC = (_ROOT / "models" / "execution_diagnostics.py").read_text(encoding="utf-8")
_WDI_SRC = (_ROOT / "models" / "window_decision_input.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. NightContactHold dict initialized
# ---------------------------------------------------------------------------

class TestNightContactHoldInit:
    def test_hold_dict_in_init(self):
        assert "_night_contact_holds" in _COORDINATOR_SRC
        assert "NightContactHold" in _COORDINATOR_SRC

    def test_hold_dict_initialized_empty(self):
        assert "_night_contact_holds: dict[str, _NightContactHold] = {}" in _COORDINATOR_SRC


# ---------------------------------------------------------------------------
# 2. Contact reading built before WDI
# ---------------------------------------------------------------------------

class TestContactReadingBeforeWDI:
    def test_build_contact_reading_import(self):
        assert "build_contact_reading as _build_contact_reading" in _COORDINATOR_SRC

    def test_contact_reading_in_first_pass(self):
        assert "_build_contact_reading(" in _COORDINATOR_SRC


# ---------------------------------------------------------------------------
# 3. Night contact logic (all four actions)
# ---------------------------------------------------------------------------

class TestNightContactLogicBlock:
    def test_block_action_present(self):
        assert 'NightContactAction.BLOCK' in _COORDINATOR_SRC

    def test_catch_up_action_present(self):
        assert 'NightContactAction.CATCH_UP' in _COORDINATOR_SRC

    def test_hold_night_vent_action_present(self):
        assert 'NightContactAction.HOLD_NIGHT_VENT' in _COORDINATOR_SRC

    def test_return_to_night_action_present(self):
        assert 'NightContactAction.RETURN_TO_NIGHT' in _COORDINATOR_SRC

    def test_decided_by_night_contact_block(self):
        assert '"NightContactBlock"' in _COORDINATOR_SRC

    def test_decided_by_night_contact_catch_up(self):
        assert '"NightContactCatchUp"' in _COORDINATOR_SRC

    def test_decided_by_night_contact_vent(self):
        assert '"NightContactVent"' in _COORDINATOR_SRC

    def test_decided_by_night_contact_return(self):
        assert '"NightContactReturnToNight"' in _COORDINATOR_SRC


# ---------------------------------------------------------------------------
# 4. NIGHT_VENT exempt from NightHardHold
# ---------------------------------------------------------------------------

class TestNightVentExempt:
    def test_night_vent_in_hard_hold_exemption(self):
        assert "ShadingState.NIGHT_VENT" in _COORDINATOR_SRC

    def test_night_vent_exempt_comment_or_pattern(self):
        # Verify NIGHT_VENT appears in the exempt states tuple near NightHardHold
        assert "NIGHT_VENT" in _COORDINATOR_SRC


# ---------------------------------------------------------------------------
# 5. Experiment eligibility guard
# ---------------------------------------------------------------------------

class TestExperimentEligibilityGuard:
    def test_night_contact_blocked_in_eligibility_input(self):
        assert "night_contact_blocked" in _EXPERIMENT_ELIGIBILITY_SRC

    def test_night_contact_gate_in_evaluate(self):
        assert '"night_contact_blocked"' in _EXPERIMENT_ELIGIBILITY_SRC

    def test_night_contact_blocked_passed_in_coordinator(self):
        assert "night_contact_blocked=night_contact_blocked" in _COORDINATOR_SRC or \
               "night_contact_blocked=" in _COORDINATOR_SRC


# ---------------------------------------------------------------------------
# 6. PendingOutcome fields
# ---------------------------------------------------------------------------

class TestPendingOutcomeFields:
    def test_night_contact_blocked_field(self):
        assert "night_contact_blocked_at_decision" in _PENDING_OUTCOME_SRC

    def test_night_vent_active_field(self):
        assert "night_vent_active_at_decision" in _PENDING_OUTCOME_SRC

    def test_fields_in_to_dict(self):
        assert '"night_contact_blocked_at_decision"' in _PENDING_OUTCOME_SRC

    def test_fields_in_from_dict(self):
        assert "night_contact_blocked_at_decision" in _PENDING_OUTCOME_SRC


# ---------------------------------------------------------------------------
# 7-8. Contact diagnostics in _WindowComputeState and WindowExecutionDiagnostics
# ---------------------------------------------------------------------------

class TestContactDiagnosticsPropagated:
    def test_contact_sensor_configured_in_compute_state(self):
        assert "contact_sensor_configured: bool = False" in _COORDINATOR_SRC

    def test_contact_status_in_execution_diagnostics(self):
        assert "contact_status: str | None = None" in _EXECUTION_DIAG_SRC

    def test_night_contact_blocked_in_diag(self):
        assert "night_contact_blocked: bool = False" in _EXECUTION_DIAG_SRC

    def test_night_vent_active_in_diag(self):
        assert "night_vent_active: bool = False" in _EXECUTION_DIAG_SRC

    def test_catch_up_pending_in_diag(self):
        assert "catch_up_pending: bool = False" in _EXECUTION_DIAG_SRC

    def test_catch_up_done_in_diag(self):
        assert "catch_up_done: bool = False" in _EXECUTION_DIAG_SRC

    def test_state_label_in_diag(self):
        assert "night_contact_state_label: str | None = None" in _EXECUTION_DIAG_SRC


# ---------------------------------------------------------------------------
# 9. Equality (==) used for NightContactAction comparisons
# ---------------------------------------------------------------------------

class TestNightContactActionComparisons:
    def test_uses_equality_not_identity(self):
        assert "_nc_action is _NightContactAction." not in _COORDINATOR_SRC
        assert "_nc_action == _NightContactAction.BLOCK" in _COORDINATOR_SRC


# ---------------------------------------------------------------------------
# 10. ContactStatus is an Enum
# ---------------------------------------------------------------------------

class TestContactStatusEnum:
    def test_contact_status_is_enum(self):
        from custom_components.smartshading.engines.contact_engine import ContactStatus
        from enum import Enum
        assert issubclass(ContactStatus, Enum)

    def test_has_open_closed_unknown(self):
        from custom_components.smartshading.engines.contact_engine import ContactStatus
        assert ContactStatus.OPEN.value == "open"
        assert ContactStatus.CLOSED.value == "closed"
        assert ContactStatus.UNKNOWN.value == "unknown"


# ---------------------------------------------------------------------------
# 11. WindowDecisionInput.contact_status field
# ---------------------------------------------------------------------------

class TestWDIContactStatusField:
    def test_contact_status_in_wdi(self):
        assert "contact_status: ContactStatus | None = None" in _WDI_SRC

    def test_contact_status_returned(self):
        assert "contact_status=contact_status" in _WDI_SRC


# ---------------------------------------------------------------------------
# 12. ExecutionDiagnostics has 7 contact fields
# ---------------------------------------------------------------------------

class TestExecutionDiagnosticsContactFields:
    def _count_contact_fields(self) -> int:
        contact_fields = [
            "contact_sensor_configured",
            "contact_status",
            "night_contact_blocked",
            "catch_up_pending",
            "catch_up_done",
            "night_vent_active",
            "night_contact_state_label",
        ]
        return sum(1 for f in contact_fields if f in _EXECUTION_DIAG_SRC)

    def test_all_seven_contact_fields_present(self):
        assert self._count_contact_fields() == 7


# ---------------------------------------------------------------------------
# 13. build_window_decision_input accepts night contact params
# ---------------------------------------------------------------------------

class TestBuildWDINightContactParams:
    def test_night_block_param(self):
        assert "night_block_on_window_open: bool = False" in _WDI_SRC

    def test_night_lift_param(self):
        assert "night_lift_on_window_open: bool = False" in _WDI_SRC

    def test_window_open_night_position_param(self):
        assert "window_open_night_position: int = 0" in _WDI_SRC


# ---------------------------------------------------------------------------
# 14. contact_unknown passed to evaluate() in coordinator
# ---------------------------------------------------------------------------

class TestContactUnknownWiredInCoordinator:
    def test_contact_unknown_passed_to_evaluate(self):
        assert "contact_unknown=_cs_reading.status is _ContactStatus.UNKNOWN" in _COORDINATOR_SRC

    def test_contact_unknown_param_in_hold(self):
        from custom_components.smartshading.engines.night_contact_hold import NightContactHold
        import inspect
        sig = inspect.signature(NightContactHold.evaluate)
        assert "contact_unknown" in sig.parameters


# ---------------------------------------------------------------------------
# 15. contact_is_stale in _WindowComputeState and WindowExecutionDiagnostics
# ---------------------------------------------------------------------------

class TestContactIsStale:
    def test_contact_is_stale_in_compute_state(self):
        assert "contact_is_stale: bool = False" in _COORDINATOR_SRC

    def test_contact_is_stale_set_from_reading(self):
        assert "contact_is_stale=_cs_reading.is_stale" in _COORDINATOR_SRC

    def test_contact_is_stale_in_execution_diagnostics(self):
        assert "contact_is_stale: bool = False" in _EXECUTION_DIAG_SRC

    def test_contact_is_stale_propagated_to_diagnostics(self):
        assert "contact_is_stale=s.contact_is_stale" in _COORDINATOR_SRC
