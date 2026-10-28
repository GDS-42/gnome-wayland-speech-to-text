"""environment.py, wlclipboard.py and ydotool.py with fake executables on PATH."""

from __future__ import annotations

import os
import socket
import stat
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "desktop"))
from glibtest import run_until  # noqa: E402

from greffier.const import EXTENSION_UUID  # noqa: E402
from greffier.desktop import environment  # noqa: E402
from greffier.desktop.environment import CommandResult, run_command  # noqa: E402
from greffier.desktop.wlclipboard import WlClipboard  # noqa: E402
from greffier.desktop.ydotool import Ydotool  # noqa: E402

WL_COPY = """#!/bin/sh
target=clipboard
for a in "$@"; do [ "$a" = "--primary" ] && target=primary; done
echo "$*" >> "$FAKE_DIR/wl-copy.argv"
cat > "$FAKE_DIR/$target"
# like the real wl-copy: a background child keeps serving (and inherits fds)
( sleep 3 ) &
exit 0
"""

WL_PASTE = """#!/bin/sh
echo "$*" >> "$FAKE_DIR/wl-paste.argv"
f="$FAKE_DIR/clipboard"
[ -f "$f" ] || { echo "Nothing is copied" >&2; exit 1; }
cat "$f"
"""

YDOTOOL = """#!/bin/sh
echo "$*" >> "$FAKE_DIR/ydotool.argv"
if [ $# -eq 0 ]; then
  if [ -n "$FAKE_LEGACY" ]; then
    printf 'Usage: ydotool <cmd> <args>\\nAvailable commands:\\n  type\\n  recorder\\n  key\\n'
  else
    printf 'Usage: ydotool <cmd> <args>\\nAvailable commands:\\n'
    printf '  click\\n  type\\n  key\\n  bakers\\n'
  fi
  exit 0
fi
[ "$1" = "type" ] && cat > "$FAKE_DIR/ydotool.stdin"
echo "$YDOTOOL_SOCKET" > "$FAKE_DIR/ydotool.socket"
exit 0
"""


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("wl-copy", WL_COPY), ("wl-paste", WL_PASTE), ("ydotool", YDOTOOL)):
        script = bindir / name
        script.write_text(body)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    return tmp_path


@pytest.fixture
def ydotoold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "ydotool.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))
    monkeypatch.setenv("YDOTOOL_SOCKET", str(path))
    yield path
    sock.close()


def run(fn, *args, **kwargs) -> tuple:
    got: list[tuple] = []
    fn(*args, lambda *a: got.append(a), **kwargs)
    assert run_until(lambda: got, 5)
    return got[0]


# --- environment ------------------------------------------------------------


@pytest.mark.parametrize(("env", "expected"), [
    ({"XDG_SESSION_TYPE": "wayland"}, "wayland"),
    ({"XDG_SESSION_TYPE": "X11"}, "x11"),
    ({"WAYLAND_DISPLAY": "wayland-0"}, "wayland"),
    ({"DISPLAY": ":0"}, "x11"),
    ({}, "unknown"),
])
def test_session_type(monkeypatch, env: dict[str, str], expected: str) -> None:
    for var in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY"):
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert environment.session_type() == expected


def test_ydotoold_socket_detection(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("YDOTOOL_SOCKET", str(tmp_path / "missing"))
    if not Path("/tmp/.ydotool_socket").exists():
        assert environment.ydotoold_socket() is None
    plain = tmp_path / "plain"
    plain.write_text("")
    monkeypatch.setenv("YDOTOOL_SOCKET", str(plain))  # not a socket
    assert environment.ydotoold_socket() != plain
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    (tmp_path / "run").mkdir()
    sock.bind(str(tmp_path / "run" / ".ydotool_socket"))
    try:
        assert environment.ydotoold_socket() == tmp_path / "run" / ".ydotool_socket"
    finally:
        sock.close()


def test_extension_installed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "none"))
    assert not environment.extension_installed()
    ext = tmp_path / "gnome-shell" / "extensions" / EXTENSION_UUID
    ext.mkdir(parents=True)
    (ext / "metadata.json").write_text("{}")
    assert environment.extension_installed()


def test_shell_major() -> None:
    assert environment.shell_major("48.2") == 48
    assert environment.shell_major("50.alpha") == 50
    assert environment.shell_major("") == 0


