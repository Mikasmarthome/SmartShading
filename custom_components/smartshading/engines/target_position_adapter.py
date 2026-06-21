"""Per-window, per-intensity learned target position adapter — v1.0 Step 6.

Implements confidence-gated position adaptation:

    configured target position
    + learned window-specific delta
    = effective target position

All positions in this module use HA convention: 0 = closed, 100 = open.
No Home Assistant imports.  Pure Python, safe to test without a running
Home Assistant instance.

Design
------
Configured positions (light / normal / strong shade) are the starting point
and remain the fallback at any time.  As the user manually adjusts a cover
after SmartShading has set a shade position, the adapter accumulates weighted
observations of the user's preferred positions.  When enough consistent
observations exist, the confidence gate opens and the effective target starts
moving toward the learned average.

Confidence gating prevents premature or noisy adaptation:

    very_low  (< 0.20 strength): 0 %  — configured positions only
    low       (< 0.40 strength): ±5 %  maximum delta from configured
    medium    (< 0.60 strength): ±15 % maximum delta
    high      (< 0.80 strength): ±25 % maximum delta
    very_high (≤ 1.00 strength): ±40 % maximum delta (clipped to [0, 100])

These gates are intentionally NOT permanently capped at ±15 %.  A very_high
confidence window with many consistent user observations can freely shift the
effective target up to ±40 percentage points from the configured base.

Learning signals
----------------
Only "expired" and "cleared_by_lifecycle" override events are used as
signals.  A signal is weighted by how long the user kept their position:

    ≥ 2 h   : weight 2.0  (very strong preference)
    ≥ 30 min : weight 1.5  (strong preference)
    ≥ 10 min : weight 1.0  (clear preference)
    ≥ 5 min  : weight 0.5  (possible preference)
    < 5 min  : weight 0.0  (ignored — likely accidental)

A minimum accumulated weight of 3.0 is required before any adaptation kicks
in (roughly 2–3 consistent strong-preference signals).

Stability
---------
Effective positions are rounded to the nearest 5 % step.  A position change
of < 5 % (before rounding) is treated as no adaptation, preventing
micro-oscillation.  The existing StateGuard and command throttle are NOT
bypassed — this module only adjusts the target that goes into the decision
pipeline; all execution guards remain in place.

Excluded decisions
------------------
Signals are only accepted when the overridden state is a configurable shade
intensity (light_shade / normal_shade / strong_shade).  Safety, night,
absence, storm, and wind decisions are never used as learning signals.

Storage format (within learning persistence key smartshading_learning_{id})
---------------------------------------------------------------------------
    "target_adaptations": {
        "<window_id>": {
            "light":  {"weighted_sum_ha": float, "total_weight": float,
                       "sample_count": int, "last_updated": "<ISO-8601>"},
            "normal": { ... },
            "strong": { ... }
        }
    }
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..cover_control.position_semantics import to_ha_position

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

_MAX_DELTA_HA_BY_CONFIDENCE: dict[str, int] = {
    "very_low": 0,
    "low": 5,
    "medium": 15,
    "high": 25,
    "very_high": 40,
}

# Minimum accumulated weight before adaptation is applied.
# At weight 1.0 per signal, this means ~3 consistent strong-preference signals.
_MIN_WEIGHT_FOR_ADAPTATION: float = 3.0

# Effective positions are rounded to the nearest step (percentage points).
_POSITION_STEP_HA: int = 5

# Minimum override duration before a signal is recorded.
_MIN_OVERRIDE_DURATION_MIN: float = 5.0

# States that map to configurable shade intensity levels (ShadingState.value strings).
_STATE_TO_INTENSITY: dict[str, str] = {
    "light_shade": "light",
    "normal_shade": "normal",
    "strong_shade": "strong",
}


def _compute_signal_weight(duration_min: float) -> float:
    """Map override duration to a signal weight.

    Longer durations indicate stronger user preference — the user kept the
    cover at their chosen position for a significant time.
    """
    if duration_min >= 120:
        return 2.0
    if duration_min >= 30:
        return 1.5
    if duration_min >= 10:
        return 1.0
    if duration_min >= _MIN_OVERRIDE_DURATION_MIN:
        return 0.5
    return 0.0


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ShadeIntensityAdaptation:
    """Accumulated weighted signal for one shade intensity level.

    Positions are stored in HA convention: 0 = closed, 100 = open.
    The learned average is a weighted mean of observed user preference
    positions, not a simple average.
    """

    weighted_sum_ha: float = 0.0
    total_weight: float = 0.0
    sample_count: int = 0
    last_updated: datetime | None = None

    @property
    def has_enough_data(self) -> bool:
        """True when accumulated weight meets the minimum threshold."""
        return self.total_weight >= _MIN_WEIGHT_FOR_ADAPTATION

    @property
    def learned_avg_ha(self) -> float | None:
        """Weighted average of observed user positions, or None if no data."""
        if self.total_weight == 0:
            return None
        return self.weighted_sum_ha / self.total_weight

    def record_signal(self, position_ha: float, weight: float, now: datetime) -> None:
        """Accumulate one new preference observation."""
        self.weighted_sum_ha += position_ha * weight
        self.total_weight += weight
        self.sample_count += 1
        self.last_updated = now


@dataclass
class WindowTargetAdaptation:
    """Per-window target position adaptation state covering all shade levels."""

    window_id: str
    light: ShadeIntensityAdaptation = field(default_factory=ShadeIntensityAdaptation)
    normal: ShadeIntensityAdaptation = field(default_factory=ShadeIntensityAdaptation)
    strong: ShadeIntensityAdaptation = field(default_factory=ShadeIntensityAdaptation)

    def get_intensity(self, intensity: str) -> ShadeIntensityAdaptation:
        if intensity == "light":
            return self.light
        if intensity == "normal":
            return self.normal
        if intensity == "strong":
            return self.strong
        raise ValueError(f"Unknown intensity: {intensity!r}")


# ---------------------------------------------------------------------------
# Single-position computation
# ---------------------------------------------------------------------------

def compute_effective_target_ha(
    configured_ha: int,
    adaptation: ShadeIntensityAdaptation | None,
    confidence_level: str,
) -> tuple[int, bool]:
    """Compute effective target position for one shade intensity level.

    Parameters
    ----------
    configured_ha:
        Configured base target (HA convention: 0 = closed, 100 = open).
    adaptation:
        Accumulated learning state, or None when no data has been collected.
    confidence_level:
        AdaptiveProfile confidence level string.

    Returns
    -------
    (effective_position_ha, adaptation_active)
        effective_position_ha is the (possibly adapted) target in HA convention.
        adaptation_active is True when the configured value was actually changed.
    """
    max_delta = _MAX_DELTA_HA_BY_CONFIDENCE.get(confidence_level, 0)
    if max_delta == 0:
        return configured_ha, False

    if adaptation is None or not adaptation.has_enough_data:
        return configured_ha, False

    learned_avg = adaptation.learned_avg_ha
    if learned_avg is None:
        return configured_ha, False

    raw_delta = learned_avg - configured_ha
    clamped_delta = max(-max_delta, min(max_delta, raw_delta))
    effective_raw = configured_ha + clamped_delta

    # Round to the nearest step; final clip keeps the result in [0, 100].
    effective = max(0, min(100, round(effective_raw / _POSITION_STEP_HA) * _POSITION_STEP_HA))
    adapted = abs(effective - configured_ha) >= _POSITION_STEP_HA
    return effective, adapted


# ---------------------------------------------------------------------------
# TargetPositionAdapter
# ---------------------------------------------------------------------------

class TargetPositionAdapter:
    """Stateful per-window target position adapter.

    Maintained in-memory by SmartShadingCoordinator.  Persisted as the
    ``target_adaptations`` section within the per-entry learning store
    (``smartshading_learning_{entry_id}``).
    """

    def __init__(self) -> None:
        self._windows: dict[str, WindowTargetAdaptation] = {}

    # ------------------------------------------------------------------
    # Signal ingestion
    # ------------------------------------------------------------------

    def record_override_signal(
        self,
        *,
        window_id: str,
        overridden_state_str: str,
        override_position_internal: int,
        overridden_position_internal: int | None,
        duration_min: float,
        now: datetime,
    ) -> None:
        """Record a learning signal from a completed manual override.

        Only "expired" and "cleared_by_lifecycle" events should be passed
        here — these represent positions the user chose and maintained.

        Parameters
        ----------
        overridden_state_str:
            ShadingState value string (e.g. ``"normal_shade"``) that was
            active when the user started the override.
        override_position_internal:
            Position the user moved to in INTERNAL convention (0 = open,
            100 = shaded).
        overridden_position_internal:
            SmartShading's target position in INTERNAL convention, or None
            when unknown.
        duration_min:
            How long the user kept the override position.
        now:
            UTC timestamp.
        """
        intensity = _STATE_TO_INTENSITY.get(overridden_state_str)
        if intensity is None:
            return  # Not a shade-intensity state — safety/night/absence ignored

        weight = _compute_signal_weight(duration_min)
        if weight == 0.0:
            return  # Duration too short to be meaningful

        # Convert to standard HA convention (invert=False) for storage.
        # Configured base positions are in standard HA convention, so the
        # delta comparison is always in the same coordinate system.
        user_pos_ha = to_ha_position(override_position_internal, invert=False)

        # Skip signal when user's position matches SmartShading's target
        # (within rounding tolerance) — no adaptation information to extract.
        if overridden_position_internal is not None:
            ss_pos_ha = to_ha_position(overridden_position_internal, invert=False)
            if abs(user_pos_ha - ss_pos_ha) < _POSITION_STEP_HA:
                return

        window_adaptation = self._windows.setdefault(
            window_id, WindowTargetAdaptation(window_id=window_id)
        )
        window_adaptation.get_intensity(intensity).record_signal(
            float(user_pos_ha), weight, now
        )

    # ------------------------------------------------------------------
    # Effective target computation
    # ------------------------------------------------------------------

    def get_effective_targets(
        self,
        *,
        window_id: str,
        light_ha: int,
        normal_ha: int,
        strong_ha: int,
        confidence_level: str,
    ) -> tuple[int, int, int, bool]:
        """Return effective (possibly adapted) target positions.

        Parameters
        ----------
        window_id:
            Window whose adaptation state to look up.
        light_ha, normal_ha, strong_ha:
            Configured base targets in HA convention.
        confidence_level:
            Current AdaptiveProfile confidence string.

        Returns
        -------
        (light_eff, normal_eff, strong_eff, any_adapted)
            All values in HA convention.
            ``any_adapted`` is True when at least one level differs from
            the configured base.
        """
        adaptation = self._windows.get(window_id)

        light_eff, la = compute_effective_target_ha(
            light_ha,
            adaptation.light if adaptation else None,
            confidence_level,
        )
        normal_eff, na = compute_effective_target_ha(
            normal_ha,
            adaptation.normal if adaptation else None,
            confidence_level,
        )
        strong_eff, sa = compute_effective_target_ha(
            strong_ha,
            adaptation.strong if adaptation else None,
            confidence_level,
        )

        return light_eff, normal_eff, strong_eff, any((la, na, sa))

    def get_adaptation_diagnostics(
        self,
        window_id: str,
        confidence_level: str,
        *,
        light_configured_ha: int | None = None,
        normal_configured_ha: int | None = None,
        strong_configured_ha: int | None = None,
    ) -> dict[str, Any]:
        """Return privacy-safe diagnostics for the recommendation sensor.

        All values use HA convention.  No raw positions from individual
        override records are exposed.
        """
        adaptation = self._windows.get(window_id)
        max_delta = _MAX_DELTA_HA_BY_CONFIDENCE.get(confidence_level, 0)

        if adaptation is None:
            return {
                "target_adaptation_active": False,
                "target_adaptation_confidence": confidence_level,
            }

        any_active = max_delta > 0 and any(
            a.has_enough_data
            for a in (adaptation.light, adaptation.normal, adaptation.strong)
        )

        result: dict[str, Any] = {
            "target_adaptation_active": any_active,
            "target_adaptation_confidence": confidence_level,
        }

        # Include learned effective positions when configured bases are provided.
        if light_configured_ha is not None:
            light_eff, la = compute_effective_target_ha(
                light_configured_ha, adaptation.light, confidence_level
            )
            if la:
                result["learned_light_target_ha"] = light_eff

        if normal_configured_ha is not None:
            normal_eff, na = compute_effective_target_ha(
                normal_configured_ha, adaptation.normal, confidence_level
            )
            if na:
                result["learned_normal_target_ha"] = normal_eff

        if strong_configured_ha is not None:
            strong_eff, sa = compute_effective_target_ha(
                strong_configured_ha, adaptation.strong, confidence_level
            )
            if sa:
                result["learned_strong_target_ha"] = strong_eff

        return result

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------

    def remove_window(self, window_id: str) -> None:
        """Remove adaptation state for a deleted window."""
        self._windows.pop(window_id, None)

    def get_window_ids(self) -> set[str]:
        """All window IDs that have accumulated any adaptation state."""
        return set(self._windows.keys())

    # ------------------------------------------------------------------
    # Serialization / deserialization
    # ------------------------------------------------------------------

    def to_storage_dict(self) -> dict:
        """Serialize to a JSON-serializable dict.

        Intended for embedding as the ``target_adaptations`` key in the
        learning persistence format.
        """
        out: dict[str, dict] = {}
        for window_id, wa in self._windows.items():
            out[window_id] = {
                "light": _serialize_intensity(wa.light),
                "normal": _serialize_intensity(wa.normal),
                "strong": _serialize_intensity(wa.strong),
            }
        return out

    @classmethod
    def from_storage_dict(cls, data: Any) -> "TargetPositionAdapter":
        """Deserialize from storage.  Never raises — bad data → empty adapter."""
        adapter = cls()
        if not isinstance(data, dict):
            return adapter
        for window_id, window_data in data.items():
            if not isinstance(window_data, dict):
                continue
            try:
                wa = WindowTargetAdaptation(window_id=str(window_id))
                wa.light = _deserialize_intensity(window_data.get("light"))
                wa.normal = _deserialize_intensity(window_data.get("normal"))
                wa.strong = _deserialize_intensity(window_data.get("strong"))
                adapter._windows[str(window_id)] = wa
            except Exception:
                pass  # Corrupt window entry — skip silently
        return adapter


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _serialize_intensity(a: ShadeIntensityAdaptation) -> dict:
    return {
        "weighted_sum_ha": a.weighted_sum_ha,
        "total_weight": a.total_weight,
        "sample_count": a.sample_count,
        "last_updated": a.last_updated.isoformat() if a.last_updated else None,
    }


def _deserialize_intensity(data: Any) -> ShadeIntensityAdaptation:
    if not isinstance(data, dict):
        return ShadeIntensityAdaptation()
    try:
        last_updated = None
        raw_ts = data.get("last_updated")
        if isinstance(raw_ts, str):
            from datetime import timezone
            dt = datetime.fromisoformat(raw_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            last_updated = dt
        return ShadeIntensityAdaptation(
            weighted_sum_ha=float(data.get("weighted_sum_ha", 0.0)),
            total_weight=float(data.get("total_weight", 0.0)),
            sample_count=int(data.get("sample_count", 0)),
            last_updated=last_updated,
        )
    except Exception:
        return ShadeIntensityAdaptation()


# ---------------------------------------------------------------------------
# Export summary (used by zone_learning_export.py)
# ---------------------------------------------------------------------------

def build_target_adaptation_export_summary(adapter: "TargetPositionAdapter | None") -> dict:
    """Build a privacy-safe aggregate summary for the global learning export.

    No raw positions, no window identifiers, no individual timestamps.
    Returns only aggregate counts and confidence distribution.
    """
    if adapter is None:
        return {"available": False}

    windows_with_data = 0
    adapted_intensity_levels = 0
    confidence_distribution: dict[str, int] = {}

    for window_id in adapter.get_window_ids():
        window_data = adapter._windows[window_id]
        window_has_data = False
        for intensity_name in ("light", "normal", "strong"):
            a = window_data.get_intensity(intensity_name)
            if a.sample_count > 0:
                adapted_intensity_levels += 1
                window_has_data = True
                # Approximate confidence level from accumulated weight.
                if a.total_weight >= 25:
                    conf = "very_high"
                elif a.total_weight >= 15:
                    conf = "high"
                elif a.total_weight >= 8:
                    conf = "medium"
                elif a.total_weight >= _MIN_WEIGHT_FOR_ADAPTATION:
                    conf = "low"
                else:
                    conf = "very_low"
                confidence_distribution[conf] = confidence_distribution.get(conf, 0) + 1
        if window_has_data:
            windows_with_data += 1

    return {
        "available": True,
        "windows_with_target_adaptation": windows_with_data,
        "adapted_intensity_levels": adapted_intensity_levels,
        "confidence_distribution": confidence_distribution,
    }
