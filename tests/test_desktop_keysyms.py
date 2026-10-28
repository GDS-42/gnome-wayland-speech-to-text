"""Unicode → keysym mapping, paste chords and accelerator conversions."""

from __future__ import annotations

import pytest

from greffier.desktop import keysyms
from greffier.desktop.global_shortcuts import accelerator_to_trigger
from greffier.desktop.gnome_keybinding import Accelerator, accelerator_label, command_line


@pytest.mark.parametrize(
    ("char", "expected"),
    [
        ("a", 0x61),
        ("Z", 0x5A),
        (" ", 0x20),
        ("~", 0x7E),
        ("é", 0xE9),  # Latin-1: direct
        ("ÿ", 0xFF),
        (" ", 0xA0),  # no-break space (French typography)
        ("«", 0xAB),
        ("œ", 0x01000153),  # outside Latin-1: 0x01000000 + code point
        ("€", 0x010020AC),
        (" ", 0x0100202F),  # narrow no-break space
        ("’", 0x01002019),
        ("😀", 0x0101F600),
        ("\n", keysyms.KEY_Return),
        ("\t", keysyms.KEY_Tab),
    ],
)
def test_char_to_keysym(char: str, expected: int) -> None:
    assert keysyms.char_to_keysym(char) == expected


@pytest.mark.parametrize("char", ["\x00", "\x07", "\x1b", "\x7f", "\x85", "\x9f"])
def test_control_characters_are_dropped(char: str) -> None:
    assert keysyms.char_to_keysym(char) is None


def test_char_to_keysym_rejects_strings() -> None:
    with pytest.raises(ValueError):
        keysyms.char_to_keysym("ab")


def test_text_to_keysyms_normalises_newlines() -> None:
    assert keysyms.text_to_keysyms("a\r\nb\rc\x07") == [
        0x61, keysyms.KEY_Return, 0x62, keysyms.KEY_Return, 0x63]
    assert keysyms.text_to_keysyms("") == []


def test_paste_chords() -> None:
    assert keysyms.paste_keysyms("ctrl-v") == ((keysyms.KEY_Control_L,), keysyms.KEY_v)
    assert keysyms.paste_keysyms("auto") == keysyms.paste_keysyms("ctrl-v")
    assert keysyms.paste_keysyms("ctrl-shift-v")[0] == (keysyms.KEY_Control_L,
                                                         keysyms.KEY_Shift_L)
    assert keysyms.paste_keysyms("shift-insert") == ((keysyms.KEY_Shift_L,), keysyms.KEY_Insert)
    assert keysyms.paste_evdev("ctrl-shift-v") == ((29, 42), 47)
    assert keysyms.paste_evdev("shift-insert") == ((42,), 110)
    with pytest.raises(ValueError):
        keysyms.paste_keysyms("alt-v")


def test_combo_events_release_in_reverse_order() -> None:
    assert keysyms.combo_events((1, 2), 3) == [
        (1, True), (2, True), (3, True), (3, False), (2, False), (1, False)]


@pytest.mark.parametrize(
    ("layout", "keycodes"),
    [("", False), ("us", False), ("fr", False), ("fr+oss", False), ("de", False),
     ("ru", True), ("ua", True), ("gr", True), ("il", True), ("ara", True),
     ("us+dvorak", False), ("fr+bepo", False), ("rs+latin", False), ("rs", True)],
)
def test_prefer_keycodes(layout: str, keycodes: bool) -> None:
    assert keysyms.prefer_keycodes(layout) is keycodes


# --- GTK accelerator → XDG shortcuts trigger ---------------------------------


@pytest.mark.parametrize(
    ("accel", "trigger"),
    [
        ("<Super><Alt>d", "LOGO+ALT+d"),
        ("<Alt><Super>d", "ALT+LOGO+d"),
        ("<Primary><Shift>Return", "CTRL+SHIFT+Return"),
        ("<Control>F12", "CTRL+F12"),
        ("<Ctrl><Alt>space", "CTRL+ALT+space"),
        ("<Super>D", "LOGO+d"),  # GTK lower-cases letters
        ("<Mod4><Mod1>x", "LOGO+ALT+x"),
        ("<Shift><Shift>a", "SHIFT+a"),
        ("F9", "F9"),
        ("<Super>KP_Add", "LOGO+KP_Add"),
        (" <Super>d ", "LOGO+d"),
    ],
)
def test_accelerator_to_trigger(accel: str, trigger: str) -> None:
    assert accelerator_to_trigger(accel) == trigger


@pytest.mark.parametrize("accel", ["", "   ", "<Super>", "<Hyper>d", "<Super><Alt>", "<Bogus>d",
                                   "<Super>a b"])
def test_accelerator_to_trigger_rejects(accel: str) -> None:
    with pytest.raises(ValueError):
        accelerator_to_trigger(accel)


# --- accelerator parsing used for conflict detection -------------------------


def test_accelerator_parse_and_compare() -> None:
    a = Accelerator.parse("<Super><Alt>d")
    assert a is not None
    assert a == Accelerator.parse("<Alt><Mod4>D")
    assert a != Accelerator.parse("<Super>d")
    assert Accelerator.parse("<Primary>q") == Accelerator.parse("<Control>Q")
    assert Accelerator.parse("") is None
    assert Accelerator.parse("disabled") is None
    assert Accelerator.parse("<Nope>x") is None
    assert a.label() == "Alt+Super+D"
    assert a.to_gtk() == "<Alt><Super>d"
    assert accelerator_label("<Control><Shift>Return") == "Shift+Ctrl+Return"
    assert accelerator_label("garbage<") == "garbage<"


def test_command_line_quotes_and_escapes_percent() -> None:
    argv = ["gdbus", "call", "--session", "--dest", "a.b", "--method", "x.Toggle", "50% off"]
    assert command_line(argv) == "gdbus call --session --dest a.b --method x.Toggle '50%% off'"
