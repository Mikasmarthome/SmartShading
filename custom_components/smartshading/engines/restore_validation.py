"""Central per-section restore validation — LE 2.0 / Phase P10 (pure).

THE single section-validation authority.  Runs BEFORE each section's from_dict so
unsafe records never reach a model and never become adaptive authority.  Produces
structured, privacy-safe reason counters (no raw ids / payloads).  An invalid
ISOLATED record is skipped/suspended; valid neighbours survive; an invalid root /
owner mismatch is a whole-payload rejection handled by the caller.

Duplicate semantics: identical-content duplicates are de-duplicated (keep one);
conflicting duplicates are marked AMBIGUOUS and all conflicting copies removed, so
dependent authority later suspends via the reference validator.

No Home Assistant import.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .storage_validation import (
    FUTURE_TOLERANCE,
    is_finite_number,
    parse_utc,
    payload_has_nan_or_inf,
)

# stable, privacy-safe reason codes
R_NOT_MAPPING = "invalid_mapping"
R_NAN_OR_INF = "nan_or_infinity"
R_MISSING_FIELD = "missing_required_field"
R_FIELD_TYPE = "invalid_field_type"
R_INVALID_NUMERIC = "invalid_numeric"
R_DUPLICATE_ID = "duplicate_id"
R_AMBIGUOUS = "ambiguous_duplicate"
R_FUTURE_TIMESTAMP = "future_timestamp"
R_INVALID_TIMESTAMP = "invalid_timestamp"
R_UNSUPPORTED_VERSION = "unsupported_record_version"
R_BAD_ENUM = "invalid_enum"
R_NEGATIVE_COUNT = "negative_count"
R_NEGATIVE_DURATION = "negative_duration"
R_INVALID_ID = "invalid_id"
R_WINDOW_MISMATCH = "window_mismatch"
R_ZONE_MISMATCH = "zone_mismatch"
R_OUT_OF_RANGE = "out_of_range"
R_INVALID_CONFIG_GENERATION = "invalid_config_generation"


@dataclass
class SectionValidationResult:
    valid_records: list = field(default_factory=list)
    suspended_records: list = field(default_factory=list)
    invalid_count: int = 0
    invalid_by_reason: dict = field(default_factory=dict)
    duplicate_count: int = 0
    unsupported_version_count: int = 0
    ambiguous_count: int = 0
    ambiguous_records: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def _bump(self, reason: str) -> None:
        self.invalid_count += 1
        self.invalid_by_reason[reason] = self.invalid_by_reason.get(reason, 0) + 1

    def merge(self, other: "SectionValidationResult") -> None:
        """Aggregate another result of the SAME section (e.g. per-window) into self."""
        self.valid_records.extend(other.valid_records)
        self.suspended_records.extend(other.suspended_records)
        self.invalid_count += other.invalid_count
        self.duplicate_count += other.duplicate_count
        self.unsupported_version_count += other.unsupported_version_count
        self.ambiguous_count += other.ambiguous_count
        self.ambiguous_records.extend(other.ambiguous_records)
        for reason, n in other.invalid_by_reason.items():
            self.invalid_by_reason[reason] = self.invalid_by_reason.get(reason, 0) + n


def _is_bool(v: object) -> bool:
    return isinstance(v, bool)


def validate_records(
    raw_list: object,
    *,
    now: datetime,
    id_key: str | None = None,
    required_fields: tuple[str, ...] = (),
    timestamp_fields: tuple[str, ...] = (),
    required_timestamp_fields: tuple[str, ...] = (),  # must be present, non-null, valid UTC
    enum_fields: dict | None = None,            # field -> allowed set
    numeric_fields: tuple[str, ...] = (),       # must be finite real numbers (no bool)
    nonneg_count_fields: tuple[str, ...] = (),  # non-negative int counts
    nonneg_duration_fields: tuple[str, ...] = (),  # non-negative numeric durations/seconds
    range_fields: dict | None = None,           # field -> (lo, hi) inclusive bounds
    expected_window_id: str | None = None,
    window_field: str = "window_id",
    expected_zone_id: str | None = None,
    zone_field: str = "zone_id",
    conflict_check: bool = False,               # ambiguous-duplicate detection
    max_record_version: int | None = None,
    version_field: str = "record_schema_version",
) -> SectionValidationResult:
    """Validate one list-shaped section.  valid_records is the safe subset to hand
    to from_dict.  See module docstring for duplicate/ambiguity semantics."""
    res = SectionValidationResult()
    if not isinstance(raw_list, list):
        if raw_list not in (None, {}):
            res.warnings.append("section_not_a_list")
        return res
    enum_fields = enum_fields or {}
    range_fields = range_fields or {}
    seen: dict = {}          # id -> record (first valid occurrence)
    ambiguous_ids: set = set()
    for rec in raw_list:
        if not isinstance(rec, dict):
            res._bump(R_NOT_MAPPING)
            continue
        if payload_has_nan_or_inf(rec):
            res._bump(R_NAN_OR_INF)
            continue
        if max_record_version is not None:
            ver = rec.get(version_field)
            if isinstance(ver, int) and not _is_bool(ver) and ver > max_record_version:
                res.unsupported_version_count += 1
                res._bump(R_UNSUPPORTED_VERSION)
                continue
        if any(f not in rec for f in required_fields):
            res._bump(R_MISSING_FIELD)
            continue
        # scope checks
        if expected_window_id is not None and rec.get(window_field) not in (None, expected_window_id):
            res._bump(R_WINDOW_MISMATCH)
            continue
        if expected_zone_id is not None and rec.get(zone_field) not in (None, expected_zone_id):
            res._bump(R_ZONE_MISMATCH)
            continue
        # numeric finiteness (rejects bool-as-number too)
        bad = False
        for nf in numeric_fields:
            if nf in rec and not is_finite_number(rec[nf]):
                res._bump(R_INVALID_NUMERIC)
                bad = True
                break
        if bad:
            continue
        for cf in nonneg_count_fields:
            v = rec.get(cf)
            if v is not None and (not isinstance(v, int) or _is_bool(v) or v < 0):
                res._bump(R_NEGATIVE_COUNT)
                bad = True
                break
        if bad:
            continue
        for df in nonneg_duration_fields:
            v = rec.get(df)
            if v is not None and (not is_finite_number(v) or float(v) < 0):
                res._bump(R_NEGATIVE_DURATION)
                bad = True
                break
        if bad:
            continue
        for rf, (lo, hi) in range_fields.items():
            v = rec.get(rf)
            if v is not None and (not is_finite_number(v) or not (lo <= float(v) <= hi)):
                res._bump(R_OUT_OF_RANGE)
                bad = True
                break
        if bad:
            continue
        # enums
        for fld, allowed in enum_fields.items():
            if fld in rec and rec[fld] not in allowed:
                res._bump(R_BAD_ENUM)
                bad = True
                break
        if bad:
            continue
        # required timestamps: must be present, non-null and valid (null → invalid)
        for tf in required_timestamp_fields:
            dt = parse_utc(rec.get(tf))
            if dt is None:
                res._bump(R_INVALID_TIMESTAMP)
                bad = True
                break
            if dt > now + FUTURE_TOLERANCE:
                res._bump(R_FUTURE_TIMESTAMP)
                bad = True
                break
        if bad:
            continue
        # optional timestamps: null allowed, but must be valid if present
        for tf in timestamp_fields:
            val = rec.get(tf)
            if val is None:
                continue
            dt = parse_utc(val)
            if dt is None:
                res._bump(R_INVALID_TIMESTAMP)
                bad = True
                break
            if dt > now + FUTURE_TOLERANCE:
                res._bump(R_FUTURE_TIMESTAMP)
                bad = True
                break
        if bad:
            continue
        # id / duplicate / ambiguity
        if id_key is not None:
            rid = rec.get(id_key)
            if rid is None or (isinstance(rid, str) and not rid):
                res._bump(R_INVALID_ID)
                continue
            if rid in ambiguous_ids:
                res._bump(R_AMBIGUOUS)
                continue
            if rid in seen:
                if not conflict_check or seen[rid] == rec:
                    res.duplicate_count += 1
                    continue  # identical → keep the first, drop the duplicate
                # conflicting duplicate → mark ambiguous, remove the first too
                ambiguous_ids.add(rid)
                res.ambiguous_count += 1
                res.ambiguous_records.append(rid)
                res._bump(R_AMBIGUOUS)
                try:
                    res.valid_records.remove(seen[rid])
                except ValueError:
                    pass
                del seen[rid]
                continue
            seen[rid] = rec
        res.valid_records.append(rec)
    return res


def validate_keyed_models(
    raw: object, *, now: datetime, value_validator,
) -> SectionValidationResult:
    """Validate a dict[id -> single-record-dict] section (e.g. thermal/contribution
    models keyed by zone/window).  *value_validator(rec)* returns a reason code or
    None.  Returns valid_records as a list of (key, rec) pairs."""
    res = SectionValidationResult()
    if not isinstance(raw, dict):
        if raw not in (None, {}):
            res.warnings.append("section_not_a_mapping")
        return res
    for key, rec in raw.items():
        if not isinstance(key, str) or not key:
            res._bump(R_INVALID_ID)
            continue
        if not isinstance(rec, dict):
            res._bump(R_NOT_MAPPING)
            continue
        if payload_has_nan_or_inf(rec):
            res._bump(R_NAN_OR_INF)
            continue
        reason = value_validator(rec) if value_validator else None
        if reason is not None:
            res._bump(reason)
            continue
        res.valid_records.append((key, rec))
    return res


def validate_config_snapshot(snap: object) -> tuple[bool, dict]:
    """Validate the persisted normalised config snapshot BEFORE typed diffing.

    Returns (ok, reason_counts).  A corrupt snapshot must NOT drive typed
    invalidation (no false orientation/cover/sensor/provider change); the generic
    config_generation gate remains the safety net."""
    reasons: dict = {}

    def _bump(code: str) -> None:
        reasons[code] = reasons.get(code, 0) + 1

    if not isinstance(snap, dict):
        _bump(R_NOT_MAPPING)
        return (False, reasons)
    zones = snap.get("zones", {})
    windows = snap.get("windows", {})
    if not isinstance(zones, dict) or not isinstance(windows, dict):
        _bump(R_FIELD_TYPE)
        return (False, reasons)
    ok = True
    seen: set = set()
    for wid, w in windows.items():
        if not isinstance(wid, str) or not wid:
            _bump(R_INVALID_ID)
            ok = False
            continue
        if wid in seen:
            _bump(R_DUPLICATE_ID)
            ok = False
            continue
        seen.add(wid)
        if not isinstance(w, dict):
            _bump(R_NOT_MAPPING)
            ok = False
            continue
        if w.get("zone_id") is not None and not isinstance(w.get("zone_id"), str):
            _bump(R_ZONE_MISMATCH)
            ok = False
            continue
        positions = w.get("positions")
        if positions is not None:
            if not isinstance(positions, (list, tuple)):
                _bump(R_FIELD_TYPE)
                ok = False
                continue
            for p in positions:
                if p is not None and not is_finite_number(p):
                    _bump(R_INVALID_NUMERIC)
                    ok = False
                    break
    return (ok, reasons)


def merge_section_diagnostics(results: dict) -> dict:
    """Aggregate {section: SectionValidationResult} into the privacy-safe structured
    learning_restore diagnostics dict (counts only — never raw ids/payloads)."""
    invalid_by_section: dict = {}
    invalid_by_reason: dict = {}
    suspended_by_section: dict = {}
    duplicate_by_section: dict = {}
    unsupported_by_section: dict = {}
    ambiguous_by_section: dict = {}
    for section, r in results.items():
        if r.invalid_count:
            invalid_by_section[section] = r.invalid_count
        for reason, n in r.invalid_by_reason.items():
            invalid_by_reason[reason] = invalid_by_reason.get(reason, 0) + n
        if r.suspended_records:
            suspended_by_section[section] = len(r.suspended_records)
        if r.duplicate_count:
            duplicate_by_section[section] = r.duplicate_count
        if r.unsupported_version_count:
            unsupported_by_section[section] = r.unsupported_version_count
        if r.ambiguous_count:
            ambiguous_by_section[section] = r.ambiguous_count
    return {
        "invalid_records_by_section": invalid_by_section,
        "invalid_records_by_reason": invalid_by_reason,
        "suspended_records_by_section": suspended_by_section,
        "duplicate_ids_by_section": duplicate_by_section,
        "unsupported_record_versions_by_section": unsupported_by_section,
        "ambiguous_records_by_section": ambiguous_by_section,
    }
