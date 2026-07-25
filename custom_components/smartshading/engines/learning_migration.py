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


# T15: capped at 2, matching the actual payload format learning_persistence.py
# reads and writes (PAYLOAD_SCHEMA_V1/V2). A v3 step existed here previously
# but its migrated output was never consumed anywhere — deserialize_into_
# learning_store() only ever accepts version 1 or 2 and raises for anything
# else. Advertising CURRENT_PAYLOAD_SCHEMA=3 here meant a hypothetical future
# v3 payload would pass THIS module's accept_authority gate and then be
# rejected by the real deserializer, silently discarding a whole learning
# store (empty-store fallback) instead of erroring cleanly at the gate that's
# supposed to catch it. Raise this back to 3 only alongside a real payload
# writer + reader that actually produce/consume schema 3.
CURRENT_PAYLOAD_SCHEMA: int = 2


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


def migrate_payload(
    data: object, *, owner_entry_id: str | None,
) -> MigrationResult:
    """Migrate a loaded payload to CURRENT_PAYLOAD_SCHEMA.

    Returns accept_authority=False for an unknown newer payload (baseline only)
    or an unreadable root payload."""
    # Root gate is TYPE-only: a NaN/Inf inside a single record is an isolated-record
    # problem handled by per-section validation, not a whole-payload rejection.
    if not isinstance(data, dict):
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
    return MigrationResult(
        cur, CURRENT_PAYLOAD_SCHEMA, tuple(steps), accept_authority=True)
