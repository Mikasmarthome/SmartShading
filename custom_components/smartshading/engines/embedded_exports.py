"""Support + research export as embedded in the official HA diagnostics download.

The support export and the research export are not separate user exports; they
are parts of the one Home Assistant diagnostics download.  Both used to carry
their own multi-megabyte cap, per zone, so a download with several zones could
reach tens of megabytes and the cap loops blocked the event loop for minutes.

Here both payloads share ONE hard budget
(:data:`~.export_budget.EMBEDDED_EXPORT_BUDGET_BYTES`).  The fixed parts of both
(overviews, current state, error counters, learning status, metadata) are always
kept; only the history lists compete for what is left.  The split between the
two follows a 60/40 preference but a side that needs less hands the remainder to
the other, and inside each payload the allowance is shared max-min fairly
between the zones, newest records first.  Everything is deterministic: the same
input always yields the same selection.

Pure Python, no Home Assistant objects; the two builders are driven by the
caller (``diagnostics.py``), which hands control back to the event loop between
zones.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .export_budget import (
    EMBEDDED_EXPORT_BUDGET_BYTES,
    RESEARCH_BUDGET_WEIGHT,
    SUPPORT_BUDGET_WEIGHT,
    meta_reserve,
    trim_jointly,
)
from .research_export_v3 import (
    finalize_research_all_zones,
    research_history_groups,
)
from .support_export import (
    _history_factor,
    finalize_support_all_zones,
    support_history_groups,
    support_meta_reserve,
)

BUDGET_SCOPE = "embedded_support_and_research"


def support_history_factor(zone_count: int) -> float:
    """Per-zone record-cap scale of the support export for the shared budget."""
    return _history_factor(EMBEDDED_EXPORT_BUDGET_BYTES, zone_count)


def fit_embedded_exports(support: dict, research: dict, *,
                         budget: int = EMBEDDED_EXPORT_BUDGET_BYTES,
                         now: datetime | None = None) -> tuple:
    """Fit the assembled support and research exports into one shared *budget*.

    ``support`` and ``research`` are the not-yet-trimmed results of
    ``iter_support_export_all_zones(..., byte_cap=None)`` and
    ``iter_research_export_all_zones(..., byte_cap=None)``.  Returns
    ``(support, research, reports)``; both exports are sanitised and carry
    truncation metadata that states what was really removed.
    """
    now = now or datetime.now(timezone.utc)
    support_groups = support_history_groups(support)
    research_groups, commit = research_history_groups(research)
    reports = trim_jointly(
        [
            (support, support_groups, support_meta_reserve(support)),
            (research, research_groups, meta_reserve(0), commit),
        ],
        budget,
        [SUPPORT_BUDGET_WEIGHT, RESEARCH_BUDGET_WEIGHT],
        fair_sections=True,
    )
    commit()
    support = finalize_support_all_zones(support, reports[0], now=now, budget_scope=BUDGET_SCOPE)
    research = finalize_research_all_zones(research, reports[1], now=now, budget_scope=BUDGET_SCOPE)
    return support, research, reports
