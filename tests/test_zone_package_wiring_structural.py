"""Structural guarantees of the zone-package wiring (source level).

Behaviour is proven in test_zone_package_coordination.py and, in real Home
Assistant, in tests/real_ha/test_real_ha_zone_packages.py.  These checks pin the
shape that behaviour depends on: where a package is requested, that Safety never
requests one, that the per-cover ports read the cover they act on, and that the
teardown paths release what they hold.
"""
from __future__ import annotations

import ast
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
_COORD_SRC = (_BASE / "coordinator.py").read_text(encoding="utf-8")
_EXEC_SRC = (_BASE / "cover_control" / "dispatch_plan_executor.py").read_text(encoding="utf-8")
_COORD = ast.parse(_COORD_SRC)
_EXEC = ast.parse(_EXEC_SRC)


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _function(tree: ast.AST, name: str):
    found = [f for f in _functions(tree) if f.name == name]
    assert len(found) == 1, (name, len(found))
    return found[0]


def _calls(node: ast.AST, attr: str):
    return [
        c for c in ast.walk(node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == attr
    ]


class TestPackageIsRequestedInOnePlace:
    def test_only_the_comfort_plan_path_requests_a_package(self):
        users = {
            f.name for f in _functions(_COORD)
            if _calls(f, "package")
        }
        assert users == {"_predispatch_sequential_plan"}, users

    def test_the_package_wraps_the_executor_run(self):
        fn = _function(_COORD, "_predispatch_sequential_plan")
        with_nodes = [n for n in ast.walk(fn) if isinstance(n, ast.AsyncWith)]
        wrapping = [
            w for w in with_nodes
            if any(isinstance(c.func, ast.Attribute) and c.func.attr == "package"
                   for it in w.items for c in ast.walk(it.context_expr) if isinstance(c, ast.Call))
        ]
        assert len(wrapping) == 1
        assert _calls(wrapping[0], "execute_plan"), "the executor must run inside the package"
        assert not _calls(fn, "admit") or all(
            w is not wrapping[0] for w in with_nodes if _calls(w, "admit")), "no admission inside the package scope setup"

    def test_the_executor_receives_the_shared_pacing(self):
        fn = _function(_COORD, "_predispatch_sequential_plan")
        (call,) = _calls(fn, "execute_plan")
        assert "pacing" in {k.arg for k in call.keywords}


class TestSafetyNeverNeedsAPackage:
    def test_the_safety_branch_uses_the_admission_and_no_package(self):
        fn = _function(_COORD, "_async_update_data_cycle")
        assert not _calls(fn, "package"), "the update cycle itself must not request a package"
        assert not _calls(fn, "retain_slot") and not _calls(fn, "withdraw")
        src = ast.get_source_segment(_COORD_SRC, fn)
        # the Safety else-branch is reached only after the comfort routing branches
        safety_idx = src.index("Serial Dispatch (Step 10)")
        assert "admit(safety=" in src[safety_idx:] or "safety=_intent.is_safety" in src[safety_idx:] \
            or "admit(" in src[safety_idx:]

    def test_the_safety_cycle_only_preempts_it_never_builds_a_comfort_plan(self):
        fn = _function(_COORD, "_async_update_data_cycle")
        src = ast.get_source_segment(_COORD_SRC, fn)
        gate = src.index("if _cycle_has_executable_safety:")
        branch = src[gate:gate + 400]
        assert "_preempt_active_comfort_plan()" in branch
        assert "_run_comfort_dispatch_for_cycle" in branch.split("else:")[1]

    def test_a_normal_command_outside_the_plan_is_refused_not_sent(self):
        fn = _function(_COORD, "_async_update_data_cycle")
        src = ast.get_source_segment(_COORD_SRC, fn)
        refusal = src.index("elif not _intent.is_safety:")
        safety_else = src.index("Serial Dispatch (Step 10)")
        assert refusal < safety_else
        between = src[refusal:safety_else]
        assert "normal_dispatch_requires_package" in between
        assert "dispatch_cover_intent" not in between and "admit" not in between


class TestPortsReadTheCoverTheyActOn:
    def _snapshot_calls(self):
        fn = _function(_COORD, "_predispatch_sequential_plan")
        return _calls(fn, "_build_cover_entity_snapshot_for_window")

    def test_every_snapshot_read_of_the_plan_names_its_cover(self):
        calls = self._snapshot_calls()
        assert len(calls) >= 2
        for call in calls:
            assert len(call.args) + len(call.keywords) >= 3, ast.get_source_segment(_COORD_SRC, call)

    def test_the_snapshot_builder_defaults_to_the_first_cover_only_without_a_name(self):
        fn = _function(_COORD, "_build_cover_entity_snapshot_for_window")
        names = [a.arg for a in fn.args.args]
        assert names == ["self", "window", "now", "cover_entity_id"]
        assert isinstance(fn.args.defaults[-1], ast.Constant) and fn.args.defaults[-1].value is None

    def test_the_completion_wait_is_given_the_sent_cover(self):
        fn = _function(_COORD, "_predispatch_sequential_plan")
        src = ast.get_source_segment(_COORD_SRC, fn)
        port = src.index("async def _wait_for_completion_port")
        segment = src[port:port + 2500]
        assert "intent.cover_entity_id" in segment and "cover_state_by_entity.get(" in segment
        assert "cover_ids[0]" not in segment


class TestTeardownReleasesWhatIsHeld:
    def test_unload_discards_only_this_instances_tickets(self):
        fn = _function(_COORD, "async_shutdown")
        (call,) = _calls(fn, "discard_owner")
        assert isinstance(call.args[0], ast.Name) and call.args[0].id == "self"

    def test_a_kept_queue_position_is_dropped_in_a_finally(self):
        fn = _function(_COORD, "_run_comfort_dispatch_for_cycle")
        tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
        assert any(_calls(ast.Module(body=t.finalbody, type_ignores=[]), "drop_slot") for t in tries)

    def test_the_package_context_releases_in_exit(self):
        gate_src = (_BASE / "cover_control" / "global_dispatch_throttle.py").read_text(encoding="utf-8")
        tree = ast.parse(gate_src)
        ctx = [c for c in ast.walk(tree) if isinstance(c, ast.ClassDef) and c.name == "_PackageContext"][0]
        aexit = [f for f in ctx.body if isinstance(f, ast.AsyncFunctionDef) and f.name == "__aexit__"][0]
        assert _calls(aexit, "release")

    def test_the_executor_resolves_an_unfinished_travel_in_a_finally(self):
        fn = _function(_EXEC, "execute_plan")
        tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
        assert any(_calls(ast.Module(body=t.finalbody, type_ignores=[]), "resolve_pending") for t in tries)


class TestNoSecondDispatcher:
    def test_the_coordination_lives_in_the_existing_global_component(self):
        gate_src = (_BASE / "cover_control" / "global_dispatch_throttle.py").read_text(encoding="utf-8")
        tree = ast.parse(gate_src)
        classes = {c.name for c in ast.walk(tree) if isinstance(c, ast.ClassDef)}
        assert {"GlobalSerialDispatch", "ZonePackageGate"} <= classes
        serial = [c for c in ast.walk(tree) if isinstance(c, ast.ClassDef) and c.name == "GlobalSerialDispatch"][0]
        props = {f.name for f in serial.body if isinstance(f, ast.FunctionDef)}
        assert {"pacing", "packages", "package"} <= props

    def test_the_executor_does_not_import_the_global_component(self):
        assert "global_dispatch_throttle" not in _EXEC_SRC
