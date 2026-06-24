"""Strategy runtime application — LE 2.0 / Phase P9B Live Authority Completion.

Pure decision helpers that turn bounded strategy deltas (timing / exit-threshold /
hysteresis / tier-choice / minimum-hold) into a REAL effect on the live decision,
applied by the coordinator AFTER the deterministic tier decision and BEFORE the
execution pipeline.

Design rules:
  - Every helper is a NO-OP when its delta is 0 / absent → zero behaviour change
    without an active strategy adoption/experiment (preserves the deterministic
    baseline for all windows).
  - Current measured exposure stays authoritative; forecast only provides a lead
    for *earlier* entry/exit and only at high trust.
  - Higher authorities (Safety / Lifecycle / Manual Override / Behavior Mode)
    are resolved by the caller AFTER these helpers and always win.
  - No Home Assistant import.

HA convention: 0 = closed, 100 = open.  More shading = lower position / higher tier.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..state_machine.states import ShadingState

# Native hysteresis unit: W/m² gap below the entry threshold per bounded step.
HYSTERESIS_STEP_WM2: float = 40.0

# Ordered shade tiers (weak → strong) for TIER_CHOICE shifting.
_TIER_SEQUENCE: tuple[ShadingState, ...] = (
    ShadingState.LIGHT_SHADE, ShadingState.NORMAL_SHADE, ShadingState.STRONG_SHADE,
)
_SHADE_STATES: frozenset[ShadingState] = frozenset(_TIER_SEQUENCE)


# ---------------------------------------------------------------------------
# EXIT_THRESHOLD + HYSTERESIS  (value-based de-escalation guard)
# ---------------------------------------------------------------------------

def effective_exit_threshold(
    entry_threshold_wm2: float, *, hysteresis_steps: float = 0.0,
    exit_threshold_delta_wm2: float = 0.0,
) -> float:
    """Exit (de-escalation) threshold = entry − hysteresis_gap + exit_delta.

    hysteresis_steps × HYSTERESIS_STEP_WM2 widens the entry→exit gap; the
    exit_threshold_delta shifts the exit boundary absolutely.  Composed once.
    With both 0 the exit threshold equals the entry threshold (no hysteresis →
    identical to the stateless baseline)."""
    gap = abs(hysteresis_steps) * HYSTERESIS_STEP_WM2
    return entry_threshold_wm2 - gap + exit_threshold_delta_wm2


def apply_deescalation_hysteresis(
    *, current_state: ShadingState, proposed_state: ShadingState,
    exposure_wm2: float | None, current_tier_exit_threshold_wm2: float | None,
) -> tuple[ShadingState, bool]:
    """Hold the current shade tier while exposure stays above its exit threshold.

    Only acts on DE-ESCALATIONS (proposed weaker than current).  Returns
    (effective_state, held).  No-op when not in a shade tier, when escalating, or
    when no exit threshold is supplied (gap 0)."""
    if current_state not in _SHADE_STATES or current_tier_exit_threshold_wm2 is None:
        return (proposed_state, False)
    if exposure_wm2 is None:
        return (proposed_state, False)
    cur_rank = _TIER_SEQUENCE.index(current_state)
    prop_rank = _TIER_SEQUENCE.index(proposed_state) if proposed_state in _SHADE_STATES else -1
    if prop_rank >= cur_rank:
        return (proposed_state, False)  # escalation or same → no hysteresis hold
    if exposure_wm2 >= current_tier_exit_threshold_wm2:
        return (current_state, True)    # still above exit threshold → hold
    return (proposed_state, False)


# ---------------------------------------------------------------------------
# TIER_CHOICE  (shift the proposed tier by a bounded ±1, valid tiers only)
# ---------------------------------------------------------------------------

def apply_tier_choice(
    proposed_state: ShadingState, *, tier_delta: int,
) -> tuple[ShadingState, bool]:
    """Shift the proposed shade tier by tier_delta (∈ {-1,0,+1}); OPEN↔LIGHT
    boundary handled.  Never sets an arbitrary position — only selects a valid
    tier (OPEN/LIGHT/NORMAL/STRONG).  Returns (effective_state, changed)."""
    if tier_delta == 0:
        return (proposed_state, False)
    open_state = ShadingState.OPEN
    seq = (open_state,) + _TIER_SEQUENCE  # OPEN, LIGHT, NORMAL, STRONG
    if proposed_state not in seq:
        return (proposed_state, False)
    idx = seq.index(proposed_state)
    new_idx = max(0, min(len(seq) - 1, idx + tier_delta))
    new_state = seq[new_idx]
    return (new_state, new_state != proposed_state)


# ---------------------------------------------------------------------------
# ENTRY / EXIT timing  (bounded transition-time gate; per-window state)
# ---------------------------------------------------------------------------

@dataclass
class TimingState:
    """Mutable per-window timing tracker (coordinator-owned)."""
    entry_candidate_since: datetime | None = None
    exit_candidate_since: datetime | None = None


def apply_entry_timing(
    *, current_state: ShadingState, proposed_state: ShadingState, now: datetime,
    state: TimingState, delta_min: float, forecast_lead_minutes: float | None,
) -> tuple[ShadingState, bool]:
    """Gate OPEN→shade entry by a bounded ±delta minutes.

    delta_min > 0 → enter LATER: suppress entry until exposure has proposed shade
                    for delta_min minutes (held OPEN meanwhile).
    delta_min < 0 → enter EARLIER: only when a trusted forecast lead predicts the
                    crossing within |delta_min| minutes (current measurement still
                    authoritative; never fabricates entry without forecast).
    Returns (effective_state, changed)."""
    entering = current_state == ShadingState.OPEN and proposed_state in _SHADE_STATES
    waiting = current_state == ShadingState.OPEN and proposed_state == ShadingState.OPEN
    if delta_min > 0:
        if entering:
            if state.entry_candidate_since is None:
                state.entry_candidate_since = now
            elapsed = (now - state.entry_candidate_since).total_seconds() / 60.0
            if elapsed < delta_min:
                return (ShadingState.OPEN, True)   # hold OPEN (later entry)
            return (proposed_state, False)
        state.entry_candidate_since = None
        return (proposed_state, False)
    if delta_min < 0 and waiting:
        # earlier entry only with a trusted forecast lead within |delta_min|.
        if forecast_lead_minutes is not None and forecast_lead_minutes <= abs(delta_min):
            return (ShadingState.LIGHT_SHADE, True)
        return (proposed_state, False)
    if entering:
        state.entry_candidate_since = None
    return (proposed_state, False)


def apply_exit_timing(
    *, current_state: ShadingState, proposed_state: ShadingState, now: datetime,
    state: TimingState, delta_min: float,
) -> tuple[ShadingState, bool]:
    """Gate shade→weaker/OPEN release by a bounded ±delta minutes.

    delta_min > 0 → release LATER: hold the current tier until it has been a
                    de-escalation candidate for delta_min minutes.
    delta_min < 0 → release EARLIER: a de-escalation already proposed is allowed
                    immediately (no extra hold); the bounded earlier-release is a
                    no-op beyond passing the proposal through (StateGuard/min-hold
                    still apply downstream).
    Returns (effective_state, changed)."""
    deescalating = (current_state in _SHADE_STATES
                    and (proposed_state == ShadingState.OPEN
                         or (proposed_state in _SHADE_STATES
                             and _TIER_SEQUENCE.index(proposed_state)
                             < _TIER_SEQUENCE.index(current_state))))
    if delta_min > 0:
        if deescalating:
            if state.exit_candidate_since is None:
                state.exit_candidate_since = now
            elapsed = (now - state.exit_candidate_since).total_seconds() / 60.0
            if elapsed < delta_min:
                return (current_state, True)   # hold current tier (later release)
            return (proposed_state, False)
        state.exit_candidate_since = None
        return (proposed_state, False)
    if not deescalating:
        state.exit_candidate_since = None
    return (proposed_state, False)


# ---------------------------------------------------------------------------
# MINIMUM_HOLD  (bounded delta on StateGuard minimum_state_duration)
# ---------------------------------------------------------------------------

def effective_min_hold_minutes(
    base_minutes: float, *, delta_min: float, safe_floor_minutes: float,
) -> float:
    """Effective minimum hold = base + delta, never below the safe absolute floor."""
    return max(safe_floor_minutes, base_minutes + delta_min)
