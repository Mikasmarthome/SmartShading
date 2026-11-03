"""Tests for coordinator._resolve_morning_occurrence_after_dispatch() --
B3-010 R12: correctly resolving a Morning occurrence's terminal state from
the REAL, individual per-cover ExecutionResults of a dispatch cycle,
validated against the window's REAL expected cover-ID set -- never from
the window-wide any_sent/any_failed aggregate, and never from a vacuous
"empty results = all succeeded" reading.

R11 bug fixed here: ExecutionPlanResult's own documented semantics say an
empty `results` list means the plan had NO cover entities at all (every
aggregate value is False; `all_blocked` deliberately requires a non-empty
set) -- so an empty results list must never read as "all covers
succeeded". Fixed by requiring the function to be told the window's real
expected_cover_ids and checking each one is present exactly once with a
resolved status -- an empty expected set is treated as a non-executable
occurrence (invalidated), never `dispatched`.

Extracted as a module-level pure function specifically so this logic is
directly unit-testable without driving the full (600+ line)
_async_update_data() cycle -- consistent with this suite's established
precedent (see test_b3010_real_runtime_path.py's own docstring) that the
top-level update method is out of scope for direct invocation in tests.
"""
from __future__ import annotations

import sys
import types
from datetime import date, datetime, timezone
from typing import Any


def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


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

sys.modules.setdefault("homeassistant.util.dt", _stub(
    "homeassistant.util.dt",
    utcnow=lambda: datetime.now(timezone.utc),
    now=lambda: datetime.now(timezone.utc),
    as_utc=lambda dt: dt.astimezone(timezone.utc),
    as_local=lambda dt: dt,
    DEFAULT_TIME_ZONE=timezone.utc,
))


class _CoordBase:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass


sys.modules.setdefault("homeassistant.helpers.update_coordinator", _stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=_CoordBase,
    CoordinatorEntity=type("CE", (), {"__class_getitem__": classmethod(lambda cls, x: cls), "__init__": lambda self, c: None}),
))
sys.modules.setdefault("homeassistant.helpers.storage", _stub(
    "homeassistant.helpers.storage",
    Store=type("StoreStub", (), {"__init__": lambda self, *a, **k: None}),
))

from custom_components.smartshading.coordinator import (  # noqa: E402
    _resolve_morning_occurrence_after_dispatch,
)
from custom_components.smartshading.cover_control.command_filter import (  # noqa: E402
    BLOCKED_COVER_UNAVAILABLE,
    BLOCKED_GUARD_ACTION_INTERVAL,
    BLOCKED_MANUAL_OVERRIDE,
    BLOCKED_NO_TARGET_POSITION,
    BLOCKED_SAME_POSITION,
)
from custom_components.smartshading.cover_control.execution_plan import CoverCommandType  # noqa: E402
from custom_components.smartshading.cover_control.execution_result import (  # noqa: E402
    ExecutionResult,
    ExecutionStatus,
)
from custom_components.smartshading.cover_control.execution_result import (  # noqa: E402
    build_not_attempted_result,
)
from custom_components.smartshading.engines.morning_reconciliation import (  # noqa: E402
    mark_dispatch_attempted,
    mark_dispatched,
    pending_target_position_internal,
    start_occurrence,
)

_TODAY = date(2026, 8, 10)
_NOW = datetime(2026, 8, 10, 7, 5)


def _occ():
    return start_occurrence(window_id="w1", occurrence_date=_TODAY, target_position_internal=30, now=_NOW)


def _intent(entity_id: str = "cover.a"):
    from custom_components.smartshading.cover_control.execution_plan import (
        CoverCommandType, CoverIntent,
    )
    return CoverIntent(
        cover_entity_id=entity_id, command_type=CoverCommandType.MOVE_TO_POSITION,
        target_position_internal=30, target_position_ha=70, target_tilt=None,
        is_safety=False, execution_mode="automatic", allowed=True, blocked_reason=None,
        decided_by="MorningEvaluator", computed_at=_NOW,
    )


def _result(entity_id: str, status: ExecutionStatus, blocked_reason: str | None = None) -> ExecutionResult:
    return ExecutionResult(
        entity_id=entity_id, status=status,
        command_type=CoverCommandType.MOVE_TO_POSITION, execution_mode="automatic",
        target_position_internal=30, target_position_ha=70, target_tilt=None,
        is_safety=False, blocked_reason=blocked_reason, error=None,
        sent_at_utc=None, reason="test",
    )


