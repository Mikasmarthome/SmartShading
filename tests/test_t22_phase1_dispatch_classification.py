"""T22 Phase 1 — pure semantic dispatch classification
(engines/dispatch_classification.py). Covers: FULL_OPEN/INTERMEDIATE/
NO_MOVEMENT/BLOCKED content tests, robustness edge cases, and architecture
protection tests (no HA/coordinator/service imports, no queue/executor,
exactly four class values).
"""
from __future__ import annotations

import math

from custom_components.smartshading.engines.dispatch_classification import (
    DispatchClassification,
    DispatchTargetClass,
    classify_dispatch_target,
)

_TOL = 3  # matches command_filter.py's ExecutionCapability.position_tolerance default


def _classify(**kwargs):
    kwargs.setdefault("position_tolerance", _TOL)
    return classify_dispatch_target(**kwargs)


class TestFullOpen:
    def test_exact_full_open_target(self) -> None:
        r = _classify(resolved_target_ha=100, current_position_ha=50, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.FULL_OPEN
        assert r.movement_required is True

    def test_target_within_open_tolerance(self) -> None:
        r = _classify(resolved_target_ha=98, current_position_ha=50, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.FULL_OPEN

    def test_target_just_outside_open_tolerance_is_intermediate(self) -> None:
        r = _classify(resolved_target_ha=96, current_position_ha=50, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.INTERMEDIATE

    def test_inverted_cover_already_normalized_before_call(self) -> None:
        # Inversion is handled upstream (position_semantics.to_ha_position);
        # this module only ever sees the already-normalized HA value.
        r = _classify(resolved_target_ha=100, current_position_ha=0, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.FULL_OPEN

    def test_current_not_at_target_requires_movement(self) -> None:
        r = _classify(resolved_target_ha=100, current_position_ha=20, dispatch_action="sent")
        assert r.movement_required is True


class TestIntermediate:
    def test_typical_shading_position(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=100, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.INTERMEDIATE

    def test_fully_closed_target(self) -> None:
        r = _classify(resolved_target_ha=0, current_position_ha=100, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.INTERMEDIATE
        assert r.normalized_target == 0

    def test_target_zero_is_preserved_not_lost(self) -> None:
        r = _classify(resolved_target_ha=0, current_position_ha=50, dispatch_action="sent")
        assert r.normalized_target == 0
        assert r.target_class is not DispatchTargetClass.BLOCKED

    def test_target_just_below_open_tolerance(self) -> None:
        r = _classify(resolved_target_ha=97 - _TOL, current_position_ha=50, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.INTERMEDIATE

    def test_unknown_current_position_with_valid_target(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=None, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.INTERMEDIATE

    def test_assumed_state_position_treated_like_any_current_position(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=90, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.INTERMEDIATE

    def test_closed_target_with_unknown_current_position_is_not_assumed_no_movement(self) -> None:
        # A closed target must not be assumed equal to an unknown current
        # position — that would silently fabricate a "nothing to do"
        # conclusion the data doesn't actually support.
        r = _classify(resolved_target_ha=0, current_position_ha=None, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.INTERMEDIATE


class TestNoMovement:
    def test_current_exactly_at_target(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=40, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.NO_MOVEMENT
        assert r.movement_required is False

    def test_current_within_tolerance_of_target(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=42, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.NO_MOVEMENT

    def test_existing_same_position_no_dispatch_reason(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=None, dispatch_action="unchanged")
        assert r.target_class is DispatchTargetClass.NO_MOVEMENT

    def test_target_zero_and_current_zero(self) -> None:
        r = _classify(resolved_target_ha=0, current_position_ha=0, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.NO_MOVEMENT

    def test_full_open_target_already_at_full_open(self) -> None:
        r = _classify(resolved_target_ha=100, current_position_ha=100, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.NO_MOVEMENT


class TestBlocked:
    def test_manual_override(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=90, dispatch_action="blocked",
                      blocked_reason="manual_override")
        assert r.target_class is DispatchTargetClass.BLOCKED

    def test_contact_hold(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=90, dispatch_action="blocked",
                      blocked_reason="behavior_mode_hold")
        assert r.target_class is DispatchTargetClass.BLOCKED

    def test_night_hold(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=90, dispatch_action="blocked",
                      blocked_reason="night_contact_blocked")
        assert r.target_class is DispatchTargetClass.BLOCKED

    def test_unavailable(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=None, dispatch_action="blocked",
                      blocked_reason="cover_unavailable")
        assert r.target_class is DispatchTargetClass.BLOCKED

    def test_generic_command_filter_block(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=90, dispatch_action="blocked",
                      blocked_reason="comfort_position_hold")
        assert r.target_class is DispatchTargetClass.BLOCKED

    def test_block_takes_precedence_over_same_position(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=40, dispatch_action="blocked",
                      blocked_reason="manual_override")
        assert r.target_class is DispatchTargetClass.BLOCKED

    def test_block_takes_precedence_over_full_open(self) -> None:
        r = _classify(resolved_target_ha=100, current_position_ha=0, dispatch_action="blocked",
                      blocked_reason="manual_override")
        assert r.target_class is DispatchTargetClass.BLOCKED


class TestSafetyPassthrough:
    def test_safety_ignores_comfort_only_block_reason(self) -> None:
        r = _classify(resolved_target_ha=0, current_position_ha=50, dispatch_action="sent",
                      blocked_reason="guard_action_interval", is_safety=True)
        assert r.target_class is not DispatchTargetClass.BLOCKED

    def test_safety_still_blocked_by_manual_override(self) -> None:
        r = _classify(resolved_target_ha=0, current_position_ha=50, dispatch_action="blocked",
                      blocked_reason="manual_override", is_safety=True)
        assert r.target_class is DispatchTargetClass.BLOCKED

    def test_safety_still_blocked_by_cover_unavailable(self) -> None:
        r = _classify(resolved_target_ha=0, current_position_ha=None, dispatch_action="blocked",
                      blocked_reason="cover_unavailable", is_safety=True)
        assert r.target_class is DispatchTargetClass.BLOCKED

    def test_safety_movement_not_silently_lost(self) -> None:
        # A safety target requiring real movement, with only a comfort-only
        # block reason present, must classify by its actual target/position
        # data — never silently vanish as BLOCKED.
        r = _classify(resolved_target_ha=0, current_position_ha=80, dispatch_action="sent",
                      blocked_reason="same_position", is_safety=True)
        assert r.target_class is DispatchTargetClass.INTERMEDIATE
        assert r.movement_required is True


class TestRobustness:
    def test_target_position_zero_not_missing(self) -> None:
        r = _classify(resolved_target_ha=0, current_position_ha=50, dispatch_action="sent")
        assert r.normalized_target == 0

    def test_current_position_zero_not_missing(self) -> None:
        r = _classify(resolved_target_ha=0, current_position_ha=0, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.NO_MOVEMENT

    def test_missing_current_position_never_forces_no_movement(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=None, dispatch_action="sent")
        assert r.target_class is not DispatchTargetClass.NO_MOVEMENT

    def test_missing_target_is_blocked_no_target_position(self) -> None:
        r = _classify(resolved_target_ha=None, current_position_ha=40, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.BLOCKED
        assert r.reason == "blocked:no_target_position"

    def test_invalid_non_numeric_target_never_silently_misclassified(self) -> None:
        r = _classify(resolved_target_ha="not-a-number", current_position_ha=40,
                      dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.BLOCKED

    def test_nan_target_is_blocked_not_intermediate(self) -> None:
        r = _classify(resolved_target_ha=math.nan, current_position_ha=40, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.BLOCKED

    def test_infinity_target_is_blocked(self) -> None:
        r = _classify(resolved_target_ha=math.inf, current_position_ha=40, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.BLOCKED

    def test_out_of_range_target_is_clamped_not_rejected(self) -> None:
        r = _classify(resolved_target_ha=150, current_position_ha=50, dispatch_action="sent")
        assert r.normalized_target == 100
        assert r.target_class is DispatchTargetClass.FULL_OPEN

    def test_negative_target_is_clamped(self) -> None:
        r = _classify(resolved_target_ha=-10, current_position_ha=50, dispatch_action="sent")
        assert r.normalized_target == 0

    def test_result_is_immutable(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=40, dispatch_action="sent")
        try:
            r.target_class = DispatchTargetClass.BLOCKED
        except Exception:
            pass
        else:
            raise AssertionError("DispatchClassification must be frozen")

    def test_no_local_magic_number_tolerance_ignored_when_caller_supplies_one(self) -> None:
        # A wider caller-supplied tolerance must actually take effect — proves
        # the function has no independent hardcoded tolerance overriding it.
        r = _classify(resolved_target_ha=100, current_position_ha=90, dispatch_action="sent",
                      position_tolerance=15)
        assert r.target_class is DispatchTargetClass.NO_MOVEMENT

    def test_default_tolerance_is_not_silently_widened(self) -> None:
        # Gap-closing test: a difference of 10 is within a wrongly-hardcoded
        # 15 but outside the caller-supplied tolerance of 3 — proves no local
        # tolerance constant silently overrides the caller's value.
        r = _classify(resolved_target_ha=40, current_position_ha=50, dispatch_action="sent",
                      position_tolerance=3)
        assert r.target_class is not DispatchTargetClass.NO_MOVEMENT


class TestPriorityOrder:
    def test_blocked_checked_before_no_movement(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=40, dispatch_action="unchanged",
                      blocked_reason="manual_override")
        assert r.target_class is DispatchTargetClass.BLOCKED

    def test_no_movement_checked_before_full_open(self) -> None:
        r = _classify(resolved_target_ha=100, current_position_ha=100, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.NO_MOVEMENT

    def test_no_movement_checked_before_intermediate(self) -> None:
        r = _classify(resolved_target_ha=40, current_position_ha=40, dispatch_action="sent")
        assert r.target_class is DispatchTargetClass.NO_MOVEMENT


class TestArchitectureProtection:
    def test_module_has_exactly_the_four_approved_enum_values(self) -> None:
        assert {m.value for m in DispatchTargetClass} == {
            "full_open", "intermediate", "no_movement", "blocked",
        }

    def test_enum_values_are_stable_lowercase_strings(self) -> None:
        for member in DispatchTargetClass:
            assert member.value == member.value.lower()
            assert isinstance(member.value, str)

    def test_module_imports_no_coordinator_or_ha_service_layer(self) -> None:
        import ast
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent
            / "custom_components" / "smartshading" / "engines" / "dispatch_classification.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src, filename="dispatch_classification.py")
        forbidden_substrings = ("coordinator", "homeassistant", "hass", "cover_control.command_filter",
                                "cover_control.dispatch", "cover_control.global_dispatch")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module_name = getattr(node, "module", None) or ""
                names = [alias.name for alias in node.names]
                joined = (module_name + " " + " ".join(names)).lower()
                for forbidden in forbidden_substrings:
                    assert forbidden not in joined, (
                        f"dispatch_classification.py must not import {forbidden!r}: {joined}"
                    )

    def test_no_service_call_literals_in_source(self) -> None:
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent
            / "custom_components" / "smartshading" / "engines" / "dispatch_classification.py"
        ).read_text(encoding="utf-8")
        for forbidden in ("open_cover", "set_cover_position", "async_call", "hass.services"):
            assert forbidden not in src

    def test_no_queue_or_executor_class_defined(self) -> None:
        import ast
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent
            / "custom_components" / "smartshading" / "engines" / "dispatch_classification.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src, filename="dispatch_classification.py")
        class_names = {n.name.lower() for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        for forbidden in ("dispatchplan", "dispatchplanitem", "executor", "queue"):
            assert not any(forbidden in name for name in class_names), (
                f"unexpected class matching {forbidden!r} found in Phase 1 module: {class_names}"
            )

    def test_result_object_has_no_speculative_fields(self) -> None:
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(DispatchClassification)}
        assert field_names == {"target_class", "reason", "normalized_target", "movement_required"}
        for forbidden in ("queue_position", "plan_id", "completion_method", "timeout", "trigger"):
            assert forbidden not in field_names
