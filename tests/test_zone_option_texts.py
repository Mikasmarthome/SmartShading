"""Option texts of a zone must not send the user to the legacy System entry.

The Manual Override and Presence pages of a zone are the same step ids for every zone, so
their page descriptions are shown to fully zone-local entries too.  Only the transitional
"Use System default" switch (shown to a zone that has not been converted) may still name the
old System entry; Repairs notices and the legacy entry's abort text are not checked here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
FILES = [ROOT / "strings.json", *sorted((ROOT / "translations").glob("*.json"))]

# "system" in every shipped language (word start only: "assist", "consist" ... do not count).
SYSTEM_WORD = re.compile(
    r"\b(?:syst|sist|систем|σ[υύ]στ[ηή]μ|järjestelm|rendszer)|系统", re.IGNORECASE)

# language: (how the next screen is named, how the OLD System entry is named)
MARKERS = {
    "bg": ("следващия екран", "стария системен запис"),
    "ca": ("pantalla següent", "antiga entrada del sistema"),
    "cs": ("další obrazovce", "starého systémového záznamu"),
    "da": ("næste skærm", "gamle systemindgang"),
    "de": ("nächsten Bildschirm", "alten System-Eintrag"),
    "el": ("επόμενη οθόνη", "παλιά καταχώριση συστήματος"),
    "en": ("next screen", "old System entry"),
    "es": ("siguiente pantalla", "antigua entrada del sistema"),
    "fi": ("seuraavalla näytöllä", "vanhasta järjestelmämerkinnästä"),
    "fr": ("écran suivant", "ancienne entrée système"),
    "hu": ("következő képernyőn", "régi rendszerbejegyzés"),
    "it": ("schermata successiva", "vecchia voce di sistema"),
    "nb": ("neste skjerm", "gamle systeminnføringen"),
    "nl": ("volgende scherm", "oude systeeminvoer"),
    "pl": ("następnym ekranie", "starego wpisu systemowego"),
    "pt": ("ecrã seguinte", "antiga entrada do sistema"),
    "ro": ("ecranul următor", "vechea intrare de sistem"),
    "ru": ("следующем экране", "старой системной записи"),
    "sk": ("ďalšej obrazovke", "starého systémového záznamu"),
    "sl": ("naslednjem zaslonu", "starega sistemskega vnosa"),
    "sv": ("nästa skärm", "gamla systemposten"),
    "tr": ("sonraki ekranda", "eski sistem girişinden"),
    "uk": ("наступному екрані", "старого системного запису"),
    "zh-Hans": ("下一屏幕", "旧的系统条目"),
}

# A generic "the system recognises ..." that has nothing to do with the System entry.
GENERIC_USE = {("hu.json", "override_detection_tolerance")}

TRANSITIONAL = (
    ("manual_override", "override_use_system_default"),
    ("presence", "absence_delay_use_system_default"),
)


def _language(path: Path) -> str:
    return "en" if path.name == "strings.json" else path.stem


def _steps(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["options"]["step"]


def test_every_shipped_language_is_covered() -> None:
    assert {_language(p) for p in FILES} == set(MARKERS)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
class TestZoneOptionTexts:
    @pytest.mark.parametrize("step", ["manual_override", "presence"])
    def test_the_page_description_points_to_this_zone_and_the_next_screen(self, path, step) -> None:
        description = _steps(path)[step]["description"]
        assert not SYSTEM_WORD.search(description), description
        assert MARKERS[_language(path)][0] in description, description

    def test_the_texts_a_zone_local_page_shows_never_name_the_system(self, path) -> None:
        """Everything but the transitional switch, which a zone-local form does not contain."""
        steps = _steps(path)
        for step, transitional in TRANSITIONAL:
            page = steps[step]
            shown = [page["title"], page["description"]]
            for block in ("data", "data_description"):
                shown += [text for key, text in page.get(block, {}).items()
                          if key != transitional and (path.name, key) not in GENERIC_USE]
            for text in shown:
                assert not SYSTEM_WORD.search(text), (step, text)

    @pytest.mark.parametrize(("step", "key"), TRANSITIONAL)
    def test_the_transitional_switch_is_explained_as_a_legacy_leftover(self, path, step, key) -> None:
        text = _steps(path)[step]["data_description"][key]
        next_screen, old_entry = MARKERS[_language(path)]
        assert old_entry in text and next_screen in text, text


def test_english_source_and_en_translation_agree() -> None:
    source, english = _steps(ROOT / "strings.json"), _steps(ROOT / "translations" / "en.json")
    for step, _ in TRANSITIONAL:
        assert source[step] == english[step]


def test_the_english_pages_say_what_follows() -> None:
    steps = _steps(ROOT / "strings.json")
    override = steps["manual_override"]["description"]
    assert "next screen" in override and "release strategy" in override
    assert "absence delay is set on the next screen" in steps["presence"]["description"]
