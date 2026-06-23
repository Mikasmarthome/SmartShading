"""Window Contribution models — LE 2.0 / Phase P5.

Conservative multi-window attribution and a per-zone relative window-contribution
model.  P5 sets attribution_quality (incl. the first window_isolated), learns a
RELATIVE contribution index per window within its zone, and prepares
shadow/experiment eligibility — but has NO cover/threshold/shadow/experiment
authority.

Key separations (P5 sharpenings):
  - prior vs observed vs learned vs normalized contribution are stored distinctly.
  - global zone model (stable, bounded) vs event-specific eligibility (transient).
  - confirmed vs assumed vs unconfirmed position change.
  - confidence is capped for shared-only / candidate-only histories.

No Home Assistant import.  Frozen dataclasses.  Fully serializable.  All
positions HA convention (0=closed, 100=open).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

WINDOW_CONTRIBUTION_SCHEMA_VERSION: int = 1

# Attribution classes (must match multi_objective_outcome semantics).
ATTR_UNKNOWN: str = "unknown"
ATTR_ZONE_SHARED: str = "zone_shared"
ATTR_WINDOW_CANDIDATE: str = "window_candidate"
ATTR_WINDOW_ISOLATED: str = "window_isolated"

# Position-change confirmation classes.
POS_CONFIRMED: str = "position_change_confirmed"
POS_ASSUMED: str = "position_change_assumed"
POS_UNCONFIRMED: str = "position_change_unconfirmed"
POS_NONE: str = "position_change_none"          # already at target / no movement

# --- Evidence weights (named, documented; multiplied by ObservationReliability) ---
# isolated dominates; candidate is weak; shared is a very weak prior support and
# never assigns the full thermal outcome to each window.
WEIGHT_ISOLATED: float = 1.0
WEIGHT_CANDIDATE: float = 0.3
WEIGHT_SHARED: float = 0.1
WEIGHT_UNKNOWN: float = 0.0

# --- Confidence caps (prevent shared/candidate-only histories from gaining
# high window-specific confidence regardless of sample count) ---
CONF_CAP_SHARED_ONLY: float = 0.2
CONF_CAP_CANDIDATE_ONLY: float = 0.5

# --- Eligibility thresholds ---
SHADOW_MIN_CONFIDENCE: float = 0.4
EXPERIMENT_MIN_CONFIDENCE: float = 0.7
EXPERIMENT_MIN_ISOLATED_SAMPLES: int = 8
EXPERIMENT_MIN_DISTINCT_DAYS: int = 5

# Prior sources
PRIOR_NEUTRAL: str = "neutral"
PRIOR_EXPOSURE_SECTOR: str = "exposure_sector"
PRIOR_EXPOSURE_SECTOR_AREA: str = "exposure_sector_area"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    d = datetime.fromisoformat(ts)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def _clamp01(v: float | None) -> float | None:
    if v is None:
        return None
    return max(0.0, min(1.0, v))


def event_weight_for(attribution_quality: str) -> float:
    return {
        ATTR_WINDOW_ISOLATED: WEIGHT_ISOLATED,
        ATTR_WINDOW_CANDIDATE: WEIGHT_CANDIDATE,
        ATTR_ZONE_SHARED: WEIGHT_SHARED,
    }.get(attribution_quality, WEIGHT_UNKNOWN)


# ---------------------------------------------------------------------------
# WindowAttributionResult (per zone event)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowAttributionResult:
    """Deterministic output of the solo-event gate for one zone observation."""

    attribution_quality: str
    candidate_window_id: str | None = None
    contributing_window_ids: tuple[str, ...] = ()
    excluded_window_ids: tuple[str, ...] = ()
    solo_event: bool = False
    isolation_confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    disqualifiers: tuple[str, ...] = ()
    model_eligible: bool = False

    def to_dict(self) -> dict:
        return {
            "attribution_quality": self.attribution_quality,
            "candidate_window_id": self.candidate_window_id,
            "contributing_window_ids": list(self.contributing_window_ids),
            "excluded_window_ids": list(self.excluded_window_ids),
            "solo_event": self.solo_event,
            "isolation_confidence": self.isolation_confidence,
            "evidence": list(self.evidence),
            "disqualifiers": list(self.disqualifiers),
            "model_eligible": self.model_eligible,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WindowAttributionResult":
        return cls(
            attribution_quality=d["attribution_quality"],
            candidate_window_id=d.get("candidate_window_id"),
            contributing_window_ids=tuple(d.get("contributing_window_ids", []) or []),
            excluded_window_ids=tuple(d.get("excluded_window_ids", []) or []),
            solo_event=bool(d.get("solo_event", False)),
            isolation_confidence=float(d.get("isolation_confidence", 0.0)),
            evidence=tuple(d.get("evidence", []) or []),
            disqualifiers=tuple(d.get("disqualifiers", []) or []),
            model_eligible=bool(d.get("model_eligible", False)),
        )


# ---------------------------------------------------------------------------
# WindowContributionEvidence (bounded, per window)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowContributionEvidence:
    """One contribution observation for a window, referencing the zone event."""

    window_id: str
    zone_id: str
    decision_id: str | None
    observation_decision_ids: tuple[str, ...]
    timestamp: datetime
    attribution_quality: str
    event_weight: float
    observation_reliability: float
    observed_contribution_signal: float | None   # thermal score attributed this event
    effective_exposure: float | None
    blocked_or_no_exposure: bool
    context_key: str
    config_generation: int

    def to_dict(self) -> dict:
        return {
            "window_id": self.window_id, "zone_id": self.zone_id,
            "decision_id": self.decision_id,
            "observation_decision_ids": list(self.observation_decision_ids),
            "timestamp": _iso(self.timestamp),
            "attribution_quality": self.attribution_quality,
            "event_weight": self.event_weight,
            "observation_reliability": self.observation_reliability,
            "observed_contribution_signal": self.observed_contribution_signal,
            "effective_exposure": self.effective_exposure,
            "blocked_or_no_exposure": self.blocked_or_no_exposure,
            "context_key": self.context_key,
            "config_generation": self.config_generation,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WindowContributionEvidence":
        return cls(
            window_id=d["window_id"], zone_id=d["zone_id"],
            decision_id=d.get("decision_id"),
            observation_decision_ids=tuple(d.get("observation_decision_ids", []) or []),
            timestamp=_parse(d["timestamp"]),  # type: ignore[arg-type]
            attribution_quality=d.get("attribution_quality", ATTR_UNKNOWN),
            event_weight=float(d.get("event_weight", 0.0)),
            observation_reliability=float(d.get("observation_reliability", 0.0)),
            observed_contribution_signal=d.get("observed_contribution_signal"),
            effective_exposure=d.get("effective_exposure"),
            blocked_or_no_exposure=bool(d.get("blocked_or_no_exposure", False)),
            context_key=d.get("context_key", "global"),
            config_generation=int(d.get("config_generation", 0)),
        )


# ---------------------------------------------------------------------------
# WindowContributionModel (per window, stable global zone model)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowContributionModel:
    """Per-window relative contribution model within its zone.

    prior / observed / learned / normalized are kept distinct so it is always
    reconstructable what was assumed vs observed vs how strongly real evidence
    overrode the prior.  All indices are relative within the zone, NOT a claimed
    physical heat share.
    """

    window_id: str
    zone_id: str
    schema_version: int = WINDOW_CONTRIBUTION_SCHEMA_VERSION
    prior_contribution_index: float | None = None
    prior_source: str = PRIOR_NEUTRAL
    observed_contribution_signal: float | None = None     # aggregate observed evidence
    learned_relative_contribution_index: float | None = None   # prior blended with evidence
    normalized_relative_contribution_index: float | None = None  # normalized within zone
    confidence: float = 0.0
    reliability: float = 0.0
    isolated_sample_count: int = 0
    candidate_sample_count: int = 0
    shared_sample_count: int = 0
    distinct_days: int = 0
    context_coverage: int = 0
    evidence_sources: tuple[str, ...] = ()
    last_updated: datetime | None = None
    config_generation: int = 0
    fallback_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "window_id": self.window_id, "zone_id": self.zone_id,
            "schema_version": self.schema_version,
            "prior_contribution_index": self.prior_contribution_index,
            "prior_source": self.prior_source,
            "observed_contribution_signal": self.observed_contribution_signal,
            "learned_relative_contribution_index": self.learned_relative_contribution_index,
            "normalized_relative_contribution_index": self.normalized_relative_contribution_index,
            "confidence": self.confidence, "reliability": self.reliability,
            "isolated_sample_count": self.isolated_sample_count,
            "candidate_sample_count": self.candidate_sample_count,
            "shared_sample_count": self.shared_sample_count,
            "distinct_days": self.distinct_days,
            "context_coverage": self.context_coverage,
            "evidence_sources": list(self.evidence_sources),
            "last_updated": _iso(self.last_updated),
            "config_generation": self.config_generation,
            "fallback_reason": self.fallback_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WindowContributionModel":
        return cls(
            window_id=d["window_id"], zone_id=d["zone_id"],
            schema_version=int(d.get("schema_version", WINDOW_CONTRIBUTION_SCHEMA_VERSION)),
            prior_contribution_index=d.get("prior_contribution_index"),
            prior_source=d.get("prior_source", PRIOR_NEUTRAL),
            observed_contribution_signal=d.get("observed_contribution_signal"),
            learned_relative_contribution_index=d.get("learned_relative_contribution_index"),
            normalized_relative_contribution_index=d.get("normalized_relative_contribution_index"),
            confidence=float(d.get("confidence", 0.0)),
            reliability=float(d.get("reliability", 0.0)),
            isolated_sample_count=int(d.get("isolated_sample_count", 0)),
            candidate_sample_count=int(d.get("candidate_sample_count", 0)),
            shared_sample_count=int(d.get("shared_sample_count", 0)),
            distinct_days=int(d.get("distinct_days", 0)),
            context_coverage=int(d.get("context_coverage", 0)),
            evidence_sources=tuple(d.get("evidence_sources", []) or []),
            last_updated=_parse(d.get("last_updated")),
            config_generation=int(d.get("config_generation", 0)),
            fallback_reason=d.get("fallback_reason"),
        )
