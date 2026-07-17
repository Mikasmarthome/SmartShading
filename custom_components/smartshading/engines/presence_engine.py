"""Presence entity aggregation — v1.2.0-beta.1, Beta.1-T5. No Home Assistant
dependency, consistent with the rest of engines/.

Architecture
------------
This module owns exactly ONE responsibility: turning a list of raw
`person.*` entity state strings into a single, policy-aggregated
PresenceReading (PRESENT / ABSENT / INDETERMINATE). Time-based debouncing
(the `absence_delay_min` grace period) is a SEPARATE, unrelated
responsibility that stays entirely in PresenceDebouncer
(engines/lifecycle_engine.py) — this module never sees a clock and
PresenceDebouncer never sees an entity string:

    entity states -> evaluate_presence_policy() -> PresenceReading
        -> Coordinator maps INDETERMINATE to a safe default -> bool
        -> PresenceDebouncer.is_absence_active() -> stable presence state

Why VACATION_HOME/VACATION_AWAY are not PresencePolicy values
---------------------------------------------------------------
T5's audit deliberately rejected folding "vacation" concepts into this
module. A PresencePolicy answers "how are the selected entities combined
into one signal" (an aggregation RULE over entity states) — it has no
opinion on what SmartShading should DO once that signal is known. "Vacation
at home" / "vacation away" describe an intended OPERATING STATE (which
schedule, which comfort thresholds, which shading behavior should apply),
not an entity-aggregation rule — cramming them in here would require this
module to start making product decisions it has no business making, and
would need to be un-done (a real migration) once a general Profile system
lands. That system is the correct home for "vacation" as a named operating
state; PresencePolicy stays a narrow, purely evaluative concept.

Ignore-indeterminate quantifier rule (ANY_HOME / INVERTED_ANY_HOME) and the
ANY_AWAY / ALL_AWAY redundancy proof
--------------------------------------------------------------------------
ANY_HOME and INVERTED_ANY_HOME are evaluated ONLY over the DETERMINATE
subset of entities (those that are definitively "home" or "away" —
unknown/unavailable/missing entities are excluded from the quantifier
entirely, not treated as a third truth value inside it). This is a
deliberate, non-standard (non-Kleene) choice, REQUIRED for ANY_HOME to
reproduce the pre-T5 Coordinator behavior exactly: pre-T5, a mix of one
determinately-away entity and one indeterminate entity already yielded a
definitive "absent" (indeterminate entities were skipped, not treated as
blocking the verdict) — see _read_presence()'s history in coordinator.py
before this module existed.

ALL_HOME does NOT use this symmetric rule — see _evaluate_all_home()'s own
docstring for why an "ALL" (unanimity) quantifier needs asymmetric
indeterminate handling (a T5 pre-push-review correction: the original
implementation applied the same "ignore indeterminate" rule to ALL_HOME
too, which let it silently declare PRESENT from just the readable subset —
contradicting what "ALL home" promises).

The determinate-subset rule for ANY_HOME/INVERTED_ANY_HOME is also what
makes ANY_AWAY and ALL_AWAY provably redundant with ALL_HOME and ANY_HOME
(which is why only ANY_HOME / ALL_HOME / INVERTED_ANY_HOME are implemented
— see models/presence.py PresencePolicy docstring). Proof sketch: let D be
the determinate subset, which by construction contains only "home"/"away"
classifications (never "indeterminate"). Within D:
    any_away(D)  ==  NOT all_home(D)   (De Morgan, D has only 2 outcomes)
    all_away(D)  ==  NOT any_home(D)
So a policy defined as "present unless any_away" is IDENTICAL, for every
possible D, to a policy defined as "present iff all_home" — there is no D
for which they disagree. Same argument for "present unless all_away" vs
"present iff any_home". Only when D is empty do all four hypothetical
policies coincide anyway (all abstain). Implementing both members of either
pair would be two names for one behavior — exactly what T5 was asked to
avoid. This proof concerns the fully-determinate universe only (D itself
never contains "indeterminate" by construction) — ALL_HOME's asymmetric
extension for when indeterminate entities ARE present does not affect it:
with zero indeterminate entities, _evaluate_all_home()'s asymmetric rule
and the plain "all_home(D)" check it's compared against here coincide
exactly.

Indeterminate handling is intentionally split into two layers: ALL_HOME's
own asymmetric rule lives inside _evaluate_all_home() (see its docstring)
because "ALL" genuinely needs to know not just "which entities are
determinate" but "did I get a full house or not" — that's information only
this module has. ANY_HOME/INVERTED_ANY_HOME's simpler "D empty ->
INDETERMINATE" case, and the FINAL policy-independent safety mapping
(INDETERMINATE -> treated as present for the absence-debounce input,
matching the "never falsely trigger absence from missing data" principle
already established pre-T5) both stay the Coordinator's responsibility —
every policy's "I don't know, so play it safe" case is handled by exactly
one, auditable rule at the call site, not N slightly-different copies.
"""
from __future__ import annotations

