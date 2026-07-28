"""T22 Phase 2 — pure DispatchPlan/DispatchPlanItem data model, deterministic
sorting, and deduplication (cover_control/dispatch_plan.py).
"""
from __future__ import annotations

import math

import pytest

from custom_components.smartshading.cover_control.dispatch_plan import (
    DispatchPlan,
    DispatchPlanItem,
    build_dispatch_plan,
)
from custom_components.smartshading.engines.dispatch_classification import DispatchTargetClass

_NOW = "2026-06-15T14:00:00+00:00"


def _item(*, zone_id="z1", zone_index=0, cover_entity_id="cover.a", cover_index=0,
          target_ha=40, target_class=DispatchTargetClass.INTERMEDIATE, decision_ref="d1",
          zone_generation=0, reason=None) -> DispatchPlanItem:
    return DispatchPlanItem(
        zone_id=zone_id, zone_index=zone_index, cover_entity_id=cover_entity_id,
        cover_index=cover_index, target_ha=target_ha, target_class=target_class,
        decision_ref=decision_ref, zone_generation=zone_generation, reason=reason,
    )


def _plan(items, *, plan_id="p1", trigger="scheduled_evaluation", created_at=_NOW) -> DispatchPlan:
    return build_dispatch_plan(plan_id=plan_id, trigger=trigger, items=items, created_at=created_at)


class TestDataModel:
    def test_item_fully_constructible(self) -> None:
        i = _item()
        assert i.cover_entity_id == "cover.a"

    def test_plan_fully_constructible(self) -> None:
        p = _plan([_item()])
        assert p.plan_id == "p1"
        assert len(p.items) == 1

    def test_item_is_frozen(self) -> None:
        i = _item()
        with pytest.raises(Exception):
            i.target_ha = 50

    def test_plan_is_frozen(self) -> None:
        p = _plan([_item()])
        with pytest.raises(Exception):
            p.plan_id = "other"

    def test_item_uses_slots(self) -> None:
        i = _item()
        assert not hasattr(i, "__dict__")

    def test_plan_uses_slots(self) -> None:
        p = _plan([_item()])
        assert not hasattr(p, "__dict__")

    def test_input_list_defensively_copied(self) -> None:
        items = [_item()]
        p = _plan(items)
        items.append(_item(cover_entity_id="cover.b", target_class=DispatchTargetClass.FULL_OPEN,
                           target_ha=100))
        assert len(p.items) == 1

    def test_empty_plan_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan([_item()], plan_id="")

    def test_empty_trigger_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan([_item()], trigger="")

    def test_invalid_target_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan([_item(target_ha="not-a-number")])

    def test_nan_target_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan([_item(target_ha=math.nan)])

    def test_infinity_target_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan([_item(target_ha=math.inf)])

    def test_target_zero_remains_valid(self) -> None:
        p = _plan([_item(target_ha=0)])
        assert p.items[0].target_ha == 0

    def test_target_100_remains_valid(self) -> None:
        p = _plan([_item(target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)])
        assert p.items[0].target_ha == 100

    def test_intermediate_cannot_carry_full_open_value(self) -> None:
        with pytest.raises(ValueError):
            _plan([_item(target_ha=100, target_class=DispatchTargetClass.INTERMEDIATE)])

    def test_executable_item_requires_target(self) -> None:
        with pytest.raises(ValueError):
            _plan([_item(target_ha=None, target_class=DispatchTargetClass.FULL_OPEN)])

    def test_blocked_item_may_have_no_target(self) -> None:
        p = _plan([_item(target_ha=None, target_class=DispatchTargetClass.BLOCKED,
                         reason="blocked:no_target_position")])
        assert p.items[0].target_ha is None

    def test_negative_zone_index_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan([_item(zone_index=-1)])

    def test_negative_cover_index_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan([_item(cover_index=-1)])

    def test_negative_generation_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan([_item(zone_generation=-1)])

    def test_generation_zero_is_valid(self) -> None:
        p = _plan([_item(zone_generation=0)])
        assert p.items[0].zone_generation == 0

    def test_empty_decision_ref_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan([_item(decision_ref="")])

    def test_plan_id_is_used_verbatim_no_hidden_counter(self) -> None:
        # Calling the builder twice with the identical caller-supplied
        # plan_id must yield the identical plan_id both times — proves no
        # hidden module-level sequence/counter silently mutates it.
        p1 = _plan([_item()], plan_id="fixed-id")
        p2 = _plan([_item()], plan_id="fixed-id")
        assert p1.plan_id == "fixed-id"
        assert p2.plan_id == "fixed-id"


