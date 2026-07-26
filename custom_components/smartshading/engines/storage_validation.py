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


