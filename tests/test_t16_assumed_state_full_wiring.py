"""T16 — Assumed State full wiring: persistence round-trip, CommandFilter
confidence-gated tolerance widening, and unload-ordering.

Before T16: AssumedStateManager computed confidence/drift/uncertainty but
nothing consumed it (zero callers of is_position_trustworthy()/
is_drift_suspected()), nothing persisted it (on_restart()/
initialize_from_restore()/export_for_restore() had zero call sites outside
this module), and it was invisible in the support export.

After T16:
  - CommandFilter.evaluate(position_confidence_low=True) widens the
    position-tolerance check instead of issuing corrective commands against
    an assumed position we are not confident about (ARCHITECTURE.md §6.2).
  - learning_persistence.py serializes/deserializes AssumedStateManager
    records exactly like current_states/active_overrides; the coordinator
    restores them via initialize_from_restore() before the first dispatch
    decision.
  - engines/support_export.py exposes a per-window assumed_state section.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.smartshading.cover_control.assumed_state_manager import (
    AssumedPositionState,
    AssumedStateManager,
)
from custom_components.smartshading.cover_control.command_filter import (
    BLOCKED_SAME_POSITION,
    LOW_CONFIDENCE_TOLERANCE_MULTIPLIER,
    CommandFilter,
    ExecutionCapability,
    ExecutionMode,
)
from custom_components.smartshading.engines.learning_persistence import (
    LearningPersistenceConfig,
    deserialize_into_learning_store,
    serialize_learning_store,
)
from custom_components.smartshading.engines.learning_store import LearningStore

_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
_BASE = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"


# ---------------------------------------------------------------------------
# CommandFilter: confidence-gated tolerance widening
# ---------------------------------------------------------------------------

def _evaluate(**overrides):
    base = dict(
        target_position_internal=50,
        current_position_internal=50 + ExecutionCapability().position_tolerance + 1,
        execution_mode=ExecutionMode.AUTOMATIC,
        is_safety=False,
        is_manual_override=False,
        is_cover_available=True,
        state_guard_allowed=True,
        execution_capability=ExecutionCapability(),
    )
    base.update(overrides)
    return CommandFilter().evaluate(**base)


class TestCommandFilterConfidenceGatedTolerance:
    def test_default_behavior_unchanged_when_confidence_flag_omitted(self) -> None:
        # A gap just outside the normal tolerance is NOT suppressed by default.
        result = _evaluate()
        assert result.allowed is True

    def test_low_confidence_widens_tolerance_and_suppresses_the_command(self) -> None:
        # Same gap, but now flagged as low-confidence — must be suppressed
        # instead of issuing a corrective command against an uncertain
        # assumed position.
        tol = ExecutionCapability().position_tolerance
        result = _evaluate(
            current_position_internal=50 + tol + 1,
            position_confidence_low=True,
        )
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_SAME_POSITION

    def test_gap_beyond_widened_tolerance_still_dispatches(self) -> None:
        tol = ExecutionCapability().position_tolerance
        gap_beyond_widened = tol * LOW_CONFIDENCE_TOLERANCE_MULTIPLIER + 5
        result = _evaluate(
            current_position_internal=50 + gap_beyond_widened,
            position_confidence_low=True,
        )
        assert result.allowed is True

    def test_safety_still_bypasses_tolerance_regardless_of_confidence(self) -> None:
        tol = ExecutionCapability().position_tolerance
        result = _evaluate(
            current_position_internal=50 + tol - 1,  # within tolerance
            is_safety=True,
            position_confidence_low=True,
        )
        assert result.allowed is True

    def test_reliable_feedback_cover_never_widens_tolerance_in_practice(self) -> None:
        # A reliable-feedback cover's confidence is always 1.0, so the
        # coordinator's is_position_trustworthy() always yields
        # position_confidence_low=False for it — this test just confirms
        # the default (False) path matches pre-T16 behavior exactly.
        tol = ExecutionCapability().position_tolerance
        within = _evaluate(current_position_internal=50 + tol - 1)
        assert within.allowed is False
        assert within.blocked_reason == BLOCKED_SAME_POSITION


# ---------------------------------------------------------------------------
# AssumedStateManager: confidence/trustworthiness now actually consumed
# ---------------------------------------------------------------------------

class TestIsPositionTrustworthyNowHasARealConsumer:
    def test_no_bare_hass_reference_needed_low_confidence_flows_from_manager(self) -> None:
        mgr = AssumedStateManager()
        mgr.update("cover.rts", 50, _NOW, has_reliable_position_feedback=False)
        # Force silence beyond max_silence_duration so confidence decays low.
        later = _NOW + timedelta(days=30)
        assert mgr.is_position_trustworthy("cover.rts", later) is False

    def test_fresh_reliable_cover_is_always_trustworthy(self) -> None:
        mgr = AssumedStateManager()
        mgr.update("cover.reliable", 50, _NOW, has_reliable_position_feedback=True)
        assert mgr.is_position_trustworthy("cover.reliable", _NOW) is True


# ---------------------------------------------------------------------------
# Persistence round-trip: AssumedStateManager survives serialize/deserialize
# ---------------------------------------------------------------------------

class TestAssumedStatePersistenceRoundtrip:
    def _config(self) -> LearningPersistenceConfig:
        return LearningPersistenceConfig()

    def test_serialize_includes_assumed_state(self) -> None:
        mgr = AssumedStateManager()
        mgr.update("cover.rts", 42, _NOW, has_reliable_position_feedback=False)
        store = LearningStore()
        data = serialize_learning_store(
            store, self._config(), _NOW,
            assumed_state={
                cid: mgr.export_for_restore(cid) for cid in mgr.known_cover_ids()
            },
        )
        assert data["assumed_state"]["cover.rts"]["assumed_position"] == 42
        assert data["assumed_state"]["cover.rts"]["last_commanded_position"] == 42

    def test_serialize_defaults_to_empty_dict_when_not_provided(self) -> None:
        store = LearningStore()
        data = serialize_learning_store(store, self._config(), _NOW)
        assert data["assumed_state"] == {}

    def test_deserialize_restores_assumed_state(self) -> None:
        mgr = AssumedStateManager()
        mgr.update("cover.rts", 42, _NOW, has_reliable_position_feedback=False)
        store = LearningStore()
        data = serialize_learning_store(
            store, self._config(), _NOW,
            assumed_state={
                cid: mgr.export_for_restore(cid) for cid in mgr.known_cover_ids()
            },
        )
        extras = deserialize_into_learning_store(data, LearningStore(), self._config(), _NOW)
        assert "cover.rts" in extras.assumed_state
        assert extras.assumed_state["cover.rts"]["assumed_position"] == 42

    def test_deserialize_tolerates_missing_assumed_state_key(self) -> None:
        store = LearningStore()
        data = serialize_learning_store(store, self._config(), _NOW)
        del data["assumed_state"]
        extras = deserialize_into_learning_store(data, LearningStore(), self._config(), _NOW)
        assert extras.assumed_state == {}

    def test_deserialize_skips_malformed_entry_without_raising(self) -> None:
        store = LearningStore()
        data = serialize_learning_store(store, self._config(), _NOW)
        data["assumed_state"] = {"cover.broken": "not-a-dict", "cover.also_broken": {}}
        extras = deserialize_into_learning_store(data, LearningStore(), self._config(), _NOW)
        assert extras.assumed_state == {}

    def test_full_roundtrip_reconstructs_a_usable_manager_state(self) -> None:
        # Simulates the actual coordinator restore path end-to-end using
        # only the pure functions/classes — no HA stubs needed.
        original = AssumedStateManager()
        original.update("cover.rts", 77, _NOW, has_reliable_position_feedback=False)
        original.update("cover.rts", 60, _NOW + timedelta(minutes=5),
                        has_reliable_position_feedback=False)

        store = LearningStore()
        data = serialize_learning_store(
            store, LearningPersistenceConfig(), _NOW,
            assumed_state={
                cid: original.export_for_restore(cid) for cid in original.known_cover_ids()
            },
        )
        extras = deserialize_into_learning_store(
            data, LearningStore(), LearningPersistenceConfig(), _NOW,
        )

        restored = AssumedStateManager()
        for cover_id, raw in extras.assumed_state.items():
            restored.initialize_from_restore(AssumedPositionState(
                cover_id=cover_id,
                assumed_position=raw["assumed_position"],
                assumed_tilt=raw.get("assumed_tilt"),
                last_commanded_at=(
                    datetime.fromisoformat(raw["last_commanded_at"])
                    if raw.get("last_commanded_at") else None
                ),
                last_known_good_at=datetime.fromisoformat(raw["last_known_good_at"]),
                confidence=0.0,
                position_uncertainty_pct=raw.get("position_uncertainty_pct", 0.0),
                is_drift_suspected=False,
                interrupted_travel=raw.get("interrupted_travel", False),
                last_commanded_position=raw.get("last_commanded_position"),
            ))

        state = restored.get_state("cover.rts", _NOW + timedelta(minutes=5))
        assert state is not None
        assert state.assumed_position == 60
        # Two update() calls without reliable feedback -> uncertainty grew twice.
        assert state.position_uncertainty_pct == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Structural guard: unload shuts the coordinator down before its own teardown
# ---------------------------------------------------------------------------

class TestCoordinatorComputesConfidenceGate:
    """Structural guard: coordinator.py must actually compute
    position_confidence_low from AssumedStateManager and pass it into the
    CommandFilter call — a regression could silently hardcode False (no-op)
    without any syntax error."""

    def test_coordinator_calls_is_position_trustworthy(self) -> None:
        source = (_BASE / "coordinator.py").read_text(encoding="utf-8")
        assert "is_position_trustworthy" in source

    def test_position_confidence_low_is_threaded_into_command_filter_call(self) -> None:
        source = (_BASE / "coordinator.py").read_text(encoding="utf-8")
        idx = source.index("_position_confidence_low = not self.assumed_state_manager")
        window = source[idx:idx + 1000]
        assert "CommandFilter().evaluate(" in window
        assert "position_confidence_low=_position_confidence_low" in window


class TestUnloadOrdering:
    def test_async_shutdown_called_before_teardown_in_source(self) -> None:
        source = (_BASE / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_unload_entry":
                target = node
                break
        assert target is not None, "async_unload_entry not found"

        # ast.walk() is breadth-first, not source order — collect with line
        # numbers and sort, since call order in the source is what matters.
        found: list[tuple[int, str]] = []
        for node in ast.walk(target):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = getattr(func, "attr", None)
            if attr in (
                "async_shutdown", "async_teardown_presence_listeners",
                "async_teardown_contact_listeners",
                "async_teardown_lifecycle_boundary_timer", "async_flush_learning",
            ):
                found.append((node.lineno, attr))
        found.sort(key=lambda t: t[0])
        call_order = [attr for _, attr in found]

        assert call_order, "expected to find coordinator lifecycle calls"
        assert call_order[0] == "async_shutdown", (
            f"async_shutdown must be the first coordinator lifecycle call in "
            f"async_unload_entry so no new refresh is scheduled during "
            f"teardown/flush; found order: {call_order}"
        )
