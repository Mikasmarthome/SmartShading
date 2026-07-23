"""Translation completeness for the heat protection hysteresis field —
v1.2.0-beta.1, T9.

Same pattern as CFMO-08 / CFLP-14 (test_config_flow_manual_override.py /
test_config_flow_lifecycle_profile.py): iterate strings.json + all 24
translations/*.json and assert the new keys are present with a non-empty
value in both the initial ConfigFlow ("config") and OptionsFlow ("options")
comfort step, plus the new validation error key.
"""
from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_ROOT = _REPO_ROOT / "custom_components" / "smartshading"


def _all_i18n_files():
    yield _INTEGRATION_ROOT / "strings.json"
    yield from sorted((_INTEGRATION_ROOT / "translations").glob("*.json"))


class TestHeatHysteresisTranslationCompleteness:
    def test_every_file_has_the_new_field_and_error_strings(self) -> None:
        files = list(_all_i18n_files())
        assert len(files) == 25
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            for top in ("config", "options"):
                comfort = data[top]["step"]["comfort"]
                label = comfort["data"].get("heat_protection_hysteresis_c")
                desc = comfort["data_description"].get("heat_protection_hysteresis_c")
                err = data[top]["error"].get("invalid_heat_hysteresis")
                assert label and label.strip(), f"{path.name}: missing/empty {top}.step.comfort.data.heat_protection_hysteresis_c"
                assert desc and desc.strip(), f"{path.name}: missing/empty {top}.step.comfort.data_description.heat_protection_hysteresis_c"
                assert err and err.strip(), f"{path.name}: missing/empty {top}.error.invalid_heat_hysteresis"

    def test_no_english_leftovers_in_translations(self) -> None:
        en = json.loads((_INTEGRATION_ROOT / "translations" / "en.json").read_text(encoding="utf-8"))
        en_texts = {
            en["options"]["step"]["comfort"]["data"]["heat_protection_hysteresis_c"],
            en["options"]["error"]["invalid_heat_hysteresis"],
        }
        for path in sorted((_INTEGRATION_ROOT / "translations").glob("*.json")):
            if path.name == "en.json":
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            texts = {
                data["options"]["step"]["comfort"]["data"]["heat_protection_hysteresis_c"],
                data["options"]["error"]["invalid_heat_hysteresis"],
            }
            assert not (texts & en_texts), f"{path.name} has untranslated English text: {texts & en_texts}"

    def test_pre_existing_comfort_strings_unchanged(self) -> None:
        """The insertion must be purely additive — no pre-existing comfort
        step string may have been altered or dropped."""
        strings = json.loads((_INTEGRATION_ROOT / "strings.json").read_text(encoding="utf-8"))
        for top in ("config", "options"):
            comfort = strings[top]["step"]["comfort"]
            assert comfort["data"]["heat_protection_enabled"] == "Enable heat protection"
            assert comfort["data"]["glare_min_exposure_wm2"] == "Minimum exposure for glare protection"
            assert strings[top]["error"]["invalid_glare_min_exposure"] == "Enter a glare minimum exposure between 0 and 500 W/m²."
