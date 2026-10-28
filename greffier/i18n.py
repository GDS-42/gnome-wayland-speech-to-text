"""Tiny gettext replacement reading .po files directly.

Source strings are English. Translations live in ``greffier/data/locale/<lang>.po``
(standard gettext format, editable with Poedit/GNOME Translation Editor) and
are parsed at start-up, so no ``msgfmt`` compilation step is needed when the
application is installed with pip.

Usage::

    from greffier.i18n import _, ngettext
    label = _("Ready to dictate")
"""

from __future__ import annotations

import ast
import locale
import os
from functools import lru_cache
from pathlib import Path

LOCALE_DIR = Path(__file__).resolve().parent / "data" / "locale"


def _languages() -> list[str]:
    """User languages by priority, e.g. ["fr_FR", "fr"] (GNU LANGUAGE semantics)."""
    for var in ("GREFFIER_LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            break
    else:
        value = locale.getlocale()[0] or ""
    out: list[str] = []
    for item in value.split(":"):
        item = item.split(".")[0].split("@")[0]
        if not item or item in ("C", "POSIX"):
            continue
        out.append(item)
        if "_" in item:
            out.append(item.split("_")[0])
    return out


def _unquote(s: str) -> str:
    return ast.literal_eval(s)  # .po strings use C escapes, a subset of Python's


def parse_po(text: str) -> dict[str, str | list[str]]:
    """Parse a .po file into {msgid: msgstr} (plural entries map to a list).

    Context entries are keyed as "ctxt\\x04msgid" like GNU gettext. Fuzzy and
    untranslated entries are skipped.
    """
    catalog: dict[str, str | list[str]] = {}
    entry: dict[str, str] = {}
    plurals: dict[int, str] = {}
    current: str | None = None
    fuzzy = False

    def flush() -> None:
        nonlocal entry, plurals, current, fuzzy
        msgid = entry.get("msgid")
        if msgid and not fuzzy:
            key = f"{entry['msgctxt']}\x04{msgid}" if "msgctxt" in entry else msgid
            if plurals:
                forms = [plurals[i] for i in sorted(plurals)]
                if all(forms):
                    catalog[key] = forms
            elif entry.get("msgstr"):
                catalog[key] = entry["msgstr"]
        entry, plurals, current, fuzzy = {}, {}, None, False

    def complete() -> bool:
        return "msgstr" in entry or bool(plurals)

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("#"):
            if complete():
                flush()
            if line.startswith("#,") and "fuzzy" in line:
                fuzzy = True
            continue
        if line.startswith('"'):
            value = _unquote(line)
            if current and current.startswith("msgstr["):
                idx = int(current[7:-1])
                plurals[idx] = plurals.get(idx, "") + value
            elif current:
                entry[current] = entry.get(current, "") + value
            continue
        keyword, _, rest = line.partition(" ")
        if keyword in ("msgctxt", "msgid") and complete():
            flush()
        current = keyword
        value = _unquote(rest) if rest else ""
        if keyword.startswith("msgstr["):
            plurals[int(keyword[7:-1])] = value
        else:
            entry[keyword] = value
    flush()
    return catalog


@lru_cache(maxsize=1)
def _catalog() -> tuple[dict[str, str | list[str]], str]:
    for lang in _languages():
        po = LOCALE_DIR / f"{lang}.po"
        if po.is_file():
            try:
                return parse_po(po.read_text(encoding="utf-8")), lang
            except (OSError, ValueError, SyntaxError):
                break
    return {}, "en"


def current_language() -> str:
    """Language actually used for the interface ("en" when untranslated)."""
    return _catalog()[1]


def _(message: str) -> str:
    found = _catalog()[0].get(message)
    return found if isinstance(found, str) else message


def pgettext(context: str, message: str) -> str:
    found = _catalog()[0].get(f"{context}\x04{message}")
    return found if isinstance(found, str) else message


def _plural_index(lang: str, n: int) -> int:
    # French (and pt_BR) treat 0 and 1 as singular; most others only 1.
    if lang.split("_")[0] in ("fr", "pt"):
        return 0 if n <= 1 else 1
    return 0 if n == 1 else 1


def ngettext(singular: str, plural: str, n: int) -> str:
    catalog, lang = _catalog()
    forms = catalog.get(singular)
    if isinstance(forms, list):
        idx = min(_plural_index(lang, n), len(forms) - 1)
        return forms[idx]
    return singular if n == 1 else plural


N_ = lambda message: message  # noqa: E731 - mark for extraction only