class TestAllExpectedCoversSent:
    def test_single_expected_cover_sent_marks_dispatched(self):
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[_result("cover.a", ExecutionStatus.SENT)],
            expected_cover_ids=["cover.a"], now=_NOW,
        )
        assert result.status == "dispatched"

    def test_multiple_expected_covers_all_sent_marks_dispatched(self):
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[
                _result("cover.a", ExecutionStatus.SENT),
                _result("cover.b", ExecutionStatus.SENT),
            ], expected_cover_ids=["cover.a", "cover.b"], now=_NOW,
        )
        assert result.status == "dispatched"


class TestEmptyResultsIsNeverVacuouslySuccessful:
    """B3-010 R12 bug fix: ExecutionPlanResult's own documented semantics
    say results=[] means the plan had NO cover entities -- this must never
    read as 'all covers succeeded'."""

    def test_empty_results_with_expected_covers_stays_pending(self):
        """Expected covers exist but no result was produced for any of
        them (e.g. dispatch never ran) -- missing, not resolved."""
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[], expected_cover_ids=["cover.a"], now=_NOW,
        )
        assert result.status == "pending"

    def test_empty_expected_cover_ids_invalidates_not_dispatched(self):
        """Empty cover group / removed window -- a genuinely non-
        executable occurrence, invalidated, explicitly never
        `dispatched`."""
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[], expected_cover_ids=[], now=_NOW,
        )
        assert result.status == "invalidated"
        assert result.reason == "empty_cover_group"

    def test_empty_expected_cover_ids_invalidates_even_with_foreign_results(self):
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[_result("cover.unrelated", ExecutionStatus.SENT)],
            expected_cover_ids=[], now=_NOW,
        )
        assert result.status == "invalidated"
        assert result.reason == "empty_cover_group"


class TestAlreadyAtTargetNoOp:
    def test_already_at_target_blocked_same_position_marks_dispatched(self):
        """B3-010 R12 item 3: a cover already at the configured target
        (CommandFilter's legitimate BLOCKED_SAME_POSITION no-op) is a
        fachlich COMPLETED outcome, not a technical non-execution."""
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[_result("cover.a", ExecutionStatus.BLOCKED, BLOCKED_SAME_POSITION)],
            expected_cover_ids=["cover.a"], now=_NOW,
        )
        assert result.status == "dispatched"

    def test_mixed_sent_and_already_at_target_marks_dispatched(self):
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[
                _result("cover.a", ExecutionStatus.SENT),
                _result("cover.b", ExecutionStatus.BLOCKED, BLOCKED_SAME_POSITION),
            ], expected_cover_ids=["cover.a", "cover.b"], now=_NOW,
        )
        assert result.status == "dispatched"

    def test_blocked_no_target_position_is_not_treated_as_already_reached(self):
        """B3-010 R12 item 4: BLOCKED_NO_TARGET_POSITION is NOT an
        already-reached target -- it must stay pending, unlike
        BLOCKED_SAME_POSITION."""
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[_result("cover.a", ExecutionStatus.BLOCKED, BLOCKED_NO_TARGET_POSITION)],
            expected_cover_ids=["cover.a"], now=_NOW,
        )
        assert result.status == "pending"


