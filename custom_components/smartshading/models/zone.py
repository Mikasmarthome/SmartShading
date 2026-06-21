"""Zone configuration - the comfort context. See ARCHITECTURE.md §3.0 / §3.1."""
from __future__ import annotations

from dataclasses import dataclass, field

from .zone_execution_config import ZoneExecutionConfig


@dataclass
class ZoneConfig:
    """A zone groups one or more windows that share comfort context
    (indoor temperature, presence, comfort profile) - e.g. a room with a
    south and a west window (ARCHITECTURE.md §3.0).

    All fields besides id/name are optional: None means "inherit from
    GlobalDefaults" (see ConfigResolver in models/config.py). A window may
    further override any of these at the window level (models/window.py).
    """

    id: str
    name: str

    tolerance_start: float | None = None
    tolerance_end: float | None = None
    night_shading_enabled: bool | None = None
    absence_shading_enabled: bool | None = None
    absence_position: int | None = None
    learning_enabled: bool | None = None
    comfort_profile_id: str | None = None
    lifecycle_config_id: str | None = None

    # Execution mode flags (Step 9G5a).  Controls per-zone observation/learning
    # and active cover control independently.  Defaults: observation on,
    # active control off (safe post-install experience).
    execution: ZoneExecutionConfig = field(default_factory=ZoneExecutionConfig)
