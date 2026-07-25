"""Tests for the T11 completion-diagnostics fields added to ExecutionResult
(cover_control/execution_result.py) — v1.2.0-beta.1.

Coverage:
  ERC-01  build_sent_result() defaults completion_method/wait/timed_out to
          None/None/False (PARALLEL/SPACED dispatches never populate them).
  ERC-02  dataclasses.replace() can attach completion diagnostics after the
          fact (the pattern coordinator.py uses post-completion-wait),
          without disturbing any other field.
  ERC-03  build_blocked_result()/build_not_attempted_result() also default
          the new fields (never populated for non-SENT results).
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from custom_components.smartshading.cover_control.command_filter import ExecutionMode
from custom_components.smartshading.cover_control.execution_plan import (
    CoverCommandType,
    CoverIntent,
)
from custom_components.smartshading.cover_control.execution_result import (
    build_blocked_result,
    build_not_attempted_result,
    build_sent_result,
)


def _intent(**overrides) -> CoverIntent:
    defaults = dict(
        cover_entity_id="cover.test",
        command_type=CoverCommandType.MOVE_TO_POSITION,
        target_position_internal=50,
        target_position_ha=50,
        target_tilt=None,
        execution_mode=ExecutionMode.AUTOMATIC.value,
        is_safety=False,
        allowed=True,
        blocked_reason=None,
        decided_by="TestEvaluator",
        computed_at=datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return CoverIntent(**defaults)


class TestSentResultDefaults:
    def test_completion_fields_default_none_false(self) -> None:
        result = build_sent_result(
            _intent(), sent_at_utc=datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
            reason="test dispatch",
        )
        assert result.completion_method is None
        assert result.completion_wait_s is None
        assert result.completion_timed_out is False


class TestReplaceAttachesCompletionDiagnostics:
    def test_replace_sets_completion_fields_without_disturbing_others(self) -> None:
        original = build_sent_result(
            _intent(), sent_at_utc=datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
            reason="test dispatch",
        )
        updated = dataclasses.replace(
            original, completion_method="position", completion_wait_s=12.5,
            completion_timed_out=False,
        )
        assert updated.completion_method == "position"
        assert updated.completion_wait_s == 12.5
        assert updated.completion_timed_out is False
        # Everything else is untouched.
        assert updated.entity_id == original.entity_id
        assert updated.status == original.status
        assert updated.target_position_ha == original.target_position_ha
        assert updated.sent_at_utc == original.sent_at_utc

    def test_replace_can_mark_timeout(self) -> None:
        original = build_sent_result(
            _intent(), sent_at_utc=datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
            reason="test dispatch",
        )
        updated = dataclasses.replace(
            original, completion_method="timeout", completion_wait_s=40.0,
            completion_timed_out=True,
        )
        assert updated.completion_timed_out is True
        assert updated.completion_method == "timeout"


class TestNonSentResultsDefaultToo:
    def test_blocked_result_defaults(self) -> None:
        result = build_blocked_result(_intent(allowed=False, blocked_reason="test"), reason="blocked")
        assert result.completion_method is None
        assert result.completion_timed_out is False

    def test_not_attempted_result_defaults(self) -> None:
        result = build_not_attempted_result(_intent(), reason="not attempted")
        assert result.completion_method is None
        assert result.completion_timed_out is False
