"""T21 — P9B "Strategy Experiment Reserve" removal lock-in.

T17 and a fresh T21 audit both confirmed the entire P9B "strategy adoption"
subtree (creation/injection side AND runtime-application/monitoring side) was
a guaranteed permanent no-op on every real install: `_maybe_adopt_strategy`
(the only code path that ever inserted a NEW key into
`self._strategy_adoptions_active`) had zero callers, so the "live" runtime-
application cluster that read that always-empty dict every cycle could never
do anything either. T21 removed both halves together, plus the whole
`models/strategy_learning.py` and `engines/strategy_experiment_engine.py`
support modules.

This file protects that removal:
  1. models/strategy_learning.py no longer exists (or exposes none of the
     removed symbols, if some future change makes full removal impossible).
  2. engines/strategy_experiment_engine.py no longer exists.
  3. coordinator.py source no longer contains the removed P9B symbols.
  4. A REAL BEHAVIOR test: an old learning-store payload carrying the now-
     removed "strategy_experiments"/"persistent_strategy_adoptions" keys must
     still restore cleanly (unknown keys silently ignored, never a crash) —
     protecting real users upgrading from an earlier build.
  5. P7 (bounded position experiments) and P8 (persistent position
     adoptions) — the LIVE, unrelated systems that must not have been
     collaterally damaged — remain fully intact and importable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"

_REMOVED_STRATEGY_LEARNING_SYMBOLS = (
    "STRATEGY_LEARNING_SCHEMA_VERSION", "CAUSAL_LIMITATION",
    "FAMILY_ENTRY_TIMING", "FAMILY_EXIT_TIMING", "FAMILY_ENTRY_THRESHOLD",
    "FAMILY_EXIT_THRESHOLD", "FAMILY_TIER_CHOICE", "FAMILY_MINIMUM_HOLD",
    "FAMILY_HYSTERESIS", "FamilyBounds", "FAMILY_BOUNDS",
    "ADOPTION_HISTORY_PER_KEY",
    "StrategyShadowCandidate", "StrategyMonitoringState",
    "BoundedStrategyExperiment", "PersistentStrategyAdoption",
)

_REMOVED_STRATEGY_ENGINE_SYMBOLS = (
    "StrategyCandidateResult", "compute_strategy_candidate", "StrategyEvidence",
    "StrategyEvidenceResult", "evaluate_strategy_evidence", "StrategyNeedInput",
    "evaluate_strategy_experiment_need", "classify_strategy_outcome",
    "update_strategy_monitoring", "evaluate_strategy_confirmation",
    "StrategyMonitoringActionInput", "evaluate_strategy_monitoring_action",
    "route_cause_to_family", "rollback_cooldown_until", "is_cooldown_active",
    "reconcile_restored_strategy_experiments", "reconcile_restored_strategy_adoptions",
)


class TestModelsStrategyLearningRemoved:
    def test_module_does_not_exist_or_exposes_no_removed_symbols(self) -> None:
        path = _BASE / "models" / "strategy_learning.py"
        if not path.exists():
            return
        import ast
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
        present = names & set(_REMOVED_STRATEGY_LEARNING_SYMBOLS)
        assert not present, f"models/strategy_learning.py still exposes {present}"


class TestEnginesStrategyExperimentEngineRemoved:
    def test_module_does_not_exist_or_exposes_no_removed_symbols(self) -> None:
        path = _BASE / "engines" / "strategy_experiment_engine.py"
        if not path.exists():
            return
        import ast
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
        present = names & set(_REMOVED_STRATEGY_ENGINE_SYMBOLS)
        assert not present, f"engines/strategy_experiment_engine.py still exposes {present}"


class TestCoordinatorSourceHasNoP9BSymbols:
    def test_no_removed_symbols_in_coordinator_source(self) -> None:
        source = (_BASE / "coordinator.py").read_text(encoding="utf-8")
        for needle in (
            "def _maybe_adopt_strategy",
            "_strategy_adoptions_active",
            "_strategy_experiments_active",
            "_monitor_strategy_adoption",
        ):
            assert needle not in source, (
                f"coordinator.py still contains {needle!r} — the T21 P9B removal "
                "was reverted or incomplete."
            )


class TestOldPayloadWithRemovedKeysRestoresWithoutRaising:
    """Real-behavior protection: a user upgrading from a hypothetical earlier
    build may have a persisted learning-store payload that still carries the
    now-removed "strategy_experiments"/"persistent_strategy_adoptions" keys
    (with real-looking fake data). Restore must silently ignore them — never
    raise — exactly like any other unknown/legacy key."""

    def test_deserialize_ignores_removed_strategy_keys(self) -> None:
        from custom_components.smartshading.engines.learning_persistence import (
            LearningPersistenceConfig,
            deserialize_into_learning_store,
        )
        from custom_components.smartshading.engines.learning_store import LearningStore

        now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        old_payload = {
            "version": 1,
            "schema_version": 2,
            "exported_at": now.isoformat(),
            "windows": {},
            "pending_outcomes": [],
            "config_generations": {"fingerprint_version": 1, "windows": {}},
            "bounded_experiments": [],
            "persistent_adoptions": [],
            # Removed P9B sections — simulating an old build's payload.
            "strategy_experiments": [
                {
                    "experiment_id": "strat-exp-1",
                    "window_id": "w1",
                    "zone_id": "z1",
                    "parameter_family": "entry_threshold",
                    "status": "activated",
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                }
            ],
            "persistent_strategy_adoptions": [
                {
                    "adoption_id": "strat-ad-1",
                    "window_id": "w1",
                    "zone_id": "z1",
                    "parameter_family": "entry_threshold",
                    "context_family": "global",
                    "adopted_delta": -5.0,
                    "status": "adopted",
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                }
            ],
            "consumed_experiment_ledger": {},
            "shadow_tombstones": [],
        }

        store = LearningStore()
        config = LearningPersistenceConfig()
        # Must not raise — unknown/removed keys are silently ignored.
        extras = deserialize_into_learning_store(old_payload, store, config, now)
        assert extras is not None
        assert not hasattr(extras, "strategy_experiments")
        assert not hasattr(extras, "persistent_strategy_adoptions")


class TestP7AndP8RemainIntact:
    """P7 (position bounded experiments) and P8 (position persistent
    adoptions) are LIVE, unrelated systems that must not have been
    collaterally damaged by the P9B removal."""

    def test_bounded_experiment_constructible(self) -> None:
        from custom_components.smartshading.models.bounded_experiment import BoundedExperiment

        now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        exp = BoundedExperiment(
            experiment_id="exp-1", source_shadow_id="shadow-1",
            window_id="w1", zone_id="z1", intensity_level="normal",
            context_family="global", created_at=now, updated_at=now,
        )
        assert exp.experiment_id == "exp-1"
        assert exp.experiment_key == ("w1", "normal", "global")

    def test_persistent_target_adoption_constructible(self) -> None:
        from custom_components.smartshading.models.persistent_adoption import (
            PersistentTargetAdoption,
        )

        adoption = PersistentTargetAdoption(
            adoption_id="ad-1", window_id="w1", zone_id="z1",
            intensity_level="normal", context_family="global",
        )
        assert adoption.adoption_id == "ad-1"


class TestDeadManualOverrideEvaluatorRemoved:
    """T21 also removed the confirmed-unreachable ManualOverrideEvaluator
    class (evaluators/manual_override_evaluator.py) — its production role
    was fully taken over by engines/manual_override_policy.py well before
    this ticket; the class had zero callers in the Tier 1-5 pipeline."""

    def test_module_file_does_not_exist(self) -> None:
        path = _BASE / "evaluators" / "manual_override_evaluator.py"
        assert not path.exists(), (
            "evaluators/manual_override_evaluator.py was confirmed dead "
            "(zero production callers since T7) and removed in T21 — it "
            "must not be silently reintroduced."
        )

    def test_real_override_policy_still_importable_and_functional(self) -> None:
        from custom_components.smartshading.engines.manual_override_policy import (
            evaluate_manual_override_policy,
        )
        from custom_components.smartshading.models.manual_override import ManualOverride
        from custom_components.smartshading.models.window_decision import WindowDecision
        from custom_components.smartshading.state_machine.states import (
            DecisionCategory,
            ShadingState,
        )

        now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        override = ManualOverride(
            window_id="w1", override_position=42, started_at=now,
            expires_at=now, source="position_delta",
            overridden_state=ShadingState.OPEN, overridden_position=0,
        )
        candidate = WindowDecision(
            window_id="w1", shading_state=ShadingState.LIGHT_SHADE,
            target_position=50, decided_by="SolarEvaluator",
            category=DecisionCategory.COMFORT,
        )
        decision = evaluate_manual_override_policy(
            active_override=override, candidate=candidate,
            allow_comfort=False, allow_protection=False,
        )
        assert decision.shading_state is ShadingState.MANUAL_OVERRIDE
        assert decision.target_position == 42


class TestOrphanedStrategyRuntimeModuleRemoved:
    def test_strategy_runtime_module_does_not_exist(self) -> None:
        path = _BASE / "engines" / "strategy_runtime.py"
        assert not path.exists(), (
            "engines/strategy_runtime.py became fully orphaned once P9B's "
            "runtime-application cluster was removed (zero remaining "
            "importers) and was removed in T21 — it must not silently "
            "reappear as a dead module."
        )
