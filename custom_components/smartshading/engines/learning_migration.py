"""Learning payload migration framework — LE 2.0 / Phase P10 completion (pure).

Strictly separates the three version concepts:
  - HA Store wrapper version  (handled by homeassistant.helpers.storage.Store)
  - payload_schema_version    (this module's top-level chain: v1 → v2 → v3)
  - record_schema_version     (each model's own from_dict, additive-tolerant)

Every migration step is deterministic, idempotent, pure and adds only safe
defaults — it never invents causal evidence and never reuses consumed ids.

No Home Assistant import.
"""
from __future__ import annotations

from dataclasses import dataclass

from .storage_validation import root_payload_is_valid

CURRENT_PAYLOAD_SCHEMA: int = 3

# Sections that must exist (as empty defaults) from v3 onwards.
_V3_LIST_SECTIONS: tuple[str, ...] = (
    "shadow_proposals", "bounded_experiments", "persistent_adoptions",
    "strategy_experiments", "persistent_strategy_adoptions",
)


@dataclass(frozen=True)
class MigrationResult:
    data: dict
    payload_schema_version: int
    applied_steps: tuple[str, ...]
    accept_authority: bool          # False ⇒ load baseline only, no adaptive authority
    reason: str | None = None


def detect_payload_schema_version(data: dict) -> int:
    """v2+ carries an explicit schema_version; legacy v1 has only HA 'version'==1."""
    sv = data.get("schema_version")
    if isinstance(sv, int):
        return sv
    return 1


def _v1_to_v2(data: dict) -> dict:
    data = dict(data)
    data["schema_version"] = 2
    return data


def _v2_to_v3(data: dict, *, owner_entry_id: str | None) -> dict:
    data = dict(data)
    data["schema_version"] = 3
    # Owner: a learning store file is entry-scoped (filename carries entry_id), so
    # filling owner_entry_id from the opening entry is unambiguous when absent.
    if not data.get("owner_entry_id") and owner_entry_id is not None:
        data["owner_entry_id"] = owner_entry_id
    data.setdefault("created_by_domain", "smartshading")
    data.setdefault("consumed_experiment_ledger", {})
    for key in _V3_LIST_SECTIONS:
        data.setdefault(key, [])
    return data


def migrate_payload(
    data: object, *, owner_entry_id: str | None,
) -> MigrationResult:
    """Migrate a loaded payload to CURRENT_PAYLOAD_SCHEMA.

    Returns accept_authority=False for an unknown newer payload (baseline only)
    or an unreadable root payload."""
    if not root_payload_is_valid(data):
        return MigrationResult({}, 0, (), accept_authority=False, reason="malformed_root")
    version = detect_payload_schema_version(data)
    if version > CURRENT_PAYLOAD_SCHEMA:
        # Newer than we understand → never load adaptive authority.
        return MigrationResult(
            dict(data), version, (), accept_authority=False, reason="unknown_newer_schema")

    steps: list[str] = []
    cur = data
    if version <= 1:
        cur = _v1_to_v2(cur)
        steps.append("v1_to_v2")
    if detect_payload_schema_version(cur) <= 2:
        cur = _v2_to_v3(cur, owner_entry_id=owner_entry_id)
        steps.append("v2_to_v3")
    return MigrationResult(
        cur, CURRENT_PAYLOAD_SCHEMA, tuple(steps), accept_authority=True)
