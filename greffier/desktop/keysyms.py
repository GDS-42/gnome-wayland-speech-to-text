"""Unicode → X keysym mapping and the few key codes Greffier sends.

Wayland compositors (and the RemoteDesktop portal) accept *keysyms*: the
compositor looks the symbol up in the active keyboard layout and presses the
matching key, adding Shift/AltGr as needed. The mapping follows
``xkb_utf32_to_keysym()``:

* printable ASCII and Latin-1 (U+0020–U+007E, U+00A0–U+00FF) map directly;
* every other code point maps to ``0x01000000 + code point``;
* ``\\n`` → ``Return``, ``\\t`` → ``Tab``; other control characters are dropped.

A keysym that is absent from the current layout cannot be typed (mutter drops
it with a warning), which is why the default injection method pastes through
the clipboard instead.

The second half of the module holds Linux evdev key codes for ``ydotool`` and
for the portal's ``NotifyKeyboardKeycode``.
"""

from __future__ import annotations

from collections.abc import Iterator

# --- keysyms (X11/keysymdef.h) ---------------------------------------------
KEY_BackSpace = 0xFF08
KEY_Tab = 0xFF09
KEY_Return = 0xFF0D
KEY_Escape = 0xFF1B
KEY_Insert = 0xFF63
KEY_Shift_L = 0xFFE1
KEY_Control_L = 0xFFE3
KEY_Alt_L = 0xFFE9
KEY_Super_L = 0xFFEB
KEY_v = 0x0076

UNICODE_OFFSET = 0x01000000

_SPECIAL: dict[str, int] = {
    "\n": KEY_Return,
    "\r": KEY_Return,
    "\t": KEY_Tab,
}

# --- evdev codes (linux/input-event-codes.h) --------------------------------
EV_KEY_LEFTCTRL = 29
EV_KEY_LEFTSHIFT = 42
EV_KEY_V = 47
EV_KEY_ENTER = 28
EV_KEY_TAB = 15
EV_KEY_INSERT = 110

PASTE_SHORTCUTS = ("ctrl-v", "ctrl-shift-v", "shift-insert")

# shortcut -> (modifier keysyms, key keysym)
_PASTE_KEYSYMS: dict[str, tuple[tuple[int, ...], int]] = {
    "ctrl-v": ((KEY_Control_L,), KEY_v),
    "ctrl-shift-v": ((KEY_Control_L, KEY_Shift_L), KEY_v),
    "shift-insert": ((KEY_Shift_L,), KEY_Insert),
}

# shortcut -> (modifier evdev codes, key evdev code)
_PASTE_EVDEV: dict[str, tuple[tuple[int, ...], int]] = {
    "ctrl-v": ((EV_KEY_LEFTCTRL,), EV_KEY_V),
    "ctrl-shift-v": ((EV_KEY_LEFTCTRL, EV_KEY_LEFTSHIFT), EV_KEY_V),
    "shift-insert": ((EV_KEY_LEFTSHIFT,), EV_KEY_INSERT),
}


def char_to_keysym(char: str) -> int | None:
    """Keysym for one character, or ``None`` if it cannot be typed.

    >>> hex(char_to_keysym("é"))
    '0xe9'
    >>> hex(char_to_keysym("€"))
    '0x10020ac'
    """
    if len(char) != 1:
        raise ValueError(f"expected a single character, got {char!r}")
    special = _SPECIAL.get(char)
    if special is not None:
        return special
    cp = ord(char)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    if cp < 0x20 or 0x7F <= cp <= 0x9F:
        return None  # C0/C1 control characters
    if 0xD800 <= cp <= 0xDFFF:
        return None  # lone surrogates (cannot come from valid UTF-8)
    return UNICODE_OFFSET + cp


def normalize_newlines(text: str) -> str:
    """Turn CRLF and lone CR into LF so each line break is one Return."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def iter_keysyms(text: str) -> Iterator[int]:
    """Keysyms for ``text`` in order; untypable characters are skipped."""
    for char in normalize_newlines(text):
        keysym = char_to_keysym(char)
        if keysym is not None:
            yield keysym


def text_to_keysyms(text: str) -> list[int]:
    """List form of :func:`iter_keysyms`."""
    return list(iter_keysyms(text))


def paste_keysyms(shortcut: str) -> tuple[tuple[int, ...], int]:
    """(modifier keysyms, key keysym) for a paste shortcut name.

    ``auto`` resolves to ``ctrl-v`` (only the extension can see which window
    has focus). Raises ``ValueError`` for an unknown shortcut.
    """
    shortcut = "ctrl-v" if shortcut == "auto" else shortcut
    try:
        return _PASTE_KEYSYMS[shortcut]
    except KeyError:
        raise ValueError(f"unknown paste shortcut {shortcut!r}") from None


def paste_evdev(shortcut: str) -> tuple[tuple[int, ...], int]:
    """(modifier evdev codes, key evdev code) for a paste shortcut name."""
    shortcut = "ctrl-v" if shortcut == "auto" else shortcut
    try:
        return _PASTE_EVDEV[shortcut]
    except KeyError:
        raise ValueError(f"unknown paste shortcut {shortcut!r}") from None


def combo_events(modifiers: tuple[int, ...], key: int) -> list[tuple[int, bool]]:
    """Press/release sequence for a chord: mods down, key down/up, mods up (reversed)."""
    events = [(mod, True) for mod in modifiers]
    events += [(key, True), (key, False)]
    events += [(mod, False) for mod in reversed(modifiers)]
    return events


# Layouts whose base group has no Latin letters: a "v" keysym cannot be found
# there, so the paste chord must be sent as raw key codes instead (apps map
# Ctrl+<key at the V position> to paste on such layouts).
NON_LATIN_LAYOUTS = frozenset({
    "af", "am", "ara", "bd", "bg", "bt", "by", "eg", "et", "ge", "gr", "il",
    "in", "iq", "ir", "kg", "kh", "kz", "la", "lk", "ma", "mk", "mm", "mn",
    "mv", "np", "pk", "rs", "ru", "sy", "th", "tj", "ua", "uz",
})

# Latin layouts where the letter V is *not* on the QWERTY V key: key codes
# would paste with the wrong key there, keysyms are right.
_REMAPPED_VARIANTS = ("dvorak", "colemak", "bepo", "workman", "neo", "bone", "koy",
                      "adnw", "carpalx", "norman", "dvp")


def prefer_keycodes(xkb_layout: str) -> bool:
    """Whether the paste chord should be sent as key codes for this layout.

    ``xkb_layout`` is an input-source id such as ``"fr+oss"``, ``"ru"`` or
    ``"us+dvorak"`` (``""`` when unknown → keysyms).
    """
    if not xkb_layout:
        return False
    base, _, variant = xkb_layout.lower().partition("+")
    if any(v in variant for v in _REMAPPED_VARIANTS):
        return False
    if base == "rs" and "latin" in variant:
        return False
    return base in NON_LATIN_LAYOUTS
