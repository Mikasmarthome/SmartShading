"""ForecastLearningStore — Phase 9F12i.

Pure Python in-memory store for all Forecast Learning raw data.
No HA dependencies.  No I/O.  No scheduling.

Data held
---------
  forecast_snapshots   dict[forecast_snapshot_id → ForecastSnapshot]
  reality_snapshots    dict[reality_snapshot_id  → RealitySnapshot]
  forecast_records     dict[forecast_snapshot_id → ForecastRecord]

Not stored (computed on demand):
  ForecastTrustResult, ForecastTrustSummary

Serialization contract
----------------------
  datetime  → ISO 8601 string with explicit UTC offset (+00:00)
              re-parsed with datetime.fromisoformat() on restore
  Enum      → .value (str), re-parsed with EnumClass(value) on restore
  float     → JSON number
  float|None → JSON number or null
  bool      → JSON bool

Restore invariants
------------------
  from_dict() NEVER raises or propagates exceptions.
  Corrupt individual records are skipped (logged at WARNING level).
  Unknown or missing version → empty store + WARNING.
  None or empty input → empty store (normal first start).

Deduplication
-------------
  All three add_*() methods key by the object's deterministic ID.
  Adding a duplicate returns False without modifying the store.
  First stored wins; later identical IDs are ignored.

Pruning
-------
  prune(cutoff_utc) removes all entries whose reference timestamp
  is strictly before cutoff_utc:
    ForecastSnapshot   forecast_created_utc
    RealitySnapshot    observed_at_utc
    ForecastRecord     forecast_created_utc
  Returns the total number of entries removed across all three dicts.

Tier safety
-----------
Purely a data-container.  No threshold is read or written.
No runtime state is affected.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .forecast_learning import ForecastRecord, ForecastVariable
from .forecast_snapshots import ForecastSnapshot, RealitySnapshot

_log = logging.getLogger(__name__)

# Increment when the JSON schema changes incompatibly.
CURRENT_VERSION: int = 1


# ---------------------------------------------------------------------------
# Private serialization helpers — ForecastSnapshot
# ---------------------------------------------------------------------------

def _snapshot_to_dict(snap: ForecastSnapshot) -> dict:
    return {
        "forecast_snapshot_id": snap.forecast_snapshot_id,
        "source_id":            snap.source_id,
        "variable":             snap.variable.value,
        "forecast_created_utc": snap.forecast_created_utc.isoformat(),
        "forecast_target_utc":  snap.forecast_target_utc.isoformat(),
        "forecast_value":       snap.forecast_value,
    }


def _snapshot_from_dict(raw: dict) -> ForecastSnapshot:
    return ForecastSnapshot(
        forecast_snapshot_id=str(raw["forecast_snapshot_id"]),
        source_id=str(raw["source_id"]),
        variable=ForecastVariable(raw["variable"]),
        forecast_created_utc=datetime.fromisoformat(raw["forecast_created_utc"]),
        forecast_target_utc=datetime.fromisoformat(raw["forecast_target_utc"]),
        forecast_value=float(raw["forecast_value"]),
    )


# ---------------------------------------------------------------------------
# Private serialization helpers — RealitySnapshot
# ---------------------------------------------------------------------------

def _reality_to_dict(real: RealitySnapshot) -> dict:
    return {
        "reality_snapshot_id": real.reality_snapshot_id,
        "observed_at_utc":     real.observed_at_utc.isoformat(),
        "cloud_coverage":      real.cloud_coverage,
        "temperature":         real.temperature,
        "solar_irradiance":    real.solar_irradiance,
    }


def _reality_from_dict(raw: dict) -> RealitySnapshot:
    def _opt(val: object) -> float | None:
        return None if val is None else float(val)

    return RealitySnapshot(
        reality_snapshot_id=str(raw["reality_snapshot_id"]),
        observed_at_utc=datetime.fromisoformat(raw["observed_at_utc"]),
        # nullable fields — None in JSON is passed as None; missing key → None
        cloud_coverage=_opt(raw.get("cloud_coverage")),
        temperature=_opt(raw.get("temperature")),
        solar_irradiance=_opt(raw.get("solar_irradiance")),
    )


# ---------------------------------------------------------------------------
# Private serialization helpers — ForecastRecord
# ---------------------------------------------------------------------------

def _record_to_dict(rec: ForecastRecord) -> dict:
    return {
        "forecast_snapshot_id":    rec.forecast_snapshot_id,
        "variable":                rec.variable.value,
        "forecast_created_utc":    rec.forecast_created_utc.isoformat(),
        "forecast_target_utc":     rec.forecast_target_utc.isoformat(),
        "forecast_horizon_minutes": rec.forecast_horizon_minutes,
        "forecast_value":          rec.forecast_value,
        "actual_value":            rec.actual_value,
        "absolute_error":          rec.absolute_error,
        "bias_error":              rec.bias_error,
        "is_outlier":              rec.is_outlier,
        "is_data_error":           rec.is_data_error,
    }


def _record_from_dict(raw: dict) -> ForecastRecord:
    return ForecastRecord(
        forecast_snapshot_id=str(raw["forecast_snapshot_id"]),
        variable=ForecastVariable(raw["variable"]),
        forecast_created_utc=datetime.fromisoformat(raw["forecast_created_utc"]),
        forecast_target_utc=datetime.fromisoformat(raw["forecast_target_utc"]),
        forecast_horizon_minutes=int(raw["forecast_horizon_minutes"]),
        forecast_value=float(raw["forecast_value"]),
        actual_value=float(raw["actual_value"]),
        absolute_error=float(raw["absolute_error"]),
        bias_error=float(raw["bias_error"]),
        is_outlier=bool(raw["is_outlier"]),
        is_data_error=bool(raw["is_data_error"]),
    )


# ---------------------------------------------------------------------------
# ForecastLearningStore
# ---------------------------------------------------------------------------

@dataclass
class ForecastLearningStore:
    """Mutable in-memory store for Forecast Learning raw data.

    All three dicts are keyed by the object's deterministic ID so that
    deduplication and lookup are O(1).

    Thread-safety: assumes single-threaded HA event-loop access.

    Lifecycle
    ---------
      1. Start with ForecastLearningStore.empty() or from_dict(loaded_raw).
      2. Populate via add_forecast_snapshot() / add_reality_snapshot() /
         add_forecast_record() after each collection cycle.
      3. Call prune(cutoff_utc) before saving to enforce the retention window.
      4. Persist via to_dict(); restore via from_dict().

    What is NOT stored
    ------------------
      ForecastTrustResult and ForecastTrustSummary are computed on demand
      from the ForecastRecords in this store.  They are never persisted.
    """

    forecast_snapshots: dict[str, ForecastSnapshot]
    reality_snapshots:  dict[str, RealitySnapshot]
    forecast_records:   dict[str, ForecastRecord]

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def empty(cls) -> ForecastLearningStore:
        """Return a new store with no data."""
        return cls(
            forecast_snapshots={},
            reality_snapshots={},
            forecast_records={},
        )

    # ------------------------------------------------------------------
    # Add APIs — return True if added, False if duplicate (first wins)
    # ------------------------------------------------------------------

    def add_forecast_snapshot(self, snap: ForecastSnapshot) -> bool:
        """Add *snap* to the store.

        Returns False when a snapshot with the same forecast_snapshot_id
        already exists; the existing entry is not modified.
        """
        if snap.forecast_snapshot_id in self.forecast_snapshots:
            return False
        self.forecast_snapshots[snap.forecast_snapshot_id] = snap
        return True

    def add_reality_snapshot(self, snap: RealitySnapshot) -> bool:
        """Add *snap* to the store.

        Returns False when a snapshot with the same reality_snapshot_id
        already exists; the existing entry is not modified.
        """
        if snap.reality_snapshot_id in self.reality_snapshots:
            return False
        self.reality_snapshots[snap.reality_snapshot_id] = snap
        return True

    def add_forecast_record(self, record: ForecastRecord) -> bool:
        """Add *record* to the store.

        Keyed by forecast_snapshot_id — one ForecastRecord per ForecastSnapshot.
        Returns False when a record for the same snapshot already exists.
        """
        if record.forecast_snapshot_id in self.forecast_records:
            return False
        self.forecast_records[record.forecast_snapshot_id] = record
        return True

    # ------------------------------------------------------------------
    # Prune
    # ------------------------------------------------------------------

    def prune(self, cutoff_utc: datetime) -> int:
        """Remove all entries whose reference timestamp is strictly before cutoff_utc.

        Reference timestamps:
          ForecastSnapshot  forecast_created_utc
          RealitySnapshot   observed_at_utc
          ForecastRecord    forecast_created_utc

        Returns the total number of entries removed (sum across all three dicts).
        Call this immediately before saving to enforce the 90-day retention window.
        The caller supplies cutoff_utc so that the store has no dependency on
        wall-clock time.
        """
        removed = 0

        old_snaps = [
            k for k, v in self.forecast_snapshots.items()
            if v.forecast_created_utc < cutoff_utc
        ]
        for k in old_snaps:
            del self.forecast_snapshots[k]
        removed += len(old_snaps)

        old_reals = [
            k for k, v in self.reality_snapshots.items()
            if v.observed_at_utc < cutoff_utc
        ]
        for k in old_reals:
            del self.reality_snapshots[k]
        removed += len(old_reals)

        old_recs = [
            k for k, v in self.forecast_records.items()
            if v.forecast_created_utc < cutoff_utc
        ]
        for k in old_recs:
            del self.forecast_records[k]
        removed += len(old_recs)

        return removed

    # ------------------------------------------------------------------
    # Count-cap pruning
    # ------------------------------------------------------------------

    def prune_to_count(
        self,
        max_forecast_snapshots: int,
        max_reality_snapshots: int,
        max_forecast_records: int,
    ) -> int:
        """Remove oldest entries when any collection exceeds its hard count cap.

        Oldest-first removal: entries with the earliest reference timestamp are
        removed until the collection is at or below its limit.

        Reference timestamps:
          ForecastSnapshot  forecast_created_utc
          RealitySnapshot   observed_at_utc
          ForecastRecord    forecast_created_utc

        Returns the total number of entries removed across all three collections.
        Called by the persistence adapter immediately after age-based prune().
        """
        removed = 0

        if len(self.forecast_snapshots) > max_forecast_snapshots:
            ordered = sorted(
                self.forecast_snapshots.items(),
                key=lambda kv: kv[1].forecast_created_utc,
            )
            excess = len(ordered) - max_forecast_snapshots
            for k, _ in ordered[:excess]:
                del self.forecast_snapshots[k]
            removed += excess

        if len(self.reality_snapshots) > max_reality_snapshots:
            ordered = sorted(
                self.reality_snapshots.items(),
                key=lambda kv: kv[1].observed_at_utc,
            )
            excess = len(ordered) - max_reality_snapshots
            for k, _ in ordered[:excess]:
                del self.reality_snapshots[k]
            removed += excess

        if len(self.forecast_records) > max_forecast_records:
            ordered = sorted(
                self.forecast_records.items(),
                key=lambda kv: kv[1].forecast_created_utc,
            )
            excess = len(ordered) - max_forecast_records
            for k, _ in ordered[:excess]:
                del self.forecast_records[k]
            removed += excess

        return removed

    # ------------------------------------------------------------------
    # Unmatched snapshots
    # ------------------------------------------------------------------

    def get_unmatched_snapshots(self) -> list[ForecastSnapshot]:
        """Return ForecastSnapshots that have no corresponding ForecastRecord.

        Used by the Forecast Matcher to find snapshots that still need to be
        matched against available RealitySnapshots.
        """
        return [
            snap
            for snap_id, snap in self.forecast_snapshots.items()
            if snap_id not in self.forecast_records
        ]

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize the store to a JSON-compatible dict.

        All datetime fields are serialized as ISO 8601 strings with explicit
        UTC offset (+00:00).  All Enum fields are serialized as their .value.
        None values are preserved as JSON null.
        """
        return {
            "version": CURRENT_VERSION,
            "forecast_snapshots": {
                snap_id: _snapshot_to_dict(snap)
                for snap_id, snap in self.forecast_snapshots.items()
            },
            "reality_snapshots": {
                real_id: _reality_to_dict(real)
                for real_id, real in self.reality_snapshots.items()
            },
            "forecast_records": {
                rec_id: _record_to_dict(rec)
                for rec_id, rec in self.forecast_records.items()
            },
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> ForecastLearningStore:
        """Restore a store from a previously serialized dict.

        Restoration invariants (all enforced without raising):
          None / empty dict            → empty store     (normal first start)
          Missing or unknown version   → empty store + WARNING
          Section not a dict           → that section empty + WARNING
          Individual corrupt record    → that record skipped + WARNING
          Partially corrupt data       → all valid records loaded

        Never raises.  The Coordinator is never blocked.
        """
        if not raw:
            return cls.empty()

        if raw.get("version") != CURRENT_VERSION:
            _log.warning(
                "ForecastLearningStore: unknown version %r — starting with empty store",
                raw.get("version"),
            )
            return cls.empty()

        # --- ForecastSnapshots ---
        snapshots: dict[str, ForecastSnapshot] = {}
        try:
            section = raw.get("forecast_snapshots", {})
            if not isinstance(section, dict):
                raise TypeError(f"expected dict, got {type(section).__name__}")
            for snap_id, raw_snap in section.items():
                try:
                    snapshots[snap_id] = _snapshot_from_dict(raw_snap)
                except Exception as exc:
                    _log.warning(
                        "ForecastLearningStore: skipping corrupt forecast snapshot %r "
                        "(%s: %s)", snap_id, type(exc).__name__, exc,
                    )
        except Exception as exc:
            _log.warning(
                "ForecastLearningStore: forecast_snapshots section unreadable — skipping "
                "(%s: %s)", type(exc).__name__, exc,
            )

        # --- RealitySnapshots ---
        realities: dict[str, RealitySnapshot] = {}
        try:
            section = raw.get("reality_snapshots", {})
            if not isinstance(section, dict):
                raise TypeError(f"expected dict, got {type(section).__name__}")
            for real_id, raw_real in section.items():
                try:
                    realities[real_id] = _reality_from_dict(raw_real)
                except Exception as exc:
                    _log.warning(
                        "ForecastLearningStore: skipping corrupt reality snapshot %r "
                        "(%s: %s)", real_id, type(exc).__name__, exc,
                    )
        except Exception as exc:
            _log.warning(
                "ForecastLearningStore: reality_snapshots section unreadable — skipping "
                "(%s: %s)", type(exc).__name__, exc,
            )

        # --- ForecastRecords ---
        records: dict[str, ForecastRecord] = {}
        try:
            section = raw.get("forecast_records", {})
            if not isinstance(section, dict):
                raise TypeError(f"expected dict, got {type(section).__name__}")
            for rec_id, raw_rec in section.items():
                try:
                    records[rec_id] = _record_from_dict(raw_rec)
                except Exception as exc:
                    _log.warning(
                        "ForecastLearningStore: skipping corrupt forecast record %r "
                        "(%s: %s)", rec_id, type(exc).__name__, exc,
                    )
        except Exception as exc:
            _log.warning(
                "ForecastLearningStore: forecast_records section unreadable — skipping "
                "(%s: %s)", type(exc).__name__, exc,
            )

        return cls(
            forecast_snapshots=snapshots,
            reality_snapshots=realities,
            forecast_records=records,
        )
