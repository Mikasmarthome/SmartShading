"""Pytest configuration for SmartShading tests.

Registers lightweight namespace placeholders for `custom_components` and
`custom_components.smartshading` so that relative imports inside the
integration resolve correctly without executing the real __init__.py files
(which require Home Assistant).

Also pre-registers critical shared HA stubs so the correct versions are
present before any test module runs.  Individual test files may override
specific attributes for their own needs, but the baseline stubs registered
here prevent contamination when alphabetically-earlier test files register
weaker versions (e.g. EntityCategory=MagicMock).
"""
import enum
import sys
import types
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_ROOT = _REPO_ROOT / "custom_components" / "smartshading"


# ---------------------------------------------------------------------------
# Namespace placeholders for the integration package tree
# ---------------------------------------------------------------------------

def _register_namespace_placeholder(dotted_name: str, path: Path) -> None:
    if dotted_name in sys.modules:
        return
    placeholder = types.ModuleType(dotted_name)
    placeholder.__path__ = [str(path)]
    placeholder.__package__ = dotted_name
    sys.modules[dotted_name] = placeholder


_register_namespace_placeholder("custom_components", _REPO_ROOT / "custom_components")
_register_namespace_placeholder("custom_components.smartshading", _INTEGRATION_ROOT)


# ---------------------------------------------------------------------------
# Shared HA stubs — registered BEFORE any test module loads.
# These prevent Category-C contamination where an alphabetically-earlier test
# registers MagicMock() for EntityCategory, dict for DeviceInfo, or a
# featureless OptionsFlow that then poisons later test imports.
# ---------------------------------------------------------------------------

def _stub_mod(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _EntityCategory(enum.Enum):
    """Real enum so .value comparisons work regardless of import order."""
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


class _OptionsFlowStub:
    """Baseline OptionsFlow stub with all methods SmartShadingOptionsFlow calls."""

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, config_entry=None) -> None:
        self._config_entry = config_entry
        self.hass = None

    def async_show_menu(self, *, step_id: str, menu_options,
                        description_placeholders=None) -> dict:
        return {
            "type": "menu",
            "step_id": step_id,
            "menu_options": list(menu_options),
            "description_placeholders": description_placeholders,
        }

    def async_show_form(self, *, step_id: str, data_schema=None, errors=None,
                        description_placeholders=None) -> dict:
        return {"type": "form", "step_id": step_id, "errors": errors or {}, "data_schema": data_schema}

    def async_create_entry(self, *, data: dict) -> dict:
        return {"type": "create_entry", "data": data}

    def async_abort(self, *, reason: str) -> dict:
        return {"type": "abort", "reason": reason}


class _ConfigFlowStub:
    def __class_getitem__(cls, item):
        return cls

    def __init_subclass__(cls, domain: str = "", **kwargs) -> None:
        super().__init_subclass__(**kwargs)


class _CoordEntityBase:
    _attr_has_entity_name: bool = False

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = None
        self._attr_translation_key = None
        self._attr_device_info = None
        self._attr_entity_registry_enabled_default = True

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)


class _DataUpdateCoordinatorBase:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, *args, **kwargs) -> None:
        pass


class _DeviceInfoBase(dict):
    """DeviceInfo that supports both dict-style and attribute-style access."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.__dict__.update(kw)

    @property
    def identifiers(self):
        return self.get("identifiers", set())


def _noop(*args, **kwargs):
    return None


_SHARED_HA_STUBS = {
    "homeassistant": _stub_mod("homeassistant"),
    "homeassistant.components": _stub_mod("homeassistant.components"),
    # entities/button.py imports this at module scope; without a baseline
    # stub here, whichever test file happens to import entities.button first
    # in a given collection order determines whether homeassistant.components.
    # button already exists in sys.modules — a fragile cross-file dependency
    # that broke whenever an unrelated test file was inserted earlier
    # alphabetically. Pre-registering it here (setdefault, so a more specific
    # per-file stub still wins if registered first) removes that dependency.
    "homeassistant.components.button": _stub_mod(
        "homeassistant.components.button", ButtonEntity=object,
    ),
    "homeassistant.const": _stub_mod(
        "homeassistant.const",
        PERCENTAGE="%",
        CONF_NAME="name",
        EntityCategory=_EntityCategory,
    ),
    "homeassistant.core": _stub_mod(
        "homeassistant.core",
        HomeAssistant=object,
        Event=object,
        callback=lambda f: f,
    ),
    "homeassistant.config_entries": _stub_mod(
        "homeassistant.config_entries",
        ConfigEntry=object,
        ConfigFlow=_ConfigFlowStub,
        OptionsFlow=_OptionsFlowStub,
    ),
    "homeassistant.helpers": _stub_mod("homeassistant.helpers"),
    "homeassistant.helpers.device_registry": _stub_mod(
        "homeassistant.helpers.device_registry",
        DeviceInfo=_DeviceInfoBase,
    ),
    "homeassistant.helpers.entity": _stub_mod("homeassistant.helpers.entity"),
    "homeassistant.helpers.entity_platform": _stub_mod(
        "homeassistant.helpers.entity_platform", AddEntitiesCallback=object
    ),
    "homeassistant.helpers.entity_registry": _stub_mod(
        "homeassistant.helpers.entity_registry",
        EntityRegistry=object,
        async_get=lambda hass: None,
    ),
    "homeassistant.helpers.event": _stub_mod(
        "homeassistant.helpers.event",
        async_call_later=_noop,
        async_track_state_change_event=_noop,
        async_track_point_in_time=_noop,
    ),
    "homeassistant.helpers.storage": _stub_mod(
        "homeassistant.helpers.storage",
        Store=type("Store", (object,), {
            "__init__": lambda self, *a, **k: None,
            "async_load": lambda self: None,
            "async_save": lambda self, data: None,
        }),
    ),
    "homeassistant.helpers.update_coordinator": _stub_mod(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=_CoordEntityBase,
        DataUpdateCoordinator=_DataUpdateCoordinatorBase,
    ),
    "homeassistant.util": _stub_mod("homeassistant.util"),
    "homeassistant.util.dt": _stub_mod(
        "homeassistant.util.dt",
        utcnow=_noop,
        as_local=lambda dt: dt,
    ),
}

for _name, _mod in _SHARED_HA_STUBS.items():
    sys.modules.setdefault(_name, _mod)