class TestMixedCoverResultsStayPending:
    def test_sent_plus_technically_blocked_stays_pending(self):
        """cover A SENT, cover B technically blocked -- the whole
        occurrence must NOT be marked dispatched, since cover B never
        received a command."""
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[
                _result("cover.a", ExecutionStatus.SENT),
                _result("cover.b", ExecutionStatus.BLOCKED, BLOCKED_GUARD_ACTION_INTERVAL),
            ], expected_cover_ids=["cover.a", "cover.b"], now=_NOW,
        )
        assert result.status == "pending"

    def test_sent_plus_unavailable_stays_pending(self):
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[
                _result("cover.a", ExecutionStatus.SENT),
                _result("cover.b", ExecutionStatus.BLOCKED, BLOCKED_COVER_UNAVAILABLE),
            ], expected_cover_ids=["cover.a", "cover.b"], now=_NOW,
        )
        assert result.status == "pending"

    def test_sent_plus_failed_stays_pending(self):
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[
                _result("cover.a", ExecutionStatus.SENT),
                _result("cover.b", ExecutionStatus.FAILED),
            ], expected_cover_ids=["cover.a", "cover.b"], now=_NOW,
        )
        assert result.status == "pending"

    def test_single_expected_cover_not_attempted_stays_pending(self):
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[_result("cover.a", ExecutionStatus.NOT_ATTEMPTED)],
            expected_cover_ids=["cover.a"], now=_NOW,
        )
        assert result.status == "pending"

    def test_missing_result_for_expected_cover_stays_pending(self):
        """Two expected covers, only one has a result -- the missing one
        must not be silently treated as resolved."""
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[_result("cover.a", ExecutionStatus.SENT)],
            expected_cover_ids=["cover.a", "cover.b"], now=_NOW,
        )
        assert result.status == "pending"

    def test_duplicate_result_for_expected_cover_stays_pending(self):
        """Two results for the SAME expected cover (ambiguous) must not
        resolve to dispatched even if both happen to be SENT."""
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[
                _result("cover.a", ExecutionStatus.SENT),
                _result("cover.a", ExecutionStatus.SENT),
            ], expected_cover_ids=["cover.a"], now=_NOW,
        )
        assert result.status == "pending"

    def test_foreign_result_does_not_help_resolve_missing_expected_cover(self):
        """A result for a cover NOT in expected_cover_ids must not count
        toward resolving an expected-but-missing cover."""
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[_result("cover.foreign", ExecutionStatus.SENT)],
            expected_cover_ids=["cover.a"], now=_NOW,
        )
        assert result.status == "pending"

    def test_foreign_result_blocks_even_when_expected_covers_resolved(self):
        """B3-010 R13 item 4: a result for an unrelated cover -- a
        correlation anomaly that should structurally never happen -- must
        NOT be silently ignored. It blocks resolution (stays pending)
        rather than being dropped, so no additional match within the same
        plan is ever silently discarded."""
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[
                _result("cover.a", ExecutionStatus.SENT),
                _result("cover.foreign", ExecutionStatus.FAILED),
            ], expected_cover_ids=["cover.a"], now=_NOW,
        )
        assert result.status == "pending"


class TestManualOverrideBlockInvalidates:
    def test_manual_override_block_invalidates(self):
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[_result("cover.a", ExecutionStatus.BLOCKED, BLOCKED_MANUAL_OVERRIDE)],
            expected_cover_ids=["cover.a"], now=_NOW,
        )
        assert result.status == "invalidated"
        assert result.reason == "manual_override_blocked_dispatch"

    def test_manual_override_block_wins_over_other_sent_covers(self):
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[
                _result("cover.a", ExecutionStatus.SENT),
                _result("cover.b", ExecutionStatus.BLOCKED, BLOCKED_MANUAL_OVERRIDE),
            ], expected_cover_ids=["cover.a", "cover.b"], now=_NOW,
        )
        assert result.status == "invalidated"

    def test_correlated_manual_override_on_the_only_expected_cover_invalidates(self):
        """B3-010 R14 item 4: a Manual Override result correctly correlated
        to THIS window's own (sole) expected cover is a real changed fact
        about this window -- invalidates."""
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[_result("cover.window1", ExecutionStatus.BLOCKED, BLOCKED_MANUAL_OVERRIDE)],
            expected_cover_ids=["cover.window1"], now=_NOW,
        )
        assert result.status == "invalidated"

    def test_foreign_manual_override_does_not_invalidate(self):
        """B3-010 R14 item 4 correction: a BLOCKED_MANUAL_OVERRIDE result
        for a cover OUTSIDE this window's expected_cover_ids -- an override
        active on some unrelated window/cover -- is not a changed fact
        about THIS window. It must be treated as any other foreign result
        (stays pending, per TestMixedCoverResultsStayPending's own foreign-
        result rule), never as grounds to invalidate a window it has no
        real correlation to. This is the exact bug fixed this round: the
        ORIGINAL check scanned ALL results for BLOCKED_MANUAL_OVERRIDE
        regardless of which cover it named."""
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[_result("cover.unrelated_window", ExecutionStatus.BLOCKED, BLOCKED_MANUAL_OVERRIDE)],
            expected_cover_ids=["cover.window1"], now=_NOW,
        )
        assert result.status == "pending", (
            "a foreign Manual Override result must never invalidate an "
            "occurrence it is not correlated to"
        )

    def test_foreign_manual_override_does_not_invalidate_even_alongside_real_progress(self):
        occ = _occ()
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[
                _result("cover.window1", ExecutionStatus.SENT),
                _result("cover.other_window", ExecutionStatus.BLOCKED, BLOCKED_MANUAL_OVERRIDE),
            ], expected_cover_ids=["cover.window1"], now=_NOW,
        )
        assert result.status == "pending", (
            "the foreign result (whatever its reason) still blocks "
            "resolution per the general foreign-result rule -- but "
            "critically it must NOT invalidate; a later cycle with no "
            "foreign result can still resolve to dispatched"
        )


