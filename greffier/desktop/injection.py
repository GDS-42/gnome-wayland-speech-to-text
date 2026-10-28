"""Text injection into the focused application (ARCHITECTURE §3).

The :class:`Injector` composes a *clipboard* backend (GNOME Shell extension,
then ``wl-copy``) and a *keyboard* backend (extension, then the RemoteDesktop
portal, then ``ydotool``):

* ``paste`` (and ``auto``): optionally save the previous text clipboard, set
  CLIPBOARD+PRIMARY, wait for the clipboard to settle, press the paste
  shortcut, and ~400 ms later restore the previous text — only if the
  clipboard still holds what we pasted;
* ``type``: synthesise the characters (extension → portal → ydotool);
* ``clipboard``: only copy, the user pastes.

Whatever fails, the text is never lost: without a working keyboard backend it
stays in the clipboard (``ok=True`` with an explanatory message).

Injections are serialised: a second dictation waits until the first one is
completely done, including the clipboard restoration.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from gi.repository import Gio, GLib

from greffier.config import InjectionConfig
from greffier.desktop import environment, keysyms, portal
from greffier.desktop.remote_desktop import RemoteDesktopKeyboard
from greffier.desktop.shell_client import ShellClient
from greffier.desktop.wlclipboard import WlClipboard
from greffier.desktop.ydotool import Ydotool
from greffier.i18n import _

log = logging.getLogger(__name__)

RESTORE_DELAY_MS = 400  # time given to the target app to read the clipboard
JOB_TIMEOUT_MS = 20_000  # watchdog: the queue never stalls longer than this
PORTAL_FIRST_USE_TIMEOUT_MS = 120_000  # the consent dialog may stay open a while

KEYBOARD_ORDER = ("extension", "portal", "ydotool")
CLIPBOARD_ORDER = ("extension", "wl-copy")


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #


@dataclass
class InjectResult:
    """Outcome of one injection, reported to the daemon."""

    ok: bool
    method: str  # paste | type | clipboard | none
    keyboard: str  # extension | portal | ydotool | none
    clipboard: str  # extension | wl-copy | none
    message: str = ""  # localised, for the user (empty when all went well)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ClipboardBackend(Protocol):
    """Something that can set and read the text clipboard."""

    name: str
    settle_ms: int

    def available(self) -> bool: ...

    def set_text(self, text: str, callback: Callable[[bool, str], None]) -> None: ...

    def get_text(self, callback: Callable[[bool, str | None], None]) -> None: ...


class KeyboardBackend(Protocol):
    """Something that can press keys in the focused window."""

    name: str

    def available(self) -> bool: ...

    def paste(self, shortcut: str, callback: Callable[[bool, str, str], None]) -> None: ...

    def type_text(self, text: str, callback: Callable[[bool, int, str], None]) -> None: ...


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #


class ExtensionClipboard:
    """Clipboard through the extension (``St.Clipboard``, no focus change)."""

    name = "extension"
    settle_ms = 30

    def __init__(self, shell: ShellClient) -> None:
        self._shell = shell

    def available(self) -> bool:
        return self._shell.available

    def set_text(self, text: str, callback: Callable[[bool, str], None]) -> None:
        self._shell.set_clipboard(text, callback)

    def get_text(self, callback: Callable[[bool, str | None], None]) -> None:
        self._shell.get_clipboard(callback)


class ExtensionKeyboard:
    """Clutter virtual keyboard inside gnome-shell."""

    name = "extension"

    def __init__(self, shell: ShellClient) -> None:
        self._shell = shell

    def available(self) -> bool:
        return self._shell.available

    def paste(self, shortcut: str, callback: Callable[[bool, str, str], None]) -> None:
        self._shell.paste(shortcut, callback)

    def type_text(self, text: str, callback: Callable[[bool, int, str], None]) -> None:
        self._shell.type_text(text, callback)


class YdotoolKeyboard:
    """Adapter giving :class:`Ydotool` the keyboard-backend signatures."""

    name = "ydotool"

    def __init__(self, tool: Ydotool) -> None:
        self._tool = tool

    def available(self) -> bool:
        return self._tool.available()

    def paste(self, shortcut: str, callback: Callable[[bool, str, str], None]) -> None:
        used = "ctrl-v" if shortcut == "auto" else shortcut
        self._tool.paste(used, lambda ok, err: callback(ok, used, err))

    def type_text(self, text: str, callback: Callable[[bool, int, str], None]) -> None:
        count = len(keysyms.text_to_keysyms(text))
        self._tool.type_text(text, lambda ok, err: callback(ok, count if ok else 0, err))


# --------------------------------------------------------------------------- #
# Injector
# --------------------------------------------------------------------------- #


@dataclass
class _Job:
    text: str
    callback: Callable[[InjectResult], None]
    cfg: InjectionConfig
    number: int
    delivered: bool = False
    # previous clipboard text to put back (None: nothing to restore)
    saved: str | None = None
    saved_inherited: bool = False
    clipboard: ClipboardBackend | None = None
    timers: list[int] = field(default_factory=list)


def _with_trailing_space(text: str, cfg: InjectionConfig) -> str:
    if cfg.trailing_space and text and not text[-1].isspace():
        return text + " "
    return text


class Injector:
    """Serialised text injection with clipboard + keyboard backends."""

    restore_delay_ms = RESTORE_DELAY_MS
    job_timeout_ms = JOB_TIMEOUT_MS

    def __init__(
        self,
        bus: Gio.DBusConnection,
        cfg: InjectionConfig,
        *,
        shell: ShellClient | None = None,
        clipboards: list[ClipboardBackend] | None = None,
        keyboards: list[KeyboardBackend] | None = None,
        check_screen_lock: bool = True,
    ) -> None:
        """``bus`` must be the connection that owns the daemon's bus name.

        The keyword arguments replace the real backends (tests); the
        documented two-argument form builds everything itself. Call
        :func:`greffier.desktop.portal.register_host_app` *before* creating
        the Injector: it probes the portals.
        """
        self._bus = bus
        self._cfg = cfg
        self._check_lock = check_screen_lock
        self._queue: deque[_Job] = deque()
        self._current: _Job | None = None
        self._counter = 0
        self._closed = False
        self._idle_id = 0
        self._probe = {
            "gnome_shell": "",
            "extension_info": None,
            "global_shortcuts": None,
            "remote_desktop": None,
            "registry": None,
        }
        self._portal: RemoteDesktopKeyboard | None = None
        if clipboards is None or keyboards is None:
            self._shell = shell or ShellClient(bus, self._on_shell_changed)
            self._portal = RemoteDesktopKeyboard(bus)
            self._wl = WlClipboard()
            self._ydotool = Ydotool()
            self._clipboards: list[ClipboardBackend] = clipboards or [
                ExtensionClipboard(self._shell), self._wl]
            self._keyboards: list[KeyboardBackend] = keyboards or [
                ExtensionKeyboard(self._shell), self._portal, YdotoolKeyboard(self._ydotool)]
            self._owns_shell = shell is None
            self.refresh()
        else:
            self._shell = shell  # type: ignore[assignment]
            self._clipboards = clipboards
            self._keyboards = keyboards
            self._owns_shell = False

    # -- configuration -------------------------------------------------
    @property
    def config(self) -> InjectionConfig:
        return self._cfg

    def update_config(self, cfg: InjectionConfig) -> None:
        """Use ``cfg`` for the next injections (queued ones keep theirs)."""
        self._cfg = cfg

    # -- backend selection ---------------------------------------------
    def _keyboard_candidates(self, cfg: InjectionConfig) -> list[KeyboardBackend]:
        wanted = KEYBOARD_ORDER if cfg.backend == "auto" else (cfg.backend,)
        by_name = {kb.name: kb for kb in self._keyboards}
        return [by_name[n] for n in wanted if n in by_name and by_name[n].available()]

    def _clipboard_candidates(self) -> list[ClipboardBackend]:
        return [cb for cb in self._clipboards if cb.available()]

    @staticmethod
    def _effective_method(cfg: InjectionConfig) -> str:
        return "paste" if cfg.method == "auto" else cfg.method

    def describe(self) -> dict[str, str]:
        """``{"method", "keyboard", "clipboard"}`` that would be used now."""
        keyboards = self._keyboard_candidates(self._cfg)
        clipboards = self._clipboard_candidates()
        method = self._effective_method(self._cfg)
        keyboard = keyboards[0].name if keyboards and method != "clipboard" else "none"
        return {
            "method": method,
            "keyboard": keyboard,
            "clipboard": clipboards[0].name if clipboards else "none",
        }

    # -- diagnostics ---------------------------------------------------
    def _on_shell_changed(self, _available: bool) -> None:
        self.refresh()

    def refresh(self, callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        """Re-probe GNOME Shell and the portals asynchronously.

        :meth:`diagnostics` returns the cached result; ``callback`` receives
        the fresh one once every probe answered.
        """
        pending = {"n": 5}

        def one_done() -> None:
            pending["n"] -= 1
            if pending["n"] == 0 and callback is not None and not self._closed:
                callback(self.diagnostics())

        def setter(key: str) -> Callable[[Any], None]:
            def store(value: Any) -> None:
                self._probe[key] = value
                one_done()

            return store

        environment.gnome_shell_version(self._bus, setter("gnome_shell"))
        environment.extension_info(self._bus, setter("extension_info"))
        portal.get_interface_version(self._bus, portal.GLOBAL_SHORTCUTS_IFACE,
                                     setter("global_shortcuts"))
        portal.get_interface_version(self._bus, portal.REGISTRY_IFACE, setter("registry"))
        if self._portal is not None:
            self._portal.probe(setter("remote_desktop"))
        else:
            portal.get_interface_version(self._bus, portal.REMOTE_DESKTOP_IFACE,
                                         setter("remote_desktop"))

    def _extension_report(self) -> dict[str, Any]:
        info = self._probe["extension_info"] or {}
        running = bool(self._shell is not None and getattr(self._shell, "running", False))
        installed = bool(info) or environment.extension_installed()
        if "enabled" in info:
            enabled = bool(info["enabled"])
        else:
            from_settings = environment.extension_enabled_in_settings()
            enabled = bool(from_settings) if from_settings is not None else running
        state = info.get("state")
        return {
            "installed": installed or running,
            "enabled": enabled or running,
            "active": running,
            "compatible": bool(self._shell is not None and getattr(self._shell, "available",
                                                                     False)),
            "version": getattr(self._shell, "version", None) if running else None,
            "state": int(state) if isinstance(state, (int, float)) else None,
            "error": str(info.get("error", "")) if info else "",
        }

    def diagnostics(self) -> dict[str, Any]:
        """JSON-able report: extension, portals, tools (cached probes)."""

        def version(key: str) -> int | bool:
            value = self._probe[key]
            return int(value) if isinstance(value, int) else False

        return {
            "gnome_shell": self._probe["gnome_shell"] or "",
            "session_type": environment.session_type(),
            "extension": self._extension_report(),
            "portals": {
                "global_shortcuts": version("global_shortcuts"),
                "remote_desktop": version("remote_desktop"),
                "registry": version("registry"),
            },
            "tools": {
                "wl-copy": environment.have_tool("wl-copy"),
                "wl-paste": environment.have_tool("wl-paste"),
                "ydotool": environment.have_tool("ydotool"),
                "ydotoold": environment.ydotoold_socket() is not None,
            },
            "injection": self.describe(),
        }

    # -- queue ---------------------------------------------------------
    def inject(self, text: str, callback: Callable[[InjectResult], None]) -> None:
        """Queue ``text`` for injection; ``callback`` gets one InjectResult."""
        if self._closed:
            raise RuntimeError("Injector is closed")
        self._counter += 1
        cfg = self._cfg
        job = _Job(_with_trailing_space(text, cfg), callback, cfg, self._counter)
        self._queue.append(job)
        log.debug("injection #%d queued (%d chars, method %s)", job.number, len(text), cfg.method)
        self._schedule_pump()

    @property
    def busy(self) -> bool:
        """An injection is running or queued."""
        return self._current is not None or bool(self._queue)

    def _schedule_pump(self) -> None:
        if not self._idle_id and not self._closed:
            self._idle_id = GLib.idle_add(self._pump)

    def _pump(self) -> bool:
        self._idle_id = 0
        if self._current is not None or not self._queue or self._closed:
            return GLib.SOURCE_REMOVE
        job = self._queue.popleft()
        self._current = job
        timeout = self.job_timeout_ms
        if job.cfg.method == "type":
            timeout += 15 * len(job.text)
        if self._portal is not None and self._portal.needs_consent \
                and any(kb.name == "portal" for kb in self._keyboard_candidates(job.cfg)):
            timeout = max(timeout, PORTAL_FIRST_USE_TIMEOUT_MS)
        self._timer(job, timeout, lambda: self._on_watchdog(job))
        self._run(job)
        return GLib.SOURCE_REMOVE

    def _timer(self, job: _Job, ms: int, fn: Callable[[], None]) -> None:
        def fire() -> bool:
            if source_id[0] in job.timers:
                job.timers.remove(source_id[0])
            if self._current is job:
                fn()
            return GLib.SOURCE_REMOVE

        source_id = [GLib.timeout_add(ms, fire)]
        job.timers.append(source_id[0])

    def _guard(self, job: _Job, fn: Callable[..., None]) -> Callable[..., None]:
        """Wrap a backend callback so it is ignored once ``job`` is over."""

        def wrapper(*args: Any) -> None:
            if self._current is job and not self._closed:
                fn(*args)

        return wrapper

    def _on_watchdog(self, job: _Job) -> None:
        log.error("injection #%d timed out", job.number)
        if self._portal is not None:
            # Never press keys later, in whatever window has focus by then.
            self._portal.cancel_pending()
        if job.clipboard is not None:
            self._deliver(job, InjectResult(
                True, "clipboard", "none", job.clipboard.name,
                _("Inserting the text took too long. The text is in the clipboard: press "
                  "Ctrl+V to paste it.")))
        else:
            self._deliver(job, InjectResult(
                False, "none", "none", "none",
                _("Inserting the text took too long and was abandoned.")))
        self._finish(job)

    def _deliver(self, job: _Job, result: InjectResult) -> None:
        if job.delivered:
            return
        job.delivered = True
        level = logging.INFO if result.ok else logging.WARNING
        log.log(level, "injection #%d: ok=%s method=%s keyboard=%s clipboard=%s %s",
                job.number, result.ok, result.method, result.keyboard, result.clipboard,
                result.message)
        try:
            job.callback(result)
        except Exception:  # never let a caller break the queue
            log.exception("injection callback failed")

    def _finish(self, job: _Job) -> None:
        if self._current is not job:
            return
        for source in job.timers:
            GLib.source_remove(source)
        job.timers.clear()
        self._current = None
        self._schedule_pump()

    def _complete(self, job: _Job, result: InjectResult) -> None:
        self._deliver(job, result)
        self._finish(job)

    # -- job ----------------------------------------------------------
    def _run(self, job: _Job) -> None:
        if not job.text:
            self._complete(job, InjectResult(True, "none", "none", "none"))
            return
        method = self._effective_method(job.cfg)
        if method == "clipboard":
            self._run_clipboard_only(job)
            return
        if self._check_lock:
            environment.screen_locked(self._bus, self._guard(job, lambda locked:
                                                             self._after_lock_check(job, locked)))
        else:
            self._after_lock_check(job, False)

    def _after_lock_check(self, job: _Job, locked: bool) -> None:
        if locked:
            log.info("screen locked: not simulating key presses")
            self._copy_only(job, _("The screen is locked."))
            return
        if self._effective_method(job.cfg) == "type":
            self._run_type(job, self._keyboard_candidates(job.cfg))
        else:
            self._run_paste(job)

    # ---- clipboard only
    def _run_clipboard_only(self, job: _Job) -> None:
        def copied(clipboard: ClipboardBackend | None) -> None:
            if clipboard is None:
                self._complete(job, InjectResult(
                    False, "clipboard", "none", "none",
                    _("Could not copy the text: install wl-clipboard or enable the "
                      "Greffier GNOME Shell extension.")))
                return
            self._complete(job, InjectResult(
                True, "clipboard", "none", clipboard.name,
                _("Text copied to the clipboard: press Ctrl+V to paste it.")))

        self._set_clipboard(job, job.text, self._clipboard_candidates(), copied)

    def _set_clipboard(
        self,
        job: _Job,
        text: str,
        candidates: list[ClipboardBackend],
        done: Callable[[ClipboardBackend | None], None],
    ) -> None:
        """Try each clipboard backend in order; ``done(backend or None)``."""
        if not candidates:
            done(None)
            return
        first, rest = candidates[0], candidates[1:]

        def result(ok: bool, error: str = "") -> None:
            if ok:
                done(first)
            else:
                log.warning("clipboard backend %s failed: %s", first.name, error)
                self._set_clipboard(job, text, rest, done)

        first.set_text(text, self._guard(job, result))

    def _copy_only(self, job: _Job, reason: str) -> None:
        """Fallback: leave the text in the clipboard and tell the user."""

        def copied(clipboard: ClipboardBackend | None) -> None:
            if clipboard is None:
                self._complete(job, InjectResult(
                    False, "none", "none", "none",
                    reason + " " + _("The text could not be copied either: install "
                                     "wl-clipboard or enable the Greffier GNOME Shell "
                                     "extension.")))
                return
            self._complete(job, InjectResult(
                True, "clipboard", "none", clipboard.name,
                reason + " " + _("The text is in the clipboard: press Ctrl+V to paste it.")))

        if job.clipboard is not None:
            copied(job.clipboard)  # already copied by the paste attempt
        else:
            self._set_clipboard(job, job.text, self._clipboard_candidates(), copied)

    # ---- paste
    def _run_paste(self, job: _Job) -> None:
        clipboards = self._clipboard_candidates()
        keyboards = self._keyboard_candidates(job.cfg)
        if not clipboards:
            log.info("no clipboard backend: typing instead of pasting")
            self._run_type(job, keyboards,
                           note=_("No clipboard access (install wl-clipboard or the extension): "
                                  "the text was typed instead."))
            return
        if not keyboards:
            self._copy_only(job, self._no_keyboard_reason(job.cfg))
            return

        def after_save(ok: bool, previous: str | None) -> None:
            if not job.saved_inherited:
                job.saved = previous if ok else None
            self._set_clipboard(job, job.text, clipboards, after_set)

        def after_set(clipboard: ClipboardBackend | None) -> None:
            if clipboard is None:
                self._run_type(job, keyboards,
                               note=_("The clipboard could not be set: the text was typed "
                                      "instead."))
                return
            job.clipboard = clipboard
            self._timer(job, clipboard.settle_ms,
                        lambda: self._resolve_shortcut(job, keyboards, send_paste))

        def send_paste(shortcut_for: Callable[[KeyboardBackend], str]) -> None:
            self._try_paste(job, keyboards, shortcut_for)

        if job.cfg.restore_clipboard and not job.saved_inherited:
            clipboards[0].get_text(self._guard(job, after_save))
        else:
            after_save(True, job.saved)

    def _no_keyboard_reason(self, cfg: InjectionConfig) -> str:
        if cfg.backend != "auto":
            return _("The keyboard backend “{name}” is not available.").format(name=cfg.backend)
        return _("No way to simulate key presses was found (enable the Greffier GNOME Shell "
                 "extension, or allow remote desktop access).")

    def _resolve_shortcut(
        self,
        job: _Job,
        keyboards: list[KeyboardBackend],
        then: Callable[[Callable[[KeyboardBackend], str]], None],
    ) -> None:
        """Work out the paste shortcut per backend (``auto`` → terminal check)."""
        configured = job.cfg.paste_shortcut
        if configured != "auto":
            then(lambda _kb: configured)
            return
        needs_focus_info = any(kb.name != "extension" for kb in keyboards)
        shell = self._shell
        if not needs_focus_info or shell is None or not getattr(shell, "available", False):
            then(lambda kb: "auto" if kb.name == "extension" else "ctrl-v")
            return

        def got(ok: bool, wm_class: str, is_terminal: bool) -> None:
            resolved = "ctrl-shift-v" if ok and is_terminal else "ctrl-v"
            log.debug("focused window %r → %s", wm_class, resolved)
            then(lambda kb: "auto" if kb.name == "extension" else resolved)

        shell.get_focus_info(self._guard(job, got))

    def _try_paste(
        self,
        job: _Job,
        keyboards: list[KeyboardBackend],
        shortcut_for: Callable[[KeyboardBackend], str],
        errors: list[str] | None = None,
    ) -> None:
        errors = errors if errors is not None else []
        if not keyboards:
            # Portal messages are user-facing (e.g. "access was refused");
            # the other backends only report technical details for the logs.
            self._copy_only(job, errors[-1] if errors else
                            _("Simulating the paste shortcut failed."))
            return
        keyboard, rest = keyboards[0], keyboards[1:]

        def pasted(ok: bool, used: str, error: str) -> None:
            if not ok:
                log.warning("paste through %s failed: %s", keyboard.name, error)
                if keyboard.name == "portal" and error:
                    errors.append(error)
                self._try_paste(job, rest, shortcut_for, errors)
                return
            assert job.clipboard is not None
            self._deliver(job, InjectResult(True, "paste", keyboard.name, job.clipboard.name))
            log.debug("pasted with %s via %s", used, keyboard.name)
            self._timer(job, self.restore_delay_ms, lambda: self._restore(job))

        keyboard.paste(shortcut_for(keyboard), self._guard(job, pasted))

    def _restore(self, job: _Job) -> None:
        """Put the previous text back if nobody changed the clipboard meanwhile."""
        saved, clipboard = job.saved, job.clipboard
        if saved is None or clipboard is None or saved == job.text:
            self._finish(job)
            return
        nxt = self._queue[0] if self._queue else None
        if (nxt is not None and nxt.cfg.restore_clipboard
                and self._effective_method(nxt.cfg) == "paste"):
            # The next dictation will overwrite the clipboard anyway: hand the
            # original contents over instead of restoring twice.
            nxt.saved, nxt.saved_inherited = saved, True
            self._finish(job)
            return

        def checked(ok: bool, current: str | None) -> None:
            if not ok or current != job.text:
                log.debug("clipboard changed or unreadable: not restoring it")
                self._finish(job)
                return
            clipboard.set_text(saved, self._guard(job, lambda _ok, _err="": self._finish(job)))

        if not clipboard.available():
            self._finish(job)
            return
        clipboard.get_text(self._guard(job, checked))

    # ---- type
    def _run_type(
        self, job: _Job, keyboards: list[KeyboardBackend], note: str = ""
    ) -> None:
        if not keyboards:
            self._copy_only(job, self._no_keyboard_reason(job.cfg))
            return
        keyboard, rest = keyboards[0], keyboards[1:]
        expected = len(keysyms.text_to_keysyms(job.text))

        def typed(ok: bool, count: int, error: str) -> None:
            if not ok:
                log.warning("typing through %s failed: %s", keyboard.name, error)
                self._run_type(job, rest, note)
                return
            message = note
            if keyboard.name == "ydotool":
                message = " ".join(filter(None, [message, _(
                    "Typed with ydotool, which assumes a US keyboard layout: accented "
                    "characters may be wrong.")]))
            if count < expected:
                partial = _("Some characters could not be typed with the current keyboard "
                            "layout; the full text is in the clipboard.")

                def copied(clipboard: ClipboardBackend | None) -> None:
                    self._complete(job, InjectResult(
                        True, "type", keyboard.name, clipboard.name if clipboard else "none",
                        " ".join(filter(None, [message, partial]))))

                self._set_clipboard(job, job.text, self._clipboard_candidates(), copied)
                return
            self._complete(job, InjectResult(True, "type", keyboard.name, "none", message))

        keyboard.type_text(job.text, self._guard(job, typed))

    # -- shutdown ------------------------------------------------------
    def close(self) -> None:
        """Stop everything; queued injections are dropped."""
        if self._closed:
            return
        self._closed = True
        if self._idle_id:
            GLib.source_remove(self._idle_id)
            self._idle_id = 0
        if self._current is not None:
            for source in self._current.timers:
                GLib.source_remove(source)
            self._current.timers.clear()
            self._current = None
        if self._queue:
            log.info("dropping %d queued injection(s)", len(self._queue))
        self._queue.clear()
        if self._portal is not None:
            self._portal.close()
        if self._owns_shell and self._shell is not None:
            self._shell.close()
