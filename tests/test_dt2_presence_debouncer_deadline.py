"""DT-2: the debouncer exposes when the current absence started counting (the coordinator derives
the activation deadline from it instead of keeping a second copy)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.smartshading.engines.lifecycle_engine import PresenceDebouncer

T0 = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def test_absent_since_follows_the_presence_rules() -> None:
    d = PresenceDebouncer()
    assert d.absent_since is None
    assert d.is_absence_active(False, T0, 30) is False
    assert d.absent_since == T0
    assert d.is_absence_active(False, T0 + timedelta(minutes=10), 30) is False
    assert d.absent_since == T0, "later evaluations do not move the start"
    assert d.is_absence_active(False, T0 + timedelta(minutes=30), 30) is True
    assert d.is_absence_active(True, T0 + timedelta(minutes=31), 30) is False
    assert d.absent_since is None, "coming home resets the count"


def test_a_resumed_absence_is_already_past_its_deadline() -> None:
    d = PresenceDebouncer()
    d.resume_absence(T0, 30)
    assert d.absent_since == T0 - timedelta(minutes=30)
    assert d.is_absence_active(False, T0, 30) is True, "a restored absence has no pending deadline"
