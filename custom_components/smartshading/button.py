"""Platform entry point required by Home Assistant's loader. Implementation
lives in entities/button.py, per the entities/ layout in ARCHITECTURE.md §2.
"""
from __future__ import annotations

from .entities.button import async_setup_entry  # noqa: F401
