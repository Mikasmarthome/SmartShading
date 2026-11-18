"""Platform entry point for the switch platform (zone entries only).

Zone entries get the Learning Mode and Active Control switches
(entities/switch.py).  The legacy System entry forwards no platform.
"""
from __future__ import annotations

from .entities.switch import async_setup_entry  # noqa: F401
