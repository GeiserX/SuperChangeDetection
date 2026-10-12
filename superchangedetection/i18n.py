"""Translations: English strings in locales/en.yaml, other languages beside it with the same keys.

The instance picks its language with `locale` in settings.yaml. A key missing from a translation
falls back to English, so a partial translation still works.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

LOCALES = Path(__file__).parent / "locales"


@lru_cache
def _load(locale: str) -> dict:
    path = LOCALES / f"{locale}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


def available() -> list[str]:
    return sorted(p.stem for p in LOCALES.glob("*.yaml"))


class Translator:
    def __init__(self, locale: str = "en"):
        self.locale = locale
        self._strings = {**_load("en"), **_load(locale)}

    def __call__(self, key: str, **kw) -> str:
        s = self._strings.get(key, key)
        return s.format(**kw) if kw and isinstance(s, str) else s

    def raw(self, key: str):
        return self._strings.get(key)
