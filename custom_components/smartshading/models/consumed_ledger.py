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

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

CONSUMED_LEDGER_SCHEMA_VERSION: int = 1

TYPE_POSITION: str = "position"
TYPE_STRATEGY: str = "strategy"
_TYPES: tuple[str, ...] = (TYPE_POSITION, TYPE_STRATEGY)

# Bounded exact-id cap per type before compaction into the watermark.
MAX_EXACT_IDS_PER_TYPE: int = 5000
# A restored exact-id set larger than this is structurally implausible → corrupt.
_MAX_RESTORE_IDS_PER_TYPE: int = MAX_EXACT_IDS_PER_TYPE * 2
# Watermark/created timestamps beyond now + this are clock artefacts → corrupt.
_FUTURE_TOLERANCE = timedelta(hours=6)

# --- per-namespace integrity states (P10 acceptance fix: fail-closed) ---
LEDGER_VALID: str = "valid"
LEDGER_MISSING: str = "missing"
LEDGER_CORRUPT: str = "corrupt"
LEDGER_UNSUPPORTED: str = "unsupported"
LEDGER_OWNER_MISMATCH: str = "owner_mismatch"

# stable, privacy-safe reason codes
LR_INVALID_ROOT = "invalid_root"
LR_UNSUPPORTED_SCHEMA = "unsupported_schema"
LR_INVALID_NAMESPACE = "invalid_namespace"
LR_INVALID_ID = "invalid_id"
LR_DUPLICATE_ID = "duplicate_id"
LR_INVALID_TIMESTAMP = "invalid_timestamp"
LR_FUTURE_TIMESTAMP = "future_timestamp"
LR_INVALID_WATERMARK = "invalid_watermark"
LR_OWNER_MISMATCH = "owner_mismatch"
LR_COUNT_EXCEEDED = "count_exceeded"


