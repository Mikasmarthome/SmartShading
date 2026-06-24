"""Central reference-integrity validator — LE 2.0 / Phase P10 completion (pure).

Validates the parsed+migrated snapshot BEFORE runtime registration.  Hard
references missing → suspend/invalidate; provenance-only references missing →
never a false invalidation; duplicate ids handled deterministically; no
timestamp-approximation, no silent "most-likely" repair.

No Home Assistant import.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# reason codes
R_OWNER_MISMATCH = "owner_mismatch"
R_MISSING_SOURCE_EXPERIMENTS = "missing_source_experiments"
R_MISSING_SOURCE_EXPERIMENT = "missing_source_experiment"
R_MISSING_DECISION_LINK = "missing_decision_link"
R_DUPLICATE_ID = "duplicate_id"


@dataclass(frozen=True)
class ReferenceValidationResult:
    valid_ids: tuple[str, ...] = ()
    suspended_ids: tuple[str, ...] = ()
    invalid_ids: tuple[str, ...] = ()
    reason_codes: dict = field(default_factory=dict)   # id → reason
    owner_ok: bool = True

    @property
    def all_rejected(self) -> bool:
        return not self.owner_ok


def validate_adoptions(
    adoptions: list, *, owner_entry_id: str | None, current_entry_id: str,
    resolvable_experiment_ids: set | None = None,
) -> ReferenceValidationResult:
    """Validate persistent adoptions (position or strategy).

    Each adoption object must expose: adoption_id, source_experiment_ids,
    consumed_experiment_ids.  HARD reference rule:
      - source_experiment_ids must be non-empty, AND
      - when *resolvable_experiment_ids* is provided, EVERY required
        source_experiment_id must resolve to a restored experiment.  One missing
        id makes the whole adoption unsafe (no silent partial acceptance).
    Consumed-ledger membership and shadow-provenance are NOT accepted as hard
    resolution.  A whole-payload owner mismatch rejects everything."""
    if owner_entry_id is not None and owner_entry_id != current_entry_id:
        ids = tuple(getattr(a, "adoption_id", None) for a in adoptions)
        return ReferenceValidationResult(
            invalid_ids=ids, reason_codes={i: R_OWNER_MISMATCH for i in ids}, owner_ok=False)

    valid: list[str] = []
    invalid: list[str] = []
    reasons: dict = {}
    seen: set = set()
    for a in adoptions:
        aid = getattr(a, "adoption_id", None)
        if aid in seen:
            invalid.append(aid)
            reasons[aid] = R_DUPLICATE_ID
            continue
        seen.add(aid)
        src = tuple(getattr(a, "source_experiment_ids", ()) or ())
        consumed = tuple(getattr(a, "consumed_experiment_ids", ()) or ())
        if not src and not consumed:
            invalid.append(aid)
            reasons[aid] = R_MISSING_SOURCE_EXPERIMENTS
            continue
        if not src:
            # consumed-ledger membership alone is NOT a resolvable source experiment
            invalid.append(aid)
            reasons[aid] = R_MISSING_SOURCE_EXPERIMENT
            continue
        if resolvable_experiment_ids is not None and any(
            sid not in resolvable_experiment_ids for sid in src
        ):
            # one unresolved required id ⇒ whole adoption unsafe
            invalid.append(aid)
            reasons[aid] = R_MISSING_SOURCE_EXPERIMENT
            continue
        valid.append(aid)
    return ReferenceValidationResult(
        valid_ids=tuple(valid), invalid_ids=tuple(invalid), reason_codes=reasons, owner_ok=True)


def validate_experiments(experiments: list) -> ReferenceValidationResult:
    """Terminal experiments require an exact decision link (hard reference)."""
    valid: list[str] = []
    suspended: list[str] = []
    reasons: dict = {}
    for e in experiments:
        eid = getattr(e, "experiment_id", None)
        is_terminal = getattr(e, "is_terminal", False)
        decision = getattr(e, "experiment_decision_id", None) or getattr(e, "decision_id", None)
        if is_terminal and not decision:
            suspended.append(eid)
            reasons[eid] = R_MISSING_DECISION_LINK
        else:
            valid.append(eid)
    return ReferenceValidationResult(
        valid_ids=tuple(valid), suspended_ids=tuple(suspended), reason_codes=reasons)
