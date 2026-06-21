"""Assumed-state management for covers without reliable position feedback
(Somfy RTS, ESP Somfy, other RF systems). Implements ARCHITECTURE.md §6.2
exactly: AssumedPositionState, confidence/uncertainty model, restart-during-
travel handling, drift suspicion, and reference-travel (endstop)
calibration.

No Home Assistant dependencies. RestoreEntity-style persistence is
supported via initialize_from_restore()/export_for_restore() - the actual
HA Storage read/write happens in a later integration phase
(ARCHITECTURE.md §16.1: STORAGE_VERSION + migration hook are also deferred
to that phase, this module is the pure logic the storage layer will wrap).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

# Defaults - tunable, not yet validated against real installations (same
# caveat as the floor_level_factor placeholders in TODO.md).
DEFAULT_MAX_SILENCE_DURATION = timedelta(days=7)
DEFAULT_CONFIDENCE_HALF_LIFE = timedelta(days=30)
DEFAULT_RESTART_PENALTY_FACTOR = 0.5
DEFAULT_UNCERTAINTY_INCREMENT_PCT = 2.0
DEFAULT_LOW_CONFIDENCE_THRESHOLD = 0.5  # ARCHITECTURE.md §6.2 "Anwendung in der Decision Engine"

# Confidence Level bands (2026-06-16, Capability Detector round) - display
# labels over the continuous confidence value. Does not change the
# underlying computation in any way - purely a presentation aid, same
# pattern as the observability-cleanup round.
CONFIDENCE_LEVEL_HIGH_THRESHOLD = 0.90
CONFIDENCE_LEVEL_GOOD_THRESHOLD = 0.60
CONFIDENCE_LEVEL_LOW_THRESHOLD = 0.30


def confidence_level(confidence: float) -> str:
    """Human-readable band for a continuous confidence value. Pure
    function, no side effects - never used to gate decisions, only to
    label the existing AssumedPositionState.confidence for display."""
    if confidence >= CONFIDENCE_LEVEL_HIGH_THRESHOLD:
        return "High"
    if confidence >= CONFIDENCE_LEVEL_GOOD_THRESHOLD:
        return "Good"
    if confidence >= CONFIDENCE_LEVEL_LOW_THRESHOLD:
        return "Low"
    return "Unreliable"


@dataclass
class AssumedPositionState:
    """ARCHITECTURE.md §6.2 - exact field set."""

    cover_id: str
    assumed_position: int
    assumed_tilt: int | None
    last_commanded_at: datetime | None
    last_known_good_at: datetime  # last point in time with high certainty (e.g. endstop reached)
    confidence: float  # 0.0-1.0, recomputed on read - see AssumedStateManager.get_state()
    position_uncertainty_pct: float  # monotonically grows per unconfirmed travel, see update()
    is_drift_suspected: bool
    interrupted_travel: bool  # True after a restart was detected mid-travel
    last_commanded_position: int | None = None  # internal convention; set ONLY by update() / on_reference_travel(). Never overwritten by observe().


@dataclass
class AssumedStateManagerConfig:
    max_silence_duration: timedelta = DEFAULT_MAX_SILENCE_DURATION
    confidence_half_life: timedelta = DEFAULT_CONFIDENCE_HALF_LIFE
    restart_penalty_factor: float = DEFAULT_RESTART_PENALTY_FACTOR
    uncertainty_increment_pct: float = DEFAULT_UNCERTAINTY_INCREMENT_PCT


class AssumedStateManager:
    """Per-cover assumed-state tracking. No background tasks: confidence is
    a pure function of stored state and the caller-supplied `now`, never
    of a hidden clock - consistent with the rest of this core (see
    state_machine.guards.StateGuard for the same pattern).
    """

    def __init__(self, config: AssumedStateManagerConfig | None = None) -> None:
        self._config = config or AssumedStateManagerConfig()
        self._records: dict[str, AssumedPositionState] = {}

    # -- RestoreEntity-style persistence (storage I/O lives in a later phase) --

    def initialize_from_restore(self, restored: AssumedPositionState) -> None:
        """Hydrate from a previously persisted AssumedPositionState (HA
        RestoreEntity / Storage, read elsewhere). Use on_restart() right
        after this if the cover may have been mid-travel when HA stopped.
        """
        self._records[restored.cover_id] = replace(restored)

    def export_for_restore(self, cover_id: str) -> AssumedPositionState | None:
        """Snapshot for persistence. Returns a copy, not the live record."""
        record = self._records.get(cover_id)
        return replace(record) if record is not None else None

    # -- Restart-during-travel (ARCHITECTURE.md §6.2) --

    def on_restart(self, cover_id: str, was_traveling: bool, now: datetime) -> None:
        """Call once per cover after a HA restart/reload, after
        initialize_from_restore() has hydrated the stored state.

        If `was_traveling` is True, the cover's true position when HA
        stopped is unknown beyond the last persisted progress snapshot -
        this is "Drift sicher", not just suspected, per §6.2.
        """
        record = self._records.get(cover_id)
        if record is None:
            return
        if was_traveling:
            self._records[cover_id] = replace(
                record,
                interrupted_travel=True,
                last_commanded_at=now,
            )

    # -- Normal command/observation flow --

    def update(
        self,
        cover_id: str,
        position: int,
        commanded_at: datetime,
        has_reliable_position_feedback: bool,
        assumed_tilt: int | None = None,
    ) -> None:
        """Record a new commanded/confirmed position.

        For covers WITH reliable feedback, `position` is ground truth
        (e.g. reported by the device) - confidence is always 1.0 and
        uncertainty stays 0, since drift cannot occur when every position
        is independently confirmed.

        For covers WITHOUT reliable feedback (Somfy RTS etc.), this models
        one more "travel without external confirmation":
        position_uncertainty_pct grows monotonically (§6.2) until the next
        on_reference_travel() calibration.
        """
        record = self._records.get(cover_id)

        if has_reliable_position_feedback:
            self._records[cover_id] = AssumedPositionState(
                cover_id=cover_id,
                assumed_position=position,
                assumed_tilt=assumed_tilt,
                last_commanded_at=commanded_at,
                last_known_good_at=commanded_at,
                confidence=1.0,
                position_uncertainty_pct=0.0,
                is_drift_suspected=False,
                interrupted_travel=False,
                last_commanded_position=position,
            )
            return

        uncertainty = record.position_uncertainty_pct if record is not None else 0.0
        uncertainty += self._config.uncertainty_increment_pct
        last_known_good_at = record.last_known_good_at if record is not None else commanded_at

        self._records[cover_id] = AssumedPositionState(
            cover_id=cover_id,
            assumed_position=position,
            assumed_tilt=assumed_tilt,
            last_commanded_at=commanded_at,
            last_known_good_at=last_known_good_at,
            confidence=0.0,  # placeholder, recomputed in get_state()
            position_uncertainty_pct=uncertainty,
            is_drift_suspected=False,  # recomputed in get_state()
            interrupted_travel=record.interrupted_travel if record is not None else False,
            last_commanded_position=position,
        )

    def record_progress(self, cover_id: str, estimated_position: int, now: datetime) -> None:
        """Lightweight mid-travel update for polling (e.g. from the future
        Coordinator while TravelTracker reports is_moving()). Unlike
        update(), this does NOT increment position_uncertainty_pct -
        uncertainty grows once per commanded travel, not once per poll
        tick.
        """
        record = self._records.get(cover_id)
        if record is None:
            return
        self._records[cover_id] = replace(record, assumed_position=estimated_position)

    def observe(
        self,
        cover_id: str,
        position: int,
        now: datetime,
        has_reliable_position_feedback: bool,
    ) -> None:
        """Passive observation (Capability Detector round, 2026-06-16):
        SmartShading read a position from Home Assistant without having
        commanded anything itself - there is no real cover control yet.

        `position` must be in SmartShading internal convention (0=open,
        100=shaded). The Coordinator converts from HA convention before
        calling this method. This ensures assumed_position is always in
        internal convention, consistent with update().

        Unlike update(), this NEVER increments position_uncertainty_pct,
        regardless of reliability - merely observing a number is not the
        same as SmartShading issuing an unconfirmed command. Unlike
        record_progress(), this bootstraps a record if the cover has
        never been seen before, so it works as the very first read too.

        For reliable covers, an observed reading IS ground truth (whether
        or not SmartShading commanded it) - confidence resets to "fresh"
        just like update() already does for them. For unreliable covers,
        only the displayed position is refreshed; confidence/uncertainty
        are left untouched, since a fundamentally unreliable source being
        observed once is not a calibration event (only on_reference_travel()
        is).

        Reserve update() for once SmartShading itself sends a command
        (later phase) - that is the only case ARCHITECTURE.md §6.2 intends
        position_uncertainty_pct to grow for.
        """
        if has_reliable_position_feedback:
            existing = self._records.get(cover_id)
            if existing is not None:
                # Use replace() to preserve last_commanded_position (and other SmartShading-
                # managed fields). observe() must never overwrite what update() recorded.
                self._records[cover_id] = replace(
                    existing,
                    assumed_position=position,
                    assumed_tilt=None,
                    last_known_good_at=now,
                    confidence=0.0,  # placeholder, recomputed in get_state()
                    position_uncertainty_pct=0.0,
                    is_drift_suspected=False,
                    interrupted_travel=False,
                )
            else:
                self._records[cover_id] = AssumedPositionState(
                    cover_id=cover_id,
                    assumed_position=position,
                    assumed_tilt=None,
                    last_commanded_at=None,
                    last_known_good_at=now,
                    confidence=0.0,  # placeholder, recomputed in get_state()
                    position_uncertainty_pct=0.0,
                    is_drift_suspected=False,
                    interrupted_travel=False,
                )
            return

        record = self._records.get(cover_id)
        if record is None:
            self._records[cover_id] = AssumedPositionState(
                cover_id=cover_id,
                assumed_position=position,
                assumed_tilt=None,
                last_commanded_at=None,
                last_known_good_at=now,
                confidence=0.0,
                position_uncertainty_pct=0.0,
                is_drift_suspected=False,
                interrupted_travel=False,
            )
            return
        self._records[cover_id] = replace(record, assumed_position=position)

    def on_reference_travel(
        self,
        cover_id: str,
        reached_endstop: Literal["min", "max"],
        position_at_endstop: int,
        now: datetime,
    ) -> None:
        """Calibration (ARCHITECTURE.md §6.2): a cover provably reached a
        mechanical endstop (full open/close). Resets confidence to 1.0 and
        uncertainty to 0 - the only way uncertainty decreases for covers
        without real feedback.
        """
        del reached_endstop  # not needed for the reset itself, kept for caller-side logging/clarity
        self._records[cover_id] = AssumedPositionState(
            cover_id=cover_id,
            assumed_position=position_at_endstop,
            assumed_tilt=None,
            last_commanded_at=now,
            last_known_good_at=now,
            confidence=1.0,
            position_uncertainty_pct=0.0,
            is_drift_suspected=False,
            interrupted_travel=False,
            last_commanded_position=position_at_endstop,
        )

    # -- Reads (confidence/drift are recomputed against `now`, not stored) --

    def get_state(self, cover_id: str, now: datetime) -> AssumedPositionState | None:
        """Returns the current assumed state with confidence and
        is_drift_suspected recomputed against `now`. Returns None if this
        cover has never been seen (no update()/initialize_from_restore()
        call yet)."""
        record = self._records.get(cover_id)
        if record is None:
            return None

        confidence = self._compute_confidence(record, now)
        drift_suspected = self._compute_drift_suspected(record, now)
        return replace(record, confidence=confidence, is_drift_suspected=drift_suspected)

    def is_drift_suspected(self, cover_id: str, now: datetime) -> bool:
        state = self.get_state(cover_id, now)
        return state.is_drift_suspected if state is not None else False

    def is_position_trustworthy(self, cover_id: str, now: datetime) -> bool:
        """ARCHITECTURE.md §6.2 "Anwendung in der Decision Engine": below
        this, the Decision Engine should widen position_tolerance instead
        of issuing more frequent corrective commands."""
        state = self.get_state(cover_id, now)
        if state is None:
            return False
        return state.confidence >= DEFAULT_LOW_CONFIDENCE_THRESHOLD

    # -- Internal confidence/drift model (§6.2) --

    def _compute_confidence(self, record: AssumedPositionState, now: datetime) -> float:
        time_decay_factor = self._time_decay_factor(record.last_known_good_at, now)
        restart_penalty = self._config.restart_penalty_factor if record.interrupted_travel else 1.0
        silence_penalty = self._silence_penalty(record, now)
        confidence = 1.0 * time_decay_factor * restart_penalty * silence_penalty
        return max(0.0, min(1.0, confidence))

    def _time_decay_factor(self, last_known_good_at: datetime, now: datetime) -> float:
        age_s = max(0.0, (now - last_known_good_at).total_seconds())
        half_life_s = self._config.confidence_half_life.total_seconds()
        if half_life_s <= 0.0:
            return 1.0
        return math.pow(0.5, age_s / half_life_s)

    def _silence_penalty(self, record: AssumedPositionState, now: datetime) -> float:
        reference = record.last_commanded_at or record.last_known_good_at
        silence_s = max(0.0, (now - reference).total_seconds())
        max_silence_s = self._config.max_silence_duration.total_seconds()
        if max_silence_s <= 0.0 or silence_s <= max_silence_s:
            return 1.0
        overrun_s = silence_s - max_silence_s
        # Linear falloff beyond max_silence_duration, floor at 0.1 so
        # confidence never silently reaches exactly zero from silence alone.
        return max(0.1, 1.0 - (overrun_s / max_silence_s))

    def _compute_drift_suspected(self, record: AssumedPositionState, now: datetime) -> bool:
        if record.interrupted_travel:
            return True  # "Drift sicher" per §6.2
        reference = record.last_commanded_at or record.last_known_good_at
        silence_s = (now - reference).total_seconds()
        if silence_s > self._config.max_silence_duration.total_seconds():
            return True
        return False
