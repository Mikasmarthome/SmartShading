"""Home Assistant side of cover ownership: claims from the stored config
entries, the integration-wide registry, and the Repairs issues.

The decision logic is the pure ``engines.cover_ownership``; this module only
feeds it the configuration and mirrors its result into Home Assistant.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .const import CONF_ENTRY_TYPE, DATA_COVER_OWNERSHIP, DOMAIN, ENTRY_TYPE_ZONE
from .engines.cover_ownership import (
    CoverClaim,
    CoverOwnership,
    CoverOwnershipRegistry,
    KIND_CONFLICT,
    KIND_IDENTICAL,
    build_claims,
)

if TYPE_CHECKING:  # the Home Assistant helpers are imported lazily where needed
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

ISSUE_PREFIX_CONFLICT = "duplicate_cover_"
ISSUE_PREFIX_IDENTICAL = "duplicate_cover_identical_"


def get_registry(hass: "HomeAssistant") -> CoverOwnershipRegistry:
    domain_data = hass.data.setdefault(DOMAIN, {})
    registry = domain_data.get(DATA_COVER_OWNERSHIP)
    if registry is None:
        registry = domain_data[DATA_COVER_OWNERSHIP] = CoverOwnershipRegistry()
    return registry


def current_claims(hass: "HomeAssistant", *, exclude_entry_id: str | None = None) -> list[CoverClaim]:
    """Claims of every enabled zone entry, loaded or not: a conflict is a
    property of the configuration, so it does not flap while an entry reloads."""
    entries = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude_entry_id or entry.disabled_by is not None:
            continue
        if entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_ZONE) != ENTRY_TYPE_ZONE:
            continue
        entries.append((entry.entry_id, entry.title, entry.data))
    return build_claims(entries)


def _issue_id(own: CoverOwnership) -> str:
    base = own.cover_id.replace(".", "_")
    return (ISSUE_PREFIX_IDENTICAL if own.kind == KIND_IDENTICAL else ISSUE_PREFIX_CONFLICT) + base


def _locations(own: CoverOwnership) -> str:
    labels: list[str] = []
    for party in own.parties:
        claim = next(c for c in own.claims if c.key == party)
        label = claim.label()
        if label not in labels:
            labels.append(label)
    return "; ".join(labels)


def _sync_issues(hass: "HomeAssistant", registry: CoverOwnershipRegistry) -> None:
    from homeassistant.helpers import issue_registry as ir

    wanted: dict[str, tuple[str, "ir.IssueSeverity", CoverOwnership]] = {}
    for own in registry.result.duplicates():
        if own.kind == KIND_CONFLICT:
            key = (
                "duplicate_cover_conflict_no_safety"
                if not own.safety_ok else "duplicate_cover_conflict"
            )
            wanted[_issue_id(own)] = (key, ir.IssueSeverity.ERROR, own)
        else:
            wanted[_issue_id(own)] = ("duplicate_cover_identical", ir.IssueSeverity.WARNING, own)

    for issue_id, (key, severity, own) in wanted.items():
        ir.async_create_issue(
            hass, DOMAIN, issue_id,
            is_fixable=False, is_persistent=False, severity=severity,
            translation_key=key,
            translation_placeholders={
                "cover": own.cover_id,
                "locations": _locations(own),
                "count": str(len(own.parties)),
            },
        )
    issue_reg = ir.async_get(hass)
    for (domain, issue_id) in list(issue_reg.issues):
        if domain == DOMAIN and issue_id.startswith(ISSUE_PREFIX_CONFLICT) and issue_id not in wanted:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def async_sync_cover_ownership(
    hass: "HomeAssistant", *, exclude_entry_id: str | None = None
) -> CoverOwnershipRegistry:
    """Recompute ownership from the stored configuration, notify affected
    coordinators and bring the Repairs issues in line.  Synchronous within the
    event loop, so concurrent setups see one consistent result."""
    registry = get_registry(hass)
    registry.update(current_claims(hass, exclude_entry_id=exclude_entry_id))
    try:
        _sync_issues(hass, registry)
    except Exception:  # Repairs must never break setup
        _LOGGER.warning("SmartShading: could not update cover ownership repairs", exc_info=True)
    return registry
