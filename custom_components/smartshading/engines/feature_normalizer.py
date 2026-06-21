"""FeatureNormalizer — Phase 9F5-2.

Maps the continuous features of a SituationRecord to the [0.0, 1.0] interval
so that the SimilarityCalculator (9F5-3) can compute weighted distances
without unit bias.

Normalization rules:
  All ranges are fixed architecture constants — they never depend on the
  actual data distribution. This guarantees determinism: the same input
  always produces the same output regardless of how many SituationRecords
  exist in the LearningStore.

  Values outside the defined range are clamped rather than rejected:
    below range → 0.0   (floor)
    above range → 1.0   (ceiling)

  None inputs propagate as None outputs (no imputation, no fallback).
  Similarity must degrade gracefully when sensors are missing.

Feature ranges:
  effective_exposure_wm2   :   0 – 1 000 W/m²   → [0.0, 1.0]
  sun_elevation            :   0 –    90 °       → [0.0, 1.0]
  solar_relative_azimuth   : abs(°)  0 –   180 ° → [0.0, 1.0]
      Interpretation after abs():
        0.0  → sun directly in front of window (maximum potential exposure)
        1.0  → sun directly behind window      (minimum / no direct exposure)
      Both +45° and -45° are treated as equal offset from window centre.
  indoor_temp              :  10 –    35 °C      → [0.0, 1.0]
  outdoor_temp             : -20 –    40 °C      → [0.0, 1.0]

Context fields (window_id, decision_timestamp, from_state, decided_state,
decided_by, lifecycle_state, absence_active) are copied without modification.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .situation_joiner import SituationRecord
from ..state_machine.states import ShadingState

# ---------------------------------------------------------------------------
# Fixed normalization ranges (architecture constants — never change at runtime)
# ---------------------------------------------------------------------------

_EXPOSURE_LO: float = 0.0
_EXPOSURE_HI: float = 1000.0

_ELEVATION_LO: float = 0.0
_ELEVATION_HI: float = 90.0

_ABS_AZIMUTH_HI: float = 180.0   # after abs(); floor is always 0

_INDOOR_TEMP_LO: float = 10.0
_INDOOR_TEMP_HI: float = 35.0

_OUTDOOR_TEMP_LO: float = -20.0
_OUTDOOR_TEMP_HI: float = 40.0


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NormalizedSituation:
    """SituationRecord with continuous features mapped to [0.0, 1.0].

    Context fields are copied verbatim.  Normalized feature fields carry
    None when the corresponding sensor was absent — the Similarity Engine
    must handle None by redistributing the missing feature's weight.
    """

    # Context (unmodified from SituationRecord)
    window_id: str
    decision_timestamp: datetime
    from_state: ShadingState | None
    decided_state: ShadingState
    decided_by: str
    lifecycle_state: str
    absence_active: bool

    # Normalized continuous features [0.0, 1.0] or None
    effective_exposure_norm: float | None
    sun_elevation_norm: float | None
    solar_relative_azimuth_norm: float | None
    indoor_temp_norm: float | None
    outdoor_temp_norm: float | None


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _norm(value: float, lo: float, hi: float) -> float:
    """Linearly map *value* from [lo, hi] to [0.0, 1.0] with clamping."""
    return (max(lo, min(hi, value)) - lo) / (hi - lo)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_situation(situation: SituationRecord) -> NormalizedSituation:
    """Normalize all continuous features of *situation* to [0.0, 1.0].

    None fields propagate as None — no imputation or artificial fill.
    All other normalization is purely a function of the input value and the
    fixed architecture constants above.
    """
    raw_exposure = situation.effective_exposure_wm2
    raw_elevation = situation.sun_elevation
    raw_azimuth = situation.solar_relative_azimuth
    raw_indoor = situation.indoor_temp_at_decision
    raw_outdoor = situation.outdoor_temp_c

    return NormalizedSituation(
        window_id=situation.window_id,
        decision_timestamp=situation.decision_timestamp,
        from_state=situation.from_state,
        decided_state=situation.decided_state,
        decided_by=situation.decided_by,
        lifecycle_state=situation.lifecycle_state,
        absence_active=situation.absence_active,

        effective_exposure_norm=(
            _norm(raw_exposure, _EXPOSURE_LO, _EXPOSURE_HI)
            if raw_exposure is not None else None
        ),
        sun_elevation_norm=(
            _norm(raw_elevation, _ELEVATION_LO, _ELEVATION_HI)
            if raw_elevation is not None else None
        ),
        solar_relative_azimuth_norm=(
            # Take absolute value first so ±45° are treated as equal offset.
            # Then clamp to [0, 180°] and normalize.
            _norm(abs(raw_azimuth), 0.0, _ABS_AZIMUTH_HI)
            if raw_azimuth is not None else None
        ),
        indoor_temp_norm=(
            _norm(raw_indoor, _INDOOR_TEMP_LO, _INDOOR_TEMP_HI)
            if raw_indoor is not None else None
        ),
        outdoor_temp_norm=(
            _norm(raw_outdoor, _OUTDOOR_TEMP_LO, _OUTDOOR_TEMP_HI)
            if raw_outdoor is not None else None
        ),
    )


def normalize_situations(situations: list[SituationRecord]) -> list[NormalizedSituation]:
    """Normalize a list of SituationRecords, preserving order."""
    return [normalize_situation(s) for s in situations]
