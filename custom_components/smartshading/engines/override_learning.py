"""Override Learning Engine — Phase 9F7.

Answers: is there a consistent, data-backed pattern of user corrections that
SmartShading should learn from?

Input:
  SimilarityResult   — situational neighbourhood signal (9F5-6)
  ConfidenceResult   — data-richness gate (9F6)
  override_signal_strength — pre-computed from DecisionOutcome delay
  global_override_rate     — total overrides / total resolved (per window)
  decided_by               — which evaluator produced the decision

Output:
  OverrideLearningResult — learning score, level, and all intermediate signals

---

Architecture
------------

Signal chain:

  DecisionOutcome.override_delay_min
        ↓  compute_override_signal_strength()
  override_signal_strength            (0.0 – 1.0)

  SimilarityResult.override_rate      → situational override rate
  SimilarityResult.agreement_rate     → directional consistency
  SimilarityResult.similar_count      → sample gate

  ConfidenceResult.level              → confidence gate

        ↓  compute_override_learning()
  OverrideLearningResult

---

Gate order (all must pass for a learning signal)
-------------------------------------------------

  1. Confidence gate   — level must be >= MEDIUM (enough data overall)
  2. Sample gate       — similar_count >= 5 (enough local neighbours)
  3. Consistency gate  — agreement_rate >= 0.60 (corrections go the same way)
  4. Override-rate gate — situational override_rate >= 0.50
                          (majority of similar situations were overridden)
  5. Signal-strength gate — override_signal_strength must not be None

Any gate failure → learning_level = NONE, learning_score = None.

---

Learning score formula (when all gates pass)
--------------------------------------------

  learning_score = situational_override_rate
                 × override_signal_strength
                 × agreement_rate

  All three factors are in [0.0, 1.0], so learning_score ∈ [0.0, 1.0].
  A product is used so that a weak signal in any dimension suppresses the
  overall score — consistent with the multiplicative confidence model in 9F6.

---

Learning level classification
------------------------------

  NONE      gate not passed
  WEAK      [0.00, 0.30)
  MODERATE  [0.30, 0.60)
  STRONG    [0.60, 1.00]

---

Override-rate gate justification (>= 0.50)
-------------------------------------------

  A situational_override_rate < 0.50 means the majority of similar
  historical situations were NOT overridden.  The pattern is ambiguous —
  more often accepted than rejected.  Treating this as a learning signal
  would generate noise.  Below 0.50 the result is NONE (not WEAK) because
  no actionable conclusion can be drawn.

---

Tier safety
-----------

This module is purely observational.  It produces an OverrideLearningResult
that describes the detected pattern.  No threshold is modified here.  No
Coordinator state is touched.  No Tier 1–5 logic is affected.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .confidence_engine import ConfidenceLevel, ConfidenceResult
from .outcome_aggregation import SimilarityResult


# ---------------------------------------------------------------------------
# Learning level classification
# ---------------------------------------------------------------------------

class OverrideLearningLevel(Enum):
    """Discrete classification of the override learning score.

    NONE      — at least one gate did not pass; no learning signal.
    WEAK      — [0.00, 0.30)
    MODERATE  — [0.30, 0.60)
    STRONG    — [0.60, 1.00]
    """

    NONE     = "none"
    WEAK     = "weak"
    MODERATE = "moderate"
    STRONG   = "strong"


# ---------------------------------------------------------------------------
# Architecture constants
# ---------------------------------------------------------------------------

# ConfidenceLevels that allow the confidence gate to open.
_GATE_OPEN_LEVELS: frozenset[ConfidenceLevel] = frozenset(
    {ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH, ConfidenceLevel.VERY_HIGH}
)

_MIN_SAMPLE_COUNT:    int   = 5     # minimum similar_count to produce a signal
_MIN_AGREEMENT_RATE:  float = 0.60  # directional consistency threshold
_MIN_OVERRIDE_RATE:   float = 0.50  # situational override rate threshold


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OverrideLearningInput:
    """Input to compute_override_learning().

    similarity_result       — from SimilarityPipeline (9F5-6)
    confidence_result       — from ConfidenceEngine (9F6)
    override_signal_strength — delay-weighted signal [0.0, 1.0] | None;
                               computed by compute_override_signal_strength()
    global_override_rate    — total overrides / total resolved for this window;
                               None when insufficient data
    decided_by              — evaluator that produced the decision (pattern dim.)
    """

    similarity_result:        SimilarityResult
    confidence_result:        ConfidenceResult
    override_signal_strength: float | None
    global_override_rate:     float | None
    decided_by:               str | None


@dataclass(frozen=True)
class OverrideLearningResult:
    """Output of compute_override_learning().

    learning_score            — product signal in [0.0, 1.0] | None (if no signal)
    learning_level            — discrete classification
    situational_override_rate — SimilarityResult.override_rate (pass-through)
    global_override_rate      — pass-through from input
    override_signal_strength  — pass-through from input
    confidence_gate_passed    — whether ConfidenceLevel >= MEDIUM
    directional_consistent    — whether agreement_rate >= 0.60 (False if None)
    pattern_dimension         — decided_by (evaluator name or None)
    """

    learning_score:            float | None
    learning_level:            OverrideLearningLevel
    situational_override_rate: float | None
    global_override_rate:      float | None
    override_signal_strength:  float | None
    confidence_gate_passed:    bool
    directional_consistent:    bool | None
    pattern_dimension:         str | None


# ---------------------------------------------------------------------------
# Override signal strength
# ---------------------------------------------------------------------------

def compute_override_signal_strength(
    *,
    override_occurred: bool,
    override_delay_min: float | None,
    override_event_type: str | None,  # accepted for API completeness; reserved for future patterns
) -> float:
    """Map a DecisionOutcome override event to a signal strength in [0.0, 1.0].

    Rules:
      override_occurred = False       → 0.00  (no correction at all)
      override_occurred = True:
        delay < 5 min                 → 1.00  (immediate rejection; clearly wrong)
        delay < 30 min                → 0.75  (quick correction; likely wrong)
        delay < 120 min               → 0.40  (moderate delay; possibly situational)
        delay >= 120 min              → 0.10  (long delay; weak signal at best)
        delay = None (unknown)        → 0.50  (override happened, timing unknown)

    override_event_type is accepted but not yet used in signal computation.
    It is reserved for future pattern extensions (e.g. distinguishing a
    MANUAL_OPEN from a SCENE_TRIGGERED correction).
    """
    if not override_occurred:
        return 0.0

    if override_delay_min is None:
        return 0.50

    if override_delay_min < 5:
        return 1.00
    if override_delay_min < 30:
        return 0.75
    if override_delay_min < 120:
        return 0.40
    return 0.10


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _classify(score: float) -> OverrideLearningLevel:
    """Map a learning score to an OverrideLearningLevel."""
    if score < 0.30:
        return OverrideLearningLevel.WEAK
    if score < 0.60:
        return OverrideLearningLevel.MODERATE
    return OverrideLearningLevel.STRONG


def _none_result(
    inp: OverrideLearningInput,
    *,
    confidence_gate_passed: bool,
    directional_consistent: bool | None,
) -> OverrideLearningResult:
    """Return a NONE-level result when any gate did not pass."""
    return OverrideLearningResult(
        learning_score=None,
        learning_level=OverrideLearningLevel.NONE,
        situational_override_rate=inp.similarity_result.override_rate,
        global_override_rate=inp.global_override_rate,
        override_signal_strength=inp.override_signal_strength,
        confidence_gate_passed=confidence_gate_passed,
        directional_consistent=directional_consistent,
        pattern_dimension=inp.decided_by,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_override_learning(inp: OverrideLearningInput) -> OverrideLearningResult:
    """Compute an OverrideLearningResult from an OverrideLearningInput.

    Gate order:
      1. Confidence gate  (ConfidenceLevel >= MEDIUM)
      2. Sample gate      (similar_count >= 5)
      3. Consistency gate (agreement_rate >= 0.60)
      4. Override-rate gate (situational_override_rate >= 0.50)
      5. Signal-strength gate (override_signal_strength is not None)

    All gates must pass for a learning score to be computed.
    Any gate failure → learning_level = NONE, learning_score = None.

    Pure function — no state, no randomness, no I/O.
    """
    result     = inp.similarity_result
    confidence = inp.confidence_result

    # ------------------------------------------------------------------
    # Pre-compute derived values (needed in all returned results)
    # ------------------------------------------------------------------
    confidence_gate_passed: bool = confidence.level in _GATE_OPEN_LEVELS

    agreement_rate = result.agreement_rate
    if agreement_rate is None:
        directional_consistent: bool | None = False
    else:
        directional_consistent = agreement_rate >= _MIN_AGREEMENT_RATE

    # ------------------------------------------------------------------
    # Gate 1 — Confidence
    # ------------------------------------------------------------------
    if not confidence_gate_passed:
        return _none_result(
            inp,
            confidence_gate_passed=False,
            directional_consistent=directional_consistent,
        )

    # ------------------------------------------------------------------
    # Gate 2 — Sample count
    # ------------------------------------------------------------------
    if result.similar_count < _MIN_SAMPLE_COUNT:
        return _none_result(
            inp,
            confidence_gate_passed=True,
            directional_consistent=directional_consistent,
        )

    # ------------------------------------------------------------------
    # Gate 3 — Directional consistency
    # ------------------------------------------------------------------
    if not directional_consistent:
        return _none_result(
            inp,
            confidence_gate_passed=True,
            directional_consistent=directional_consistent,
        )

    # ------------------------------------------------------------------
    # Gate 4 — Override rate
    # ------------------------------------------------------------------
    situational_override_rate = result.override_rate
    if situational_override_rate is None or situational_override_rate < _MIN_OVERRIDE_RATE:
        return _none_result(
            inp,
            confidence_gate_passed=True,
            directional_consistent=True,
        )

    # ------------------------------------------------------------------
    # Gate 5 — Signal strength
    # ------------------------------------------------------------------
    override_signal_strength = inp.override_signal_strength
    if override_signal_strength is None:
        return _none_result(
            inp,
            confidence_gate_passed=True,
            directional_consistent=True,
        )

    # ------------------------------------------------------------------
    # Learning score
    # agreement_rate is not None here (directional_consistent = True implies it)
    # ------------------------------------------------------------------
    score = situational_override_rate * override_signal_strength * agreement_rate  # type: ignore[operator]
    score = max(0.0, min(1.0, score))

    return OverrideLearningResult(
        learning_score=score,
        learning_level=_classify(score),
        situational_override_rate=situational_override_rate,
        global_override_rate=inp.global_override_rate,
        override_signal_strength=override_signal_strength,
        confidence_gate_passed=True,
        directional_consistent=True,
        pattern_dimension=inp.decided_by,
    )
