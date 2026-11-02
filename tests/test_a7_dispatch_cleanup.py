"""A7 -- one fixed dispatch rule, no legacy dispatch leftovers.

Lock-in for the target behaviour (executor level), proof that the removed
architecture is gone (AST / source guards), tolerant loading of old stored
data, and parity of UI strings, translations and README with the real state.

Self-contained: imports only production modules, never sibling test modules.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path

import pytest

from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    from_storage_dict,
    resolve_zone_override_policy,
    to_storage_dict,
)
from custom_components.smartshading.cover_control import dispatch_plan_executor as dpe
from custom_components.smartshading.cover_control.dispatch_plan import (
    DispatchPlanItem,
    build_dispatch_plan,
)
from custom_components.smartshading.cover_control.dispatch_plan_executor import (
    CompletionOutcome,
    DispatchOutcome,
    DispatchPlanExecutor,
    ExecutionValidation,
    ValidationAction,
)
from custom_components.smartshading.engines.dispatch_classification import DispatchTargetClass

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "smartshading"

INTERMEDIATE = DispatchTargetClass.INTERMEDIATE
FULL_OPEN = DispatchTargetClass.FULL_OPEN
FULL_CLOSE = DispatchTargetClass.FULL_CLOSE


# ---------------------------------------------------------------------------
# Executor harness: fake clock, observable overlap of dispatch/wait/settle
# ---------------------------------------------------------------------------

class Rig:
    def __init__(self) -> None:
        self.t = 0.0
        self.in_flight = 0
        self.max_in_flight = 0
        self.dispatches: list[tuple[str, float]] = []
        self.completions: list[tuple[str, float]] = []
        self.sleeps: list[float] = []
        self.completion_outcomes: dict[str, CompletionOutcome] = {}
        self.travel_s = 7.0
        self.cancellation = asyncio.Event()
        self.on_sleep = None

    async def dispatch_item(self, item):
        self.dispatches.append((item.cover_entity_id, self.t))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        return DispatchOutcome(success=True)

    async def wait_for_completion(self, item, cancellation):
        self.t += self.travel_s
        self.completions.append((item.cover_entity_id, self.t))
        self.in_flight -= 1
        return self.completion_outcomes.get(
            item.cover_entity_id, CompletionOutcome(status="completed", completion_method="position"))

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
        if self.on_sleep is not None:
            self.on_sleep()
        await asyncio.sleep(0)

    def run(self, plan):
        return asyncio.run(DispatchPlanExecutor().execute_plan(
            plan=plan,
            dispatch_item=self.dispatch_item,
            wait_for_completion=self.wait_for_completion,
            validate_item=lambda item: ExecutionValidation(ValidationAction.EXECUTE),
            is_generation_current=lambda item: True,
            cancellation=self.cancellation,
            sleep=self.sleep,
            clock=lambda: self.t,
        ))


def _item(cover, index, *, zone="z1", zone_index=0, target=40, klass=INTERMEDIATE):
    return DispatchPlanItem(
        zone_id=zone, zone_index=zone_index, cover_entity_id=cover, cover_index=index,
        target_ha=target, target_class=klass, decision_ref=f"d_{cover}",
        zone_generation=0, reason=None,
    )


def _plan(items):
    return build_dispatch_plan(plan_id="p", trigger="scheduled_evaluation", items=items, created_at="t0")


class TestFixedRuleConstants:
    def test_settle_and_end_position_spacing_are_two_seconds(self) -> None:
        assert dpe.INTERMEDIATE_POST_COMPLETION_PAUSE_S == 2.0
        assert dpe.FULL_OPEN_START_INTERVAL_S == 2.0


class TestIntermediateGroupsAreStrictlySequential:
    @pytest.mark.parametrize("count", [2, 3, 4])
    def test_never_two_covers_in_flight_and_settle_after_completion(self, count) -> None:
        rig = Rig()
        result = rig.run(_plan([_item(f"c{i}", i) for i in range(count)]))
        assert rig.max_in_flight == 1
        assert [c for c, _ in rig.dispatches] == [f"c{i}" for i in range(count)]
        assert result.status == "completed"
        for index in range(1, count):
            completed_at = rig.completions[index - 1][1]
            started_at = rig.dispatches[index][1]
            assert started_at - completed_at == pytest.approx(dpe.INTERMEDIATE_POST_COMPLETION_PAUSE_S)

    def test_timeout_never_counts_as_success_and_stops_the_rest(self) -> None:
        rig = Rig()
        rig.completion_outcomes["c1"] = CompletionOutcome(status="timed_out", completion_method="timeout")
        result = rig.run(_plan([_item(f"c{i}", i) for i in range(4)]))
        assert [c for c, _ in rig.dispatches] == ["c0", "c1"]
        assert [r.status for r in result.item_results] == ["completed", "timed_out", "skipped", "skipped"]
        assert {r.reason for r in result.item_results[2:]} == {"previous_completion_timed_out"}

    def test_other_zones_stay_functional_after_a_timeout(self) -> None:
        rig = Rig()
        rig.completion_outcomes["a0"] = CompletionOutcome(status="timed_out", completion_method="timeout")
        result = rig.run(_plan([
            _item("a0", 0, zone="za", zone_index=0),
            _item("a1", 1, zone="za", zone_index=0),
            _item("b0", 0, zone="zb", zone_index=1),
            _item("b1", 1, zone="zb", zone_index=1),
        ]))
        assert [c for c, _ in rig.dispatches] == ["a0", "b0", "b1"]
        statuses = {r.item.cover_entity_id: r.status for r in result.item_results}
        assert statuses == {"a0": "timed_out", "a1": "skipped", "b0": "completed", "b1": "completed"}
        assert rig.max_in_flight == 1

    def test_safety_during_the_settle_pause_stops_the_group_without_replay(self) -> None:
        rig = Rig()
        rig.on_sleep = rig.cancellation.set
        result = rig.run(_plan([_item(f"c{i}", i) for i in range(3)]))
        assert result.status == "preempted"
        assert [c for c, _ in rig.dispatches] == ["c0"]

    def test_safety_during_the_completion_wait_stops_the_group(self) -> None:
        rig = Rig()
        original = rig.wait_for_completion

        async def cancelled_wait(item, cancellation):
            rig.cancellation.set()
            return await original(item, cancellation)

        rig.wait_for_completion = cancelled_wait
        result = rig.run(_plan([_item(f"c{i}", i) for i in range(3)]))
        assert result.status == "preempted"
        assert [c for c, _ in rig.dispatches] == ["c0"]

    def test_safety_between_covers_blocks_the_next_start(self) -> None:
        rig = Rig()
        rig.cancellation.set()
        result = rig.run(_plan([_item("c0", 0), _item("c1", 1)]))
        assert result.status == "preempted" and rig.dispatches == []


class TestEndPositionsAreTheFastPath:
    @pytest.mark.parametrize("klass,target", [(FULL_OPEN, 100), (FULL_CLOSE, 0)])
    def test_end_positions_never_wait_for_completion_and_use_the_start_spacing(self, klass, target) -> None:
        rig = Rig()
        rig.run(_plan([_item(f"c{i}", i, target=target, klass=klass) for i in range(4)]))
        assert rig.completions == []
        starts = [t for _, t in rig.dispatches]
        assert [round(b - a, 6) for a, b in zip(starts, starts[1:])] == [dpe.FULL_OPEN_START_INTERVAL_S] * 3


# ---------------------------------------------------------------------------
# Removed architecture stays removed
# ---------------------------------------------------------------------------

_REMOVED_TOKENS = (
    "DispatchConfig", "DispatchMode", "dispatch_config", "dispatch_batch", "dispatch_orchestrator",
    "_predispatch_parallel_batches", "_run_parallel_batch_list", "_dispatch_one_parallel_item",
    "_race_gather_against_cancellation", "_parallel_results", "CONF_DISPATCH_", "resolve_zone_dispatch_config",
    "system_dispatch", "on_reference_travel", "min_interval_override", "parallel_batch", "zone_batching",
    "dispatch_mode", "dispatch_start_interval_s", "dispatch_max_travel_wait_s", "dispatch_post_travel_pause_s",
    "dispatch_zone_batching", "requires_completion_wait", "effective_interval_s", "group_into_batches",
    "start_interval_s", "post_travel_pause_s",
)
_REMOVED_WORD_PATTERNS = (re.compile(r"\bon_restart\b"),)
_REMOVED_MODULES = (
    "models/dispatch_config.py", "cover_control/dispatch_batch.py", "cover_control/dispatch_orchestrator.py",
)


def _package_files(*suffixes):
    return [p for p in PKG.rglob("*") if p.is_file() and p.suffix in suffixes and "__pycache__" not in p.parts]


class TestLegacyDispatchIsGone:
    def test_removed_modules_do_not_exist(self) -> None:
        for rel in _REMOVED_MODULES:
            assert not (PKG / rel).exists(), rel

    def test_no_removed_symbol_anywhere_in_code_or_texts(self) -> None:
        offenders = []
        for path in _package_files(".py", ".json") + [ROOT / "README.md"]:
            text = path.read_text(encoding="utf-8")
            for token in _REMOVED_TOKENS:
                if token in text:
                    offenders.append((path.name, token))
            for pattern in _REMOVED_WORD_PATTERNS:
                if pattern.search(text):
                    offenders.append((path.name, pattern.pattern))
        assert offenders == []

    def test_no_string_constant_or_getattr_consumer_of_removed_names(self) -> None:
        bad = []
        for path in _package_files(".py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if any(token in node.value for token in _REMOVED_TOKENS):
                        bad.append((path.name, node.value[:60]))
        assert bad == []

    def test_no_orphan_modules(self) -> None:
        # Every module must be imported by another module; Home Assistant
        # itself loads the platform / flow / diagnostics entry points by name.
        ha_loaded = {"binary_sensor", "button", "config_flow", "diagnostics", "sensor", "switch"}
        mods = {}
        for path in _package_files(".py"):
            rel = path.relative_to(PKG).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            mods[".".join(parts)] = (path, parts)
        imported: set[str] = set()
        for name, (path, parts) in mods.items():
            package = parts if path.name == "__init__.py" else parts[:-1]
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    if node.level:
                        base_parts = package[: len(package) - (node.level - 1)]
                        base = ".".join(base_parts + ([node.module] if node.module else []))
                    else:
                        base = (node.module or "").removeprefix("custom_components.smartshading").strip(".")
                    imported.add(base)
                    imported.update(f"{base}.{alias.name}".strip(".") for alias in node.names)
        packages = {name for name, (path, _) in mods.items() if path.name == "__init__.py"}
        orphans = [m for m in mods if m and m not in packages and m not in imported and m not in ha_loaded]
        assert orphans == []

    def test_coordinator_has_no_dispatch_config_parameter(self) -> None:
        tree = ast.parse((PKG / "coordinator.py").read_text(encoding="utf-8"))
        init = next(
            n for c in ast.walk(tree) if isinstance(c, ast.ClassDef) and c.name == "SmartShadingCoordinator"
            for n in c.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        )
        names = {a.arg for a in init.args.args + init.args.kwonlyargs}
        assert "dispatch_config" not in names

    def test_options_flow_has_no_dispatch_step(self) -> None:
        tree = ast.parse((PKG / "config_flow.py").read_text(encoding="utf-8"))
        steps = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert not [s for s in steps if "dispatch" in s]
        menus = [
            ast.literal_eval(n) for n in ast.walk(tree)
            if isinstance(n, ast.List) and all(isinstance(e, ast.Constant) for e in n.elts)
            and any(isinstance(e.value, str) and e.value.startswith("system_") for e in n.elts)
        ]
        assert ["system_manual_override", "system_presence"] in menus

    def test_execution_result_has_no_batch_fields(self) -> None:
        from dataclasses import fields

        from custom_components.smartshading.cover_control.execution_result import ExecutionResult
        names = {f.name for f in fields(ExecutionResult)}
        assert not {n for n in names if "batch" in n}

    def test_main_loop_has_a_single_comfort_pass(self) -> None:
        source = (PKG / "coordinator.py").read_text(encoding="utf-8")
        assert source.count("await self._predispatch_sequential_plan(") <= 2
        assert "def _predispatch_sequential_plan" in source


# ---------------------------------------------------------------------------
# Old stored data keeps loading and changes nothing
# ---------------------------------------------------------------------------

_OLD_KEYS = {
    "dispatch_mode": "parallel",
    "dispatch_start_interval_s": 9.0,
    "dispatch_max_travel_wait_s": 55.0,
    "dispatch_post_travel_pause_s": 4.0,
    "dispatch_zone_batching": True,
    "dispatch_config": {"mode": "parallel", "start_interval_s": 9.0, "zone_batching": True},
    "system_dispatch_config": {"mode": "sequential"},
}


def _clean_raw() -> dict:
    return to_storage_dict(SmartShadingConfigEntryData(name="Zone", use_home_location=False))


class TestOldEntryDataIsTolerated:
    @pytest.mark.parametrize("key", sorted(_OLD_KEYS))
    def test_each_old_key_loads_and_changes_nothing(self, key) -> None:
        clean = from_storage_dict(_clean_raw())
        raw = _clean_raw()
        raw[key] = _OLD_KEYS[key]
        loaded = from_storage_dict(raw)
        assert loaded == clean
        assert to_storage_dict(loaded) == to_storage_dict(clean)

    def test_all_old_keys_combined(self) -> None:
        clean = from_storage_dict(_clean_raw())
        raw = {**_clean_raw(), **_OLD_KEYS}
        loaded = from_storage_dict(raw)
        assert loaded == clean
        written = to_storage_dict(loaded)
        assert not set(_OLD_KEYS) & set(written)

    def test_load_and_save_twice_is_stable_and_other_options_survive(self) -> None:
        raw = {**_clean_raw(), **_OLD_KEYS, "name": "Kept"}
        once = to_storage_dict(from_storage_dict(raw))
        twice = to_storage_dict(from_storage_dict(once))
        assert once == twice and once["name"] == "Kept"

    def test_old_system_key_does_not_disturb_the_remaining_system_defaults(self) -> None:
        with_old = resolve_zone_override_policy({"name": "A"}, {"system_dispatch_config": {"mode": "x"}})
        without = resolve_zone_override_policy({"name": "A"}, {})
        assert with_old == without


# ---------------------------------------------------------------------------
# UI strings, translations, README
# ---------------------------------------------------------------------------

def _flatten(node, prefix=""):
    for key, value in node.items():
        if isinstance(value, dict):
            yield from _flatten(value, f"{prefix}{key}.")
        else:
            yield f"{prefix}{key}", value


_STRINGS = dict(_flatten(json.loads((PKG / "strings.json").read_text(encoding="utf-8"))))
_TRANSLATION_FILES = sorted((PKG / "translations").glob("*.json"))
_LEGACY_UI = re.compile(r"dispatch|parallel|sequential|spaced|batch|pacing|zone_batching", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")


class TestUiStringsAndTranslations:
    def test_there_are_translations(self) -> None:
        assert len(_TRANSLATION_FILES) >= 20

    def test_strings_json_has_no_legacy_dispatch_text_or_selector_value(self) -> None:
        hits = [k for k, v in _STRINGS.items() if _LEGACY_UI.search(k) or _LEGACY_UI.search(str(v))]
        assert hits == []

    @pytest.mark.parametrize("path", _TRANSLATION_FILES, ids=lambda p: p.stem)
    def test_translation_has_identical_keys_and_placeholders(self, path) -> None:
        data = dict(_flatten(json.loads(path.read_text(encoding="utf-8"))))
        assert set(data) == set(_STRINGS), sorted(set(data) ^ set(_STRINGS))[:5]
        for key, text in _STRINGS.items():
            assert set(_PLACEHOLDER.findall(data[key])) == set(_PLACEHOLDER.findall(text)), key
        assert not [k for k, v in data.items() if _LEGACY_UI.search(k) or _LEGACY_UI.search(str(v))]

    def test_every_menu_option_label_has_a_step(self) -> None:
        raw = json.loads((PKG / "strings.json").read_text(encoding="utf-8"))
        steps = raw["options"]["step"]
        for step_id, step in steps.items():
            for key in step.get("menu_options", {}):
                assert key in steps, f"{step_id}.{key}"


class TestReadmeDescribesTheFixedRule:
    _README = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_no_selectable_mode_or_lock_claims(self) -> None:
        text = self._README.lower()
        for phrase in ("parallel", "fifo", "zone batching", "dispatch mode", "spaced", "selectable"):
            assert phrase not in text, phrase

    def test_real_rule_is_described(self) -> None:
        text = self._README
        section = text[text.index("How covers are moved"):]
        assert "2-second" in section and "Safety" in section
        assert "exactly one cover moves at a time" in " ".join(section.split())
