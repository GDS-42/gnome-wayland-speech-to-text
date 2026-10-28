"""Key presses through ``ydotool`` (uinput), the last-resort keyboard backend.

ydotool needs the ``ydotoold`` daemon (access to ``/dev/uinput``). It emits
raw *key codes*, so:

* chords such as Ctrl+V are reliable on layouts where V is at the QWERTY
  position (QWERTY, AZERTY, QWERTZ…);
* ``ydotool type`` assumes a US layout: accented characters come out wrong on
  most layouts. It is only used when nothing else can type.

Two command-line dialects exist: ydotool ≥ 1.0 (Fedora) takes
``key 29:1 47:1 47:0 29:0``; the 0.1.x series (Debian/Ubuntu) takes
``key ctrl+v``. The dialect is detected once from the command list.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from greffier.desktop import environment, keysyms
from greffier.desktop.environment import CommandResult, run_command

log = logging.getLogger(__name__)

KEY_TIMEOUT_MS = 3000

_LEGACY_COMBOS = {
    "ctrl-v": "ctrl+v",
    "ctrl-shift-v": "ctrl+shift+v",
    "shift-insert": "shift+Insert",
}

KeyCallback = Callable[[bool, str], None]  # (ok, error for logs)


class Ydotool:
    """Asynchronous ydotool client."""

    name = "ydotool"

    def __init__(self, program: str = "ydotool") -> None:
        self._program = program
        self._legacy: bool | None = None  # None until detected
        self._detecting: list[Callable[[], None]] = []

    # -- availability --------------------------------------------------
    def installed(self) -> bool:
        return environment.have_tool(self._program)

    def daemon_running(self) -> bool:
        return environment.ydotoold_socket() is not None

    def available(self) -> bool:
        """ydotool is installed and ydotoold's socket is reachable."""
        return self.installed() and self.daemon_running()

    def _env(self) -> dict[str, str]:
        socket = environment.ydotoold_socket()
        return {"YDOTOOL_SOCKET": str(socket)} if socket is not None else {}

    # -- dialect -------------------------------------------------------
    def _with_dialect(self, then: Callable[[], None]) -> None:
        if self._legacy is not None:
            then()
            return
        self._detecting.append(then)
        if len(self._detecting) > 1:
            return

        def done(result: CommandResult) -> None:
            text = (result.stdout + result.stderr).decode("utf-8", "replace")
            # 0.1.x lists a "recorder" command; 1.x lists "bakers"/"debug".
            self._legacy = "recorder" in text
            log.debug("ydotool dialect: %s", "0.1.x" if self._legacy else "1.x")
            waiting, self._detecting = self._detecting, []
            for fn in waiting:
                fn()

        run_command([self._program], done, capture_stdout=True, capture_stderr=True,
                    timeout_ms=KEY_TIMEOUT_MS)

    @property
    def legacy(self) -> bool | None:
        """True for the 0.1.x syntax, None until detected."""
        return self._legacy

    def key_argv(self, shortcut: str) -> list[str]:
        """Command line for a paste chord in the detected dialect."""
        shortcut = "ctrl-v" if shortcut == "auto" else shortcut
        if self._legacy:
            return [self._program, "key", _LEGACY_COMBOS[shortcut]]
        mods, key = keysyms.paste_evdev(shortcut)
        events = keysyms.combo_events(mods, key)
        return [self._program, "key", *(f"{code}:{1 if down else 0}" for code, down in events)]

    def type_argv(self) -> list[str]:
        """Command line for ``type``; the text itself goes through stdin/argv."""
        if self._legacy:
            return [self._program, "type", "--key-delay", "4"]
        return [self._program, "type", "--key-delay", "4", "--key-hold", "2", "--file", "-"]

    # -- actions -------------------------------------------------------
    def paste(self, shortcut: str, callback: KeyCallback) -> None:
        """Send the paste chord in one ydotool invocation (atomic ordering)."""

        def go() -> None:
            try:
                argv = self.key_argv(shortcut)
            except (KeyError, ValueError) as exc:
                callback(False, str(exc))
                return
            run_command(argv, lambda r: callback(r.ok, r.error or r.stderr_text),
                        capture_stderr=True, timeout_ms=KEY_TIMEOUT_MS, env=self._env())

        self._with_dialect(go)

    def type_text(self, text: str, callback: KeyCallback) -> None:
        """Type ``text`` (US layout assumption!)."""

        def go() -> None:
            argv = self.type_argv()
            timeout = KEY_TIMEOUT_MS + 15 * len(text)
            if self._legacy:
                # 0.1.x cannot read stdin; "--" protects text starting with "-".
                run_command([*argv, "--", text], lambda r: callback(r.ok, r.error or r.stderr_text),
                            capture_stderr=True, timeout_ms=timeout, env=self._env())
            else:
                run_command(argv, lambda r: callback(r.ok, r.error or r.stderr_text),
                            stdin=text.encode("utf-8"), capture_stderr=True,
                            timeout_ms=timeout, env=self._env())

        self._with_dialect(go)
