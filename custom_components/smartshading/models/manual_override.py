"""Active manual override for one window.

Produced by OverrideDetector (engines/override_detector.py), consumed by
ManualOverrideEvaluator (evaluators/manual_override_evaluator.py, Tier 2).

An active override is now persisted across HA restart/reload (to_dict/from_dict
below) so a manual movement is not silently re-asserted after a restart.  Stale
overrides are dropped on restore via the ``expires_at`` bound.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..state_machine.states import ShadingState


class OverrideReleaseStrategy(Enum):
    """How/when an active Manual Override ends (v1.2.0-beta.1, T10).

    Replaces T7's two-value OverrideDurationMode with a full release-
    strategy architecture: the user answers "when should SmartShading get
    control back?" — a fixed duration is just one of several possible
    answers, not the primary mental model any more.

    DURATION            — expires_at = started_at + a configured duration in
                           minutes (T7's LEGACY mode, renamed). Default
                           behavior for anyone who explicitly wants a plain
                           timer.
    FIXED_TIME           — expires_at is the next occurrence of a configured
                           local clock time (unchanged from T7 — see
                           engines/override_fixed_time.py).
    LIFECYCLE            — released by the next lifecycle transition (e.g.
                           night override ends in the morning). This is the
                           new DEFAULT — it reproduces T7's default
                           break_on_lifecycle=True behavior exactly (see
                           engines/lifecycle_guard.py, unchanged). An
                           optional safety-timeout (below) remains available
                           as a defensive backstop, exactly like T7's
                           duration_min/night_duration_min already served as
                           a safety net behind the lifecycle release.
    FIRST_COMFORT        — released the moment SmartShading would make its
                           first regular Comfort-tier decision again.
    FIRST_PROTECTION      — released the moment SmartShading would make its
                           first regular Protection-tier decision again
                           (e.g. real heat or glare protection).
    FIRST_ANY_DECISION    — released by whichever of Comfort or Protection
                           fires first.
    MANUAL                — no automatic release condition at all; the user
                           clears the override explicitly. An optional
                           safety-timeout (below) remains available so a
                           forgotten override cannot persist forever.

    For LIFECYCLE / FIRST_COMFORT / FIRST_PROTECTION / FIRST_ANY_DECISION /
    MANUAL, OverridePolicyConfig.safety_timeout_enabled controls whether the
    existing duration_min/night_duration_min fields also apply as a
    defensive maximum — see engines/override_release.py.
    """

    DURATION = "duration"
    FIXED_TIME = "fixed_time"
    LIFECYCLE = "lifecycle"
    FIRST_COMFORT = "first_comfort"
    FIRST_PROTECTION = "first_protection"
    FIRST_ANY_DECISION = "first_any_decision"
    MANUAL = "manual"


# Pre-T10 stored values -> their T10 equivalents (config_entry_data.py's
# _override_policy_from_storage() migration and ManualOverride.from_dict()'s
# restart-persistence backward compat both use this single mapping).
LEGACY_DURATION_MODE_MIGRATION = {
    "legacy": OverrideReleaseStrategy.DURATION.value,
    "fixed_time": OverrideReleaseStrategy.FIXED_TIME.value,
}


@dataclass(frozen=True)
class ManualOverride:
    """A user-initiated override that keeps a window at a manually chosen position.

    Created when OverrideDetector observes that the cover position deviates
    from SmartShading's evaluation target beyond override_detection_tolerance.

    All positions use the integration-internal convention (0 = open, 100 = shaded).

    Fields:
        window_id:            Window this override belongs to.
        override_position:    Position the user moved to (internal convention).
        started_at:           UTC timestamp when the override was first detected.
        expires_at:           UTC timestamp when the override expires
                              (started_at + override_duration_min).
        source:               How the override was detected.  "position_delta"
                              is the only source in this version; "service_call" is
                              reserved for a future explicit override API.
        overridden_state:     The ShadingState SmartShading held before the
                              override — kept for the Learning Engine.
        overridden_position:  The target_position SmartShading would have held
                              (internal convention) — kept for the Learning Engine.
                              None if the previous state had no target (e.g. OPEN).
        scope:                "daytime" or "night" — which duration policy produced
                              this override's expires_at (v1.1.3).  "daytime": a
                              fixed duration from started_at (default 120 min).
                              "night": held until the Morning lifecycle transition;
                              expires_at is a generous safety-net far beyond any
                              real night, not the real release mechanism (see
                              engines/override_detector.py / lifecycle_guard.py).
                              Defaults to "daytime" for entries persisted before
                              this field existed (the pre-v1.1.3 flat duration was
                              closer in spirit to the daytime policy).
        release_strategy:      OverrideReleaseStrategy.value (v1.2.0-beta.1, T10;
                              renamed from T7's "duration_mode") — which policy
                              produced this override's expires_at. Needed for
                              OverrideDetector's renewal and post-expiry re-arm/
                              baseline semantics: DURATION mode's existing,
                              intentionally unchanged "several stale cycles
                              later, a fresh (short) override is expected
                              again" behavior (see
                              tests/test_override_detector.py
                              TestOverrideDetectorTimeoutSuppression) must not
                              be altered, while every other strategy (a much
                              longer or unbounded single-boundary expiry)
                              needs to distinguish "still the same stale
                              deviation" from "a genuine new manual move"
                              after a rare natural (safety-timeout) expiry —
                              see OverrideDetector._maybe_arm_post_expiry_baseline().
                              Defaults to "duration" for entries persisted
                              before this field existed (pre-T7 flat
                              duration, and T7's "legacy" — see
                              LEGACY_DURATION_MODE_MIGRATION).
    """

    window_id: str
    override_position: int
    started_at: datetime
    expires_at: datetime
    source: str
    overridden_state: ShadingState
    overridden_position: int | None
    scope: str = "daytime"
    release_strategy: str = "duration"

    def to_dict(self) -> dict:
        """JSON-safe serialization for restart-safe persistence."""
        return {
            "window_id": self.window_id,
            "override_position": self.override_position,
            "started_at": self.started_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "source": self.source,
            "overridden_state": self.overridden_state.value,
            "overridden_position": self.overridden_position,
            "scope": self.scope,
            "release_strategy": self.release_strategy,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ManualOverride":
        # Backward compat: a persisted entry from before T10 carries the old
        # "duration_mode" key ("legacy"/"fixed_time") instead of the new
        # "release_strategy" key — map it through the same migration table
        # config_entry_data.py uses for the stored policy config.
        _release_strategy = d.get("release_strategy")
        if _release_strategy is None:
            _release_strategy = LEGACY_DURATION_MODE_MIGRATION.get(
                d.get("duration_mode", "legacy"), "duration")
        return cls(
            window_id=d["window_id"],
            override_position=int(d["override_position"]),
            started_at=datetime.fromisoformat(d["started_at"]),
            expires_at=datetime.fromisoformat(d["expires_at"]),
            source=d.get("source", "position_delta"),
            overridden_state=ShadingState(d["overridden_state"]),
            overridden_position=d.get("overridden_position"),
            scope=d.get("scope", "daytime"),
            release_strategy=_release_strategy,
        )
