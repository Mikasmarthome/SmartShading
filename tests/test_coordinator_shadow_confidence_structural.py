"""Structural guard: coordinator.py's shadow-candidate confidence computation
must use the shared ConfidenceEngine evidence-maturity ramp (T12), not a
re-diverged ad-hoc formula.

Why structural: exercising the real ``_maybe_shadow`` path behaviorally
requires a fully wired zone with contribution models, attribution gates and
multiple resolved thermal outcomes across several days — deeper fixture
machinery than this codebase's established test depth for the P6 shadow
engine (see test_coordinator_parallel_dispatch.py's own structural/behavioral
split precedent). A structural check on the call site is the established
alternative for this kind of hard-to-behaviorally-test invariant (see
test_dispatch_safety_generation_exemption_structural.py).
"""
from __future__ import annotations

import re
from pathlib import Path

_COORDINATOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components" / "smartshading" / "coordinator.py"
)


def _source() -> str:
    return _COORDINATOR_PATH.read_text(encoding="utf-8")


class TestShadowConfidenceUsesSharedFormula:
    def test_maybe_shadow_calls_compute_sample_maturity_confidence(self) -> None:
        source = _source()
        assert "compute_sample_maturity_confidence" in source, (
            "coordinator.py must import and use the shared evidence-maturity "
            "ramp from confidence_engine — a hand-rolled ad-hoc formula here "
            "would re-introduce a duplicate, divergeable confidence concept."
        )

    def test_call_uses_documented_targets(self) -> None:
        source = _source()
        pattern = re.compile(
            r"compute_sample_maturity_confidence\(\s*neg_count,\s*"
            r"count_target=8\.0,\s*distinct_days=days,\s*day_target=3\.0,?\s*\)",
        )
        assert pattern.search(source), (
            "The shadow-candidate maturity call must use count_target=8.0 / "
            "day_target=3.0 (the historical constants) via the shared "
            "helper — a changed target would silently alter shadow-proposal "
            "maturity behavior without any test noticing outside this guard."
        )

    def test_no_hand_rolled_min_ratio_formula_remains_in_shadow_block(self) -> None:
        source = _source()
        maybe_shadow_start = source.index("def _maybe_shadow(")
        maybe_shadow_end = source.index("\n    def ", maybe_shadow_start + 10)
        block = source[maybe_shadow_start:maybe_shadow_end]
        assert "neg_count / 8.0" not in block, (
            "A literal 'neg_count / 8.0' inside _maybe_shadow indicates the "
            "ad-hoc formula was reintroduced instead of the shared helper."
        )