from ..models.presence import PresencePolicy, PresenceReading

_UNDETERMINABLE_STATES = ("unknown", "unavailable")


def classify_presence_state(raw_state: str | None) -> str:
    """Classify one entity's raw HA state string into "home" / "away" /
    "indeterminate". Never raises.

    Only the exact string "home" counts as home — anything else determinate
    (including "not_home" and any custom HA zone name a person entity may
    report) counts as away, matching pre-T5 Coordinator._read_presence()
    exactly (it never special-cased "not_home"; every non-"home",
    non-indeterminate state was already treated as away).
    """
    if raw_state is None or raw_state in _UNDETERMINABLE_STATES:
        return "indeterminate"
    return "home" if raw_state == "home" else "away"


def evaluate_presence_policy(
    raw_states: list[str | None], policy: PresencePolicy
) -> PresenceReading:
    """Aggregate raw presence-entity state strings into one PresenceReading
    under the given policy. Never raises.

    ANY_HOME and INVERTED_ANY_HOME use the "ignore indeterminate, evaluate
    over the determinate subset" rule (see module docstring) — required for
    ANY_HOME to reproduce pre-T5 Coordinator behavior exactly, and carried
    over symmetrically to its inversion since both are fundamentally "ANY"
    (existence) quantifiers: a single determinate signal is already enough
    to decide them, so an unrelated entity's indeterminate reading cannot
    change that decision either way.

    ALL_HOME is intentionally NOT symmetric with ANY_HOME's "ignore
    indeterminate" treatment — see "ALL_HOME's asymmetric indeterminate
    handling" below. This was corrected after the initial T5 review: the
    original implementation ignored indeterminate entities uniformly for
    every policy, which let ALL_HOME silently declare PRESENT from just the
    readable subset (e.g. one entity "home", one "unavailable" -> PRESENT) —
    contradicting what "ALL home" actually promises a user.

    Empty input -> INDETERMINATE (no entities configured — the same
    "nothing to evaluate" outcome as every entity being indeterminate).
    """
    classifications = [classify_presence_state(s) for s in raw_states]
    determinate = [c for c in classifications if c != "indeterminate"]
    has_indeterminate = len(determinate) < len(classifications)

    if policy is PresencePolicy.ALL_HOME:
        return _evaluate_all_home(determinate, has_indeterminate)

    if not determinate:
        return PresenceReading.INDETERMINATE
    any_home = "home" in determinate

    if policy is PresencePolicy.INVERTED_ANY_HOME:
        return PresenceReading.ABSENT if any_home else PresenceReading.PRESENT
    # ANY_HOME (legacy default) — also the fallback for any future/unknown
    # policy value, matching this codebase's "never crash, safe default"
    # convention elsewhere (e.g. config_entry_data.py's storage fallbacks).
    return PresenceReading.PRESENT if any_home else PresenceReading.ABSENT


def _evaluate_all_home(determinate: list[str], has_indeterminate: bool) -> PresenceReading:
    """ALL_HOME's asymmetric indeterminate handling (T5 pre-push review fix).

    "ALL home" promises unanimous confirmation, so it needs an asymmetric
    rule — NOT the same "ignore indeterminate" treatment ANY_HOME uses:

    - ABSENT is definitive the moment ANY entity is confirmed away,
      REGARDLESS of how many others are indeterminate. One confirmed
      absence already disproves "all home" — nothing else needs to be
      known. This is also what keeps an in-progress absence-delay
      countdown robust against an unrelated entity's tracker glitching to
      "unknown" mid-countdown: as long as at least one entity is still
      confirmed away, ALL_HOME keeps reporting ABSENT, so a flaky second
      tracker can never spuriously look like "everyone's home again" and
      cancel a real, still-active absence sequence.
    - PRESENT requires EVERY entity to be positively confirmed home —
      an indeterminate entity (when no entity is confirmed away) means
      unanimity genuinely cannot be confirmed, so the honest answer is
      INDETERMINATE, not a silent PRESENT based on whoever happens to be
      readable. (INDETERMINATE still maps to "present" for the absence
      debounce input via the caller's policy-independent safety net — so
      this does not itself cause a false absence trigger; it only means
      the raw reading, e.g. in diagnostics, honestly reflects "not
      confirmed" instead of overclaiming certainty.)
    - Empty determinate set with no indeterminate entities cannot occur
      (that only happens when raw_states itself is empty, handled by the
      has_indeterminate=False, determinate=[] branch below exactly like
      "all indeterminate" would).
    """
    if "away" in determinate:
        return PresenceReading.ABSENT
    if has_indeterminate or not determinate:
        return PresenceReading.INDETERMINATE
    return PresenceReading.PRESENT  # every entity determinate, none away -> all home
