"""T21 Phase D2 — canonical decision-record view helpers
(engines/decision_record.py) and their wiring into explainability.py /
support_export.py, guaranteeing Explainability, current_decisions, and the
support timeline can never disagree about decided_by/target/state because
they all derive from the same computation.
"""
from __future__ import annotations

from datetime import datetime, timezone

from custom_components.smartshading.engines.decision_record import (
    resolve_active_influences,
    resolve_dispatch_action,
    resolve_target_chain,
    resolve_target_ha,
)
from custom_components.smartshading.engines.explainability import (
    build_decision_explanation,
)
from custom_components.smartshading.engines.support_export import (
    build_support_export_v3,
)

_NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)


class TestResolveActiveInfluences:
    def test_nothing_active_returns_empty_dict(self):
        assert resolve_active_influences({}) == {}

    def test_only_active_influences_are_present(self):
        authorities = {
            "safety_authority": {"active": True},
            "manual_override_authority": {"active": False},
            "lifecycle_authority": {"active": False},
        }
        out = resolve_active_influences(authorities)
        assert out == {"safety": True}

    def test_applied_field_used_for_learning_and_harmonization(self):
        authorities = {
            "position_learning_authority": {"applied": True},
            "harmonization_authority": {"applied": False},
        }
        out = resolve_active_influences(authorities)
        assert out == {"learning_position": True}

    def test_heat_and_adaptation_come_from_kwargs_not_authorities(self):
        out = resolve_active_influences({}, heat_active=True, adaptation_active=True)
        assert out == {"heat_protection": True, "adaptation": True}

    def test_none_authorities_never_raises(self):
        assert resolve_active_influences(None) == {}

    def test_malformed_authority_entry_ignored(self):
        assert resolve_active_influences({"safety_authority": "not-a-dict"}) == {}


class TestResolveTargetChain:
    def test_identical_values_across_stages_yields_none(self):
        raw = {
            "recommendation_position_ha": 0.4,
            "resolved_target_position_ha": 0.4,
            "post_harmonization_target_ha": 0.4,
            "intended_payload_position_ha": 0.4,
            "actual_payload_position_ha": 0.4,
        }
        assert resolve_target_chain(raw) is None

    def test_single_present_value_yields_none(self):
        assert resolve_target_chain({"recommendation_position_ha": 0.4}) is None

    def test_real_transformation_is_reported(self):
        raw = {
            "recommendation_position_ha": 0.4,
            "resolved_target_position_ha": 0.4,
            "post_harmonization_target_ha": 0.6,
            "intended_payload_position_ha": 0.6,
            "actual_payload_position_ha": 0.6,
        }
        out = resolve_target_chain(raw)
        assert out is not None
        assert out["recommendation_position_ha"] == 0.4
        assert out["post_harmonization_target_ha"] == 0.6

    def test_empty_or_none_input_never_raises(self):
        assert resolve_target_chain(None) is None
        assert resolve_target_chain({}) is None


class TestResolveTargetHa:
    def test_prefers_final_dispatched(self):
        rec = {"target_chain": {
            "final_dispatched_target_ha": 0.9,
            "resolved_target_position_ha": 0.5,
            "recommendation_position_ha": 0.1,
        }}
        assert resolve_target_ha(rec) == 0.9

    def test_falls_back_to_resolved_then_recommendation(self):
        assert resolve_target_ha({"target_chain": {"resolved_target_position_ha": 0.5}}) == 0.5
        assert resolve_target_ha({"target_chain": {"recommendation_position_ha": 0.1}}) == 0.1

    def test_missing_target_chain_returns_none(self):
        assert resolve_target_ha({}) is None
        assert resolve_target_ha(None) is None


class TestResolveDispatchAction:
    def test_command_sent_is_sent(self):
        rec = {"no_dispatch": {"command_sent": True}}
        assert resolve_dispatch_action(rec) == "sent"

    def test_same_position_is_unchanged(self):
        rec = {"no_dispatch": {"command_sent": False, "primary_reason": "same_position"}}
        assert resolve_dispatch_action(rec) == "unchanged"

    def test_active_control_off_is_recommendation_only(self):
        rec = {"no_dispatch": {"command_sent": False, "primary_reason": "active_control_off"}}
        assert resolve_dispatch_action(rec) == "recommendation_only"

    def test_blocked_dispatch_authority_is_blocked(self):
        rec = {
            "no_dispatch": {"command_sent": False, "primary_reason": "command_filter_suppressed"},
            "authorities": {"dispatch_authority": {"applied": False, "blocked": True}},
        }
        assert resolve_dispatch_action(rec) == "blocked"

    def test_attempted_but_not_succeeded_is_failed(self):
        rec = {
            "no_dispatch": {
                "command_sent": False, "primary_reason": "service_call_error",
                "recommendation_exists": True,
            },
            "authorities": {"dispatch_authority": {"applied": False, "blocked": False}},
        }
        assert resolve_dispatch_action(rec) == "failed"

    def test_none_record_never_raises(self):
        assert resolve_dispatch_action(None) == "suppressed"


# ---------------------------------------------------------------------------
# Same-source guarantee: explainability and current_decisions/timeline must
# never disagree, because both derive from the same raw decision-trace
# record via the same decision_record.py helpers.
# ---------------------------------------------------------------------------

