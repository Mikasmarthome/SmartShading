"""Storage record validation — LE 2.0 / Phase P10 completion (pure).

Central, Home-Assistant-independent guards used during restore (after migration,
before runtime registration) to reject or safely normalise unsafe values:
NaN / Infinity, naive / invalid / far-future datetimes, wrong types, negative
counts, out-of-bounds deltas, duplicate ids.

Semantics: an invalid single record is skipped/suspended by the caller; an
invalid ROOT payload makes the whole store unreadable; unsafe adaptive authority
is never applied.  These helpers only classify — they never mutate runtime state.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

# A stored timestamp further than this into the future is treated as a clock
# artefact → clamp to now (for ordering) or invalidate (caller decides).
FUTURE_TOLERANCE = timedelta(hours=6)


def is_finite_number(value: object) -> bool:
    """True only for a real finite int/float (rejects bool, NaN, ±Infinity)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def safe_number(value: object, default: float | None = None) -> float | None:
    """Return value as float if finite, else default (rejects NaN/Inf/bool/str)."""
    return float(value) if is_finite_number(value) else default


def payload_has_nan_or_inf(obj: object) -> bool:
    """Recursively detect any NaN/±Infinity float in a JSON-like structure."""
    if isinstance(obj, float):
        return not math.isfinite(obj)
    if isinstance(obj, dict):
        return any(payload_has_nan_or_inf(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(payload_has_nan_or_inf(v) for v in obj)
    return False


def parse_utc(iso: object) -> datetime | None:
    """Parse an ISO string to a timezone-aware UTC datetime, else None.

    Naive datetimes are normalised to UTC (never left naive).  Non-string /
    malformed inputs return None."""
    if not isinstance(iso, str):
        return None
    try:
        d = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def normalise_timestamp(iso: object, now: datetime) -> tuple[datetime | None, bool]:
    """Return (utc_datetime, valid).  A far-future timestamp (> now + tolerance)
    is invalid (clock artefact).  None / unparseable → (None, False)."""
    dt = parse_utc(iso)
    if dt is None:
        return (None, False)
    if dt > now + FUTURE_TOLERANCE:
        return (dt, False)
    return (dt, True)


def is_valid_count(value: object) -> bool:
    """True for a non-negative int (rejects bool, floats, negatives)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def delta_within_bounds(delta: object, cap: float) -> bool:
    """True when |delta| is finite and ≤ cap (family bound check)."""
    return is_finite_number(delta) and abs(float(delta)) <= cap + 1e-9


def dedupe_by_id(records: list, *, id_key: str) -> tuple[list, list]:
    """Deterministically keep the FIRST occurrence of each id; return
    (unique_records, duplicate_ids).  Stable order preserved."""
    seen: set = set()
    unique: list = []
    dups: list = []
    for r in records:
        rid = r.get(id_key) if isinstance(r, dict) else getattr(r, id_key, None)
        if rid in seen:
            dups.append(rid)
            continue
        seen.add(rid)
        unique.append(r)
    return (unique, dups)
