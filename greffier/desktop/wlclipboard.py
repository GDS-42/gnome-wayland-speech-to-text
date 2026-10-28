"""Clipboard access through ``wl-copy`` / ``wl-paste`` (wl-clipboard).

Fallback for when the GNOME Shell extension is not running. Mutter does not
implement the wlr data-control protocol, so wl-clipboard briefly maps a tiny
surface to obtain keyboard focus; callers must therefore wait a little (see
:attr:`WlClipboard.settle_ms`) before sending the paste shortcut so that focus
is back on the target window.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from greffier.desktop import environment
from greffier.desktop.environment import CommandResult, run_command

log = logging.getLogger(__name__)

TEXT_MIME = "text/plain;charset=utf-8"
COPY_TIMEOUT_MS = 3000
PASTE_TIMEOUT_MS = 1500
# Previous clipboard contents larger than this are not saved for restoring.
MAX_RESTORE_BYTES = 4 * 1024 * 1024

CopyCallback = Callable[[bool, str], None]  # (ok, error message for logs)
PasteCallback = Callable[[bool, "str | None"], None]  # (ok, text or None if not text)


class WlClipboard:
    """Asynchronous wrapper around wl-copy/wl-paste."""

    name = "wl-copy"
    settle_ms = 80  # after wl-copy returned, before pressing the paste shortcut

    def __init__(self, wl_copy: str = "wl-copy", wl_paste: str = "wl-paste") -> None:
        self._wl_copy = wl_copy
        self._wl_paste = wl_paste

    # -- availability --------------------------------------------------
    def available(self) -> bool:
        """wl-copy is installed and we are in a Wayland session."""
        return environment.is_wayland() and environment.have_tool(self._wl_copy)

    def can_read(self) -> bool:
        """wl-paste is installed (it ships with wl-copy)."""
        return environment.is_wayland() and environment.have_tool(self._wl_paste)

    # -- writing -------------------------------------------------------
    def copy(self, text: str, callback: CopyCallback, *, primary: bool = False) -> None:
        """Set CLIPBOARD (or PRIMARY) to ``text``; ``callback(ok, error)``."""
        argv = [self._wl_copy, "--type", TEXT_MIME]
        if primary:
            argv.append("--primary")

        def done(result: CommandResult) -> None:
            if not result.ok:
                log.debug("wl-copy%s failed: %s (status %s)",
                          " --primary" if primary else "", result.error, result.status)
            callback(result.ok, result.error or (f"exit status {result.status}"
                                                 if not result.ok else ""))

        # The data goes through stdin: never on the command line (visible in ps).
        run_command(argv, done, stdin=text.encode("utf-8"), timeout_ms=COPY_TIMEOUT_MS)

    def set_text(self, text: str, callback: CopyCallback, *, primary: bool = True) -> None:
        """Set CLIPBOARD, then PRIMARY (best effort); ``callback`` after both.

        The two copies run one after the other so that no wl-copy surface is
        mapped when the caller sends the paste shortcut.
        """

        def clipboard_done(ok: bool, error: str) -> None:
            if not ok or not primary:
                callback(ok, error)
                return
            self.copy(text, lambda _ok, _err: callback(True, ""), primary=True)

        self.copy(text, clipboard_done)

    # -- reading -------------------------------------------------------
    def get_text(self, callback: PasteCallback) -> None:
        """Read the CLIPBOARD as text: ``callback(ok, text)``.

        ``ok=False`` when wl-paste is missing or failed (unknown state);
        ``ok=True, text=None`` when the clipboard is empty or holds no text,
        or holds text too large/invalid to restore faithfully.
        """
        if not self.can_read():
            callback(False, None)
            return

        def done(result: CommandResult) -> None:
            if result.ok:
                if len(result.stdout) > MAX_RESTORE_BYTES:
                    callback(True, None)
                    return
                try:
                    callback(True, result.stdout.decode("utf-8"))
                except UnicodeDecodeError:
                    callback(True, None)
                return
            if result.status is not None:
                # wl-paste exits 1 for "Nothing is copied" / "No suitable type".
                callback(True, None)
                return
            log.debug("wl-paste failed: %s", result.error)
            callback(False, None)

        run_command(
            [self._wl_paste, "--no-newline", "--type", "text"],
            done,
            capture_stdout=True,
            timeout_ms=PASTE_TIMEOUT_MS,
        )