@dataclass
class LedgerIntegrity:
    """Per-namespace restore integrity.  A namespace is SAFE for adaptive
    authority only when valid or (legitimately) missing.  Corruption can cost
    adaptive availability but can never release consumed evidence."""
    position: str = LEDGER_VALID
    strategy: str = LEDGER_VALID
    invalid_by_reason: dict = field(default_factory=dict)

    def is_safe(self, namespace: str) -> bool:
        status = self.position if namespace == TYPE_POSITION else self.strategy
        return status in (LEDGER_VALID, LEDGER_MISSING)

    def status(self, namespace: str) -> str:
        return self.position if namespace == TYPE_POSITION else self.strategy


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
        """Trusted deserialisation of in-memory-roundtripped data ONLY.

        NOT the restore authority: restore_with_integrity is the fail-closed
        production path.  This stays for serialization roundtrip of data we wrote
        ourselves; it must never be used to load an untrusted store on restore."""
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

    @classmethod
    def restore_with_integrity(
        cls, d: dict | None, *, owner_entry_id: str | None = None,
        current_entry_id: str | None = None, now: datetime | None = None,
    ) -> tuple["ConsumedExperimentLedger", LedgerIntegrity]:
        """FAIL-CLOSED restore (P10 acceptance fix).

        Returns (ledger, integrity).  Corruption / unsupported schema / owner
        mismatch marks the affected namespace UNSAFE so the caller blocks new and
        suspends restored adaptive authority — but consumed evidence is NEVER
        released.  A legitimately missing ledger is empty + valid (no global lock)."""
        now = now or datetime.now(timezone.utc)
        led = cls()
        integ = LedgerIntegrity()
        reasons: dict = {}

        def _bump(code: str) -> None:
            reasons[code] = reasons.get(code, 0) + 1

        # MISSING: no ledger at all (legacy/empty store) → empty + valid.
        if d is None or d == {}:
            integ.position = integ.strategy = LEDGER_MISSING
            integ.invalid_by_reason = reasons
            return led, integ
        # Invalid root → both namespaces corrupt (fail-closed).
        if not isinstance(d, dict):
            _bump(LR_INVALID_ROOT)
            integ.position = integ.strategy = LEDGER_CORRUPT
            integ.invalid_by_reason = reasons
            return led, integ
        # Unsupported newer schema → both namespaces unsafe.
        ver = d.get("schema_version", CONSUMED_LEDGER_SCHEMA_VERSION)
        if not isinstance(ver, int) or ver > CONSUMED_LEDGER_SCHEMA_VERSION:
            _bump(LR_UNSUPPORTED_SCHEMA)
            integ.position = integ.strategy = LEDGER_UNSUPPORTED
            integ.invalid_by_reason = reasons
            return led, integ
        # Owner mismatch → fail-closed both namespaces (no cross-zone evidence).
        owner = d.get("owner_entry_id")
        if (owner is not None and current_entry_id is not None
                and owner != current_entry_id):
            _bump(LR_OWNER_MISMATCH)
            integ.position = integ.strategy = LEDGER_OWNER_MISMATCH
            integ.invalid_by_reason = reasons
            return led, integ

        exact = d.get("exact_recent_ids_by_type")
        retired = d.get("retired_before_by_type")
        if not isinstance(exact, dict) or not isinstance(retired, dict):
            _bump(LR_INVALID_ROOT)
            integ.position = integ.strategy = LEDGER_CORRUPT
            integ.invalid_by_reason = reasons
            return led, integ

        # Per-namespace validation: a corrupt namespace does not block the other.
        status = {TYPE_POSITION: LEDGER_VALID, TYPE_STRATEGY: LEDGER_VALID}
        for t in _TYPES:
            ns_ids = exact.get(t, {})
            if t not in exact:
                status[t] = LEDGER_MISSING
                continue
            if not isinstance(ns_ids, dict):
                _bump(LR_INVALID_NAMESPACE)
                status[t] = LEDGER_CORRUPT
                continue
            if len(ns_ids) > _MAX_RESTORE_IDS_PER_TYPE:
                _bump(LR_COUNT_EXCEEDED)
                status[t] = LEDGER_CORRUPT
                continue
            wm = retired.get(t)
            wm_dt = None
            if wm is not None:
                wm_dt = _parse(wm)
                if wm_dt is None:
                    _bump(LR_INVALID_WATERMARK)
                    status[t] = LEDGER_CORRUPT
                    continue
                if wm_dt > now + _FUTURE_TOLERANCE:
                    _bump(LR_FUTURE_TIMESTAMP)
                    status[t] = LEDGER_CORRUPT
                    continue
            parsed_ids: dict = {}
            corrupt = False
            for eid, iso in ns_ids.items():
                if not isinstance(eid, str) or not eid:
                    _bump(LR_INVALID_ID)
                    corrupt = True
                    break
                if eid in parsed_ids:
                    _bump(LR_DUPLICATE_ID)
                    corrupt = True
                    break
                dt = _parse(iso)
                if dt is None:
                    _bump(LR_INVALID_TIMESTAMP)
                    corrupt = True
                    break
                if dt > now + _FUTURE_TOLERANCE:
                    _bump(LR_FUTURE_TIMESTAMP)
                    corrupt = True
                    break
                # Watermark must not regress behind retained evidence: a retained
                # exact id created BEFORE the watermark is an inconsistent ledger.
                if wm_dt is not None and dt < wm_dt:
                    _bump(LR_INVALID_WATERMARK)
                    corrupt = True
                    break
                parsed_ids[eid] = dt
            if corrupt:
                status[t] = LEDGER_CORRUPT
                continue
            # Namespace valid → load it.
            led._exact[t] = parsed_ids
            led._retired_before[t] = wm_dt

        integ.position = status[TYPE_POSITION]
        integ.strategy = status[TYPE_STRATEGY]
        integ.invalid_by_reason = reasons
        # A namespace that failed validation must hold NO loaded evidence (already
        # the case: we 'continue' before loading), and is reported UNSAFE so the
        # caller blocks adaptive authority for it.
        return led, integ