def test_run_command_variants() -> None:
    ok = run(run_command, ["sh", "-c", "printf 'héllo'; echo err >&2"],
             capture_stdout=True, capture_stderr=True)[0]
    assert isinstance(ok, CommandResult)
    assert ok.ok and ok.stdout.decode() == "héllo" and ok.stderr_text == "err"
    echoed = run(run_command, ["cat"], stdin="données".encode(), capture_stdout=True)[0]
    assert echoed.stdout.decode() == "données"
    failed = run(run_command, ["sh", "-c", "exit 3"])[0]
    assert not failed.ok and failed.status == 3
    missing = run(run_command, ["/nonexistent/tool"])[0]
    assert not missing.ok and missing.status is None and missing.error
    start = time.monotonic()
    slow = run(run_command, ["sleep", "5"], timeout_ms=150)[0]
    assert not slow.ok and slow.error == "timeout"
    assert time.monotonic() - start < 2


# --- wl-clipboard ------------------------------------------------------------


def test_wl_copy_sets_clipboard_and_primary(fake_bin: Path) -> None:
    clip = WlClipboard()
    assert clip.available() and clip.can_read()
    start = time.monotonic()
    assert run(clip.set_text, "« Bonjour » 😀") == (True, "")
    # wl-copy's background child must not keep us waiting
    assert time.monotonic() - start < 2
    assert (fake_bin / "clipboard").read_text() == "« Bonjour » 😀"
    assert (fake_bin / "primary").read_text() == "« Bonjour » 😀"
    argv = (fake_bin / "wl-copy.argv").read_text().splitlines()
    assert argv == ["--type text/plain;charset=utf-8",
                    "--type text/plain;charset=utf-8 --primary"]


def test_wl_paste_reads_text(fake_bin: Path) -> None:
    clip = WlClipboard()
    assert run(clip.get_text) == (True, None)  # empty clipboard: nothing to restore
    (fake_bin / "clipboard").write_text("ancien\n")
    assert run(clip.get_text) == (True, "ancien\n")  # exact bytes, no newline added
    assert (fake_bin / "wl-paste.argv").read_text().splitlines()[-1] == "--no-newline --type text"
    (fake_bin / "clipboard").write_bytes(b"\xff\xfe binary")
    assert run(clip.get_text) == (True, None)


def test_wl_clipboard_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    clip = WlClipboard()
    assert not clip.available()
    assert run(clip.get_text) == (False, None)
    ok, err = run(clip.set_text, "x")
    assert not ok and err


def test_wl_clipboard_needs_wayland(fake_bin, monkeypatch) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert not WlClipboard().available()


# --- ydotool -----------------------------------------------------------------


def test_ydotool_needs_daemon(fake_bin, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("YDOTOOL_SOCKET", str(tmp_path / "nope"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    tool = Ydotool()
    assert tool.installed()
    if not Path("/tmp/.ydotool_socket").exists():
        assert not tool.available()


def test_ydotool_modern_dialect(fake_bin, ydotoold) -> None:
    tool = Ydotool()
    assert tool.available()
    assert run(tool.paste, "ctrl-shift-v") == (True, "")
    assert tool.legacy is False
    argv = (fake_bin / "ydotool.argv").read_text().splitlines()
    assert argv == ["", "key 29:1 42:1 47:1 47:0 42:0 29:0"]
    assert (fake_bin / "ydotool.socket").read_text().strip() == str(ydotoold)
    assert run(tool.type_text, "-dash\nnext") == (True, "")
    assert (fake_bin / "ydotool.stdin").read_text() == "-dash\nnext"
    assert (fake_bin / "ydotool.argv").read_text().splitlines()[-1] == (
        "type --key-delay 4 --key-hold 2 --file -")


def test_ydotool_legacy_dialect(fake_bin, ydotoold, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_LEGACY", "1")
    tool = Ydotool()
    assert run(tool.paste, "auto") == (True, "")
    assert tool.legacy is True
    assert run(tool.paste, "shift-insert") == (True, "")
    assert run(tool.type_text, "abc") == (True, "")
    argv = (fake_bin / "ydotool.argv").read_text().splitlines()
    assert argv == ["", "key ctrl+v", "key shift+Insert", "type --key-delay 4 -- abc"]


def test_ydotoold_socket_of_another_user_is_ignored(tmp_path, monkeypatch) -> None:
    path = tmp_path / "foreign.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))
    try:
        monkeypatch.setenv("YDOTOOL_SOCKET", str(path))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
        assert environment.ydotoold_socket() == path
        real_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: real_uid + 12345)
        if real_uid != 0:  # root-owned sockets are always trusted
            assert environment.ydotoold_socket() != path
        else:
            os.chown(path, 4242, 4242)
            assert environment.ydotoold_socket() != path
    finally:
        sock.close()
