"""conftest.py for tests/real_ha/ -- tests that run against a real Home Assistant.

The stub-based suite in tests/ registers placeholder modules for
`custom_components.smartshading` so that relative imports resolve without
executing the Home-Assistant-dependent package.  This tree needs the REAL package,
so it is run on its own with pytest's --confcutdir, which keeps tests/conftest.py
from being collected:

    pytest tests/real_ha --confcutdir=tests/real_ha -p no:cacheprovider

Requirements: requirements-real-ha.txt (Linux, macOS or WSL -- Home Assistant's
runner imports the POSIX-only `fcntl`).
"""
import sys
from pathlib import Path

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

# custom_components/ lives at the repo root, two levels up from this file
# (tests/real_ha/conftest.py -> tests/ -> repo root). tests/real_ha/ has no
# __init__.py, so pytest's default "prepend" import mode only puts
# tests/real_ha/ itself on sys.path; the repo root (parent of
# custom_components/) needs to be added explicitly, exactly like running a
# script from the repo root would.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _b3013_real_wait_for_completion_safe_sleep(monkeypatch):
    """Every INTERMEDIATE dispatch item runs the real wait_for_travel_completion().
    Under freezegun's real_asyncio mode a real, nonzero asyncio.sleep() never
    returns (freezegun 1.5.x with pytest-homeassistant-custom-component and
    Python 3.14); asyncio.sleep(0) is unaffected.  The wait is therefore wrapped
    with a sleep of zero duration: the production completion logic (position and
    movement judgement, elapsed and timeout arithmetic) still runs, only the one
    primitive that cannot be served in this combination is replaced.  Tests that
    need a controlled clock install their own wrapper on top of this one.
    """
    import asyncio as _asyncio

    from custom_components.smartshading import coordinator as _coordinator_module
    from custom_components.smartshading.cover_control.dispatch_completion import (
        wait_for_travel_completion as _real_wait_for_travel_completion,
    )

    async def _safe_wait_for_travel_completion(*args, **kwargs):
        kwargs.setdefault("sleep", lambda _seconds: _asyncio.sleep(0))
        return await _real_wait_for_travel_completion(*args, **kwargs)

    monkeypatch.setattr(
        _coordinator_module, "wait_for_travel_completion",
        _safe_wait_for_travel_completion,
    )
    yield


@pytest.fixture
def virtual_completion_clock(monkeypatch):
    """Run the real completion logic against a virtual monotonic clock.

    A cover that jumps to its target without reporting movement is judged by an
    estimated travel time.  Against the wall clock that estimate would be waited
    out for real (tens of seconds per cover); here every ``sleep`` simply
    advances a virtual clock, so the production arithmetic (observed movement,
    estimate, timeout) runs unchanged and the test stays deterministic.
    Returns the clock state; ``state["t"]`` is the virtual elapsed seconds.
    """
    import asyncio as _asyncio

    from custom_components.smartshading import coordinator as _coordinator_module
    from custom_components.smartshading.cover_control.dispatch_completion import (
        wait_for_travel_completion as _real_wait_for_travel_completion,
    )

    state = {"t": 0.0}

    async def _advance(seconds):
        state["t"] += max(0.0, seconds)
        await _asyncio.sleep(0)

    async def _virtual_wait_for_travel_completion(*args, **kwargs):
        kwargs.update(mono_clock=lambda: state["t"], sleep=_advance)
        return await _real_wait_for_travel_completion(*args, **kwargs)

    monkeypatch.setattr(
        _coordinator_module, "wait_for_travel_completion",
        _virtual_wait_for_travel_completion,
    )
    yield state
