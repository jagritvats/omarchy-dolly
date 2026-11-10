"""Dolly's strings, Omarchy style.

One catalog for the bar widget/panel (QML `Commons.Translations` reads
`i18n/<code>.json`) and for notifications sent from Python (`t()` here), so a
string is written once. Like `omarchy-i18n`: the language is the code in
`~/.config/omarchy/language` (missing/unknown = English), a key missing from
it falls back to the base language (pt-BR -> pt), then to en.json, then to the
key; `%1`-style placeholders are filled from the arguments, so translations
may reorder them; a plural entry is {"one": ..., "other": ...}.
Terminal output and diagnostics stay English (Omarchy's rule).
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

CATALOG_DIR = Path(__file__).resolve().parents[1] / "i18n"


def language() -> str:
    code = os.environ.get("DOLLY_LANGUAGE")
    if code:
        return code
    try:
        return (Path.home() / ".config/omarchy/language").read_text().strip() or "en"
    except OSError:
        return "en"


@lru_cache(maxsize=8)
def _load(code: str) -> dict:
    try:
        d = json.loads((CATALOG_DIR / f"{code}.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _lookup(key: str, count: int | None = None):
    lang = language()
    for code in (lang, lang.split("-")[0], "en"):
        v = _load(code).get(key)
        if isinstance(v, dict):
            v = v.get("one") if count == 1 and "one" in v else v.get("other")
        if isinstance(v, str) and v:
            return v
    return key


def fill(s: str, *args) -> str:
    # replace %10 before %1
    for i in range(len(args), 0, -1):
        s = s.replace(f"%{i}", str(args[i - 1]))
    return s


def t(key: str, *args) -> str:
    return fill(_lookup(key), *args)


def tn(key: str, count: int, *args) -> str:
    return fill(_lookup(key, count), *(args or (count,)))


def catalog(code: str = "en") -> dict:
    return dict(_load(code))
