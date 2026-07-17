"""Presence evaluation policy — v1.2.0-beta.1, Beta.1-T5. Pure enums, no Home
Assistant dependency, consistent with the rest of models/.

PresencePolicy controls HOW the configured presence entities are aggregated
into a single house-wide presence signal — it answers "does at least one /
do all / does the inverse of at least one configured person count as home?"
It deliberately does NOT answer "what should SmartShading DO in this
operating state" (that is a future Profile concern, out of scope for T5 —
see engines/presence_engine.py module docstring for the full rationale on
why VACATION_HOME/VACATION_AWAY are not PresencePolicy values).

ANY_HOME is the legacy default: byte-for-byte the same aggregation
Coordinator._read_presence() already performed before T5 (present if any
configured person is confirmed home; absent once at least one is confirmed
away and none are home; present as the safe default when nothing is
readable at all). Existing configs without a stored presence_policy key
default to ANY_HOME, reproducing pre-T5 behavior exactly.
"""
from __future__ import annotations

from enum import Enum


class PresencePolicy(Enum):
    """See module docstring. Exactly 3 values — ANY_AWAY and ALL_AWAY were
    considered and rejected as provably redundant with ALL_HOME and ANY_HOME
    respectively once indeterminate (unknown/unavailable/missing) entities
    are excluded from the quantifier evaluation (see
    engines/presence_engine.py evaluate_presence_policy() docstring for the
    proof). INVERTED_ALL_HOME would be a legitimate 4th value but was not
    requested and is deliberately left for a later ticket (no unnecessary
    generalization in T5).

    ANY_HOME:
      Present as soon as at least one configured, determinate entity is
      "home". Absent once at least one is determinately away and none are
      home. This is the legacy default — matches pre-T5 behavior exactly.
    ALL_HOME:
      Present only when EVERY configured entity is positively confirmed
      "home" — unlike ANY_HOME/INVERTED_ANY_HOME, an indeterminate entity
      does NOT get silently ignored here: since "ALL home" is a promise of
      unanimous confirmation, one unreadable entity (with none confirmed
      away) means unanimity genuinely cannot be confirmed, so the result is
      INDETERMINATE rather than a present verdict based on whoever happens
      to be readable. Absent is still definitive the instant a single
      entity is determinately away, regardless of how many others are
      indeterminate — this is what keeps an in-progress absence-delay
      countdown robust against an unrelated entity's tracker glitching to
      "unknown" mid-countdown (see engines/presence_engine.py
      _evaluate_all_home() for the full asymmetric-rule rationale).
    INVERTED_ANY_HOME:
      The exact boolean inversion of ANY_HOME's determinate verdict: absent
      as soon as at least one entity is home, present once at least one is
      determinately away and none are home. For setups where "home"
      indicates the opposite of the desired shading behavior (e.g. reusing
      existing person entities for an inverted automation intent) without
      needing to reconfigure or wrap the underlying entities.
    """

    ANY_HOME = "any_home"
    ALL_HOME = "all_home"
    INVERTED_ANY_HOME = "inverted_any_home"


class PresenceReading(Enum):
    """Three-valued result of evaluate_presence_policy() — the "raw
    present/absent/indeterminate" stage in the pipeline:

        entity states -> presence policy evaluation -> PresenceReading
            -> (safe-default mapping) -> debouncer -> stable presence state

    INDETERMINATE means the configured policy could not reach a definitive
    verdict (no entities configured, or every entity's state is currently
    unknown/unavailable/missing). Mapping INDETERMINATE to a safe default
    (never falsely trigger absence from missing data) is the CALLER's
    responsibility (Coordinator._read_presence()), not this reading itself
    — keeping the 3-valued result honest about what is actually known.
    """

    PRESENT = "present"
    ABSENT = "absent"
    INDETERMINATE = "indeterminate"
