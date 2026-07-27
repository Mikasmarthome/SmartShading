"""Decision Explainability — T13.

SmartShading already computes, every cycle, everything needed to answer
"why is this window at this position right now" — spread across the
decision-trace record (authorities map, no_dispatch reason, candidate
winner/baseline), the per-window execution diagnostics (heat hysteresis),
and the adaptation trace (T12). Before T13 there was no single place that
combined these into one structured answer; a support case had to read three
separate export sections and reconcile them by hand.

This module adds exactly ONE structured explainability model
(``DecisionExplanation``) built by a pure, read-only, never-raising function
that synthesizes ALREADY-COMPUTED data. It does not re-run any evaluator, it
does not read or infer anything not already present in its inputs, and it
never affects control. Where the underlying data does not exist (e.g. why a
losing tier's candidate value was NOT chosen — SmartShading's evaluators do
not retain rejected candidate values today), no answer is invented; the gap
is left absent rather than guessed.

Both structured "was X involved" influence flags and structured "why not"
reasons draw on the same fields, so there is exactly one explainability
model — not a separate one per subsystem.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import decision_record as _dr
from . import reason_codes as _rc

# ---------------------------------------------------------------------------
# Human-readable translations for machine reason codes already produced
# elsewhere (CommandFilterResult.blocked_reason, dispatch_filter_reason,
# heat_hysteresis reasons). Purely a display layer — the underlying codes
# are untouched; this table only makes them readable without code access.
#
# T21 Phase D: reason_codes.py's registry is the canonical description
# source for any code it covers (_describe() below checks it first) — this
# table now only needs entries for codes reason_codes.py doesn't have (heat
# hysteresis reasons, command-filter suppression, presence-hold variants),
# avoiding two independently-maintained descriptions for the same code.
# ---------------------------------------------------------------------------

_REASON_DESCRIPTIONS: dict[str, str] = {
    "active_control_off": "Learning/observation mode only — real dispatch is disabled for this window.",
    "dispatch_not_required": "The resolved target already matches the current position — no command needed.",
    "command_filter_suppressed": "The command filter suppressed dispatch (see contributing reasons).",
    "same_position": "Target position unchanged from the current position.",
    "same_position_no_change": "Target position unchanged from the current position.",
    "no_target_position": "No target position was resolved this cycle.",
    "recommendation_only": "Recommendation-only mode — no real cover command is sent.",
    "guard_action_interval": "The minimum action interval since the last command has not elapsed yet.",
    "presence_uncertain": "Presence status is uncertain; the guarded action was withheld.",
    "min_interval_not_elapsed": "The minimum action interval since the last command has not elapsed yet.",
    "behavior_mode_hold": "The window's behavior mode is holding this position.",
    "presence_uncertain_hold": "Presence status is uncertain; the position is being held.",
    "startup_grace": "Startup grace period — dispatch is suppressed briefly after (re)start.",
    "not_recorded": "No specific reason was recorded for this cycle.",
    "held_missing_data": "Heat hysteresis held: required sensor data is missing.",
    "insufficient_data": "Heat hysteresis held: not enough data to evaluate the thresholds yet.",
    "not_needed": "Heat protection is not currently needed (thresholds not met).",
    "disabled": "Heat protection is disabled for this window.",
    "held_by_hysteresis": "Heat hysteresis is holding the current protection state to avoid oscillation.",
}


def _describe(code: str | None) -> str | None:
    if not code:
        return None
    return (
        _rc.description_for(code)
        or _REASON_DESCRIPTIONS.get(code)
        or code.replace("_", " ")
    )


# ---------------------------------------------------------------------------
# Structured reason model — one shared shape, not free strings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WhyNotReason:
    """One structured "why not" reason. ``category`` groups related reasons
    (dispatch/heat_protection/learning/adaptation/override); ``code`` is the
    underlying machine reason already produced elsewhere in the codebase;
    ``description`` is a human-readable translation of that same code."""

    category: str
    code: str
    description: str

    def to_dict(self) -> dict:
        return {"category": self.category, "code": self.code, "description": self.description}


@dataclass(frozen=True)
class DecisionInfluences:
    """Which subsystems were actually active/applied for this decision —
    read verbatim from the decision-trace authorities map (T13 adds no new
    tracking; it only names and groups fields that already existed)."""

    safety: bool = False
    manual_override: bool = False
    lifecycle: bool = False
    presence_absence: bool = False
    heat_protection: bool = False
    learning_position: bool = False
    harmonization: bool = False
    adaptation: bool = False

    def to_dict(self) -> dict:
        # T21 Phase D2: compact — only influences that were actually active
        # are present at all ("nur tatsächlich relevante Einflüsse, nicht
        # standardmäßig alle als false"). The dataclass itself still carries
        # all 8 fields (so in-process callers can keep using attribute
        # access like `explanation.influences.safety`); only the exported
        # dict shape dropped the redundant False entries.
        return {
            name: True
            for name, value in (
                ("safety", self.safety),
                ("manual_override", self.manual_override),
                ("lifecycle", self.lifecycle),
                ("presence_absence", self.presence_absence),
                ("heat_protection", self.heat_protection),
                ("learning_position", self.learning_position),
                ("harmonization", self.harmonization),
                ("adaptation", self.adaptation),
            )
            if value
        }


@dataclass(frozen=True)
class DecisionExplanation:
    """One window's fully assembled "why" for the current/latest decision."""

    window_id: str | None
    decision_id: str | None
    decided_state: str | None
    winning_rule: str | None
    dispatched: bool
    influences: DecisionInfluences = field(default_factory=DecisionInfluences)
    why_not: tuple[WhyNotReason, ...] = ()

    def to_dict(self) -> dict:
        return {
            "window_id": self.window_id,
            "decision_id": self.decision_id,
            "decided_state": self.decided_state,
            "winning_rule": self.winning_rule,
            "dispatched": self.dispatched,
            "influences": self.influences.to_dict(),
            "why_not": [r.to_dict() for r in self.why_not],
        }


