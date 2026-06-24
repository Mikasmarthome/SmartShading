"""Diagnostics privacy & pseudonymization — LE 2.0 / Phase P11 (pure).

THE single privacy authority for all three diagnostics products (HA diagnostics,
support export, research export).  Deny-by-default: builders emit explicit
allowlisted contracts; this module provides field classification, HMAC
pseudonymization, recursive JSON-safety + privacy validation, and bounded
size/depth/string truncation.

No Home Assistant import.  Read-only — never mutates runtime state.
"""
from __future__ import annotations

import hashlib
import hmac
import math
from datetime import datetime

# ---------------------------------------------------------------------------
# field classification (deny-by-default)
# ---------------------------------------------------------------------------
PUBLIC_SAFE = "public_safe"
SUPPORT_HASHED = "support_hashed"
RESEARCH_AGGREGATED = "research_aggregated"
NEVER_EXPORT = "never_export"

# Identifiers that must be pseudonymized (never raw) when exported at all.
_SUPPORT_HASHED_FIELDS = frozenset({
    "entry_id", "zone_id", "window_id", "cover_id", "cover_entity_id",
    "decision_id", "outcome_id", "experiment_id", "adoption_id",
    "harmonization_context", "harmonization_context_id", "forecast_source",
})
# Fields that must NEVER appear in any export in any form.
_NEVER_EXPORT_FIELDS = frozenset({
    "sensor_entity_id", "weather_entity_id", "entity_id", "device_name",
    "latitude", "longitude", "coordinates", "gps", "address", "user_name",
    "private_path", "file_path", "config_path", "exception_payload", "traceback",
})
# Coarse aggregated-only fields for research.
_RESEARCH_AGGREGATED_FIELDS = frozenset({
    "raw_solar_value", "raw_temperature", "exact_timestamp",
})


def classify(field: str) -> str:
    """Classify a field name into a privacy level (deny-by-default → NEVER for
    anything that looks like a raw entity/identity unless explicitly allowlisted)."""
    if field in _NEVER_EXPORT_FIELDS:
        return NEVER_EXPORT
    if field in _SUPPORT_HASHED_FIELDS:
        return SUPPORT_HASHED
    if field in _RESEARCH_AGGREGATED_FIELDS:
        return RESEARCH_AGGREGATED
    # Heuristic deny: anything ending in _entity_id / _path / _coords is NEVER.
    if field.endswith(("_entity_id", "_path", "_coords", "_latitude", "_longitude")):
        return NEVER_EXPORT
    return PUBLIC_SAFE


# ---------------------------------------------------------------------------
# pseudonymization — HMAC-SHA256, 16 lowercase hex, namespace-separated
# ---------------------------------------------------------------------------
PSEUDO_HEX_LEN = 16

NS_ENTRY = "entry"
NS_ZONE = "zone"
NS_WINDOW = "window"
NS_COVER = "cover"
NS_SENSOR = "sensor"
NS_DECISION = "decision"
NS_OUTCOME = "outcome"
NS_EXPERIMENT = "experiment"
NS_ADOPTION = "adoption"
NS_HARMONIZATION = "harmonization_context"
NS_FORECAST_SOURCE = "forecast_source"


class Pseudonymizer:
    """Stable-within-entry, non-reversible HMAC pseudonymizer.

    The Config-Entry id is the HMAC key (key material NEVER exported).  Output is
    16 lowercase hex chars, namespace-separated so the same raw id in different
    namespaces never collides.  Cross-entry stability is NOT promised."""

    def __init__(self, entry_id: str | None) -> None:
        # Key material stays in runtime; only digests leave.
        self._key = (entry_id or "smartshading-no-entry").encode("utf-8")

    def ref(self, namespace: str, raw_id: object) -> str | None:
        if raw_id is None:
            return None
        msg = f"{namespace}|{raw_id}".encode("utf-8")
        digest = hmac.new(self._key, msg, hashlib.sha256).hexdigest()
        return digest[:PSEUDO_HEX_LEN]


# ---------------------------------------------------------------------------
# recursive JSON-safety + privacy validation
# ---------------------------------------------------------------------------

def is_json_safe(obj: object) -> bool:
    """True when obj contains only JSON-safe scalars/containers and NO NaN/Infinity."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return True
    if isinstance(obj, float):
        return math.isfinite(obj)
    if isinstance(obj, dict):
        return all(isinstance(k, str) and is_json_safe(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return all(is_json_safe(v) for v in obj)
    return False


def contains_forbidden_substring(obj: object, needles: tuple[str, ...]) -> bool:
    """Privacy guard: detect any forbidden raw substring (entity prefixes, paths,
    coordinates) anywhere in a built contract."""
    if isinstance(obj, str):
        low = obj.lower()
        return any(n in low for n in needles)
    if isinstance(obj, dict):
        return any(contains_forbidden_substring(v, needles) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(contains_forbidden_substring(v, needles) for v in obj)
    return False


# Default forbidden raw markers that must never appear in an export value.
DEFAULT_FORBIDDEN_MARKERS: tuple[str, ...] = (
    "sensor.", "weather.", "binary_sensor.", "/config/", "/home/", "c:\\",
    "cover.",  # raw cover entity prefix (cover_id_hash is fine — has no dot)
)


# ---------------------------------------------------------------------------
# size / depth / string truncation
# ---------------------------------------------------------------------------
MAX_NESTED_DEPTH = 12
MAX_STRING_LENGTH = 512


def truncate_strings(obj, *, max_len: int = MAX_STRING_LENGTH):
    """Recursively cap string lengths; truncated strings get an explicit suffix."""
    if isinstance(obj, str):
        if len(obj) > max_len:
            return obj[:max_len] + "…[truncated]"
        return obj
    if isinstance(obj, dict):
        return {k: truncate_strings(v, max_len=max_len) for k, v in obj.items()}
    if isinstance(obj, list):
        return [truncate_strings(v, max_len=max_len) for v in obj]
    return obj


def enforce_depth(obj, *, max_depth: int = MAX_NESTED_DEPTH, _depth: int = 0):
    """Recursively cap nesting depth; deeper structures are replaced by a marker."""
    if _depth >= max_depth:
        return {"_truncated": "max_depth"} if isinstance(obj, (dict, list)) else obj
    if isinstance(obj, dict):
        return {k: enforce_depth(v, max_depth=max_depth, _depth=_depth + 1)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [enforce_depth(v, max_depth=max_depth, _depth=_depth + 1) for v in obj]
    return obj


def cap_records(records: list, max_records: int) -> tuple[list, dict]:
    """Keep the newest *max_records* (assumes records ordered oldest→newest).
    Returns (kept, truncation_metadata)."""
    total = len(records)
    if total <= max_records:
        return (records, {"truncated": False, "total": total, "kept": total})
    kept = records[-max_records:]
    return (kept, {"truncated": True, "total": total, "kept": len(kept),
                   "dropped": total - len(kept)})


def iso_utc(dt: datetime | None) -> str | None:
    """Serialize a datetime to an ISO UTC string, else None (never raises)."""
    try:
        return dt.isoformat() if dt is not None else None
    except Exception:
        return None
