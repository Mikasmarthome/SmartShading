"""Platform entry point required by Home Assistant's loader. Implementation
lives in entities/sensor.py, per the entities/ layout in ARCHITECTURE.md §2.
"""
from __future__ import annotations

from .entities.sensor import async_setup_entry  # noqa: F401
