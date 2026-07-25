"""Structural guard for coordinator.py's stale-generation dispatch-cancel
exemption for Safety intents (v1.2.0-beta.1, T11 — pre-existing mechanism,
confirmed sufficient by T11's architecture analysis as the existing
preemption model; this test locks in that Safety stays exempt).

Why structural, not behavioral: exercising the real race (a presence/safety
event bumping self._dispatch_generation WHILE the coordinator's own Pass-2
dispatch loop is mid-await for a DIFFERENT window) requires driving a full,
concurrent _async_update_data() cycle — this codebase's own test suite does
not do that for equally deep coordinator-internal timing behavior (see
test_startup_coordinator_dispatch.py's docstring and T11's architecture
analysis notes). A structural check on the guard's own condition is the
established alternative for this kind of hard-to-behaviorally-test
invariant in this codebase (see test_window_decision_category_completeness.py's
AST-based approach for a precedent).

Coverage:
  SGE-01  The stale-intent (generation) cancellation block in the Pass-2
          dispatch loop is still gated on `not _intent.is_safety` — i.e. a
          safety intent can never be cancelled by a mid-cycle generation
          bump, only non-safety intents can.
  SGE-02  The guard is not accidentally duplicated/removed — exactly one
          occurrence exists.
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


class TestSafetyExemptFromStaleGenerationCancellation:
    def test_stale_generation_guard_is_gated_on_not_is_safety(self) -> None:
        source = _source()
        # The exact guard shape: "if not _intent.is_safety:" immediately
        # followed (within a few lines) by the generation-mismatch check
        # that produces a "stale_presence_superseded" NOT_ATTEMPTED result.
        pattern = re.compile(
            r"if not _intent\.is_safety:\s*\n"
            r"\s*if self\._dispatch_generation != _this_dispatch_gen:\s*\n"
            r"(?:.*\n){0,4}?\s*reason=\"stale_presence_superseded\",",
        )
        assert pattern.search(source), (
            "The stale-generation dispatch cancellation must remain gated on "
            "`not _intent.is_safety` — a Safety command must never be "
            "cancelled by a mid-cycle generation bump. If this guard's shape "
            "genuinely changed, update this test's pattern to match the new "
            "(still-safety-exempt) structure rather than removing the check."
        )

    def test_stale_presence_superseded_reason_appears_exactly_once(self) -> None:
        source = _source()
        assert source.count('reason="stale_presence_superseded"') == 1

    def test_guard_not_unconditionally_true(self) -> None:
        # A regression that replaces the real condition with a tautology
        # (e.g. "if True:") must be caught even if the literal guard text
        # elsewhere still superficially matches other patterns.
        source = _source()
        stale_block_start = source.index('reason="stale_presence_superseded"')
        preceding = source[:stale_block_start]
        # The nearest preceding "if ...:" at the correct nesting is the
        # guard itself — assert it is NOT a bare tautology.
        last_if = preceding.rfind("\n                            if ")
        assert last_if != -1, "Could not locate the guard's enclosing if-statement"
        guard_line = preceding[last_if:].splitlines()[1] if "\n" in preceding[last_if:] else preceding[last_if:]
        assert "if True:" not in preceding[last_if:last_if + 200]
