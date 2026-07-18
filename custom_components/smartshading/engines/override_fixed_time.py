"""Fixed-Time Manual Override expiry — pure helper (v1.2.0-beta.1, T7).

Computes the next occurrence of a configured local clock time (e.g. 08:00)
as an absolute expires_at, given the current time. Pure function: no HA
access, no state.

Semantics (T7 review point 7):
  - If the configured time still lies strictly in the future today, the
    override ends TODAY at that time.
  - If the configured time has already been reached or passed today
    (including the exact-equal instant — "still in the future" is read as
    strictly future), the override ends TOMORROW (the following local
    calendar day) at that time.
  - Home Assistant local time is authoritative: `now` is expected to already
    be timezone-aware in HA's configured timezone (as dt_util.as_local(now)
    would produce — see OverrideDetector.tick()'s now_local parameter and
    coordinator.py's `local_now`, which is what is actually passed in the
    production call path). This function performs no UTC conversion of its
    own; the caller is responsible for converting the returned local-aware
    result to whatever storage/comparison convention it needs (the
    Coordinator/OverrideDetector path converts to UTC via .astimezone()
    before storing it as ManualOverride.expires_at).

DST correctness (explicit, deterministic rule — T7 pre-push review point 2):
  Uses `now.replace(hour=..., minute=..., second=..., microsecond=...)`,
  which preserves `now`'s tzinfo and `fold` (default fold=0) rather than
  `datetime.combine()`. For a real IANA zoneinfo tzinfo (e.g.
  ZoneInfo("Europe/Berlin"), which is what Home Assistant uses), Python
  resolves ambiguous/nonexistent local times per PEP 495 using `fold`:

  - Nonexistent local time (spring-forward gap, e.g. a configured 02:30
    during the Europe/Berlin gap where 02:00-02:59 does not exist on
    transition day): fold=0 resolves using the UTC offset in effect BEFORE
    the transition, producing a well-defined absolute instant (verified:
    2026-03-29 02:30 Europe/Berlin with fold=0 -> 2026-03-29 01:30 UTC).
    That absolute instant is what the caller stores/compares against (the
    OverrideDetector wiring converts to UTC via .astimezone(timezone.utc)
    before storing it as ManualOverride.expires_at) — never raises.
  - Ambiguous local time (fall-back overlap, e.g. 02:30 occurring twice):
    fold=0 deterministically selects the FIRST occurrence — the earlier UTC
    instant, using the pre-transition (summer/DST) offset. This is Python's
    unconfigured default; this module does not set fold explicitly, so it
    always resolves to the first occurrence, consistently.
  - The `+ timedelta(days=1)` rollover branch performs wall-clock (naive
    component) arithmetic on an aware datetime with a zoneinfo tzinfo —
    Python recomputes the correct UTC offset for the new date lazily, so a
    rollover that lands on a DST transition day is handled by the same
    fold=0 rule above; it never raises either.
  - The result is always strictly after `now` (or exactly one day later,
    strictly after `now`, in the rollover branch), regardless of which
    resolution applied — proven by test (see
    tests/test_override_fixed_time_dst_and_timezone.py).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta


def compute_fixed_time_expiry(*, now: datetime, fixed_until: time) -> datetime:
    """Return the next occurrence of `fixed_until` (local wall-clock time).

    Args:
        now: Current, timezone-aware local datetime.
        fixed_until: The configured local end-of-day clock time.

    Returns:
        `now`'s date at `fixed_until`, if that instant is strictly after
        `now`; otherwise the same clock time on the following calendar day.
    """
    candidate = now.replace(
        hour=fixed_until.hour,
        minute=fixed_until.minute,
        second=fixed_until.second,
        microsecond=fixed_until.microsecond,
    )
    if candidate > now:
        return candidate
    return candidate + timedelta(days=1)
