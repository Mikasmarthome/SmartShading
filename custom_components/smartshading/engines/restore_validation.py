"""Central per-section restore validation — LE 2.0 / Phase P10 final closure (pure).

Runs BEFORE each section's from_dict during restore so that unsafe records never
reach a model and never become active authority.  Produces structured,
privacy-safe reason counters (no raw ids / payloads).  An invalid ISOLATED record
is skipped; valid neighbours remain usable; an invalid root / owner mismatch is
handled by the caller as a whole-payload rejection.

No Home Assistant import.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .storage_validation import (
    FUTURE_TOLERANCE,
    parse_utc,
    payload_has_nan_or_inf,
)

# reason codes (stable, privacy-safe)
R_NOT_MAPPING = "not_mapping"
R_NAN_OR_INF = "nan_or_inf"
R_MISSING_FIELD = "missing_required_field"
R_DUPLICATE_ID = "duplicate_id"
R_FUTURE_TIMESTAMP = "future_timestamp"
R_UNSUPPORTED_VERSION = "unsupported_record_version"
R_BAD_ENUM = "bad_enum_value"


@dataclass
class SectionValidationResult:
    valid_records: list = field(default_factory=list)
    suspended_records: list = field(default_factory=list)
    invalid_count: int = 0
    invalid_by_reason: dict = field(default_factory=dict)
    duplicate_count: int = 0
    unsupported_version_count: int = 0
    warnings: list = field(default_factory=list)

    def _bump(self, reason: str) -> None:
        self.invalid_count += 1
        self.invalid_by_reason[reason] = self.invalid_by_reason.get(reason, 0) + 1


def validate_records(
    raw_list: object,
    *,
    now: datetime,
    id_key: str | None = None,
    required_fields: tuple[str, ...] = (),
    timestamp_fields: tuple[str, ...] = (),
    enum_fields: dict | None = None,          # field -> allowed set
    max_record_version: int | None = None,
    version_field: str = "record_schema_version",
) -> SectionValidationResult:
    """Validate one list-shaped section.  Returns a SectionValidationResult whose
    valid_records is the safe subset to hand to from_dict."""
    res = SectionValidationResult()
    if not isinstance(raw_list, list):
        if raw_list not in (None, {}):
            res.warnings.append("section_not_a_list")
        return res
    enum_fields = enum_fields or {}
    seen: set = set()
    for rec in raw_list:
        if not isinstance(rec, dict):
            res._bump(R_NOT_MAPPING)
            continue
        if payload_has_nan_or_inf(rec):
            res._bump(R_NAN_OR_INF)
            continue
        if max_record_version is not None:
            ver = rec.get(version_field)
            if isinstance(ver, int) and ver > max_record_version:
                res.unsupported_version_count += 1
                res._bump(R_UNSUPPORTED_VERSION)
                continue
        if any(f not in rec for f in required_fields):
            res._bump(R_MISSING_FIELD)
            continue
        bad_enum = False
        for fld, allowed in enum_fields.items():
            if fld in rec and rec[fld] not in allowed:
                bad_enum = True
                break
        if bad_enum:
            res._bump(R_BAD_ENUM)
            continue
        future = False
        for tf in timestamp_fields:
            val = rec.get(tf)
            if val is None:
                continue
            dt = parse_utc(val)
            if dt is not None and dt > now + FUTURE_TOLERANCE:
                future = True
                break
        if future:
            res._bump(R_FUTURE_TIMESTAMP)
            continue
        if id_key is not None:
            rid = rec.get(id_key)
            if rid is not None and rid in seen:
                res.duplicate_count += 1
                res._bump(R_DUPLICATE_ID)
                continue
            if rid is not None:
                seen.add(rid)
        res.valid_records.append(rec)
    return res


def merge_section_diagnostics(results: dict) -> dict:
    """Aggregate {section: SectionValidationResult} into the privacy-safe structured
    learning_restore diagnostics dict (counts only — never raw ids/payloads)."""
    invalid_by_section: dict = {}
    invalid_by_reason: dict = {}
    suspended_by_section: dict = {}
    duplicate_by_section: dict = {}
    unsupported_by_section: dict = {}
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
    return {
        "invalid_records_by_section": invalid_by_section,
        "invalid_records_by_reason": invalid_by_reason,
        "suspended_records_by_section": suspended_by_section,
        "duplicate_ids_by_section": duplicate_by_section,
        "unsupported_record_versions_by_section": unsupported_by_section,
    }
