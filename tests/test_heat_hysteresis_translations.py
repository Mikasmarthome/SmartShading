"""Translations of the comfort step after the technical thresholds were removed.

The glare minimum exposure and the heat release hysteresis are internal policy
(no longer user settings), so none of the 25 translation files may still carry
their field labels, descriptions or validation errors, while every remaining
comfort control keeps a non-empty label and description in each language.

Also covers the minor wording correction of the Advanced schedule page: the
sun-event descriptions refer to the configured night / morning time, not to a
time field "above" (there is none on that page).
"""
from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_ROOT = _REPO_ROOT / "custom_components" / "smartshading"

_REMOVED_FIELDS = ("glare_min_exposure_wm2", "heat_protection_hysteresis_c")
_REMOVED_ERRORS = ("invalid_glare_min_exposure", "invalid_heat_hysteresis")
_KEPT_COMFORT_FIELDS = (
    "indoor_temperature_sensor_ids",
    "heat_protection_enabled",
    "glare_protection_enabled",
    "solar_gain_enabled",
)


def _all_i18n_files():
    yield _INTEGRATION_ROOT / "strings.json"
    yield from sorted((_INTEGRATION_ROOT / "translations").glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestRemovedComfortStringsAreGone:
    def test_file_count(self) -> None:
        assert len(list(_all_i18n_files())) == 25

    def test_no_file_carries_the_removed_fields_or_errors(self) -> None:
        for path in _all_i18n_files():
            data = _load(path)
            for top in ("config", "options"):
                comfort = data[top]["step"]["comfort"]
                for field in _REMOVED_FIELDS:
                    assert field not in comfort["data"], f"{path.name}: {top} data.{field}"
                    assert field not in comfort["data_description"], f"{path.name}: {top} data_description.{field}"
                for err in _REMOVED_ERRORS:
                    assert err not in data[top]["error"], f"{path.name}: {top} error.{err}"

    def test_remaining_comfort_controls_keep_label_and_description(self) -> None:
        for path in _all_i18n_files():
            data = _load(path)
            for top in ("config", "options"):
                comfort = data[top]["step"]["comfort"]
                for field in _KEPT_COMFORT_FIELDS:
                    label = comfort["data"].get(field)
                    assert label and label.strip(), f"{path.name}: {top} data.{field}"
                    desc = comfort["data_description"].get(field)
                    assert desc and desc.strip(), f"{path.name}: {top} data_description.{field}"

    def test_comfort_step_key_sets_match_english(self) -> None:
        en = _load(_INTEGRATION_ROOT / "strings.json")
        for path in _all_i18n_files():
            data = _load(path)
            for top in ("config", "options"):
                for section in ("data", "data_description"):
                    assert set(data[top]["step"]["comfort"][section]) == set(
                        en[top]["step"]["comfort"][section]
                    ), f"{path.name}: {top} comfort {section}"

    def test_no_removed_key_is_referenced_anywhere_in_the_json(self) -> None:
        for path in _all_i18n_files():
            text = path.read_text(encoding="utf-8")
            for needle in (*_REMOVED_FIELDS, *_REMOVED_ERRORS):
                assert needle not in text, f"{path.name}: {needle}"


class TestAdvancedScheduleSunEventWording:
    def test_english_refers_to_the_configured_time(self) -> None:
        en = _load(_INTEGRATION_ROOT / "strings.json")
        desc = en["options"]["step"]["lifecycle_advanced"]["data_description"]
        assert "replaces the configured night time" in desc["night_sun_event"]
        assert "replaces the configured morning time" in desc["morning_sun_event"]
        for key in ("night_sun_event", "morning_sun_event"):
            assert "above" not in desc[key]

    def test_every_language_changed_and_stays_non_empty(self) -> None:
        for path in _all_i18n_files():
            data = _load(path)
            adv = data["options"]["step"]["lifecycle_advanced"]["data_description"]
            setup = data["config"]["step"]["lifecycle_night"]["data_description"]["night_sun_event"]
            for key in ("night_sun_event", "morning_sun_event"):
                assert adv[key].strip(), f"{path.name}: {key}"
            # the Advanced page no longer shares the setup page wording that
            # points at a time field on the same page
            if path.name not in ("strings.json",):
                assert adv["night_sun_event"] != setup, path.name

    def test_setup_pages_are_untouched(self) -> None:
        en = _load(_INTEGRATION_ROOT / "strings.json")
        for phase in ("night", "morning"):
            text = en["config"]["step"][f"lifecycle_{phase}"]["data_description"][f"{phase}_sun_event"]
            assert f"replaces the {phase} time above" in text
