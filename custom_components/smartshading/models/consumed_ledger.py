"""Consumed Experiment Ledger — LE 2.0 / Phase P10.

A permanent-but-bounded record of every experiment id that has been consumed by
an adoption stage, so an experiment can never be counted as adoption evidence
twice — not after rollback, not after reduction, not after history pruning, and
not via a stale backup that reappears.

Bounded design (no false negatives):
  - Recent/known ids are stored EXACTLY per type (position / strategy).
  - When the exact set exceeds a cap, the oldest ids are dropped and a
    ``retired_before`` watermark is advanced to the newest dropped created-time.
  - ``is_consumed`` returns True for any exact id OR any id whose created-time is
    < the watermark.  A dropped consumed id (created before the watermark) is
    still rejected → never a false negative.  A very old NON-consumed experiment
    may also be rejected (a safe false positive on stale evidence we would not
    adopt anyway), which the spec explicitly prefers over a false negative.

Position and strategy ids live in SEPARATE namespaces (no collision).
No Home Assistant import.  Frozen-by-convention; mutated via methods.
"""
from __future__ import annotations

from datetime import datetime, timezone

CONSUMED_LEDGER_SCHEMA_VERSION: int = 1

TYPE_POSITION: str = "position"
TYPE_STRATEGY: str = "strategy"
_TYPES: tuple[str, ...] = (TYPE_POSITION, TYPE_STRATEGY)

# Bounded exact-id cap per type before compaction into the watermark.
MAX_EXACT_IDS_PER_TYPE: int = 5000


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    try:
        d = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


class ConsumedExperimentLedger:
    def __init__(self) -> None:
        # type → {experiment_id: created_at}
        self._exact: dict[str, dict[str, datetime]] = {t: {} for t in _TYPES}
        # type → watermark datetime (created < watermark ⇒ consumed)
        self._retired_before: dict[str, datetime | None] = {t: None for t in _TYPES}

    # ------------------------------------------------------------------
    def record(self, ledger_type: str, experiment_id: str,
               created_at: datetime | None) -> None:
        if ledger_type not in self._exact:
            return
        self._exact[ledger_type][experiment_id] = created_at or datetime.now(timezone.utc)
        self._compact(ledger_type)

    def is_consumed(self, ledger_type: str, experiment_id: str,
                    created_at: datetime | None) -> bool:
        bucket = self._exact.get(ledger_type, {})
        if experiment_id in bucket:
            return True
        wm = self._retired_before.get(ledger_type)
        if wm is not None and created_at is not None and created_at < wm:
            return True
        return False

    def consumed_ids(self, ledger_type: str) -> set:
        return set(self._exact.get(ledger_type, {}).keys())

    def _compact(self, ledger_type: str) -> None:
        bucket = self._exact[ledger_type]
        if len(bucket) <= MAX_EXACT_IDS_PER_TYPE:
            return
        # Drop the oldest excess; advance the watermark to the newest dropped time
        # so dropped consumed ids stay rejected forever (no false negative).
        ordered = sorted(bucket.items(), key=lambda kv: kv[1])
        excess = len(bucket) - MAX_EXACT_IDS_PER_TYPE
        dropped = ordered[:excess]
        for exp_id, _ in dropped:
            del bucket[exp_id]
        newest_dropped = max(dt for _, dt in dropped)
        cur = self._retired_before.get(ledger_type)
        self._retired_before[ledger_type] = (
            newest_dropped if cur is None else max(cur, newest_dropped))

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema_version": CONSUMED_LEDGER_SCHEMA_VERSION,
            "exact_recent_ids_by_type": {
                t: {eid: _iso(dt) for eid, dt in self._exact[t].items()} for t in _TYPES
            },
            "retired_before_by_type": {
                t: _iso(self._retired_before[t]) for t in _TYPES
            },
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "ConsumedExperimentLedger":
        led = cls()
        if not isinstance(d, dict):
            return led
        exact = d.get("exact_recent_ids_by_type", {}) or {}
        for t in _TYPES:
            for eid, iso in (exact.get(t, {}) or {}).items():
                parsed = _parse(iso)
                if isinstance(eid, str):
                    led._exact[t][eid] = parsed or datetime.now(timezone.utc)
        retired = d.get("retired_before_by_type", {}) or {}
        for t in _TYPES:
            led._retired_before[t] = _parse(retired.get(t))
        return led
