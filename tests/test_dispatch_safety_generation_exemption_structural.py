"""Structural guard for coordinator.py's stale-generation dispatch-cancel
exemption for Safety intents (v1.2.0-beta.1, T11/T11.1 — pre-existing
mechanism, confirmed sufficient by T11's architecture analysis as the
existing preemption model; this test locks in that Safety stays exempt in
BOTH the sequential dispatch path (SPACED/SEQUENTIAL) and the T11.1
concurrent-batch dispatch path (PARALLEL)).

Why structural, not behavioral: exercising the real race (a presence/safety
event bumping self._dispatch_generation WHILE the coordinator's own Pass-2
dispatch is mid-await for a DIFFERENT window/batch) requires driving a full,
concurrent _async_update_data() cycle — this codebase's own test suite does
not do that for equally deep coordinator-internal timing behavior (see
test_startup_coordinator_dispatch.py's docstring and T11's architecture
analysis notes). A structural check on each guard's own condition is the
established alternative for this kind of hard-to-behaviorally-test
invariant in this codebase (see test_window_decision_category_completeness.py's
AST-based approach for a precedent).

Coverage:
  SGE-01  The sequential-path stale-intent (generation) cancellation is
          gated on `not _intent.is_safety`.
  SGE-02  The parallel-path (_dispatch_one_parallel_item) stale-intent
          cancellation is ALSO gated on `not intent.is_safety`.
  SGE-03  Exactly two occurrences of "stale_presence_superseded" exist — one
          per dispatch path — never accidentally duplicated further or
          collapsed into one shared (and therefore un-audited) path.
  SGE-04  Neither guard has been replaced with a tautology (e.g. "if True:").
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


class TestSequentialPathSafetyExemption:
    def test_stale_generation_guard_is_gated_on_not_is_safety(self) -> None:
        source = _source()
        pattern = re.compile(
            r"if not _intent\.is_safety:\s*\n"
            r"\s*if self\._dispatch_generation != _this_dispatch_gen:\s*\n"
            r"(?:.*\n){0,4}?\s*reason=\"stale_presence_superseded\",",
        )
        assert pattern.search(source), (
            "The sequential-path stale-generation dispatch cancellation "
            "must remain gated on `not _intent.is_safety` — a Safety "
            "command must never be cancelled by a mid-cycle generation "
            "bump. If this guard's shape genuinely changed, update this "
            "test's pattern to match the new (still-safety-exempt) "
            "structure rather than removing the check."
        )


class TestParallelPathSafetyExemption:
    def test_stale_generation_guard_is_gated_on_not_is_safety(self) -> None:
        source = _source()
        pattern = re.compile(
            r"if not intent\.is_safety and self\._dispatch_generation != this_dispatch_gen:\s*\n"
            r"(?:.*\n){0,4}?.*reason=\"stale_presence_superseded\"",
        )
        assert pattern.search(source), (
            "The T11.1 parallel-batch dispatch path "
            "(_dispatch_one_parallel_item) must remain gated on "
            "`not intent.is_safety` — a Safety command dispatched as part "
            "of a concurrent batch must never be cancelled by a mid-cycle "
            "generation bump either."
        )


class TestExactlyTwoOccurrencesOneNoMoreNoLess:
    def test_stale_presence_superseded_reason_appears_exactly_twice(self) -> None:
        source = _source()
        assert source.count('reason="stale_presence_superseded"') == 2, (
            "Expected exactly two occurrences: one in the sequential "
            "dispatch path, one in the T11.1 parallel dispatch path. A "
            "different count means a guard was removed, merged, or "
            "duplicated beyond the two known, audited call sites."
        )


class TestNeitherGuardIsATautology:
    def test_no_guard_is_unconditionally_true(self) -> None:
        # A regression that replaces either real condition with a tautology
        # (e.g. "if True:") must be caught even if the literal guard text
        # elsewhere still superficially matches other patterns.
        source = _source()
        for match in re.finditer(r'reason="stale_presence_superseded"', source):
            preceding = source[:match.start()]
            window = preceding[-400:]
            assert "if True:" not in window, (
                f"A stale_presence_superseded guard near offset {match.start()} "
                "appears to have been replaced with an unconditional `if True:`."
            )
