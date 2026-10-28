"""Turn the raw transcript of a model into the text that is typed.

Steps, in order:

1. voice commands (« à la ligne », « point d'interrogation », "new line"…),
   see :mod:`greffier.text.commands`;
2. user replacements: whole words, case-insensitive, accents count as letters,
   longest phrase first;
3. whitespace clean-up: single spaces, none before ``,`` and ``.``, none at
   the start/end of lines, line breaks kept;
4. optional French typography: narrow no-break space (U+202F) before ``; ! ?``,
   no-break space (U+00A0) before ``:`` and inside « », never inside URLs,
   times (10:30) or ratios (1:2). Idempotent.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

from greffier.config import Replacement, TextConfig
from greffier.text.commands import apply_commands

NBSP = " "  # no-break space: before ":" and inside « »
NNBSP = " "  # narrow no-break space: before ";", "!" and "?"

_HSPACE = r"[^\S\n]"  # any whitespace except a line break


def normalise_language(language: str | None) -> str:
    """ISO 639-1 code ("fr", "en"…) or "auto" for None/""/"auto"/"fr_FR"→"fr"."""
    if not language:
        return "auto"
    code = language.strip().lower().replace("_", "-").split("-")[0]
    return code or "auto"


def postprocess(text: str, cfg: TextConfig, language: str | None = "fr") -> str:
    """Apply voice commands, replacements, spacing rules and typography.

    ``language`` selects the voice-command table ("fr", "en"; "auto" = both)
    and gates French typography, which applies to French and "auto" only.
    """
    if not text:
        return ""
    lang = normalise_language(language)
    if cfg.voice_commands:
        text = apply_commands(text, lang)
    if cfg.replacements:
        text = apply_replacements(text, cfg.replacements)
    text = normalise_whitespace(text)
    if cfg.french_typography and lang in ("fr", "auto"):
        text = french_typography(text)
    return text


# --------------------------------------------------------------------------- #
# Replacements
# --------------------------------------------------------------------------- #


def _key(phrase: str) -> str:
    return " ".join(phrase.split()).lower()


@lru_cache(maxsize=32)
def _replacement_regex(pairs: tuple[tuple[str, str], ...]) -> tuple[re.Pattern[str], dict]:
    table: dict[str, str] = {}
    for spoken, written in pairs:
        table.setdefault(_key(spoken), written)  # the first definition wins
    keys = sorted(table, key=len, reverse=True)  # longest first
    alternation = "|".join(r"\s+".join(re.escape(word) for word in key.split()) for key in keys)
    # \w is Unicode-aware (é, ç… are letters). A hyphen between letters joins
    # a compound word: "être" must not match inside "peut-être".
    regex = re.compile(rf"(?<!\w)(?<!\w-)(?:{alternation})(?!\w)(?!-\w)", re.IGNORECASE)
    return regex, table


def apply_replacements(text: str, replacements: Iterable[Replacement]) -> str:
    """Replace each ``spoken`` phrase by its ``written`` form (whole words, any case)."""
    pairs = tuple((r.spoken, r.written) for r in replacements if r.spoken.strip())
    if not pairs or not text:
        return text
    regex, table = _replacement_regex(pairs)
    return regex.sub(lambda m: table.get(_key(m.group(0)), m.group(0)), text)


# --------------------------------------------------------------------------- #
# Whitespace
# --------------------------------------------------------------------------- #


def _collapse(match: re.Match[str]) -> str:
    run = match.group(0)
    if NNBSP in run:
        return NNBSP
    if NBSP in run:
        return NBSP
    return " "


def normalise_whitespace(text: str) -> str:
    """Single spaces, no space before ``,``/``.``/``)``, trimmed lines, newlines kept."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(rf"{_HSPACE}+", _collapse, text)
    text = re.sub(rf"{_HSPACE}*\n{_HSPACE}*", "\n", text)
    # "word , next" → "word, next"; ".NET" or " .5" are left alone.
    text = re.sub(rf"{_HSPACE}+(?=[,.…](?:\s|$|[,.;:!?…»)\]\"”']))", "", text)
    text = re.sub(rf"{_HSPACE}+(?=\))", "", text)
    text = re.sub(rf"(?<=\(){_HSPACE}+", "", text)
    return text.strip(" \t" + NBSP + NNBSP)


# --------------------------------------------------------------------------- #
# French typography
# --------------------------------------------------------------------------- #

_MARKS = re.compile(rf"({_HSPACE}*)([;:!?]+)")
_AFTER_MARK_OK = set("»)]\"”'’.,…")
_URLISH = re.compile(r"(?:[a-z][a-z0-9+.-]*:/|www\.|@)", re.IGNORECASE)


def _fix_mark(text: str, match: re.Match[str]) -> str:
    spaces, marks = match.group(1), match.group(2)
    start, end = match.start(), match.end()
    following = text[end:end + 1]
    if following and not following.isspace() and following not in _AFTER_MARK_OK:
        return match.group(0)  # 10:30, 1:2, http://, ?q=1, !=
    preceding = text[:start]
    if not preceding or preceding.endswith("\n"):
        return match.group(0)  # nothing to attach the mark to
    if not spaces:
        token = re.split(r"\s", preceding)[-1]
        if _URLISH.search(token):
            return match.group(0)  # "https://exemple.fr?" may be part of the URL
    return (NBSP if marks[0] == ":" else NNBSP) + marks


def french_typography(text: str) -> str:
    """French spacing rules (see module docstring). Idempotent."""
    text = re.sub(rf"«{_HSPACE}*(?=[^\s»])", "«" + NBSP, text)
    text = re.sub(rf"(?<=[^\s«]){_HSPACE}*»", NBSP + "»", text)
    return _MARKS.sub(lambda m: _fix_mark(text, m), text)
