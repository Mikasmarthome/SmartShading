"""PA-2c.2 (stub suite): safe retirement of the legacy System ConfigEntry.

Real Home Assistant behavior is covered in tests/real_ha/test_real_ha_system_retirement.py;
this file pins the decision logic with a faithful fake of the ConfigEntries API (state,
setup lock, unload, removal return value, exceptions) and the source contracts that keep
the retirement safe where Home Assistant is not available.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"


# ---------------------------------------------------------------------------
# Faithful fakes of the Home Assistant pieces the lifecycle module uses
# ---------------------------------------------------------------------------

class _State(Enum):
    NOT_LOADED = "not_loaded"
    LOADED = "loaded"


class _OperationNotAllowed(Exception):
    pass


class _UnknownEntry(KeyError):
    pass


class _FakeIssues:
    def __init__(self):
        self.issues: dict[tuple[str, str], dict] = {}


class _Severity:
    WARNING = "warning"


@pytest.fixture
def lifecycle(monkeypatch):
    """The lifecycle module imported against stand-ins for the HA modules it needs."""
    issues = _FakeIssues()
    ce = types.ModuleType("homeassistant.config_entries")
    ce.ConfigEntryState = _State
    ce.OperationNotAllowed = _OperationNotAllowed
    ce.UnknownEntry = _UnknownEntry
    ir = types.ModuleType("homeassistant.helpers.issue_registry")
    ir.IssueSeverity = _Severity

    def create(hass, domain, issue_id, **kw):
        issues.issues[(domain, issue_id)] = kw

    def delete(hass, domain, issue_id):
        issues.issues.pop((domain, issue_id), None)

    ir.async_create_issue = create
    ir.async_delete_issue = delete
    ir.async_get = lambda hass: issues
    helpers = sys.modules.get("homeassistant.helpers") or types.ModuleType("homeassistant.helpers")
    monkeypatch.setitem(sys.modules, "homeassistant", sys.modules.get("homeassistant") or types.ModuleType("homeassistant"))
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", ce)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.issue_registry", ir)
    monkeypatch.setattr(helpers, "issue_registry", ir, raising=False)
    name = "custom_components.smartshading._lifecycle_under_test"
    spec = importlib.util.spec_from_file_location(name, _PKG / "system_retirement_lifecycle.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    module.fake_issues = issues
    return module


_SYSTEM = {"entry_type": "system"}
_OWN_POLICY = {
    "release_strategy": "fixed_time", "fixed_until": "06:30", "duration_min": 77,
    "night_duration_min": 400, "allow_comfort_actions": True, "allow_protection_actions": False,
    "detection_tolerance": 12, "safety_timeout_enabled": False, "use_system_default": False,
}


def _zone_data(**over):
    data = {
        "entry_type": "zone", "override_policy": copy.deepcopy(_OWN_POLICY),
        "absence_delay_min": 20, "absence_delay_use_system_default": False,
    }
    data.update(over)
    return data


def _zone(entry_id, data=None, minor=2, version=1, disabled=None, title=None):
    return SimpleNamespace(
        entry_id=entry_id, data=_zone_data() if data is None else data, minor_version=minor,
        version=version, disabled_by=disabled, title=title or f"Zone {entry_id}")


class _Hass:
    def __init__(self, zones, systems=1):
        self.data = {}
        self.zone_list = list(zones)
        self.system_list = [
            SimpleNamespace(entry_id=f"system{i}", data=dict(_SYSTEM), state=_State.LOADED,
                            setup_lock=asyncio.Lock())
            for i in range(systems)
        ]
        self.config = SimpleNamespace(components={"smartshading"})   # the component is set up
        self.calls: list[tuple] = []
        self.remove_error: Exception | None = None
        self.unload_error: Exception | None = None
        self.on_unload = None            # callback run while the System entry is unloading
        self.require_restart = False
        self.config_entries = self

    # --- ConfigEntries API ---
    def async_get_entry(self, entry_id):
        return next((s for s in self.system_list if s.entry_id == entry_id), None)

    async def async_unload(self, entry_id):
        self.calls.append(("unload", entry_id))
        if self.unload_error is not None:
            raise self.unload_error
        entry = self.async_get_entry(entry_id)
        if entry is None:
            raise _UnknownEntry(entry_id)
        await asyncio.sleep(0)
        if self.on_unload is not None:
            self.on_unload(self)
        entry.state = _State.NOT_LOADED
        return True

    async def async_setup(self, entry_id):
        self.calls.append(("setup", entry_id))
        self.async_get_entry(entry_id).state = _State.LOADED
        return True

    async def async_remove(self, entry_id):
        self.calls.append(("remove", entry_id))
        if self.remove_error is not None:
            raise self.remove_error
        entry = self.async_get_entry(entry_id)
        if entry is None:
            raise _UnknownEntry(entry_id)
        self.system_list.remove(entry)
        return {"require_restart": self.require_restart}


def _ops(lifecycle, hass):
    from custom_components.smartshading.system_retirement import assess_system_retirement

    def assess(h):
        system = h.system_list[0].data if h.system_list else None
        return assess_system_retirement(h.zone_list, system)

    return lifecycle.RetirementOps(
        zone_entries=lambda h: list(h.zone_list), system_entries=lambda h: list(h.system_list),
        assess=assess)


def _run(coro):
    return asyncio.run(coro)


def _retire(lifecycle, hass):
    return _run(lifecycle.async_retire_system_entry(hass, _ops(lifecycle, hass)))


def _issue_ids(lifecycle):
    return sorted(i for (_d, i) in lifecycle.fake_issues.issues)


# ---------------------------------------------------------------------------
# Removal conditions
# ---------------------------------------------------------------------------

class TestRemovalConditions:
    def test_every_independent_zone_retires_the_system_entry(self, lifecycle):
        hass = _Hass([_zone("a"), _zone("b")])
        assert _retire(lifecycle, hass) == lifecycle.RETIRED
        assert hass.system_list == []
        assert [c[0] for c in hass.calls] == ["unload", "remove"]

    def test_one_dependent_zone_keeps_the_system_entry(self, lifecycle):
        hass = _Hass([_zone("a"), _zone("b", _zone_data(absence_delay_use_system_default=True))])
        assert _retire(lifecycle, hass) == lifecycle.BLOCKED
        assert len(hass.system_list) == 1 and hass.calls == []

    def test_an_unmigrated_zone_keeps_the_system_entry(self, lifecycle):
        hass = _Hass([_zone("a", minor=1)])
        assert _retire(lifecycle, hass) == lifecycle.BLOCKED and hass.calls == []

    def test_minor_version_two_alone_is_not_enough(self, lifecycle):
        hass = _Hass([_zone("a", {"entry_type": "zone"}, minor=2)])
        assert _retire(lifecycle, hass) == lifecycle.BLOCKED and hass.calls == []

    def test_a_disabled_dependent_zone_blocks(self, lifecycle):
        hass = _Hass([_zone("a"), _zone("b", _zone_data(absence_delay_use_system_default=True), disabled="user")])
        assert _retire(lifecycle, hass) == lifecycle.BLOCKED

    def test_a_disabled_independent_zone_does_not_block(self, lifecycle):
        hass = _Hass([_zone("a", disabled="user")])
        assert _retire(lifecycle, hass) == lifecycle.RETIRED

    @pytest.mark.parametrize("version", [2, 0, None, "1", True])
    def test_a_zone_with_an_unsupported_major_version_blocks(self, lifecycle, version):
        hass = _Hass([_zone("a", version=version)])
        assert _retire(lifecycle, hass) == lifecycle.BLOCKED and hass.calls == []

    def test_a_newer_minor_version_blocks(self, lifecycle):
        hass = _Hass([_zone("a", minor=3)])
        assert _retire(lifecycle, hass) == lifecycle.BLOCKED

    def test_without_zones_the_system_entry_is_retired(self, lifecycle):
        hass = _Hass([])
        assert _retire(lifecycle, hass) == lifecycle.RETIRED

    def test_no_system_entry_is_a_noop(self, lifecycle):
        hass = _Hass([_zone("a")], systems=0)
        assert _retire(lifecycle, hass) == lifecycle.ABSENT and hass.calls == []

    def test_retirement_is_idempotent(self, lifecycle):
        hass = _Hass([_zone("a")])
        assert _retire(lifecycle, hass) == lifecycle.RETIRED
        calls = list(hass.calls)
        assert _retire(lifecycle, hass) == lifecycle.ABSENT
        assert hass.calls == calls

    def test_zone_data_is_never_touched(self, lifecycle):
        zones = [_zone("a"), _zone("b", disabled="user")]
        before = copy.deepcopy([(z.data, z.minor_version) for z in zones])
        hass = _Hass(zones)
        _retire(lifecycle, hass)
        assert [(z.data, z.minor_version) for z in zones] == before

    def test_all_system_entries_are_removed(self, lifecycle):
        hass = _Hass([_zone("a")], systems=2)
        assert _retire(lifecycle, hass) == lifecycle.RETIRED and hass.system_list == []


# ---------------------------------------------------------------------------
# Time of check / time of use
# ---------------------------------------------------------------------------

class TestTimeOfCheckTimeOfUse:
    def test_a_zone_that_turns_dependent_while_unloading_prevents_the_removal(self, lifecycle):
        zone = _zone("a")
        hass = _Hass([zone])

        def turn_dependent(h):
            zone.data = _zone_data(absence_delay_use_system_default=True)

        hass.on_unload = turn_dependent
        assert _retire(lifecycle, hass) == lifecycle.BLOCKED
        assert len(hass.system_list) == 1
        assert [c[0] for c in hass.calls] == ["unload", "setup"]     # the very same entry is set up again
        assert hass.system_list[0].state is _State.LOADED

    def test_a_zone_added_while_unloading_that_is_independent_does_not_block(self, lifecycle):
        hass = _Hass([_zone("a")])
        hass.on_unload = lambda h: h.zone_list.append(_zone("b"))
        assert _retire(lifecycle, hass) == lifecycle.RETIRED

    def test_a_dependent_zone_added_while_unloading_prevents_the_removal(self, lifecycle):
        hass = _Hass([_zone("a")])
        hass.on_unload = lambda h: h.zone_list.append(_zone("b", minor=1))
        assert _retire(lifecycle, hass) == lifecycle.BLOCKED and len(hass.system_list) == 1

    def test_two_concurrent_attempts_remove_exactly_once(self, lifecycle):
        hass = _Hass([_zone("a")])
        ops = _ops(lifecycle, hass)

        async def both():
            return await asyncio.gather(
                lifecycle.async_retire_system_entry(hass, ops),
                lifecycle.async_retire_system_entry(hass, ops),
                lifecycle.async_retire_system_entry(hass, ops))

        results = _run(both())
        assert sorted(results) == [lifecycle.ABSENT, lifecycle.ABSENT, lifecycle.RETIRED]
        assert [c[0] for c in hass.calls].count("remove") == 1

    def test_a_setup_in_flight_finishes_before_the_system_entry_is_unloaded(self, lifecycle):
        hass = _Hass([_zone("a")])
        system = hass.system_list[0]

        async def scenario():
            await system.setup_lock.acquire()           # the System entry is being set up
            task = asyncio.ensure_future(lifecycle.async_retire_system_entry(hass, _ops(lifecycle, hass)))
            for _ in range(5):
                await asyncio.sleep(0)
            assert hass.calls == [], "nothing may be unloaded or removed while its setup runs"
            system.setup_lock.release()
            return await task

        assert _run(scenario()) == lifecycle.RETIRED
        assert [c[0] for c in hass.calls] == ["unload", "remove"]

    def test_the_removal_follows_the_assessment_without_an_await(self):
        """Contract: after the last assessment nothing may suspend before async_remove is called."""
        src = (_PKG / "system_retirement_lifecycle.py").read_text(encoding="utf-8")
        func = next(n for n in ast.walk(ast.parse(src))
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "_remove_system_entry")
        flat = [n for n in ast.walk(func) if hasattr(n, "lineno")]
        assess = max(n.lineno for n in flat if isinstance(n, ast.Attribute) and n.attr == "assess")
        remove = next(n.lineno for n in flat if isinstance(n, ast.Attribute) and n.attr == "async_remove")
        guard = next(n for n in ast.walk(func) if isinstance(n, ast.If) and n.lineno == assess)
        assert isinstance(guard.body[-1], ast.Return)       # the only await in between ends in a return
        inside = {id(n) for n in ast.walk(guard)}
        awaits_between = [n.lineno for n in flat if isinstance(n, ast.Await)
                          and id(n) not in inside and assess < n.lineno < remove]
        assert not awaits_between, awaits_between

    def test_the_assessment_happens_after_the_unload(self):
        src = (_PKG / "system_retirement_lifecycle.py").read_text(encoding="utf-8")
        func = next(n for n in ast.walk(ast.parse(src))
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "_remove_system_entry")
        flat = [n for n in ast.walk(func) if isinstance(n, ast.Attribute)]
        unload = next(n.lineno for n in flat if n.attr == "async_unload")
        assess = next(n.lineno for n in flat if n.attr == "assess")
        remove = next(n.lineno for n in flat if n.attr == "async_remove")
        assert unload < assess < remove


# ---------------------------------------------------------------------------
# Failure and retry
# ---------------------------------------------------------------------------

class TestFailureAndRetry:
    def test_a_failing_removal_keeps_the_entry_reports_failed_and_raises_an_issue(self, lifecycle, caplog):
        hass = _Hass([_zone("a")])
        hass.remove_error = RuntimeError("storage unavailable")
        assert _retire(lifecycle, hass) == lifecycle.FAILED
        assert len(hass.system_list) == 1
        assert lifecycle.ISSUE_REMOVAL_FAILED in _issue_ids(lifecycle)
        assert any("removing the legacy System entry failed" in r.message for r in caplog.records)
        assert any(r.exc_info for r in caplog.records)         # the traceback is not hidden

    def test_a_failed_removal_sets_the_kept_entry_up_again(self, lifecycle):
        hass = _Hass([_zone("a")])
        hass.remove_error = RuntimeError("boom")
        _retire(lifecycle, hass)
        assert hass.system_list[0].state is _State.LOADED
        assert [c[0] for c in hass.calls] == ["unload", "remove", "setup"]

    def test_a_failed_removal_during_the_component_setup_does_not_set_the_entry_up_from_inside_it(self, lifecycle):
        """PA-4: ConfigEntries.async_setup waits for the component setup that runs this very
        code (a hang until HA's setup timeout); HA sets the stored entry up itself afterwards."""
        hass = _Hass([_zone("a")])
        hass.config.components = set()               # the start-up retirement: component not set up yet
        hass.remove_error = RuntimeError("boom")
        assert _retire(lifecycle, hass) == lifecycle.FAILED
        assert [c[0] for c in hass.calls] == ["unload", "remove"]      # no "setup"

    def test_the_retry_after_a_failure_succeeds_and_clears_the_issue(self, lifecycle):
        hass = _Hass([_zone("a")])
        hass.remove_error = RuntimeError("boom")
        assert _retire(lifecycle, hass) == lifecycle.FAILED
        hass.remove_error = None
        assert _retire(lifecycle, hass) == lifecycle.RETIRED
        assert _issue_ids(lifecycle) == []

    def test_a_failing_unload_keeps_the_entry(self, lifecycle):
        hass = _Hass([_zone("a")])
        hass.unload_error = RuntimeError("cannot unload")
        assert _retire(lifecycle, hass) == lifecycle.FAILED and len(hass.system_list) == 1

    def test_a_non_unloadable_entry_is_left_to_the_removal_api(self, lifecycle):
        hass = _Hass([_zone("a")])
        hass.unload_error = _OperationNotAllowed("not recoverable")
        assert _retire(lifecycle, hass) == lifecycle.RETIRED

    def test_an_entry_removed_concurrently_is_not_a_failure(self, lifecycle):
        hass = _Hass([_zone("a")])
        original = hass.async_remove

        async def vanishing(entry_id):
            hass.system_list.clear()
            raise _UnknownEntry(entry_id)

        hass.async_remove = vanishing
        assert _retire(lifecycle, hass) == lifecycle.RETIRED
        assert lifecycle.ISSUE_REMOVAL_FAILED not in _issue_ids(lifecycle)
        del original

    def test_success_is_not_reported_while_the_entry_is_still_registered(self, lifecycle, caplog):
        hass = _Hass([_zone("a")])

        async def lying_remove(entry_id):
            hass.calls.append(("remove", entry_id))
            return {"require_restart": False}          # claims success, removes nothing

        hass.async_remove = lying_remove
        assert _retire(lifecycle, hass) == lifecycle.FAILED
        assert len(hass.system_list) == 1

    def test_a_restart_required_removal_is_still_a_removal_but_is_logged(self, lifecycle, caplog):
        hass = _Hass([_zone("a")])
        hass.require_restart = True
        assert _retire(lifecycle, hass) == lifecycle.RETIRED
        assert any("restart" in r.message for r in caplog.records)

    def test_a_programming_error_in_the_assessment_is_not_masked(self, lifecycle):
        hass = _Hass([_zone("a")])
        ops = _ops(lifecycle, hass)

        def broken(_hass):
            raise TypeError("defect")

        broken_ops = lifecycle.RetirementOps(ops.zone_entries, ops.system_entries, broken)
        with pytest.raises(TypeError):
            _run(lifecycle.async_retire_system_entry(hass, broken_ops))

    def test_retirement_neither_creates_nor_reads_a_custom_debug_flag(self, lifecycle):
        """PA-3: verbose logging is the standard HA logger level; there is no flag."""
        for zones in ([_zone("a")], [_zone("a", minor=1)]):
            hass = _Hass(zones)
            _retire(lifecycle, hass)
            assert "debug_logging" not in hass.data.get("smartshading", {})


# ---------------------------------------------------------------------------
# Repairs issues
# ---------------------------------------------------------------------------

class TestRepairs:
    def _blocked(self):
        return _Hass([_zone("a", _zone_data(absence_delay_use_system_default=True), title="Living room"),
                      _zone("b")])

    def test_a_blocked_zone_raises_one_stable_issue(self, lifecycle):
        hass = self._blocked()
        _retire(lifecycle, hass)
        assert _issue_ids(lifecycle) == ["system_retirement_zone_a"]
        issue = lifecycle.fake_issues.issues[("smartshading", "system_retirement_zone_a")]
        assert issue["translation_key"] == lifecycle.KEY_ZONE_BLOCKED
        assert issue["is_fixable"] is False and issue["is_persistent"] is False
        assert issue["translation_placeholders"] == {"zone": "Living room"}

    def test_repeated_runs_do_not_duplicate_issues(self, lifecycle):
        hass = self._blocked()
        for _ in range(3):
            _retire(lifecycle, hass)
        assert _issue_ids(lifecycle) == ["system_retirement_zone_a"]

    def test_a_resolved_zone_clears_its_issue_and_the_entry_is_retired(self, lifecycle):
        hass = self._blocked()
        _retire(lifecycle, hass)
        hass.zone_list[0].data = _zone_data()
        assert _retire(lifecycle, hass) == lifecycle.RETIRED
        assert _issue_ids(lifecycle) == []

    def test_a_removed_blocked_zone_clears_its_issue(self, lifecycle):
        hass = self._blocked()
        _retire(lifecycle, hass)
        hass.zone_list.pop(0)
        _retire(lifecycle, hass)
        assert _issue_ids(lifecycle) == []

    def test_only_this_integrations_retirement_issues_are_cleared(self, lifecycle):
        hass = self._blocked()
        lifecycle.fake_issues.issues[("smartshading", "duplicate_cover_x")] = {}
        lifecycle.fake_issues.issues[("other", "system_retirement_zone_zzz")] = {}
        hass.zone_list[0].data = _zone_data()
        _retire(lifecycle, hass)
        assert ("smartshading", "duplicate_cover_x") in lifecycle.fake_issues.issues
        assert ("other", "system_retirement_zone_zzz") in lifecycle.fake_issues.issues

    @pytest.mark.parametrize("zone_kwargs,expected", [
        ({"version": 2}, "KEY_ZONE_UNSUPPORTED"),
        ({"minor": 3}, "KEY_ZONE_UNSUPPORTED"),
        ({"minor": 1}, "KEY_ZONE_BLOCKED"),
    ])
    def test_the_issue_kind_follows_the_reason(self, lifecycle, zone_kwargs, expected):
        hass = _Hass([_zone("a", **zone_kwargs)])
        _retire(lifecycle, hass)
        issue = lifecycle.fake_issues.issues[("smartshading", "system_retirement_zone_a")]
        assert issue["translation_key"] == getattr(lifecycle, expected)

    def test_a_failed_persistence_has_its_own_issue_kind(self, lifecycle):
        hass = _Hass([_zone("a", minor=1)])
        lifecycle.migration_failures(hass)["a"] = lifecycle.FAILURE_PERSISTENCE
        _retire(lifecycle, hass)
        issue = lifecycle.fake_issues.issues[("smartshading", "system_retirement_zone_a")]
        assert issue["translation_key"] == lifecycle.KEY_ZONE_NOT_SAVED

    def test_an_unverifiable_zone_uses_the_generic_kind(self, lifecycle):
        hass = _Hass([_zone("a", minor=1)])
        lifecycle.migration_failures(hass)["a"] = lifecycle.FAILURE_UNVERIFIABLE
        _retire(lifecycle, hass)
        issue = lifecycle.fake_issues.issues[("smartshading", "system_retirement_zone_a")]
        assert issue["translation_key"] == lifecycle.KEY_ZONE_BLOCKED

    def test_issues_expose_no_stored_configuration(self, lifecycle):
        secret = _zone_data(absence_delay_use_system_default=True)
        secret["weather_entity_id"] = "weather.secret_home"
        hass = _Hass([_zone("a", secret, title="Zone A")])
        _retire(lifecycle, hass)
        issue = lifecycle.fake_issues.issues[("smartshading", "system_retirement_zone_a")]
        assert "secret" not in repr(issue)
        assert set(issue["translation_placeholders"]) == {"zone"}

    def test_a_blocked_zone_without_a_system_entry_still_raises_an_issue(self, lifecycle):
        hass = _Hass([_zone("a", minor=1)], systems=0)
        assert _retire(lifecycle, hass) == lifecycle.ABSENT
        assert _issue_ids(lifecycle) == ["system_retirement_zone_a"]

    def test_a_failed_removal_issue_has_no_placeholders(self, lifecycle):
        hass = _Hass([_zone("a")])
        hass.remove_error = RuntimeError("boom")
        _retire(lifecycle, hass)
        issue = lifecycle.fake_issues.issues[("smartshading", lifecycle.ISSUE_REMOVAL_FAILED)]
        assert "translation_placeholders" not in issue and issue["is_fixable"] is False

    def test_every_issue_key_is_translated_in_every_language(self):
        import json
        keys = {"system_retirement_zone_blocked", "system_retirement_zone_not_saved",
                "system_retirement_zone_unsupported_version", "system_retirement_removal_failed"}
        files = [_PKG / "strings.json", *sorted((_PKG / "translations").glob("*.json"))]
        assert len(files) == 25
        for path in files:
            issues = json.loads(path.read_text(encoding="utf-8"))["issues"]
            assert keys <= set(issues), path.name
            for key in keys:
                assert issues[key]["title"].strip() and issues[key]["description"].strip(), (path.name, key)
                assert "{" not in issues[key]["title"], (path.name, key)
                assert ("{zone}" in issues[key]["description"]) == (key != "system_retirement_removal_failed"), (path.name, key)

    def test_the_lifecycle_keys_match_the_translated_keys(self, lifecycle):
        import json
        issues = json.loads((_PKG / "strings.json").read_text(encoding="utf-8"))["issues"]
        for name in ("KEY_ZONE_BLOCKED", "KEY_ZONE_NOT_SAVED", "KEY_ZONE_UNSUPPORTED", "KEY_REMOVAL_FAILED"):
            assert getattr(lifecycle, name) in issues


# ---------------------------------------------------------------------------
# Source contracts of the integration
# ---------------------------------------------------------------------------

_INIT = (_PKG / "__init__.py").read_text(encoding="utf-8")
_INIT_TREE = ast.parse(_INIT)
_LIFE = (_PKG / "system_retirement_lifecycle.py").read_text(encoding="utf-8")


def _init_function(name):
    return next(n for n in ast.walk(_INIT_TREE)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


class TestSourceContracts:
    def test_nothing_creates_a_system_entry_any_more(self):
        for path in _PKG.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "_ensure_system_entry" not in text, path.name
            assert 'context={"source": "system"}' not in text, path.name
            assert "async_step_system(" not in text, path.name

    def test_the_config_flow_has_no_system_creation_step(self):
        text = (_PKG / "config_flow.py").read_text(encoding="utf-8")
        assert "def async_step_system(" not in text
        assert "ENTRY_TYPE_SYSTEM}" not in text.replace(" ", "") or "data={CONF_ENTRY_TYPE:ENTRY_TYPE_SYSTEM}" not in text.replace(" ", "")

    def test_the_removal_uses_only_the_supported_api(self):
        for forbidden in (".storage", "._entries", "unlink", "rmtree", "async_remove_device",
                          "async_remove_entity", "async_update_entry", "async_delete("):
            assert forbidden not in _LIFE, forbidden
        assert _LIFE.count("hass.config_entries.async_remove(") == 1

    def test_zone_entries_are_never_written_by_the_lifecycle(self):
        assert "async_update_entry" not in _LIFE and "async_reload" not in _LIFE

    def test_the_component_setup_retires_after_preparing_the_zones(self):
        body = ast.get_source_segment(_INIT, _init_function("async_setup"))
        assert body.index("_prepare_zone_entries(hass)") < body.index("_async_retire_system_entry(hass)")
        assert "await _async_retire_system_entry(hass)" in body

    def test_the_zone_setup_does_not_create_but_retires_in_a_plain_task(self):
        body = ast.get_source_segment(_INIT, _init_function("_async_setup_zone_entry"))
        assert "_ensure_system_entry" not in body
        assert "hass.async_create_task(" in body and "_async_retire_after_zone_setup" in body
        assert "async_create_background_task" not in body      # an unload must not cancel a removal

    def test_the_zone_removal_retries_the_retirement(self):
        body = ast.get_source_segment(_INIT, _init_function("async_remove_entry"))
        assert "_async_retire_system_entry(hass)" in body

    def test_the_orphan_cleanup_no_longer_recreates_the_entry(self):
        body = ast.get_source_segment(_INIT, _init_function("_async_remove_orphaned_system_entry"))
        assert "_ensure_system_entry" not in body and "flow.async_init" not in body

    def test_the_lifecycle_never_removes_or_creates_entities_or_devices(self):
        assert "entity_registry" not in _LIFE and "device_registry" not in _LIFE

    def test_the_minimum_version_is_declared_in_hacs_json_only(self):
        # PRE-ARCH PA-REL applied the agreed minimum; manifest.json has no such key.
        manifest = (_PKG / "manifest.json").read_text(encoding="utf-8")
        assert "homeassistant" not in manifest
        hacs = Path(__file__).resolve().parent.parent / "hacs.json"
        assert '"homeassistant": "2026.8.0"' in hacs.read_text(encoding="utf-8")

    def test_the_migration_records_failures_for_the_repairs(self):
        body = ast.get_source_segment(_INIT, _init_function("_migrate_zone_entry"))
        assert "ZoneMigrationPersistenceError" in body and "FAILURE_PERSISTENCE" in body
        assert "failures.pop(entry.entry_id" in body

    def test_the_persistence_error_is_a_migration_error(self):
        src = (_PKG / "zone_settings_migration.py").read_text(encoding="utf-8")
        assert "class ZoneMigrationPersistenceError(ZoneMigrationError)" in src
        assert src.count("raise ZoneMigrationPersistenceError(") == 2
