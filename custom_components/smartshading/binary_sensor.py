"""Platform entry point required by Home Assistant's loader. Implementation
lives in entities/binary_sensor.py, per the entities/ layout in
ARCHITECTURE.md §2.
"""
from __future__ import annotations

from .entities.binary_sensor import async_setup_entry  # noqa: F401
