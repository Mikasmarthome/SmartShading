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
          cancellation is ALSO gated on `not intent.is_safety` — and (T22
          cross-cycle PARALLEL safety preemption) now additionally checks
          cancellation.is_set() alongside the generation check, reporting
          the same stable "safety_preempted" reason the rest of Phase 5c's
          preemption paths already use (not "stale_presence_superseded" —
          that string is reserved for the two sites below).
  SGE-03  Exactly two occurrences of "stale_presence_superseded" exist —
          the original sequential-path guard, plus (T22 Phase 4b) the
          DispatchPlanExecutor comfort pre-pass's own defensive fallback in
          _predispatch_sequential_plan(), used only when an eligible
          comfort item never produced an executor result (e.g. generation
          went stale before dispatch started). Safety is never part of
          that pre-pass's plan at all (excluded up front), so this second
          site can never affect a safety intent — it exists purely as the
          comfort-path's own missing-result guard. The T11.1 parallel-path
          guard (SGE-02) now uses "safety_preempted" instead — see that
          test for its own dedicated occurrence check. Any other count
          means a guard was removed, merged, or duplicated beyond these
          two known, audited call sites.
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
        # T22 cross-cycle PARALLEL safety preemption: the guard now checks
        # generation OR cancellation, still gated on `not intent.is_safety`,
        # reporting "safety_preempted" (not "stale_presence_superseded").
        pattern = re.compile(
            r"if not intent\.is_safety and \(\s*\n"
            r"\s*self\._dispatch_generation != this_dispatch_gen\s*\n"
            r"\s*or \(cancellation is not None and cancellation\.is_set\(\)\)\s*\n"
            r"\s*\):\s*\n"
            r"(?:.*\n){0,4}?.*reason=\"safety_preempted\"",
        )
        assert pattern.search(source), (
            "The T11.1 parallel-batch dispatch path "
            "(_dispatch_one_parallel_item) must remain gated on "
            "`not intent.is_safety` for BOTH the generation-mismatch and "
            "cancellation checks — a Safety command dispatched as part of "
            "a concurrent batch must never be cancelled by a mid-cycle "
            "generation bump or a preemption signal either."
        )

    def test_stale_generation_guard_also_checks_cancellation(self) -> None:
        # T22 cross-cycle PARALLEL safety preemption specifically: a fast/
        # synchronous dispatch coroutine could otherwise complete before
        # _race_gather_against_cancellation's own race resolves — this
        # per-item check is the real, additional protection for that gap.
        source = _source()
        assert "cancellation is not None and cancellation.is_set()" in source, (
            "_dispatch_one_parallel_item must check cancellation.is_set() "
            "immediately before its own service call, in addition to the "
            "generation check — otherwise a dispatch that completes "
            "without yielding to the event loop can slip past the "
            "batch-level gather()/cancellation race entirely."
        )


class TestExactlyTwoOccurrencesOneNoMoreNoLess:
    def test_stale_presence_superseded_reason_appears_exactly_two_times(self) -> None:
        source = _source()
        # Bare-string count (not `reason="..."`-prefixed) so this survives
        # T22 Phase 5a's refactor of the second site into a ternary
        # (`reason = (... if ... else "stale_presence_superseded")`)
        # without weakening the invariant itself.
        assert source.count('"stale_presence_superseded"') == 2, (
            "Expected exactly two occurrences: the original sequential "
            "dispatch path guard, and (T22 Phase 4b/5a) "
            "_predispatch_sequential_plan()'s own missing-executor-result "
            "fallback for the comfort pre-pass — see this file's module "
            "docstring (SGE-03) for the full rationale. The T11.1 "
            "parallel-path guard now uses \"safety_preempted\" instead "
            "(see TestParallelPathSafetyExemption). A different count "
            "means a guard was removed, merged, or duplicated beyond "
            "these two known, audited call sites."
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