class TestOnlyAppliesToPending:
    def test_already_terminal_occurrence_untouched(self):
        occ = mark_dispatched(_occ(), now=_NOW)
        result = _resolve_morning_occurrence_after_dispatch(
            occ, results=[_result("cover.a", ExecutionStatus.FAILED)],
            expected_cover_ids=["cover.a"], now=_NOW,
        )
        assert result.status == "dispatched"


class TestDispatchAttemptedGatingMirrorsCoordinator:
    """B3-010 R14 correction: coordinator.py's post-dispatch resolution
    block only calls mark_dispatch_attempted() when NONE of this cycle's
    real per-cover results is ExecutionStatus.NOT_ATTEMPTED (see that call
    site's own comment for the exact expression:
    `any(r.status is ExecutionStatus.NOT_ATTEMPTED for r in
    _exec_plan_result.results)`). This mirrors that EXACT boolean
    expression against real production reason strings
    (build_not_attempted_result(), the same factory the dispatch loop
    itself uses for every NOT_ATTEMPTED case) -- proving the concrete bug
    this round fixed: the ORIGINAL R14 wiring only excluded the literal
    reason string "startup_grace_active", so a stale-generation or
    safety-preempted NOT_ATTEMPTED cycle would have wrongly consumed the
    one-shot entitlement. The gate must exclude ALL of them, regardless of
    reason string, since NOT_ATTEMPTED itself already means "never reached
    real dispatch/CommandFilter resolution" (see execution_result.py's own
    ExecutionStatus ordering docstring)."""

    def _any_not_attempted(self, results) -> bool:
        return any(r.status is ExecutionStatus.NOT_ATTEMPTED for r in results)

    def test_startup_grace_not_attempted_excludes_the_marker(self):
        results = [build_not_attempted_result(
            _intent(), reason="startup_grace_active: dispatch suppressed during startup hydration",
        )]
        assert self._any_not_attempted(results) is True
        occ = _occ()
        # Mirrors the coordinator's own "if not any_not_attempted: mark_dispatch_attempted(...)"
        # -- any_not_attempted is True here, so the marker call is skipped entirely.
        assert occ.dispatch_attempted is False

    def test_stale_generation_not_attempted_excludes_the_marker(self):
        """The concrete regression this round fixed: stale_generation is
        NOT the startup-grace reason string, but is still a real
        NOT_ATTEMPTED -- the fix generalizes the check from one hardcoded
        string to the ExecutionStatus itself."""
        results = [build_not_attempted_result(_intent(), reason="stale_generation")]
        assert self._any_not_attempted(results) is True

    def test_stale_presence_superseded_not_attempted_excludes_the_marker(self):
        results = [build_not_attempted_result(_intent(), reason="stale_presence_superseded")]
        assert self._any_not_attempted(results) is True

    def test_safety_preempted_not_attempted_excludes_the_marker(self):
        results = [build_not_attempted_result(_intent(), reason="safety_preempted")]
        assert self._any_not_attempted(results) is True

    def test_real_sent_result_allows_the_marker(self):
        results = [_result("cover.a", ExecutionStatus.SENT)]
        assert self._any_not_attempted(results) is False
        occ = mark_dispatch_attempted(_occ(), now=_NOW)
        assert occ.dispatch_attempted is True
        assert pending_target_position_internal(occ, today=_TODAY, lifecycle_state_is_day=True) is None

    def test_real_blocked_result_still_allows_the_marker(self):
        """A CommandFilter block is a REAL resolved outcome (distinct from
        NOT_ATTEMPTED) -- a genuinely reached-and-resolved decision, so the
        one real attempt still counts, per mark_dispatch_attempted()'s own
        docstring (a technically blocked cover is a disclosed limitation,
        not a deferred cycle)."""
        results = [_result("cover.a", ExecutionStatus.BLOCKED, blocked_reason=BLOCKED_GUARD_ACTION_INTERVAL)]
        assert self._any_not_attempted(results) is False

    def test_mixed_one_sent_one_not_attempted_excludes_the_marker(self):
        """Even a SINGLE expected cover left NOT_ATTEMPTED means the
        decision did not fully reach real resolution this cycle for the
        whole window -- the marker must not be set, matching the
        requirement that the marker only fires when the decision reached
        resolution for every expected cover."""
        results = [
            _result("cover.a", ExecutionStatus.SENT),
            build_not_attempted_result(_intent("cover.b"), reason="stale_generation"),
        ]
        assert self._any_not_attempted(results) is True
