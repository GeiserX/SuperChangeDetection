"""The shared assistant principles: shipped with the app, unchanged, and read first, once."""

import hashlib

import pytest

from superchangedetection import context
from superchangedetection.i18n import Translator
from superchangedetection.model import Settings

# The shared assistant principles, byte for byte. A change to them lands here on purpose, never by
# accident: copy the new text into superchangedetection/principles/ and update the hash.
SHA256 = {
    "en": "20c8da6b8f6fb7456d126bfe5fb4516261299e10b7556aed1849e243f43856ac",
    "es": "49e40b0b7108c578813ee343cb93edc687ad0721646a6e219ce7845547ceb5ca",
}
HEADING = {"en": "## Principles", "es": "## Principios"}


def rules(lang: str, instructions: str = "") -> str:
    s = Settings.model_validate({"locale": lang, "llm": {"instructions": instructions}})
    return context.rules(s, Translator(lang))


@pytest.mark.parametrize("lang", sorted(SHA256))
def test_the_shipped_principles_are_the_shared_text(lang):
    raw = (context.PRINCIPLES / f"{lang}.md").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SHA256[lang]


@pytest.mark.parametrize("lang", sorted(SHA256))
def test_the_rules_start_with_the_twelve_principles_exactly_once(lang):
    out = rules(lang, "Be brief.")
    text = (context.PRINCIPLES / f"{lang}.md").read_text(encoding="utf-8").rstrip()
    assert text.count("**") == 24  # twelve bold titles
    assert out.startswith(text + "\n")
    assert out.count(HEADING[lang]) == 1 and out.count(text) == 1
    assert out.index("# You are the watch assistant") > len(text)
    assert out.rstrip().endswith("Be brief.")


def test_a_language_without_a_copy_reads_the_english_principles():
    assert context.rules(Settings(), Translator("de")).startswith(HEADING["en"])
    assert context.rules(Settings(), Translator("es-ES")).startswith(HEADING["es"])


def test_the_apps_rules_no_longer_repeat_what_the_principles_say():
    own = rules("en").split("# You are the watch assistant", 1)[1]
    own = " ".join(own.split()).lower()  # a reflowed or recased sentence is the same sentence
    for gone in (
        "People reach you by mentioning",
        "Act only on what is addressed to you",
        "is not a request",
        "full spelling",
        "no notes about files or tools",
        "answer all of them",
        "say what changes",
        "without an answer nothing is done",
        "A label that declines",
    ):
        assert gone.lower() not in own, gone
