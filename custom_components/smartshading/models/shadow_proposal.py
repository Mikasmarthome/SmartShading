"""Shadow Adaptation models — LE 2.0 / Phase P6.

A ShadowProposal is a per-window, per-intensity, per-context candidate shading
target that is computed and persistently observed but NEVER applied to real
cover control.  P6 is pure analysis/persistence/evaluation and prepares P7
experiment eligibility.

Hard invariants:
  - Shadow never changes cover target, shading state, thresholds, harmonization,
    command filter, dispatch, manual override, lifecycle or safety.
  - applied_target_ha always remains the REAL non-shadow target.
  - Candidates are close-more only (HA: smaller value), one fixed 5 pp step,
    validated through the real clamp/floor functions (no `applied-5` shortcut).
  - Evaluation is never causal: limitations always contains
    'not_causally_validated'; 'supported' means only "repeated evidence supports
    testing this candidate", never "proven better".

No Home Assistant import.  Frozen dataclasses.  HA position convention
(0=closed, 100=open).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

SHADOW_SCHEMA_VERSION: int = 1

# Candidate step / caps (HA percentage points).
SHADOW_STEP_HA: int = 5                 # single fixed close-more step in P6
SHADOW_CUMULATIVE_CAP_HA: int = 10      # max cumulative deviation vs config base (for P7)
SHADOW_MATERIALITY_HA: int = 3          # P2 deadband — below this is not material

# States
STATUS_PROPOSED: str = "proposed"
STATUS_OBSERVING: str = "observing"
STATUS_SUPPORTED: str = "supported"
STATUS_INCONCLUSIVE: str = "inconclusive"
STATUS_REJECTED: str = "rejected"
STATUS_EXPIRED: str = "expired"
STATUS_INVALIDATED: str = "invalidated"

DIRECTION_CLOSE_MORE: str = "close_more"

# Supported maturity gates (isolated vs the stricter candidate path).
SUPPORTED_MIN_OUTCOMES_ISOLATED: int = 5
SUPPORTED_MIN_DAYS_ISOLATED: int = 3
SUPPORTED_MIN_OUTCOMES_CANDIDATE: int = 8
SUPPORTED_MIN_DAYS_CANDIDATE: int = 4
SUPPORTED_MIN_CONFIDENCE: float = 0.6

CAUSAL_LIMITATION: str = "not_causally_validated"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    d = datetime.fromisoformat(ts)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def _ha(v: int | None, name: str) -> int | None:
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > 100:
        raise ValueError(f"{name} must be HA position [0,100] or None, got {v!r}")
    return v


# ---------------------------------------------------------------------------
# ShadowEligibilityResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShadowEligibilityResult:
    """Output of the pure shadow eligibility gate."""

    eligible: bool
    intensity_level: str | None = None
    reasons: tuple[str, ...] = ()        # passed gates / supporting evidence
    blocked_by: tuple[str, ...] = ()     # failed gates
    block_reason: str | None = None      # primary block reason

    def to_dict(self) -> dict:
        return {
            "eligible": self.eligible, "intensity_level": self.intensity_level,
            "reasons": list(self.reasons), "blocked_by": list(self.blocked_by),
            "block_reason": self.block_reason,
        }


# ---------------------------------------------------------------------------
# ShadowEvaluation (honest, non-causal)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShadowEvaluation:
    comparable_baseline_outcomes: int = 0
    negative_baseline_outcomes: int = 0
    neutral_baseline_outcomes: int = 0
    contradictory_outcomes: int = 0
    distinct_days: int = 0
    context_consistency: float = 0.0
    candidate_direction_consistency: float = 0.0
    preference_support: bool = False
    preference_veto: bool = False
    confidence: float = 0.0
    status: str = STATUS_PROPOSED
    limitations: tuple[str, ...] = (CAUSAL_LIMITATION,)

    def to_dict(self) -> dict:
        lims = tuple(self.limitations)
        if CAUSAL_LIMITATION not in lims:
            lims = lims + (CAUSAL_LIMITATION,)
        return {
            "comparable_baseline_outcomes": self.comparable_baseline_outcomes,
            "negative_baseline_outcomes": self.negative_baseline_outcomes,
            "neutral_baseline_outcomes": self.neutral_baseline_outcomes,
            "contradictory_outcomes": self.contradictory_outcomes,
            "distinct_days": self.distinct_days,
            "context_consistency": self.context_consistency,
            "candidate_direction_consistency": self.candidate_direction_consistency,
            "preference_support": self.preference_support,
            "preference_veto": self.preference_veto,
            "confidence": self.confidence, "status": self.status,
            "limitations": list(lims),
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "ShadowEvaluation":
        if not isinstance(d, dict):
            return cls()
        lims = tuple(d.get("limitations", []) or ())
        if CAUSAL_LIMITATION not in lims:
            lims = lims + (CAUSAL_LIMITATION,)
        return cls(
            comparable_baseline_outcomes=int(d.get("comparable_baseline_outcomes", 0)),
            negative_baseline_outcomes=int(d.get("negative_baseline_outcomes", 0)),
            neutral_baseline_outcomes=int(d.get("neutral_baseline_outcomes", 0)),
            contradictory_outcomes=int(d.get("contradictory_outcomes", 0)),
            distinct_days=int(d.get("distinct_days", 0)),
            context_consistency=float(d.get("context_consistency", 0.0)),
            candidate_direction_consistency=float(d.get("candidate_direction_consistency", 0.0)),
            preference_support=bool(d.get("preference_support", False)),
            preference_veto=bool(d.get("preference_veto", False)),
            confidence=float(d.get("confidence", 0.0)),
            status=d.get("status", STATUS_PROPOSED), limitations=lims,
        )


# ---------------------------------------------------------------------------
# ShadowProposal
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShadowProposal:
    shadow_id: str
    window_id: str
    zone_id: str
    intensity_level: str
    context_family: str
    created_at: datetime
    updated_at: datetime
    # Target stages (all HA convention).  Reconstructable from P2 provenance;
    # stored here for self-contained shadow reasoning.
    configured_intensity_target_ha: int | None = None
    manual_preference_target_ha: int | None = None
    current_authoritative_intensity_target_ha: int | None = None
    shadow_parameter_target_ha: int | None = None           # current authoritative − step (pre-clamp)
    real_applied_target_ha: int | None = None                # real final non-shadow target (NEVER applied-from)
    shadow_final_candidate_target_ha: int | None = None      # after real clamps/harmonization dry-run
    net_shadow_delta_vs_real_ha: int | None = None           # final candidate − real applied (negative)
    candidate_direction: str = DIRECTION_CLOSE_MORE
    proposal_reason: str = ""
    evidence_sources: tuple[str, ...] = ()
    source_decision_ids: tuple[str, ...] = ()
    attribution_quality: str = "unknown"
    contribution_index: float | None = None
    contribution_confidence: float | None = None
    config_generation: int = 0
    status: str = STATUS_PROPOSED
    evaluation: ShadowEvaluation = field(default_factory=ShadowEvaluation)
    expiry_at: datetime | None = None
    block_reason: str | None = None
    schema_version: int = SHADOW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for n in ("configured_intensity_target_ha", "manual_preference_target_ha",
                  "current_authoritative_intensity_target_ha", "shadow_parameter_target_ha",
                  "real_applied_target_ha", "shadow_final_candidate_target_ha"):
            _ha(getattr(self, n), n)

    @property
    def timestamp(self) -> datetime:
        return self.created_at

    @property
    def proposal_key(self) -> tuple[str, str, str]:
        return (self.window_id, self.intensity_level, self.context_family)

    @property
    def experiment_candidate_ready(self) -> bool:
        """Diagnostic snapshot only — P7 must re-derive from current data."""
        return self.status == STATUS_SUPPORTED

    def to_dict(self) -> dict:
        return {
            "shadow_id": self.shadow_id, "window_id": self.window_id, "zone_id": self.zone_id,
            "intensity_level": self.intensity_level, "context_family": self.context_family,
            "created_at": _iso(self.created_at), "updated_at": _iso(self.updated_at),
            "configured_intensity_target_ha": self.configured_intensity_target_ha,
            "manual_preference_target_ha": self.manual_preference_target_ha,
            "current_authoritative_intensity_target_ha": self.current_authoritative_intensity_target_ha,
            "shadow_parameter_target_ha": self.shadow_parameter_target_ha,
            "real_applied_target_ha": self.real_applied_target_ha,
            "shadow_final_candidate_target_ha": self.shadow_final_candidate_target_ha,
            "net_shadow_delta_vs_real_ha": self.net_shadow_delta_vs_real_ha,
            "candidate_direction": self.candidate_direction,
            "proposal_reason": self.proposal_reason,
            "evidence_sources": list(self.evidence_sources),
            "source_decision_ids": list(self.source_decision_ids),
            "attribution_quality": self.attribution_quality,
            "contribution_index": self.contribution_index,
            "contribution_confidence": self.contribution_confidence,
            "config_generation": self.config_generation, "status": self.status,
            "evaluation": self.evaluation.to_dict(),
            "expiry_at": _iso(self.expiry_at), "block_reason": self.block_reason,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ShadowProposal":
        return cls(
            shadow_id=d["shadow_id"], window_id=d["window_id"], zone_id=d["zone_id"],
            intensity_level=d["intensity_level"], context_family=d.get("context_family", "global"),
            created_at=_parse(d["created_at"]),  # type: ignore[arg-type]
            updated_at=_parse(d.get("updated_at") or d["created_at"]),  # type: ignore[arg-type]
            configured_intensity_target_ha=d.get("configured_intensity_target_ha"),
            manual_preference_target_ha=d.get("manual_preference_target_ha"),
            current_authoritative_intensity_target_ha=d.get("current_authoritative_intensity_target_ha"),
            shadow_parameter_target_ha=d.get("shadow_parameter_target_ha"),
            real_applied_target_ha=d.get("real_applied_target_ha"),
            shadow_final_candidate_target_ha=d.get("shadow_final_candidate_target_ha"),
            net_shadow_delta_vs_real_ha=d.get("net_shadow_delta_vs_real_ha"),
            candidate_direction=d.get("candidate_direction", DIRECTION_CLOSE_MORE),
            proposal_reason=d.get("proposal_reason", ""),
            evidence_sources=tuple(d.get("evidence_sources", []) or []),
            source_decision_ids=tuple(d.get("source_decision_ids", []) or []),
            attribution_quality=d.get("attribution_quality", "unknown"),
            contribution_index=d.get("contribution_index"),
            contribution_confidence=d.get("contribution_confidence"),
            config_generation=int(d.get("config_generation", 0)),
            status=d.get("status", STATUS_PROPOSED),
            evaluation=ShadowEvaluation.from_dict(d.get("evaluation")),
            expiry_at=_parse(d.get("expiry_at")), block_reason=d.get("block_reason"),
            schema_version=int(d.get("schema_version", SHADOW_SCHEMA_VERSION)),
        )
