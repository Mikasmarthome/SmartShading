"""Tests for T13's structured decision-explainability model
(engines/explainability.py) — build_decision_explanation().

Coverage: influence flags read from the authorities map, why-not reasons
from no_dispatch/command_filter/heat/adaptation, defensive behavior on
malformed/missing input, and that no new inference is invented beyond what
the inputs already state.
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.engines.explainability import (
    DecisionExplanation,
    DecisionInfluences,
    WhyNotReason,
    build_decision_explanation,
)


def _rec(**overrides) -> dict:
    base = {
        "decision_id": "dec-1",
        "window_id": "w1",
        "resolved_state": "open",
        "decided_by": "HeatEvaluator",
        "authorities": {
            "safety_authority": {"active": False},
            "manual_override_authority": {"active": False},
            "lifecycle_authority": {"active": False},
            "absence_authority": {"active": False},
            "position_learning_authority": {"applied": False},
            "harmonization_authority": {"applied": False},
            "command_filter_authority": {"blocked": False, "reason_code": None},
            "dispatch_authority": {"applied": False, "blocked": False, "reason_code": None},
        },
        "no_dispatch": {
            "recommendation_exists": True,
            "command_sent": True,
            "primary_reason": None,
            "contributing_reasons": [],
        },
    }
    base.update(overrides)
    return base


class TestInfluencesFromAuthorities:
    def test_no_influences_when_nothing_active(self) -> None:
        exp = build_decision_explanation(_rec())
        assert exp.influences == DecisionInfluences()

    def test_safety_influence_reflects_authority_map(self) -> None:
        rec = _rec()
        rec["authorities"]["safety_authority"]["active"] = True
        exp = build_decision_explanation(rec)
        assert exp.influences.safety is True

    def test_manual_override_influence(self) -> None:
        rec = _rec()
        rec["authorities"]["manual_override_authority"]["active"] = True
        exp = build_decision_explanation(rec)
        assert exp.influences.manual_override is True

    def test_lifecycle_and_presence_influence(self) -> None:
        rec = _rec()
        rec["authorities"]["lifecycle_authority"]["active"] = True
        rec["authorities"]["absence_authority"]["active"] = True
        exp = build_decision_explanation(rec)
        assert exp.influences.lifecycle is True
        assert exp.influences.presence_absence is True

    def test_learning_influences_from_position_authority(self) -> None:
        rec = _rec()
        rec["authorities"]["position_learning_authority"]["applied"] = True
        exp = build_decision_explanation(rec)
        assert exp.influences.learning_position is True

    def test_heat_protection_influence_from_heat_diag(self) -> None:
        exp = build_decision_explanation(_rec(), heat_diag={"active": True, "reason": "entered"})
        assert exp.influences.heat_protection is True

    def test_adaptation_influence_from_adaptation_trace(self) -> None:
        exp = build_decision_explanation(
            _rec(), adaptation_trace={"learning_active": True, "reason": "very_high confidence"},
        )
        assert exp.influences.adaptation is True

    def test_winning_rule_and_dispatched_reflect_record(self) -> None:
        exp = build_decision_explanation(_rec(decided_by="StormEvaluator"))
        assert exp.winning_rule == "StormEvaluator"
        assert exp.dispatched is True


class TestWhyNotReasons:
    def test_no_reasons_when_dispatched(self) -> None:
        exp = build_decision_explanation(_rec())
        assert exp.why_not == ()

    def test_primary_reason_surfaced_when_not_dispatched(self) -> None:
        rec = _rec(no_dispatch={
            "recommendation_exists": True, "command_sent": False,
            "primary_reason": "min_interval_not_elapsed", "contributing_reasons": [],
        })
        exp = build_decision_explanation(rec)
        assert len(exp.why_not) == 1
        assert exp.why_not[0].category == "dispatch"
        assert exp.why_not[0].code == "min_interval_not_elapsed"
        assert "interval" in exp.why_not[0].description.lower()

    def test_contributing_reasons_appended_without_duplicating_primary(self) -> None:
        rec = _rec(no_dispatch={
            "recommendation_exists": True, "command_sent": False,
            "primary_reason": "guard_action_interval",
            "contributing_reasons": ["guard_action_interval", "presence_uncertain"],
        })
        exp = build_decision_explanation(rec)
        codes = [r.code for r in exp.why_not]
        assert codes.count("guard_action_interval") == 1
        assert "presence_uncertain" in codes

    def test_command_filter_block_surfaced_when_distinct_from_primary(self) -> None:
        rec = _rec(no_dispatch={
            "recommendation_exists": True, "command_sent": False,
            "primary_reason": "same_position", "contributing_reasons": [],
        })
        rec["authorities"]["command_filter_authority"] = {
            "blocked": True, "reason_code": "no_target_position",
        }
        exp = build_decision_explanation(rec)
        codes = [r.code for r in exp.why_not]
        assert "no_target_position" in codes

    def test_heat_not_active_reason_surfaced(self) -> None:
        exp = build_decision_explanation(
            _rec(), heat_diag={"active": False, "reason": "not_needed"},
        )
        assert any(r.category == "heat_protection" and r.code == "not_needed"
                   for r in exp.why_not)

    def test_heat_active_yields_no_heat_why_not(self) -> None:
        exp = build_decision_explanation(
            _rec(), heat_diag={"active": True, "reason": "entered"},
        )
        assert not any(r.category == "heat_protection" for r in exp.why_not)

    def test_adaptation_inactive_reason_surfaced(self) -> None:
        exp = build_decision_explanation(
            _rec(),
            adaptation_trace={"learning_active": False, "reason": "confidence too low"},
        )
        reasons = [r for r in exp.why_not if r.category == "adaptation"]
        assert len(reasons) == 1
        assert reasons[0].description == "confidence too low"

    def test_adaptation_active_yields_no_adaptation_why_not(self) -> None:
        exp = build_decision_explanation(
            _rec(), adaptation_trace={"learning_active": True, "reason": "applied"},
        )
        assert not any(r.category == "adaptation" for r in exp.why_not)


class TestDefensiveBehavior:
    def test_none_record_never_raises(self) -> None:
        exp = build_decision_explanation(None)
        assert exp.window_id is None
        assert exp.why_not == ()

    def test_empty_dict_never_raises(self) -> None:
        exp = build_decision_explanation({})
        assert exp.dispatched is False

    def test_malformed_authorities_never_raises(self) -> None:
        rec = _rec(authorities="not-a-dict")
        exp = build_decision_explanation(rec)
        assert exp.influences == DecisionInfluences()

    def test_malformed_no_dispatch_never_raises(self) -> None:
        rec = _rec(no_dispatch=None)
        exp = build_decision_explanation(rec)
        assert exp.dispatched is False
        assert exp.why_not == ()

    def test_none_heat_diag_and_adaptation_trace_yield_no_reasons_from_them(self) -> None:
        exp = build_decision_explanation(_rec())
        assert not any(r.category in ("heat_protection", "adaptation") for r in exp.why_not)


class TestSerialization:
    def test_to_dict_round_trips_structure(self) -> None:
        rec = _rec()
        rec["authorities"]["safety_authority"]["active"] = True
        exp = build_decision_explanation(rec)
        d = exp.to_dict()
        assert d["influences"]["safety"] is True
        assert d["window_id"] == "w1"
        assert isinstance(d["why_not"], list)

    def test_why_not_reason_to_dict(self) -> None:
        r = WhyNotReason(category="dispatch", code="x", description="y")
        assert r.to_dict() == {"category": "dispatch", "code": "x", "description": "y"}