class TestClassOrder:
    def test_full_open_before_intermediate(self) -> None:
        items = [
            _item(cover_entity_id="cover.b", target_class=DispatchTargetClass.INTERMEDIATE, target_ha=40),
            _item(cover_entity_id="cover.a", target_class=DispatchTargetClass.FULL_OPEN, target_ha=100),
        ]
        p = _plan(items)
        assert [i.target_class for i in p.items] == [
            DispatchTargetClass.FULL_OPEN, DispatchTargetClass.INTERMEDIATE,
        ]

    def test_intermediate_before_no_movement(self) -> None:
        items = [
            _item(cover_entity_id="cover.b", target_class=DispatchTargetClass.NO_MOVEMENT, target_ha=40),
            _item(cover_entity_id="cover.a", target_class=DispatchTargetClass.INTERMEDIATE, target_ha=40),
        ]
        p = _plan(items)
        assert [i.target_class for i in p.items] == [
            DispatchTargetClass.INTERMEDIATE, DispatchTargetClass.NO_MOVEMENT,
        ]

    def test_no_movement_before_blocked(self) -> None:
        items = [
            _item(cover_entity_id="cover.b", target_class=DispatchTargetClass.BLOCKED, target_ha=None,
                 reason="blocked:manual_override"),
            _item(cover_entity_id="cover.a", target_class=DispatchTargetClass.NO_MOVEMENT, target_ha=40),
        ]
        p = _plan(items)
        assert [i.target_class for i in p.items] == [
            DispatchTargetClass.NO_MOVEMENT, DispatchTargetClass.BLOCKED,
        ]

    def test_not_alphabetical_string_sort(self) -> None:
        # alphabetically: blocked, full_open, intermediate, no_movement —
        # the real order must NOT match that.
        items = [
            _item(cover_entity_id="cover.a", target_class=DispatchTargetClass.BLOCKED, target_ha=None,
                 reason="blocked:manual_override"),
            _item(cover_entity_id="cover.b", target_class=DispatchTargetClass.FULL_OPEN, target_ha=100),
            _item(cover_entity_id="cover.c", target_class=DispatchTargetClass.INTERMEDIATE, target_ha=40),
            _item(cover_entity_id="cover.d", target_class=DispatchTargetClass.NO_MOVEMENT, target_ha=40),
        ]
        p = _plan(items)
        classes = [i.target_class for i in p.items]
        assert classes != sorted(classes, key=lambda c: c.value)
        assert classes == [
            DispatchTargetClass.FULL_OPEN, DispatchTargetClass.INTERMEDIATE,
            DispatchTargetClass.NO_MOVEMENT, DispatchTargetClass.BLOCKED,
        ]

    def test_random_input_order_yields_identical_output(self) -> None:
        base = [
            _item(cover_entity_id="cover.a", target_class=DispatchTargetClass.BLOCKED, target_ha=None,
                 reason="blocked:manual_override"),
            _item(cover_entity_id="cover.b", target_class=DispatchTargetClass.FULL_OPEN, target_ha=100),
            _item(cover_entity_id="cover.c", target_class=DispatchTargetClass.INTERMEDIATE, target_ha=40),
            _item(cover_entity_id="cover.d", target_class=DispatchTargetClass.NO_MOVEMENT, target_ha=40),
        ]
        forward = _plan(base)
        backward = _plan(list(reversed(base)))
        assert forward.items == backward.items