class _Coord:
    def __init__(self, *, ring_records=None):
        self._support_critical_events = []
        self._research_daily_buckets = {}
        self._ring_records = list(ring_records or [])
        self.zones = {"z1": object()}
        self.windows = {"w1": object()}
        self.cover_groups = {}
        self.config_entry = type("CE", (), {"entry_id": "eid1"})()
        self._adoption_history = []
        self._strategy_adoption_history = []
        self._adoptions_active = {}
        self._pending_outcomes = type("PO", (), {"all_pending": lambda s: []})()

    def decision_trace_snapshot(self):
        if not self._ring_records:
            return {}
        return {"z1": {"records": self._ring_records, "count": len(self._ring_records)}}

    def get_decisions(self, _wid):
        return []

    def get_transitions(self, _wid):
        return []

    def get_overrides(self, _wid):
        return []

    def get_snapshots(self, _wid):
        return []

    def get_outcomes(self, _wid):
        return []

    def storage_diagnostics(self):
        return {}


def _rec_with_dispatch(ts, *, target=0.6):
    return {
        "decision_id": "dec-1",
        "window_id": "w1",
        "decision_timestamp_utc": ts,
        "resolved_state": "normal_shade",
        "decided_by": "Adaptive",
        "no_dispatch": {"command_sent": True, "primary_reason": None},
        "target_chain": {
            "recommendation_position_ha": 0.4,
            "resolved_target_position_ha": target,
            "final_dispatched_target_ha": target,
        },
        "authorities": {"safety_authority": {"active": False}},
    }


class TestExplainabilityAndSupportExportShareTheSameSource:
    def test_decided_by_and_target_match_between_explainability_and_timeline(self):
        rec = _rec_with_dispatch(_NOW.isoformat(), target=0.75)
        export = build_support_export_v3(_Coord(ring_records=[rec]), now=_NOW)

        expl_entry = next(iter(export["explainability"].values()))
        assert expl_entry["winning_rule"] == "Adaptive"

        tl_events = export["support_timeline"]["events"]
        dispatch_evt = next(e for e in tl_events if e["event_type"] == "dispatch_sent")
        assert dispatch_evt["decided_by"] == "Adaptive"
        assert dispatch_evt["target_ha"] == 0.75

        cur_dec = next(iter(export["current_decisions"].values()))
        assert cur_dec["decided_by"] == "Adaptive"
        assert cur_dec["resolved_target_ha"] == 0.75

    def test_direct_helper_and_export_pipeline_agree_on_target(self):
        rec = _rec_with_dispatch(_NOW.isoformat(), target=0.33)
        assert resolve_target_ha(rec) == 0.33
        export = build_support_export_v3(_Coord(ring_records=[rec]), now=_NOW)
        cur_dec = next(iter(export["current_decisions"].values()))
        assert cur_dec["resolved_target_ha"] == 0.33

    def test_target_chain_absent_in_current_decisions_when_unchanged(self):
        rec = _rec_with_dispatch(_NOW.isoformat())
        rec["target_chain"] = {
            "recommendation_position_ha": 0.4,
            "resolved_target_position_ha": 0.4,
            "final_dispatched_target_ha": 0.4,
        }
        export = build_support_export_v3(_Coord(ring_records=[rec]), now=_NOW)
        cur_dec = next(iter(export["current_decisions"].values()))
        assert cur_dec["target_chain"] is None

    def test_target_chain_present_in_current_decisions_when_changed(self):
        rec = _rec_with_dispatch(_NOW.isoformat(), target=0.9)
        export = build_support_export_v3(_Coord(ring_records=[rec]), now=_NOW)
        cur_dec = next(iter(export["current_decisions"].values()))
        assert cur_dec["target_chain"] is not None

    def test_candidates_no_longer_contain_not_recorded_filler(self):
        rec = _rec_with_dispatch(_NOW.isoformat())
        rec["candidates"] = [
            {"candidate_type": "winner", "recording_status": "recorded"},
            {"candidate_type": "baseline", "recording_status": "recorded"},
        ]
        export = build_support_export_v3(_Coord(ring_records=[rec]), now=_NOW)
        cur_dec = next(iter(export["current_decisions"].values()))
        assert cur_dec["candidates"] is not None
        for cand in cur_dec["candidates"]:
            assert cand["recording_status"] != "not_recorded"

    def test_explainability_influences_reflect_decision_record_helper(self):
        rec = _rec_with_dispatch(_NOW.isoformat())
        rec["authorities"] = {"safety_authority": {"active": True}}
        explanation = build_decision_explanation(rec)
        assert explanation.influences.safety is True
        assert explanation.to_dict()["influences"] == {"safety": True}


class TestCoordinatorNoLongerAppendsNotRecordedCandidateFiller:
    """T21 Phase D2: coordinator._record_decision_trace() must build its
    "candidates" list from winner + baseline only — no source-level
    structural test can construct a real Coordinator (heavy HA
    dependencies), so this checks the production source directly, the same
    technique used by test_override_policy_wiring_contract.py."""

    def test_record_decision_trace_source_has_no_not_recorded_filler_loop(self) -> None:
        import ast
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent
            / "custom_components" / "smartshading" / "coordinator.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src, filename="coordinator.py")
        func = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_record_decision_trace"
        )
        # The actual candidates list assignment must be exactly winner+baseline
        # (the docstring itself legitimately still says "not_recorded" when
        # describing the *authority* map's honest not-yet-tracked entries —
        # only the candidates list construction is asserted here).
        assign_found = False
        for node in ast.walk(func):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "candidates":
                        segment = ast.get_source_segment(src, value)
                        assert segment == "[winner, baseline]", (
                            f"candidates list is {segment!r}, expected exactly [winner, baseline]"
                        )
                        assign_found = True
        assert assign_found, "could not locate the 'candidates' key in _record_decision_trace()"
