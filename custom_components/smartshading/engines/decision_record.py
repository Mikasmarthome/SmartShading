"""Canonical decision-record view helpers — T21 Phase D2.

The coordinator's decision-trace ring (`decision_trace_snapshot()`,
`coordinator._record_decision_trace()`) is already the single production
site for a window's per-cycle decision — this module does NOT introduce a
new source of truth or re-run any evaluator. It factors out the three
pieces of derivation logic that were previously reimplemented independently
in `explainability.py` and `support_export.py` (active-influence flags,
"did the target actually change" target-chain filtering, and the resolved
target value used for a timeline event), so every consumer computes them
the same way, once.

Pure, read-only, never-raising: a malformed/missing input degrades to an
empty/absent result rather than an exception, matching the rest of the
export/diagnostics layer's fail-open convention.
"""
from __future__ import annotations

# Authority-map key -> the compact influence name exported when active.
# T21 Phase D2: previously explainability.py's DecisionInfluences carried a
# fixed 8-key dict with every unused influence explicitly False; the export
# layer now only lists influences that were actually active, so a routine
# decision with nothing unusual going on exports an (almost) empty list
# instead of eight redundant "false" fields.
_INFLUENCE_SOURCES: tuple[tuple[str, str, str], ...] = (
    # (influence_name, authority_key, field_that_means_active)
    ("safety", "safety_authority", "active"),
    ("manual_override", "manual_override_authority", "active"),
    ("lifecycle", "lifecycle_authority", "active"),
    ("presence_absence", "absence_authority", "active"),
    ("learning_position", "position_learning_authority", "applied"),
    ("harmonization", "harmonization_authority", "applied"),
)


def resolve_active_influences(
    authorities: dict | None, *, heat_active: bool = False, adaptation_active: bool = False,
) -> dict[str, bool]:
    """The compact set of influences that were ACTUALLY active for this
    decision — only keys with a True value are present at all (T21 Phase D2:
    "nur tatsächlich relevante Einflüsse, nicht standardmäßig alle als
    false"). `heat_active`/`adaptation_active` come from callers' own
    already-computed heat-hysteresis/adaptation-trace lookups (this module
    does not read the coordinator itself, matching explainability.py's
    original discipline)."""
    authorities = authorities if isinstance(authorities, dict) else {}
    out: dict[str, bool] = {}
    for name, auth_key, active_field in _INFLUENCE_SOURCES:
        auth = authorities.get(auth_key)
        if isinstance(auth, dict) and bool(auth.get(active_field)):
            out[name] = True
    if heat_active:
        out["heat_protection"] = True
    if adaptation_active:
        out["adaptation"] = True
    return out


# target_chain sub-fields considered, in the order a value should be reported
# once a real transformation is detected — mirrors the raw field names
# _record_decision_trace() (coordinator.py) already writes.
_TARGET_CHAIN_STAGES: tuple[str, ...] = (
    "recommendation_position_ha",
    "resolved_target_position_ha",
    "post_harmonization_target_ha",
    "intended_payload_position_ha",
    "actual_payload_position_ha",
)


def resolve_target_chain(target_chain_raw: dict | None) -> dict | None:
    """Only returns a target_chain dict when the target actually changed
    somewhere along the recommendation -> dispatch pipeline (T21 Phase D2:
    "Nur speichern, wenn sich der Zielwert tatsächlich verändert hat").
    A decision where every stage agrees on the same position (or is simply
    absent) returns None — the single resolved value is already available
    via resolve_target_ha() below; repeating it across 5 identical fields
    added no information."""
    raw = target_chain_raw if isinstance(target_chain_raw, dict) else {}
    values = [raw.get(k) for k in _TARGET_CHAIN_STAGES if raw.get(k) is not None]
    if len(set(values)) <= 1:
        return None
    return {k: raw[k] for k in _TARGET_CHAIN_STAGES if raw.get(k) is not None}


def resolve_target_ha(rec: dict | None) -> float | None:
    """The single real resolved/dispatched target for this decision — the
    same preference order used everywhere a "what position is this" value
    is needed (timeline events, explainability, current_decisions), so they
    can never disagree just because one reads a different target_chain key
    than another. Prefers the actual dispatched value, then the resolved
    target, then the original recommendation."""
    rec = rec if isinstance(rec, dict) else {}
    tc = rec.get("target_chain")
    tc = tc if isinstance(tc, dict) else {}
    return (
        tc.get("final_dispatched_target_ha")
        or tc.get("resolved_target_position_ha")
        or tc.get("recommendation_position_ha")
    )


# Dispatch outcome "action" — a compact classification of what happened to
# the resolved target this cycle, shared by explainability/current_decisions
# (the finer-grained timeline event_type classification in support_export.py
# is a superset used for aggregation/severity and stays separate — see T21
# Phase D1 — but both read the same resolve_target_ha() above).
_NO_ACTION_ONLY_REASONS = frozenset({"same_position", "same_position_no_change"})


def resolve_dispatch_action(rec: dict | None) -> str:
    """One of: sent / blocked / suppressed / recommendation_only / failed /
    unchanged. Never raises; an unrecognized/missing shape defaults to
    "suppressed" (the most conservative "nothing happened" reading)."""
    rec = rec if isinstance(rec, dict) else {}
    no_dispatch = rec.get("no_dispatch")
    no_dispatch = no_dispatch if isinstance(no_dispatch, dict) else {}
    authorities = rec.get("authorities")
    authorities = authorities if isinstance(authorities, dict) else {}
    dispatch_auth = authorities.get("dispatch_authority")
    dispatch_auth = dispatch_auth if isinstance(dispatch_auth, dict) else {}

    if bool(no_dispatch.get("command_sent")):
        return "sent"
    primary = no_dispatch.get("primary_reason") or ""
    if primary in _NO_ACTION_ONLY_REASONS:
        return "unchanged"
    if primary == "active_control_off":
        return "recommendation_only"
    if bool(dispatch_auth.get("blocked")):
        return "blocked"
    if (
        dispatch_auth.get("applied") is False
        and dispatch_auth.get("blocked") is False
        and bool(no_dispatch.get("recommendation_exists"))
        and primary not in ("dispatch_not_required", "command_filter_suppressed", "")
    ):
        return "failed"
    return "suppressed"