class TestZoneAndCoverOrder:
    def test_lower_zone_index_first(self) -> None:
        items = [
            _item(zone_id="z2", zone_index=1, cover_entity_id="cover.b", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="z1", zone_index=0, cover_entity_id="cover.a", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
        ]
        p = _plan(items)
        assert [i.zone_id for i in p.items] == ["z1", "z2"]

    def test_zone_index_wins_even_when_zone_id_string_disagrees(self) -> None:
        # Gap-closing: zone_id "zulu" > "alpha" alphabetically, but
        # zone_index must decide first — proves zone_index isn't dropped
        # in favor of the zone_id fallback.
        items = [
            _item(zone_id="alpha", zone_index=1, cover_entity_id="cover.b", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="zulu", zone_index=0, cover_entity_id="cover.a", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
        ]
        p = _plan(items)
        assert [i.zone_id for i in p.items] == ["zulu", "alpha"]

    def test_same_zone_lower_cover_index_first(self) -> None:
        items = [
            _item(cover_entity_id="cover.b", cover_index=1, target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="cover.a", cover_index=0, target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
        ]
        p = _plan(items)
        assert [i.cover_entity_id for i in p.items] == ["cover.a", "cover.b"]

    def test_cover_index_wins_even_when_entity_id_string_disagrees(self) -> None:
        # Gap-closing: entity_id "cover.zulu" > "cover.alpha" alphabetically,
        # but cover_index must decide first.
        items = [
            _item(cover_entity_id="cover.alpha", cover_index=1, target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="cover.zulu", cover_index=0, target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
        ]
        p = _plan(items)
        assert [i.cover_entity_id for i in p.items] == ["cover.zulu", "cover.alpha"]

    def test_tie_falls_back_to_zone_id(self) -> None:
        items = [
            _item(zone_id="z2", zone_index=0, cover_entity_id="cover.a", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="z1", zone_index=0, cover_entity_id="cover.b", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
        ]
        p = _plan(items)
        assert [i.zone_id for i in p.items] == ["z1", "z2"]

    def test_tie_then_falls_back_to_cover_entity_id(self) -> None:
        items = [
            _item(zone_id="z1", zone_index=0, cover_index=0, cover_entity_id="cover.b", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="z1", zone_index=0, cover_index=0, cover_entity_id="cover.a", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
        ]
        p = _plan(items)
        assert [i.cover_entity_id for i in p.items] == ["cover.a", "cover.b"]

    def test_submission_order_does_not_affect_result(self) -> None:
        items = [
            _item(zone_id="z1", zone_index=0, cover_entity_id="cover.a", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="z2", zone_index=1, cover_entity_id="cover.b", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
        ]
        forward = _plan(items)
        backward = _plan(list(reversed(items)))
        assert forward.items == backward.items


class TestGlobalClassOrder:
    def test_full_open_across_zones_before_any_intermediate(self) -> None:
        items = [
            _item(zone_id="z2", zone_index=1, cover_entity_id="c1", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="z1", zone_index=0, cover_entity_id="c2", target_ha=40,
                 target_class=DispatchTargetClass.INTERMEDIATE),
            _item(zone_id="z1", zone_index=0, cover_entity_id="c3", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="z3", zone_index=2, cover_entity_id="c4", target_ha=40,
                 target_class=DispatchTargetClass.INTERMEDIATE),
            _item(zone_id="z2", zone_index=1, cover_entity_id="c5", target_ha=40,
                 target_class=DispatchTargetClass.INTERMEDIATE),
        ]
        p = _plan(items)
        got = [(i.zone_id, i.target_class) for i in p.items]
        assert got == [
            ("z1", DispatchTargetClass.FULL_OPEN),
            ("z2", DispatchTargetClass.FULL_OPEN),
            ("z1", DispatchTargetClass.INTERMEDIATE),
            ("z2", DispatchTargetClass.INTERMEDIATE),
            ("z3", DispatchTargetClass.INTERMEDIATE),
        ]


class TestViews:
    def _mixed_plan(self) -> DispatchPlan:
        items = [
            _item(cover_entity_id="c.full", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="c.inter", target_ha=40, target_class=DispatchTargetClass.INTERMEDIATE),
            _item(cover_entity_id="c.nomove", target_ha=40, target_class=DispatchTargetClass.NO_MOVEMENT),
            _item(cover_entity_id="c.blocked", target_ha=None, target_class=DispatchTargetClass.BLOCKED,
                 reason="blocked:manual_override"),
        ]
        return _plan(items)

    def test_executable_items(self) -> None:
        p = self._mixed_plan()
        assert {i.cover_entity_id for i in p.executable_items} == {"c.full", "c.inter"}

    def test_non_executable_items(self) -> None:
        p = self._mixed_plan()
        assert {i.cover_entity_id for i in p.non_executable_items} == {"c.nomove", "c.blocked"}

    def test_full_open_items(self) -> None:
        p = self._mixed_plan()
        assert [i.cover_entity_id for i in p.full_open_items] == ["c.full"]

    def test_intermediate_items(self) -> None:
        p = self._mixed_plan()
        assert [i.cover_entity_id for i in p.intermediate_items] == ["c.inter"]

    def test_views_are_tuples(self) -> None:
        p = self._mixed_plan()
        assert isinstance(p.executable_items, tuple)
        assert isinstance(p.non_executable_items, tuple)

    def test_views_preserve_deterministic_order(self) -> None:
        p = self._mixed_plan()
        assert p.executable_items[0].target_class is DispatchTargetClass.FULL_OPEN

    def test_zone_ids_derived(self) -> None:
        items = [
            _item(zone_id="z2", zone_index=1, cover_entity_id="c1", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(zone_id="z1", zone_index=0, cover_entity_id="c2", target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
        ]
        p = _plan(items)
        assert p.zone_ids == ("z1", "z2")


class TestDeduplication:
    def test_exact_duplicate_collapsed(self) -> None:
        i = _item()
        p = _plan([i, i])
        assert len(p.items) == 1

    def test_conflicting_target_rejected(self) -> None:
        a = _item(target_ha=40)
        b = _item(target_ha=60)
        with pytest.raises(ValueError):
            _plan([a, b])

    def test_conflicting_class_rejected(self) -> None:
        a = _item(target_class=DispatchTargetClass.INTERMEDIATE, target_ha=40)
        b = _item(target_class=DispatchTargetClass.NO_MOVEMENT, target_ha=40)
        with pytest.raises(ValueError):
            _plan([a, b])

    def test_same_cover_different_zone_rejected(self) -> None:
        a = _item(zone_id="z1", zone_index=0, target_ha=40)
        b = _item(zone_id="z2", zone_index=1, target_ha=40)
        with pytest.raises(ValueError):
            _plan([a, b])

    def test_blocked_and_full_open_for_same_cover_rejected(self) -> None:
        a = _item(target_class=DispatchTargetClass.BLOCKED, target_ha=None,
                 reason="blocked:manual_override")
        b = _item(target_class=DispatchTargetClass.FULL_OPEN, target_ha=100)
        with pytest.raises(ValueError):
            _plan([a, b])

    def test_entity_id_case_is_not_normalized(self) -> None:
        # HA entity_ids are guaranteed lowercase by the platform itself —
        # this module deliberately does not invent case-folding, so two
        # differently-cased strings are treated as different covers.
        a = _item(cover_entity_id="cover.a", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)
        b = _item(cover_entity_id="COVER.A", target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)
        p = _plan([a, b])
        assert len(p.items) == 2

    def test_no_silent_winner_on_conflict(self) -> None:
        a = _item(target_ha=40, decision_ref="d1")
        b = _item(target_ha=40, decision_ref="d2")  # only decision_ref differs
        with pytest.raises(ValueError):
            _plan([a, b])


class TestGenerationSnapshot:
    def test_generation_does_not_affect_sort_order(self) -> None:
        items = [
            _item(cover_entity_id="cover.a", zone_generation=5, target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN),
            _item(cover_entity_id="cover.b", zone_generation=1, target_ha=100,
                 target_class=DispatchTargetClass.FULL_OPEN, cover_index=1),
        ]
        p = _plan(items)
        assert [i.cover_entity_id for i in p.items] == ["cover.a", "cover.b"]

    def test_conflicting_generation_for_same_cover_rejected(self) -> None:
        a = _item(zone_generation=1, target_ha=40)
        b = _item(zone_generation=2, target_ha=40)
        with pytest.raises(ValueError):
            _plan([a, b])


class TestSafetyBoundary:
    def test_known_safety_reason_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan([_item(reason="safety")])

    def test_module_has_no_safety_priority_class(self) -> None:
        from custom_components.smartshading.cover_control import dispatch_plan as mod
        assert not hasattr(mod, "SAFETY")

    def test_no_is_safety_field_on_item(self) -> None:
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(DispatchPlanItem)}
        assert "is_safety" not in field_names

    def test_safety_trigger_alone_does_not_special_case_plan(self) -> None:
        p = _plan([_item(target_ha=100, target_class=DispatchTargetClass.FULL_OPEN)],
                  trigger="safety")
        assert p.trigger == "safety"
        # No behavior difference — items still sort/validate identically.
        assert p.items[0].target_class is DispatchTargetClass.FULL_OPEN


class TestArchitectureProtection:
    def _source(self) -> str:
        from pathlib import Path
        return (
            Path(__file__).resolve().parent.parent
            / "custom_components" / "smartshading" / "cover_control" / "dispatch_plan.py"
        ).read_text(encoding="utf-8")

    def test_no_coordinator_or_ha_import(self) -> None:
        import ast
        tree = ast.parse(self._source(), filename="dispatch_plan.py")
        forbidden = ("coordinator", "homeassistant", "hass", "asyncio")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module_name = getattr(node, "module", None) or ""
                names = [alias.name for alias in node.names]
                joined = (module_name + " " + " ".join(names)).lower()
                for f in forbidden:
                    assert f not in joined, f"must not import {f!r}: {joined}"

    def test_no_executor_lock_or_queue_class(self) -> None:
        import ast
        tree = ast.parse(self._source(), filename="dispatch_plan.py")
        class_names = {n.name.lower() for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        for forbidden in ("executor", "queue", "lock"):
            assert not any(forbidden in name for name in class_names), class_names

    def test_no_dispatch_execution_literals(self) -> None:
        import ast
        tree = ast.parse(self._source(), filename="dispatch_plan.py")
        # Strip the module docstring (prose explaining what's out of scope
        # legitimately names these) — check only executable code/comments.
        body_without_docstring = ast.Module(body=tree.body[1:], type_ignores=[])
        code_src = ast.unparse(body_without_docstring)
        for forbidden in ("dispatch_cover_intent", "wait_for_travel_completion",
                          "GlobalSerialDispatch", "open_cover", "set_cover_position",
                          "async_call"):
            assert forbidden not in code_src, forbidden

    def test_no_local_dispatch_target_class_copy(self) -> None:
        src = self._source()
        assert "class DispatchTargetClass" not in src
        assert "from ..engines.dispatch_classification import DispatchTargetClass" in src

    def test_existing_dispatch_files_untouched_by_this_phase(self) -> None:
        # Phase 2 must not have modified any pre-existing dispatch execution
        # module — a structural sanity check that the known production
        # entry points these files define are still present and unrenamed.
        from custom_components.smartshading.cover_control import dispatch_orchestrator
        assert hasattr(dispatch_orchestrator, "effective_interval_s")
        assert hasattr(dispatch_orchestrator, "requires_completion_wait")