# ---------------------------------------------------------------------------
# Builder — pure, defensive (never raises), reads only already-computed data
# ---------------------------------------------------------------------------

def build_decision_explanation(
    decision_record: dict | None,
    *,
    heat_diag: dict | None = None,
    adaptation_trace: dict | None = None,
) -> DecisionExplanation:
    """Synthesize a DecisionExplanation from already-computed sources.

    decision_record: one raw record from the coordinator's decision-trace
        ring (same shape consumed by support_export._pseudo_decision).
    heat_diag: optional dict with at least ``active``/``reason`` describing
        this window's current heat-hysteresis state (already exported under
        support_export's inputs.heat block).
    adaptation_trace: optional dict as produced by
        support_export._adaptation_trace() (T12) — ``learning_active``,
        ``confidence_level``, ``adaptation_strength``, ``reason``.

    Never raises: any malformed/missing input degrades to absent fields
    rather than an exception, matching the rest of the export/diagnostics
    layer's fail-open convention.
    """
    rec = decision_record if isinstance(decision_record, dict) else {}
    authorities = rec.get("authorities")
    authorities = authorities if isinstance(authorities, dict) else {}
    no_dispatch = rec.get("no_dispatch")
    no_dispatch = no_dispatch if isinstance(no_dispatch, dict) else {}

    def _auth(key: str) -> dict:
        v = authorities.get(key)
        return v if isinstance(v, dict) else {}

    heat_active = bool((heat_diag or {}).get("active"))
    adaptation_active = bool((adaptation_trace or {}).get("learning_active"))

    # T21 Phase D2: computed once, shared with support_export.py's
    # current_decisions/target-chain views — see engines/decision_record.py.
    _active = _dr.resolve_active_influences(
        authorities, heat_active=heat_active, adaptation_active=adaptation_active,
    )
    influences = DecisionInfluences(
        safety=_active.get("safety", False),
        manual_override=_active.get("manual_override", False),
        lifecycle=_active.get("lifecycle", False),
        presence_absence=_active.get("presence_absence", False),
        heat_protection=_active.get("heat_protection", False),
        learning_position=_active.get("learning_position", False),
        harmonization=_active.get("harmonization", False),
        adaptation=_active.get("adaptation", False),
    )

    why_not: list[WhyNotReason] = []

    primary = no_dispatch.get("primary_reason")
    if primary and not no_dispatch.get("command_sent"):
        why_not.append(WhyNotReason(
            category="dispatch", code=primary, description=_describe(primary) or primary,
        ))
    for extra in no_dispatch.get("contributing_reasons") or ():
        if extra and extra != primary:
            why_not.append(WhyNotReason(
                category="dispatch", code=extra, description=_describe(extra) or extra,
            ))

    cf_auth = _auth("command_filter_authority")
    if cf_auth.get("blocked") and cf_auth.get("reason_code"):
        code = cf_auth["reason_code"]
        if code != primary:
            why_not.append(WhyNotReason(
                category="command_filter", code=code, description=_describe(code) or code,
            ))

    if heat_diag is not None and not heat_active:
        heat_reason = heat_diag.get("reason")
        if heat_reason:
            why_not.append(WhyNotReason(
                category="heat_protection", code=heat_reason,
                description=_describe(heat_reason) or heat_reason,
            ))

    if adaptation_trace is not None and not adaptation_active:
        adapt_reason = adaptation_trace.get("reason")
        why_not.append(WhyNotReason(
            category="adaptation",
            code="learning_inactive",
            description=adapt_reason or "Learning-based adaptation was not applied this cycle.",
        ))

    return DecisionExplanation(
        window_id=rec.get("window_id"),
        decision_id=rec.get("decision_id"),
        decided_state=rec.get("resolved_state"),
        winning_rule=rec.get("decided_by"),
        dispatched=bool(no_dispatch.get("command_sent")),
        influences=influences,
        why_not=tuple(why_not),
    )
