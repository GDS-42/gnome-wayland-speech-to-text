"""Spoken editing commands (« à la ligne », « point d'interrogation », "new line"…).

Speech models punctuate and capitalise what they hear, so a command usually
reaches us wrapped in the punctuation the model inserted at the pauses around
it: ``"Bonjour. À la ligne. Comment ça va ?"``. A command is applied only when
it stands alone; the punctuation that belongs to it is removed and the next
sentence is capitalised after a line break.

Each command declares how strictly its context is checked, which keeps
ordinary sentences intact (``"les deux points communs"``, ``"un point
d'interrogation"``, ``"aller à la ligne suivante"``):

* left ``STRICT``: start of text, line break or punctuation before it;
  ``RELAXED``: same, or a word that is not a determiner/preposition;
  ``ANY``: no check (phrases that never occur in normal prose);
* right ``STRICT``: end of text, line break or punctuation after it;
  ``ANY``: no check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

HSPACE = " \t  "  # horizontal whitespace, including no-break spaces
PUNCT = ".,;:!?…"
WEAK_PUNCT = ",;:"  # pause marks a model may put before a punctuation command
OPENERS = "«(\"“["
CLOSERS = "»)\"”]"


class Kind(Enum):
    MARK = "mark"  # punctuation and/or line breaks
    OPEN = "open"  # « or (
    CLOSE = "close"  # » or )


class Context(Enum):
    STRICT = "strict"
    RELAXED = "relaxed"
    ANY = "any"


@dataclass(frozen=True)
class VoiceCommand:
    pattern: str  # regular expression of the spoken form (case-insensitive)
    kind: Kind
    mark: str = ""  # punctuation or bracket to insert
    breaks: str = ""  # line breaks to insert after the mark
    left: Context = Context.STRICT
    right: Context = Context.STRICT
    spaced: bool = False  # French: a space goes before ; : ! ? (refined by typography)


_S = r"\s+"  # a space between spoken words
_APOS = r"['’]"
_A = "[àa]"  # models sometimes drop the accent of « à »

FR_COMMANDS: tuple[VoiceCommand, ...] = (
    VoiceCommand(rf"point{_S}{_A}{_S}la{_S}ligne", Kind.MARK, ".", "\n", Context.RELAXED),
    VoiceCommand(rf"retour{_S}{_A}{_S}la{_S}ligne", Kind.MARK, "", "\n", Context.RELAXED),
    VoiceCommand(rf"{_A}{_S}la{_S}ligne", Kind.MARK, "", "\n", Context.STRICT),
    VoiceCommand(rf"nouvelle{_S}ligne", Kind.MARK, "", "\n", Context.RELAXED),
    VoiceCommand(rf"nouveau{_S}paragraphe", Kind.MARK, "", "\n\n", Context.RELAXED),
    VoiceCommand(rf"point{_S}d{_APOS}\s*interrogation", Kind.MARK, "?",
                 left=Context.RELAXED, spaced=True),
    VoiceCommand(rf"point{_S}d{_APOS}\s*exclamation", Kind.MARK, "!",
                 left=Context.RELAXED, spaced=True),
    VoiceCommand(r"point[\s-]+virgule", Kind.MARK, ";", left=Context.RELAXED, spaced=True),
    # "deux points" is also "two points": only when it stands alone.
    VoiceCommand(r"deux[\s-]+points", Kind.MARK, ":", spaced=True),
    VoiceCommand(rf"(?:ouvrez|ouvrir|ouvre){_S}les{_S}guillemets", Kind.OPEN, "«",
                 left=Context.ANY, right=Context.ANY),
    VoiceCommand(rf"(?:fermez|fermer|ferme){_S}les{_S}guillemets", Kind.CLOSE, "»",
                 left=Context.ANY, right=Context.ANY),
    VoiceCommand(rf"(?:ouvrez|ouvrir|ouvre){_S}la{_S}parenth[èe]se", Kind.OPEN, "(",
                 left=Context.ANY, right=Context.ANY),
    VoiceCommand(rf"(?:fermez|fermer|ferme){_S}la{_S}parenth[èe]se", Kind.CLOSE, ")",
                 left=Context.ANY, right=Context.ANY),
)

EN_COMMANDS: tuple[VoiceCommand, ...] = (
    VoiceCommand(rf"new{_S}paragraph", Kind.MARK, "", "\n\n", Context.RELAXED),
    VoiceCommand(rf"new{_S}line", Kind.MARK, "", "\n", Context.RELAXED),
)

# Words after which a RELAXED command is ordinary prose ("un point
# d'interrogation", "a new line", "en nouvelle ligne").
FUNCTION_WORDS = frozenset({
    "un", "une", "le", "la", "les", "l", "ce", "cet", "cette", "ces", "du", "des", "de", "d",
    "au", "aux", "mon", "ton", "son", "ma", "ta", "sa", "mes", "tes", "ses", "notre", "votre",
    "nos", "vos", "leur", "leurs", "quel", "quelle", "quels", "quelles", "chaque", "aucun",
    "aucune", "tout", "toute", "certain", "certaine", "plusieurs", "quelques", "autre", "en",
    "à", "a", "dans", "par", "pour", "sur", "sous", "avec", "sans", "vers", "chez", "entre",
    "comme", "petit", "petite", "grand", "grande", "gros", "grosse", "beau", "belle", "bel",
    "vrai", "vraie", "an", "the", "this", "that", "these", "those", "my", "your", "his", "her",
    "its", "our", "their", "each", "every", "another", "any", "some", "no", "in", "on", "at",
    "of", "for", "to", "with", "by", "from", "into", "onto", "as", "big", "small"
})


@dataclass(frozen=True)
class _Table:
    regex: re.Pattern[str]
    commands: tuple[VoiceCommand, ...]  # group i+1 matches commands[i]


@lru_cache(maxsize=8)
def _table(language: str) -> _Table:
    if language == "fr":
        commands = FR_COMMANDS
    elif language == "en":
        commands = EN_COMMANDS
    elif language == "auto":
        commands = FR_COMMANDS + EN_COMMANDS
    else:
        commands = ()
    if not commands:
        return _Table(re.compile(r"(?!x)x"), ())  # matches nothing
    # Groups are tried in declaration order: longer phrases are declared first
    # ("retour à la ligne" before "à la ligne").
    alternation = "|".join(f"({c.pattern})" for c in commands)
    regex = re.compile(rf"(?<![\w-])(?:{alternation})(?![\w-])", re.IGNORECASE)
    return _Table(regex, commands)


def has_commands(language: str) -> bool:
    return bool(_table(language).commands)


def _last_word(text: str) -> str:
    match = re.search(r"(\w+)['’]?$", text)
    return match.group(1).casefold() if match else ""


def _left_ok(out: str, context: Context) -> bool:
    if context is Context.ANY:
        return True
    before = out.rstrip(HSPACE)
    if not before or before[-1] == "\n" or before[-1] in PUNCT + OPENERS + CLOSERS:
        return True
    if context is Context.RELAXED:
        word = _last_word(before)
        return bool(word) and word not in FUNCTION_WORDS
    return False


def _right_ok(rest: str, context: Context) -> bool:
    if context is Context.ANY:
        return True
    after = rest.lstrip(HSPACE)
    return not after or after[0] == "\n" or after[0] in PUNCT + CLOSERS


def _skip(rest: str, chars: str) -> str:
    """Drop leading spaces, then at most one of ``chars``, then spaces."""
    rest = rest.lstrip(HSPACE)
    if rest and rest[0] in chars:
        rest = rest[1:].lstrip(HSPACE)
    return rest


def _capitalize_first(text: str) -> tuple[str, bool]:
    """Upper-case the first letter of ``text``; returns (text, done)."""
    for i, char in enumerate(text):
        if char.isalpha():
            return text[:i] + char.upper() + text[i + 1:], True
        if char.isdigit():
            return text, True
    return text, False


def apply_commands(text: str, language: str = "fr") -> str:
    """Replace the voice commands of ``language`` ("fr", "en" or "auto") in ``text``."""
    table = _table(language)
    if not table.commands or not text:
        return text
    out = ""
    rest = text
    capitalize = False

    def emit(chunk: str) -> None:
        nonlocal out, capitalize
        if capitalize and chunk:
            chunk, done = _capitalize_first(chunk)
            capitalize = not done
        out += chunk

    while True:
        match = table.regex.search(rest)
        if match is None:
            emit(rest)
            return out
        command = table.commands[match.lastindex - 1]  # type: ignore[operator]
        before, after = rest[: match.start()], rest[match.end():]
        if not (_left_ok(out + before, command.left) and _right_ok(after, command.right)):
            emit(rest[: match.end()])
            rest = after
            continue
        emit(before)
        out = out.rstrip(HSPACE)
        if command.kind is Kind.MARK:
            rest, capitalize = _apply_mark(command, after, capitalize)
            out = _mark_output(out, command)
        elif command.kind is Kind.OPEN:
            if out.endswith(","):
                out = out[:-1].rstrip(HSPACE)
            space = " " if out and out[-1] not in "\n" + OPENERS else ""
            out += space + command.mark + (" " if command.mark == "«" else "")
            rest = _skip(after, ",.;:…")
        else:  # Kind.CLOSE
            if out.endswith(","):
                out = out[:-1].rstrip(HSPACE)
            ended = bool(out) and out[-1] in ".!?…"
            out += (" " if command.mark == "»" and out and out[-1] != "\n" else "") + command.mark
            rest = after.lstrip(HSPACE)
            if ended and rest.startswith("."):
                rest = rest[1:].lstrip(HSPACE)
            if rest and (rest[0].isalnum() or rest[0] in OPENERS):
                out += " "


def _mark_output(out: str, command: VoiceCommand) -> str:
    if command.mark:
        # The model's own pause mark is replaced by the dictated one.
        if out and out[-1] in WEAK_PUNCT + ".…":
            out = out[:-1].rstrip(HSPACE)
        if out and out[-1] not in "\n?!":
            out += (" " if command.spaced else "") + command.mark
        else:  # start of a line, or "?!" written together
            out += command.mark
    if command.breaks:
        out = out.rstrip(HSPACE) + command.breaks
    return out


def _apply_mark(command: VoiceCommand, after: str, capitalize: bool) -> tuple[str, bool]:
    """Consume the punctuation that follows a MARK command; returns (rest, capitalize)."""
    rest = _skip(after, PUNCT)
    if command.breaks or command.mark in ("?", "!", "."):
        capitalize = True
    if not command.breaks and rest and rest[0] not in "\n" + PUNCT + CLOSERS:
        rest = " " + rest
    return rest, capitalize
