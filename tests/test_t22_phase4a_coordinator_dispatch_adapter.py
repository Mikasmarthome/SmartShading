"""T22 Phase 4a: tests for the coordinator -> DispatchPlanItem adapter.

Covers classify_cover_intent(), build_dispatch_plan_item(), and
build_cycle_dispatch_plan() in
custom_components/smartshading/cover_control/coordinator_dispatch_adapter.py.
Pure unit tests — no Home Assistant, no coordinator instance needed.
"""
from datetime import datetime, timezone

from custom_components.smartshading.cover_control.coordinator_dispatch_adapter import (
    build_cycle_dispatch_plan,
    build_dispatch_plan_item,
    classify_cover_intent,
)
from custom_components.smartshading.cover_control.command_filter import (
    BLOCKED_MANUAL_OVERRIDE,
    BLOCKED_NO_TARGET_POSITION,
    BLOCKED_SAME_POSITION,
)
from custom_components.smartshading.cover_control.execution_plan import (
    CoverCommandType,
    CoverIntent,
)
from custom_components.smartshading.engines.dispatch_classification import (
    DispatchTargetClass,
)


def _intent(
    *,
    cover_entity_id="cover.a",
    command_type=CoverCommandType.MOVE_TO_POSITION,
    target_position_ha=40,
    is_safety=False,
    allowed=True,
    blocked_reason=None,
) -> CoverIntent:
    return CoverIntent(
        cover_entity_id=cover_entity_id,
        command_type=command_type,
        target_position_internal=None,
        target_position_ha=target_position_ha,
        target_tilt=None,
        is_safety=is_safety,
        execution_mode="automatic",
        allowed=allowed,
        blocked_reason=blocked_reason,
        decided_by="TestEvaluator",
        computed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# classify_cover_intent
# ---------------------------------------------------------------------------

class TestClassifyCoverIntent:
    def test_move_to_position_intermediate(self):
        intent = _intent(target_position_ha=40)
        c = classify_cover_intent(intent, current_position_ha=80)
        assert c.target_class is DispatchTargetClass.INTERMEDIATE
        assert c.normalized_target == 40

    def test_move_to_position_full_open(self):
        intent = _intent(target_position_ha=100)
        c = classify_cover_intent(intent, current_position_ha=40)
        assert c.target_class is DispatchTargetClass.FULL_OPEN

    def test_target_zero_preserved_not_lost_to_truthiness(self):
        intent = _intent(target_position_ha=0)
        c = classify_cover_intent(intent, current_position_ha=80)
        assert c.target_class is DispatchTargetClass.INTERMEDIATE
        assert c.normalized_target == 0

    def test_no_op_same_position_maps_to_no_movement_not_blocked(self):
        # allowed=False + BLOCKED_SAME_POSITION -> command_type NO_OP per
        # execution_plan._command_type_from_filter(). Must classify as
        # NO_MOVEMENT, never BLOCKED.
        intent = _intent(
            command_type=CoverCommandType.NO_OP,
            allowed=False,
            blocked_reason=BLOCKED_SAME_POSITION,
            target_position_ha=40,
        )
        c = classify_cover_intent(intent, current_position_ha=40)
        assert c.target_class is DispatchTargetClass.NO_MOVEMENT

    def test_no_op_no_target_maps_to_blocked_no_target(self):
        # No target position at all takes priority over the NO_OP/"unchanged"
        # signal in classify_dispatch_target's own priority order (BLOCKED
        # is checked before dispatch_action) — this is correct, not a defect.
        intent = _intent(
            command_type=CoverCommandType.NO_OP,
            allowed=False,
            blocked_reason=BLOCKED_NO_TARGET_POSITION,
            target_position_ha=None,
        )
        c = classify_cover_intent(intent, current_position_ha=None)
        assert c.target_class is DispatchTargetClass.BLOCKED
        assert c.reason == "blocked:no_target_position"

    def test_genuinely_blocked_maps_to_blocked(self):
        intent = _intent(
            command_type=CoverCommandType.BLOCKED,
            allowed=False,
            blocked_reason=BLOCKED_MANUAL_OVERRIDE,
            target_position_ha=40,
        )
        c = classify_cover_intent(intent, current_position_ha=80)
        assert c.target_class is DispatchTargetClass.BLOCKED
        assert c.reason == f"blocked:{BLOCKED_MANUAL_OVERRIDE}"

    def test_safety_intent_still_classified_normally(self):
        intent = _intent(target_position_ha=100, is_safety=True)
        c = classify_cover_intent(intent, current_position_ha=40)
        assert c.target_class is DispatchTargetClass.FULL_OPEN

    def test_default_tolerance_is_dispatch_completion_default(self):
        from custom_components.smartshading.cover_control.dispatch_completion import (
            DEFAULT_POSITION_TOLERANCE,
        )
        intent = _intent(target_position_ha=40)
        c = classify_cover_intent(
            intent, current_position_ha=40 + DEFAULT_POSITION_TOLERANCE,
        )
        assert c.target_class is DispatchTargetClass.NO_MOVEMENT


# ---------------------------------------------------------------------------
# build_dispatch_plan_item
# ---------------------------------------------------------------------------

class TestBuildDispatchPlanItem:
    def test_executable_item_fields(self):
        intent = _intent(cover_entity_id="cover.a", target_position_ha=40)
        c = classify_cover_intent(intent, current_position_ha=80)
        item = build_dispatch_plan_item(
            zone_id="z1", zone_index=0, cover_index=0, intent=intent,
            classification=c, decision_ref="d1", zone_generation=3,
        )
        assert item.zone_id == "z1"
        assert item.zone_index == 0
        assert item.cover_entity_id == "cover.a"
        assert item.cover_index == 0
        assert item.target_ha == 40
        assert item.target_class is DispatchTargetClass.INTERMEDIATE
        assert item.decision_ref == "d1"
        assert item.zone_generation == 3

    def test_executable_item_reason_is_none(self):
        intent = _intent(target_position_ha=40)
        c = classify_cover_intent(intent, current_position_ha=80)
        item = build_dispatch_plan_item(
            zone_id="z1", zone_index=0, cover_index=0, intent=intent,
            classification=c, decision_ref="d1", zone_generation=0,
        )
        assert item.reason is None

    def test_blocked_item_carries_diagnostic_reason(self):
        intent = _intent(
            command_type=CoverCommandType.BLOCKED, allowed=False,
            blocked_reason=BLOCKED_MANUAL_OVERRIDE, target_position_ha=40,
        )
        c = classify_cover_intent(intent, current_position_ha=80)
        item = build_dispatch_plan_item(
            zone_id="z1", zone_index=0, cover_index=0, intent=intent,
            classification=c, decision_ref="d1", zone_generation=0,
        )
        assert item.target_class is DispatchTargetClass.BLOCKED
        assert item.reason == f"blocked:{BLOCKED_MANUAL_OVERRIDE}"

    def test_no_movement_item_carries_diagnostic_reason(self):
        intent = _intent(
            command_type=CoverCommandType.NO_OP, allowed=False,
            blocked_reason=BLOCKED_SAME_POSITION, target_position_ha=40,
        )
        c = classify_cover_intent(intent, current_position_ha=40)
        item = build_dispatch_plan_item(
            zone_id="z1", zone_index=0, cover_index=0, intent=intent,
            classification=c, decision_ref="d1", zone_generation=0,
        )
        assert item.target_class is DispatchTargetClass.NO_MOVEMENT
        assert item.reason is not None

    def test_target_zero_preserved_through_item(self):
        intent = _intent(target_position_ha=0)
        c = classify_cover_intent(intent, current_position_ha=80)
        item = build_dispatch_plan_item(
            zone_id="z1", zone_index=0, cover_index=0, intent=intent,
            classification=c, decision_ref="d1", zone_generation=0,
        )
        assert item.target_ha == 0

    def test_target_hundred_preserved_through_item(self):
        intent = _intent(target_position_ha=100)
        c = classify_cover_intent(intent, current_position_ha=40)
        item = build_dispatch_plan_item(
            zone_id="z1", zone_index=0, cover_index=0, intent=intent,
            classification=c, decision_ref="d1", zone_generation=0,
        )
        assert item.target_ha == 100
        assert item.target_class is DispatchTargetClass.FULL_OPEN

    def test_no_second_enum_used(self):
        intent = _intent(target_position_ha=40)
        c = classify_cover_intent(intent, current_position_ha=80)
        item = build_dispatch_plan_item(
            zone_id="z1", zone_index=0, cover_index=0, intent=intent,
            classification=c, decision_ref="d1", zone_generation=0,
        )
        assert type(item.target_class) is DispatchTargetClass


# ---------------------------------------------------------------------------
# build_cycle_dispatch_plan
# ---------------------------------------------------------------------------

class TestBuildCycleDispatchPlan:
    def _item(self, cover_entity_id="cover.a", target_ha=40,
              target_class=DispatchTargetClass.INTERMEDIATE):
        return build_dispatch_plan_item(
            zone_id="z1", zone_index=0, cover_index=0,
            intent=_intent(cover_entity_id=cover_entity_id, target_position_ha=target_ha),
            classification=classify_cover_intent(
                _intent(cover_entity_id=cover_entity_id, target_position_ha=target_ha),
                current_position_ha=80,
            ),
            decision_ref="d1", zone_generation=0,
        )

    def test_plan_id_format(self):
        plan = build_cycle_dispatch_plan(
            entry_id="entry123", cycle_counter=7, items=[self._item()],
            trigger="scheduled_evaluation", created_at="t0",
        )
        assert plan.plan_id == "entry123:7"

    def test_plan_id_not_empty(self):
        plan = build_cycle_dispatch_plan(
            entry_id="e", cycle_counter=0, items=[self._item()],
            trigger="scheduled_evaluation", created_at="t0",
        )
        assert plan.plan_id

    def test_plan_id_unique_across_cycles(self):
        p1 = build_cycle_dispatch_plan(
            entry_id="entry123", cycle_counter=1, items=[self._item()],
            trigger="scheduled_evaluation", created_at="t0",
        )
        p2 = build_cycle_dispatch_plan(
            entry_id="entry123", cycle_counter=2, items=[self._item()],
            trigger="scheduled_evaluation", created_at="t1",
        )
        assert p1.plan_id != p2.plan_id

    def test_plan_id_unique_across_entries_same_cycle(self):
        p1 = build_cycle_dispatch_plan(
            entry_id="entryA", cycle_counter=5, items=[self._item()],
            trigger="scheduled_evaluation", created_at="t0",
        )
        p2 = build_cycle_dispatch_plan(
            entry_id="entryB", cycle_counter=5, items=[self._item()],
            trigger="scheduled_evaluation", created_at="t0",
        )
        assert p1.plan_id != p2.plan_id

    def test_trigger_not_empty(self):
        plan = build_cycle_dispatch_plan(
            entry_id="e", cycle_counter=0, items=[self._item()],
            trigger="state_change", created_at="t0",
        )
        assert plan.trigger == "state_change"

    def test_items_sorted_by_class_priority(self):
        full_open_item = build_dispatch_plan_item(
            zone_id="z1", zone_index=0, cover_index=1,
            intent=_intent(cover_entity_id="cover.b", target_position_ha=100),
            classification=classify_cover_intent(
                _intent(cover_entity_id="cover.b", target_position_ha=100),
                current_position_ha=40,
            ),
            decision_ref="d1", zone_generation=0,
        )
        intermediate_item = self._item(cover_entity_id="cover.a", target_ha=40)
        plan = build_cycle_dispatch_plan(
            entry_id="e", cycle_counter=0,
            items=[intermediate_item, full_open_item],
            trigger="scheduled_evaluation", created_at="t0",
        )
        assert plan.items[0].target_class is DispatchTargetClass.FULL_OPEN
        assert plan.items[1].target_class is DispatchTargetClass.INTERMEDIATE


# ---------------------------------------------------------------------------
# Architecture protection
# ---------------------------------------------------------------------------

class TestArchitectureProtection:
    def _source(self) -> str:
        from pathlib import Path
        return (
            Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
            / "cover_control" / "coordinator_dispatch_adapter.py"
        ).read_text(encoding="utf-8")

    def test_no_homeassistant_or_coordinator_import(self):
        import ast
        tree = ast.parse(self._source(), filename="coordinator_dispatch_adapter.py")
        forbidden = ("homeassistant", "coordinator")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module_name = getattr(node, "module", None) or ""
                names = [alias.name for alias in node.names]
                joined = (module_name + " " + " ".join(names)).lower()
                for f in forbidden:
                    assert f not in joined, f"must not import {f!r}: {joined}"

    def test_no_local_dispatch_target_class_definition(self):
        src = self._source()
        assert "class DispatchTargetClass" not in src

    def test_no_service_call(self):
        src = self._source()
        assert "async_call" not in src
        assert "async def" not in src
