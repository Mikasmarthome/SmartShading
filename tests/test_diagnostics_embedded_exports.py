"""Official HA diagnostics download: embedded support + research export.

Both exports are parts of the one Home Assistant diagnostics download.  They share
a single hard byte budget, keep their fixed overviews, trim the history lists
fairly across zones (newest first), report truthful truncation metadata, never
repeat the same decision history twice, stay privacy-safe and JSON-safe, never
touch the learning store, and hand control back to the event loop between zones.
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.smartshading.engines import embedded_exports as ee
from custom_components.smartshading.engines.export_budget import (
    EMBEDDED_EXPORT_BUDGET_BYTES,
    json_size,
)
from custom_components.smartshading.engines.learning_persistence import (
    LearningPersistenceConfig,
    LearningStore,
    serialize_learning_store,
)
from custom_components.smartshading.engines.research_export_v3 import (
    MAX_RESEARCH_PROJECTED_TOTAL,
    build_research_export_all_zones,
    iter_research_export_all_zones,
    privacy_scan as research_privacy_scan,
)
from custom_components.smartshading.engines.support_export import (
    build_support_export_all_zones,
    iter_support_export_all_zones,
    privacy_scan as support_privacy_scan,
)
from custom_components.smartshading.models.decision_provenance import (
    LearningDecisionRecord,
    ProvenanceSummary,
)

_NOW = datetime(2026, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
_WINDOWS_PER_ZONE = 3


# ---------------------------------------------------------------------------
# Stub zone with filled decision / dispatch rings and a real learning store
# ---------------------------------------------------------------------------

def _ring_record(z: int, i: int, n: int, pad: int) -> dict:
    return {
        "decision_id": f"decision_secret_{z}_{i}",
        "window_id": f"window_secret_{z}_{i % _WINDOWS_PER_ZONE}",
        "decision_timestamp_utc": _NOW - timedelta(minutes=5 * (n - i)),
        "baseline_state": "normal_shade",
        "resolved_state": "normal_shade",
        "decided_by": "Adaptive",
        "config_generation": 1,
        "candidates": [{"layer": f"layer{k}", "target_ha": 40,
                        "note": f"UNIQ-{z}-{i}-{k}-" + "c" * pad} for k in range(4)],
        "target_chain": {"final_dispatched_target_ha": 40 if i % 2 == 0 else None,
                         "resolved_target_position_ha": 40, "trace": "t" * 120},
        "no_dispatch": {"command_sent": i % 2 == 0,
                        "primary_reason": None if i % 2 == 0 else "min_interval_not_elapsed"},
        "authorities": {},
    }


def _dispatch_record(z: int, i: int, n: int) -> dict:
    return {"decision_id": f"decision_secret_{z}_{i}",
            "window_id": f"window_secret_{z}_{i % _WINDOWS_PER_ZONE}",
            "cover_id": f"cover.secret_cover_{z}",
            "at": _NOW - timedelta(minutes=5 * (n - i)),
            "target_ha": 40, "result": "ok", "note": "d" * 150}


def _learning_store(z: int, per_window: int) -> LearningStore:
    store = LearningStore()
    for k in range(_WINDOWS_PER_ZONE):
        for i in range(per_window):
            ts = _NOW - timedelta(minutes=5 * (per_window - i))
            store.record_decision(LearningDecisionRecord(
                decision_id=f"learn_{z}_{k}_{i}",
                decision_timestamp=ts,
                cycle_id=i,
                window_id=f"window_secret_{z}_{k}",
                summary=ProvenanceSummary(
                    decision_id=f"learn_{z}_{k}_{i}", decision_timestamp=ts,
                    baseline_requested_target_ha=50,
                    final_requested_target_ha=40 if i % 3 else 50,
                    adaptation_sources=("position_learning",) if i % 3 else (),
                    net_target_delta_ha=-10 if i % 3 else 0,
                    dispatch_status="dispatched", config_generation=1),
            ))
    return store


class _Coord:
    """Duck-typed zone coordinator: filled rings + learning store."""

    def __init__(self, z: int, *, ring: int = 300, dispatches: int = 200,
                 learning_per_window: int = 400, pad: int = 380):
        self.zones = {f"zone_secret_{z}": object()}
        self.windows = {f"window_secret_{z}_{k}": object() for k in range(_WINDOWS_PER_ZONE)}
        self.cover_groups = {}
        self.config_entry = SimpleNamespace(entry_id=f"entry-secret-{z}")
        self._ring = [_ring_record(z, i, ring, pad) for i in range(ring)]
        self._dispatches = [_dispatch_record(z, i, dispatches) for i in range(dispatches)]
        self._learning_store = _learning_store(z, learning_per_window) if learning_per_window else None
        self._support_critical_events = []
        self._research_daily_buckets = {}
        self._adoption_history = []
        self._strategy_adoption_history = []
        self._adoptions_active = {}
        self._strategy_adoptions_active = {}
        self._pending_outcomes = SimpleNamespace(all_pending=lambda: [])
        self.zone = z

    def decision_trace_snapshot(self):
        if not self._ring:
            return {}
        return {f"zone_secret_{self.zone}": {"records": list(self._ring), "count": len(self._ring)}}

    def dispatch_trace_snapshot(self):
        return {"zones": {f"zone_secret_{self.zone}": {"records": list(self._dispatches)}}}

    def storage_diagnostics(self):
        return {}


def _drain(gen):
    while True:
        try:
            next(gen)
        except StopIteration as done:
            return done.value


def _assemble(coords):
    support = _drain(iter_support_export_all_zones(
        coords, now=_NOW, integration_version="t", byte_cap=None,
        history_factor=ee.support_history_factor(len(coords))))
    research = _drain(iter_research_export_all_zones(coords, now=_NOW, integration_version="t",
                                                     byte_cap=None))
    return support, research


def _fit(coords):
    support, research = _assemble(coords)
    return ee.fit_embedded_exports(support, research, now=_NOW)


def _fit_budget(coords, budget):
    support, research = _assemble(coords)
    return ee.fit_embedded_exports(support, research, budget=budget, now=_NOW)


def _without_blocks(support):
    """The support payload without the truncation records written by the trim."""
    s = copy.deepcopy(support)
    s.pop("truncation", None)
    for z in s.get("zones", []):
        z.pop("truncation", None)
    return s


def _heavy(n=4, **kw):
    return [_Coord(z, **kw) for z in range(n)]


# ---------------------------------------------------------------------------
# Shared budget
# ---------------------------------------------------------------------------

class TestSharedBudget:
    def test_support_and_research_together_stay_within_the_budget(self):
        support, research, reports = _fit(_heavy(4))
        total = json_size(support) + json_size(research)
        assert total <= EMBEDDED_EXPORT_BUDGET_BYTES
        assert support["truncation"]["applied"] and research["truncation"]["applied"]
        assert support["truncation"]["cap_met"] and research["truncation"]["cap_met"]
        assert support["truncation"]["budget_scope"] == ee.BUDGET_SCOPE

    def test_many_zones_do_not_multiply_the_download(self):
        small = _fit(_heavy(1))
        big = _fit(_heavy(6))
        t_small = json_size(small[0]) + json_size(small[1])
        t_big = json_size(big[0]) + json_size(big[1])
        assert t_small <= EMBEDDED_EXPORT_BUDGET_BYTES and t_big <= EMBEDDED_EXPORT_BUDGET_BYTES

    def test_standalone_all_zones_builders_cap_the_aggregate_including_the_zones(self):
        coords = _heavy(6)
        support = build_support_export_all_zones(coords, now=_NOW, integration_version="t")
        research = build_research_export_all_zones(coords, now=_NOW, integration_version="t")
        # the aggregate cap used to be a no-op because the history sits below zones[...]
        assert json_size(support) <= 2_000_000
        assert json_size(research) <= 2_000_000
        assert support["truncation"]["applied"] is True
        assert all(z["truncation"]["applied"] for z in support["zones"])

    def test_everything_fits_nothing_is_removed(self):
        coords = _heavy(1, ring=20, dispatches=10, learning_per_window=20)
        support, research, reports = _fit(coords)
        assert not support["truncation"]["applied"] and not research["truncation"]["applied"]
        assert support["truncation"]["original_bytes"] == support["truncation"]["final_bytes"]
        assert len(support["zones"][0]["recent_decisions"]) == 20

    def test_fixed_overviews_survive_the_trim(self):
        coords = _heavy(5)
        untrimmed, _research = _assemble(coords)
        keys_before = [set(z) for z in untrimmed["zones"]]
        support, research, _ = ee.fit_embedded_exports(*_assemble(coords), now=_NOW)
        for z, before in zip(support["zones"], keys_before):
            for key in ("system", "configuration", "health", "current_snapshot", "inputs",
                        "position_learning", "storage", "section_errors",
                        "history_metadata", "pseudonymization"):
                assert key in z, key
            # nothing the builder produced is dropped, whatever sections this version has
            assert before <= set(z), before - set(z)
        assert support["system"]["zone_count"] == 5
        for key in ("per_zone", "aggregations", "record_accounting", "long_term_summary",
                    "development_summary", "adoption_timeline", "survivorship", "history_metadata"):
            assert key in research, key
        assert len(research["per_zone"]) == 5

    def test_budget_below_the_fixed_overhead_is_reported_not_hidden(self):
        support, research = _assemble(_heavy(2))
        support, research, reports = ee.fit_embedded_exports(support, research, budget=2000, now=_NOW)
        assert all(not z["recent_decisions"] for z in support["zones"])
        assert research["research_records"] == []
        assert support["truncation"]["cap_met"] is False
        assert support["system"]["zone_count"] == 2          # overview kept


# ---------------------------------------------------------------------------
# Fairness, newest-first, determinism
# ---------------------------------------------------------------------------

class TestFairSelection:
    def test_newest_records_of_every_zone_survive(self):
        coords = _heavy(3)
        raw_support, raw_research = _assemble(coords)
        before = [[r["decision_timestamp_utc"] for r in z["recent_decisions"]] for z in raw_support["zones"]]
        before_research = sorted(r["decision_timestamp_utc"] for r in raw_research["research_records"])
        support, research, _ = ee.fit_embedded_exports(raw_support, raw_research, now=_NOW)
        for z, ts in zip(support["zones"], before):
            kept = [r["decision_timestamp_utc"] for r in z["recent_decisions"]]
            assert 0 < len(kept) < len(ts)
            assert kept == ts[-len(kept):]                    # a suffix = the newest
        kept_r = sorted(r["decision_timestamp_utc"] for r in research["research_records"])
        assert 0 < len(kept_r) < len(before_research)
        assert kept_r[-1] == before_research[-1]              # the newest record is in

    def test_unequal_zones_a_small_zone_is_not_crowded_out(self):
        coords = [_Coord(0, ring=300, dispatches=200, learning_per_window=400),
                  _Coord(1, ring=6, dispatches=3, learning_per_window=2),
                  _Coord(2, ring=300, dispatches=200, learning_per_window=400)]
        support, research, _ = _fit(coords)
        assert len(support["zones"][1]["recent_decisions"]) == 6          # complete
        assert len(support["zones"][1]["recent_dispatches"]["records"]) == 3
        assert 0 < len(support["zones"][0]["recent_decisions"]) < 300
        zone_refs = {z["zone_ref"] for z in research["per_zone"]}
        by_zone = {}
        for r in research["research_records"]:
            by_zone[r["zone_ref"]] = by_zone.get(r["zone_ref"], 0) + 1
        assert set(by_zone) == zone_refs                                  # every zone present
        small = next(z["zone_ref"] for z in research["per_zone"] if z["eligible"] == 6)
        assert by_zone[small] == 6

    def test_equal_zones_are_treated_equally(self):
        support, research, _ = _fit(_heavy(4))
        counts = [len(z["recent_decisions"]) for z in support["zones"]]
        assert max(counts) - min(counts) <= 1
        per_zone = {}
        for r in research["research_records"]:
            per_zone[r["zone_ref"]] = per_zone.get(r["zone_ref"], 0) + 1
        assert max(per_zone.values()) - min(per_zone.values()) <= 1

    def test_selection_is_deterministic(self):
        outs = []
        for _ in range(2):
            support, research, _ = _fit(_heavy(3))
            outs.append(json.dumps([support, research], sort_keys=True, default=str))
        assert outs[0] == outs[1]


# ---------------------------------------------------------------------------
# Truthful truncation metadata
# ---------------------------------------------------------------------------

class TestTruthfulMetadata:
    def test_support_metadata_matches_what_was_removed(self):
        coords = _heavy(3)
        raw_support, raw_research = _assemble(coords)
        original = json_size(raw_support)
        counts = [len(z["recent_decisions"]) for z in raw_support["zones"]]
        support, _, _ = ee.fit_embedded_exports(raw_support, raw_research, now=_NOW)
        t = support["truncation"]
        assert t["original_bytes"] == original
        assert t["final_bytes"] == json_size(_without_blocks(support))
        assert t["final_bytes"] < t["original_bytes"]
        for z, before in zip(support["zones"], counts):
            kept = len(z["recent_decisions"])
            sec = z["truncation"]["sections"]["recent_decisions"]
            assert sec == {"original_count": before, "final_count": kept,
                           "removed_count": before - kept}
            assert z["truncation"]["applied"] is True
            assert z["history_metadata"]["records_exported"] == kept
            assert z["history_metadata"]["truncated"] is True
            assert z["truncation"]["history_bytes_final"] <= z["truncation"]["history_budget_bytes"]
        assert support["history_metadata"]["records_exported"] == sum(
            len(z["recent_decisions"]) for z in support["zones"])

    def test_research_metadata_matches_what_was_removed(self):
        coords = _heavy(3)
        raw_support, raw_research = _assemble(coords)
        original = json_size(raw_research)
        before = len(raw_research["research_records"])
        _, research, _ = ee.fit_embedded_exports(raw_support, raw_research, now=_NOW)
        kept = len(research["research_records"])
        t = research["truncation"]
        assert t["original_bytes"] == original and t["applied"] is True
        stripped = {k: v for k, v in research.items() if k != "truncation"}
        assert t["final_bytes"] == json_size(stripped)
        assert t["sections"]["research_records"] == {"original_count": before, "final_count": kept,
                                                     "removed_count": before - kept}
        assert research["record_accounting"]["serialized_record_count"] == kept
        assert research["history_metadata"]["records_exported"] == kept
        assert research["history_metadata"]["truncated"] is True
        assert sum(w["decisions"] for w in research["per_window_summaries"]) == kept

    def test_nothing_removed_is_reported_as_not_applied(self):
        support, research, _ = _fit(_heavy(1, ring=10, dispatches=5, learning_per_window=10))
        assert support["truncation"]["applied"] is False
        assert support["zones"][0]["truncation"]["applied"] is False
        assert research["truncation"]["applied"] is False
        assert research["truncation"]["original_bytes"] == research["truncation"]["final_bytes"]

    def test_total_including_the_truncation_records_is_within_the_budget(self):
        support, research, _ = _fit(_heavy(6))
        assert json_size(support) + json_size(research) <= EMBEDDED_EXPORT_BUDGET_BYTES

    def test_the_projection_limit_is_accounted_not_silent(self):
        coords = [_Coord(z, ring=20, dispatches=10, learning_per_window=1000) for z in range(2)]
        _, research, _ = _fit(coords)
        acct = research["record_accounting"]
        total = 2 * _WINDOWS_PER_ZONE * 1000
        assert acct["total_decisions_examined"] == total
        assert acct["projection_limit"] == {"max_projected_records": MAX_RESEARCH_PROJECTED_TOTAL,
                                            "not_projected_count": total - MAX_RESEARCH_PROJECTED_TOTAL}
        assert acct["excluded_by_reason"]["projection_limit"] == total - MAX_RESEARCH_PROJECTED_TOTAL
        assert acct["eligible_record_count"] == MAX_RESEARCH_PROJECTED_TOTAL
        assert research["history_metadata"]["records_not_projected"] == total - MAX_RESEARCH_PROJECTED_TOTAL
        assert research["history_metadata"]["truncated"] is True

    def test_projection_limit_keeps_the_newest_decisions_of_every_window(self):
        coords = [_Coord(z, ring=5, dispatches=2, learning_per_window=2000) for z in range(2)]
        research = build_research_export_all_zones(coords, now=_NOW, integration_version="t",
                                                   byte_cap=10 ** 9)
        # 2 zones x 3 windows x 2000 decisions; 3000 are projected -> 500 per window,
        # chosen newest first, and nothing is trimmed afterwards (count cap 4000).
        by_window = {s["window_ref"]: s["decisions"] for s in research["per_window_summaries"]}
        assert sorted(by_window.values()) == [500] * 6
        newest = _NOW - timedelta(minutes=5)
        stamps = [r["decision_timestamp_utc"] for r in research["research_records"]]
        assert max(stamps) == newest.replace(microsecond=0).isoformat()
        oldest_allowed = _NOW - timedelta(minutes=5 * 500)
        assert min(stamps) >= oldest_allowed.replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# One decision history is not embedded twice
# ---------------------------------------------------------------------------

class TestNoDuplicateHistory:
    def test_no_dispatch_section_references_instead_of_repeating_decisions(self):
        support, _, _ = _fit(_heavy(2))
        for z in support["zones"]:
            nd = z["recent_no_dispatches"]
            assert nd["record_shape"] == "compact_reference_to_recent_decisions"
            assert nd["records"], "fixture must contain no-dispatch decisions"
            for r in nd["records"]:
                expected = {"decision_ref", "window_ref", "decision_timestamp_utc",
                            "decision_timestamp_local", "resolved_state", "decided_by",
                            "no_dispatch"}
                if r["decision_ref"] is None:       # A6: decision trimmed away, reference marked
                    expected = expected | {"decision_ref_status"}
                assert set(r) == expected

    def test_a_decision_payload_appears_once_in_the_export(self):
        support, _, _ = _fit(_heavy(2))
        text = json.dumps(support, default=str)
        for z in range(2):
            for i in range(300):
                for k in range(4):
                    # The latest decision of a zone is also carried by current_decisions
                    # (the current state); no other decision may be repeated.
                    allowed = 2 if i == 299 else 1
                    assert text.count(f"UNIQ-{z}-{i}-{k}-") <= allowed

    def test_no_dispatch_records_still_link_to_their_decision(self):
        coords = _heavy(1, ring=40, dispatches=5, learning_per_window=5)
        support, _, _ = _fit(coords)
        z = support["zones"][0]
        decision_refs = {r["decision_ref"] for r in z["recent_decisions"]}
        for r in z["recent_no_dispatches"]["records"]:
            assert r["decision_ref"] in decision_refs
            assert r["no_dispatch"]["command_sent"] is False


class TestA6DecisionRefsNeverDangle:
    """After the FINAL trim every no-dispatch reference resolves or is marked."""

    @staticmethod
    def _check(support):
        for z in support["zones"]:
            present = {r["decision_ref"] for r in z["recent_decisions"]}
            records = z["recent_no_dispatches"]["records"]
            marked = 0
            for r in records:
                if r["decision_ref"] is None:
                    assert r["decision_ref_status"] == "unresolved_trimmed"
                    marked += 1
                else:
                    assert r["decision_ref"] in present, "dangling decision_ref"
                    assert "decision_ref_status" not in r
            assert z["recent_no_dispatches"].get("unresolved_decision_refs", 0) == marked

    def test_tight_budget_leaves_no_dangling_reference(self):
        support, _, _ = _fit_budget(_heavy(2), 60_000)
        self._check(support)
        assert any(r["decision_ref"] is None
                   for z in support["zones"] for r in z["recent_no_dispatches"]["records"]),             "the budget must be tight enough to drop referenced decisions"

    def test_roomy_budget_marks_nothing(self):
        support, _, _ = _fit(_heavy(1, ring=40, dispatches=5, learning_per_window=5))
        self._check(support)
        z = support["zones"][0]
        assert "unresolved_decision_refs" not in z["recent_no_dispatches"]

    def test_marking_does_not_copy_the_decision_back(self):
        support, _, _ = _fit_budget(_heavy(2), 60_000)
        for z in support["zones"]:
            for r in z["recent_no_dispatches"]["records"]:
                assert "candidates" not in r and "target_chain" not in r

    def test_validation_is_idempotent(self):
        from custom_components.smartshading.engines.support_export import _resolve_no_dispatch_refs
        support, _, _ = _fit_budget(_heavy(2), 60_000)
        before = json.dumps(support, sort_keys=True, default=str)
        for z in support["zones"]:
            assert _resolve_no_dispatch_refs(z) == z["recent_no_dispatches"].get("unresolved_decision_refs", 0)
        assert json.dumps(support, sort_keys=True, default=str) == before


class TestA6SynchronousSlice:
    """The trim runs on the event loop on a detached payload.  Its cost is bound
    by the byte budget, not by the number of zones, and stays far below a second."""

    def test_slice_is_bounded_and_does_not_grow_with_zone_count(self):
        import time
        timings = {}
        for zones in (1, 8):
            support, research = _assemble(_heavy(zones))
            t0 = time.perf_counter()
            ee.fit_embedded_exports(support, research, now=_NOW)
            timings[zones] = time.perf_counter() - t0
        assert max(timings.values()) < 2.0, timings          # sub-second in practice (~0.2 s)
        assert timings[8] < timings[1] * 6 + 0.5, timings     # not proportional to the zone count


# ---------------------------------------------------------------------------
# JSON safety, privacy, learning store untouched
# ---------------------------------------------------------------------------

class TestSafety:
    def test_json_safe_and_privacy_clean(self):
        support, research, _ = _fit(_heavy(3))
        for payload in (support, research):
            text = json.dumps(payload, allow_nan=False)
            for secret in ("window_secret", "zone_secret", "cover.secret", "entry-secret",
                           "decision_secret", "learn_"):
                assert secret not in text, secret
        assert support_privacy_scan(support) == []
        assert research_privacy_scan(research) == []

    def test_learning_store_is_not_modified(self):
        coords = _heavy(3)
        cfg = LearningPersistenceConfig()

        def snapshot():
            return [json.dumps(serialize_learning_store(c._learning_store, cfg, _NOW),
                               sort_keys=True, default=str) for c in coords]

        counts = [sum(len(c._learning_store.get_decisions(w)) for w in c._learning_store.window_ids())
                  for c in coords]
        before = snapshot()
        _fit(coords)
        build_support_export_all_zones(coords, now=_NOW, integration_version="t")
        build_research_export_all_zones(coords, now=_NOW, integration_version="t")
        assert snapshot() == before
        assert counts == [sum(len(c._learning_store.get_decisions(w))
                              for w in c._learning_store.window_ids()) for c in coords]

    def test_runtime_rings_are_not_modified(self):
        coords = _heavy(2)
        rings = [list(c._ring) for c in coords]
        dispatches = [list(c._dispatches) for c in coords]
        _fit(coords)
        assert [c._ring for c in coords] == rings
        assert [c._dispatches for c in coords] == dispatches

    def test_no_zone_stays_honest(self):
        support, research = _assemble([])
        support, research, _ = ee.fit_embedded_exports(support, research, now=_NOW)
        assert support["overall_status"] == "no_active_zone"
        assert research["overall_status"] == "no_active_zone"


# ---------------------------------------------------------------------------
# The HA hook: cooperative, safe against unload, bounded
# ---------------------------------------------------------------------------

class _Hass:
    def __init__(self, coords):
        entries = [SimpleNamespace(data={}, runtime_data=SimpleNamespace(coordinator=c)) for c in coords]
        self.config_entries = SimpleNamespace(async_entries=lambda domain: entries)

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _diagnostics_module():
    sys.modules.pop("custom_components.smartshading.diagnostics", None)
    sys.modules.pop("custom_components.smartshading.entities.button", None)
    from custom_components.smartshading import diagnostics as diagnostics_mod
    return diagnostics_mod


class TestDiagnosticsHook:
    def test_download_sections_respect_the_shared_budget(self):
        diag = _diagnostics_module()
        result = asyncio.run(diag._build_support_and_research_sections(_Hass(_heavy(5))))
        total = json_size(result["support_export"]) + json_size(result["research_export"])
        assert total <= EMBEDDED_EXPORT_BUDGET_BYTES
        assert result["support_export"]["truncation"]["applied"] is True

    def test_full_hook_returns_a_bounded_json_safe_payload(self):
        diag = _diagnostics_module()
        coords = _heavy(4)
        entry = SimpleNamespace(runtime_data=SimpleNamespace(coordinator=coords[0]))
        result = asyncio.run(diag.async_get_config_entry_diagnostics(_Hass(coords), entry))
        text = json.dumps(result, indent=2, default=str, allow_nan=False)
        assert "support_export" in result and "research_export" in result
        assert "forecast_learning" in result
        # embedded exports + consolidated overview; indentation adds whitespace only
        assert json_size(result) <= EMBEDDED_EXPORT_BUDGET_BYTES + 60_000
        assert len(text) < 6_000_000

    def test_control_returns_to_the_event_loop_between_zones(self):
        diag = _diagnostics_module()
        coords = _heavy(4, ring=40, dispatches=10, learning_per_window=30)
        ticks = {"n": 0}

        async def scenario():
            async def ticker():
                while True:
                    ticks["n"] += 1
                    await asyncio.sleep(0)
            t = asyncio.ensure_future(ticker())
            try:
                return await diag._build_support_and_research_sections(_Hass(coords))
            finally:
                t.cancel()

        result = asyncio.run(scenario())
        assert "support_export" in result
        # one yield per zone for the support build and one per zone for the research build
        assert ticks["n"] >= 2 * len(coords)

    def test_unload_while_building_leaves_no_task_and_no_error(self):
        diag = _diagnostics_module()
        coords = _heavy(3, ring=60, dispatches=20, learning_per_window=40)

        async def scenario():
            task = asyncio.ensure_future(diag._build_support_and_research_sections(_Hass(coords)))
            await asyncio.sleep(0)                      # let the build reach its first yield
            for c in coords:                            # an unload clears the runtime state
                c._learning_store = None
                c.decision_trace_snapshot = lambda: {}
                c.dispatch_trace_snapshot = lambda: {}
                c.config_entry = None
            result = await task
            leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            return result, leftover

        result, leftover = asyncio.run(scenario())
        assert "support_export" in result and "research_export" in result
        assert "section_errors" not in result or "support_research_export" not in result["section_errors"]
        assert leftover == []

    def test_cancelling_the_download_leaves_no_task_behind(self):
        diag = _diagnostics_module()
        coords = _heavy(3, ring=60, dispatches=20, learning_per_window=40)

        async def scenario():
            task = asyncio.ensure_future(diag._build_support_and_research_sections(_Hass(coords)))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

        assert asyncio.run(scenario()) == []

    def test_integration_version_is_still_read_in_the_executor(self):
        diag = _diagnostics_module()
        calls = []

        class _H(_Hass):
            async def async_add_executor_job(self, func, *args):
                calls.append(func)
                return func(*args)

        asyncio.run(diag._build_support_and_research_sections(_H([])))
        from custom_components.smartshading.const import integration_version
        assert integration_version in calls
