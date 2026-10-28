"""Keyboard through the RemoteDesktop portal (``org.freedesktop.portal.RemoteDesktop``).

Works without the GNOME Shell extension and without root. The first session
asks the user once; with ``persist_mode=2`` the portal returns a restore token
that we keep in ``$XDG_STATE_HOME/greffier/remote-desktop.token`` so later
sessions start silently.

GNOME shows a "remote access" indicator in the top bar while a session is
open, so the session is closed after a few seconds without key events and
re-created (silently, thanks to the token) for the next dictation.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from gi.repository import Gio, GLib

from greffier import paths
from greffier.desktop import environment, keysyms, portal
from greffier.desktop.portal import (
    PORTAL_BUS_NAME,
    PORTAL_OBJECT_PATH,
    REMOTE_DESKTOP_IFACE,
    PortalResponse,
    PortalSession,
)
from greffier.i18n import _

log = logging.getLogger(__name__)

DEVICE_KEYBOARD = 1
PERSIST_UNTIL_REVOKED = 2
IDLE_CLOSE_MS = 5000
# After Start, give focus time to come back from the consent dialog (if any)
# and mutter time to set up the virtual keyboard.
FRESH_SESSION_DELAY_MS = 150
# After the user refused access, do not show the dialog again for a while.
REFUSAL_BACKOFF_S = 600.0
KEY_STATE_RELEASED = 0
KEY_STATE_PRESSED = 1

ReadyCallback = Callable[[bool, str], None]  # (ok, user-facing message)
KeysCallback = Callable[[bool, int, str], None]  # (ok, events or chars sent, message)

# One key event: (is_keycode, code, pressed)
KeyEvent = tuple[bool, int, bool]


def token_file() -> Path:
    """Where the restore token is persisted."""
    return paths.state_dir() / "remote-desktop.token"


class RemoteDesktopKeyboard:
    """Keyboard-only RemoteDesktop session with idle auto-close."""

    name = "portal"

    def __init__(
        self,
        bus: Gio.DBusConnection,
        *,
        token_path: Path | None = None,
        idle_close_ms: int = IDLE_CLOSE_MS,
    ) -> None:
        self._bus = bus
        self._token_path = token_path
        self._idle_close_ms = idle_close_ms
        self.version: int | None = None
        self.probed = False
        self._session: PortalSession | None = None
        self._started = False
        self._starting = False
        self._waiters: list[ReadyCallback] = []
        self._request: portal.PortalRequest | None = None
        self._idle_id = 0
        self._busy = 0
        self._refused_at: float | None = None
        self._closed = False
        self._fresh = False
        self.fresh_delay_ms = FRESH_SESSION_DELAY_MS

    # -- availability --------------------------------------------------
    def probe(self, callback: Callable[[int | None], None] | None = None) -> None:
        """Read the portal interface version (``None`` = not available)."""

        def done(version: int | None) -> None:
            self.version = version
            self.probed = True
            if callback is not None:
                callback(version)

        portal.get_interface_version(self._bus, REMOTE_DESKTOP_IFACE, done)

    def available(self) -> bool:
        """Portal present and the user did not just refuse access."""
        if self.version is None or self._closed:
            return False
        if self._refused_at is not None:
            if time.monotonic() - self._refused_at < REFUSAL_BACKOFF_S:
                return False
            self._refused_at = None
        return True

    @property
    def session_open(self) -> bool:
        return self._session is not None and self._started

    @property
    def needs_consent(self) -> bool:
        """The next use will probably show the portal's permission dialog."""
        if self.session_open:
            return False
        return (self.version or 0) < 2 or not self._token_file().exists()

    def cancel_pending(self) -> None:
        """Abort a session still being negotiated (e.g. an unanswered dialog)."""
        if self._starting:
            self.close_session()

    # -- restore token -------------------------------------------------
    def _token_file(self) -> Path:
        return self._token_path or token_file()

    def _load_token(self) -> str:
        try:
            return self._token_file().read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _save_token(self, token: str) -> None:
        path = self._token_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rd-token-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("cannot save the remote desktop restore token: %s", exc)

    # -- session lifecycle ---------------------------------------------
    def _ensure_session(self, callback: ReadyCallback) -> None:
        if self._closed:
            callback(False, _("The remote desktop session was closed."))
            return
        if self.session_open:
            callback(True, "")
            return
        self._waiters.append(callback)
        if self._starting:
            return
        self._starting = True
        log.debug("creating a RemoteDesktop session")
        self._request = portal.create_session(
            self._bus, REMOTE_DESKTOP_IFACE, self._on_created, on_closed=self._on_session_closed
        )

    def _fail_start(self, message: str) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        self._started = False
        self._starting = False
        self._request = None
        waiters, self._waiters = self._waiters, []
        for cb in waiters:
            cb(False, message)

    def _response_message(self, response: PortalResponse) -> str:
        if response.cancelled:
            self._refused_at = time.monotonic()
            return _("Remote keyboard access was refused. To allow it, turn on “Allow Remote "
                     "Interaction”, then press “Share” in the dialog.")
        if response.code == portal.RESPONSE_DBUS_ERROR:
            return _("The remote desktop portal failed: {error}").format(error=response.error)
        return _("The remote desktop portal could not start a session.")

    def _on_created(self, session: PortalSession | None, response: PortalResponse) -> None:
        if session is None:
            self._fail_start(self._response_message(response))
            return
        if self._closed:
            session.close()
            return
        self._session = session
        options: dict[str, GLib.Variant] = {"types": GLib.Variant("u", DEVICE_KEYBOARD)}
        if (self.version or 0) >= 2:
            options["persist_mode"] = GLib.Variant("u", PERSIST_UNTIL_REVOKED)
            token = self._load_token()
            if token:
                options["restore_token"] = GLib.Variant("s", token)
        self._request = portal.call_request(
            self._bus, REMOTE_DESKTOP_IFACE, "SelectDevices", "o",
            (session.handle,), options, self._on_selected,
        )

    def _on_selected(self, response: PortalResponse) -> None:
        if self._session is None:
            return
        if not response.ok:
            self._fail_start(self._response_message(response))
            return
        self._request = portal.call_request(
            self._bus, REMOTE_DESKTOP_IFACE, "Start", "os",
            (self._session.handle, ""), {}, self._on_started,
        )

    def _on_started(self, response: PortalResponse) -> None:
        if self._session is None:
            return
        if not response.ok:
            self._fail_start(self._response_message(response))
            return
        devices = int(response.results.get("devices", 0) or 0)
        token = response.results.get("restore_token")
        if isinstance(token, str) and token:
            self._save_token(token)
        if not devices & DEVICE_KEYBOARD:
            self._fail_start(_("Keyboard access was not granted by the remote desktop portal."))
            return
        log.info("RemoteDesktop session started")
        self._started = True
        self._fresh = True
        self._starting = False
        self._request = None
        waiters, self._waiters = self._waiters, []
        for cb in waiters:
            cb(True, "")

    def _on_session_closed(self, session: PortalSession) -> None:
        if session is self._session:
            self._session = None
            self._started = False
            self._cancel_idle()
            if self._starting:
                self._fail_start(_("The remote desktop session was closed."))

    def _cancel_idle(self) -> None:
        if self._idle_id:
            GLib.source_remove(self._idle_id)
            self._idle_id = 0

    def _arm_idle(self) -> None:
        self._cancel_idle()
        if self._busy == 0 and self.session_open:
            self._idle_id = GLib.timeout_add(self._idle_close_ms, self._on_idle)

    def _on_idle(self) -> bool:
        self._idle_id = 0
        log.debug("closing the idle RemoteDesktop session")
        self.close_session()
        return GLib.SOURCE_REMOVE

    def close_session(self) -> None:
        """Close the portal session now (the top-bar indicator goes away)."""
        self._cancel_idle()
        if self._request is not None:
            self._request.cancel()
            self._request = None
        if self._session is not None:
            self._session.close()
            self._session = None
        self._started = False
        if self._starting:
            self._fail_start(_("The remote desktop session was closed."))

    def close(self) -> None:
        """Release everything; the object cannot be used afterwards."""
        self._closed = True
        self.close_session()

    # -- key events ----------------------------------------------------
    def _notify(self, event: KeyEvent, callback: Callable[[bool, str], None]) -> None:
        is_keycode, code, pressed = event
        method = "NotifyKeyboardKeycode" if is_keycode else "NotifyKeyboardKeysym"
        state = KEY_STATE_PRESSED if pressed else KEY_STATE_RELEASED
        assert self._session is not None

        def done(conn: Gio.DBusConnection, res: Gio.AsyncResult) -> None:
            try:
                conn.call_finish(res)
            except GLib.Error as exc:
                callback(False, portal.dbus_error_message(exc))
                return
            callback(True, "")

        self._bus.call(
            PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, REMOTE_DESKTOP_IFACE, method,
            GLib.Variant("(oa{sv}iu)", (self._session.handle, {}, code, state)),
            None, Gio.DBusCallFlags.NONE, portal.DEFAULT_CALL_TIMEOUT_MS, None, done,
        )

    def send_events(self, events: list[KeyEvent], callback: KeysCallback) -> None:
        """Send key events one after the other (the portal may reorder
        concurrent calls). Keys still pressed when an error occurs are released.
        """

        def ready(ok: bool, message: str) -> None:
            if not ok:
                callback(False, 0, message)
                return
            self._busy += 1
            self._cancel_idle()
            pressed: list[KeyEvent] = []
            state = {"i": 0}

            def finish(ok: bool, message: str) -> None:
                self._busy -= 1
                self._arm_idle()
                callback(ok, state["i"], message)

            def release_then_fail(message: str) -> None:
                stuck = [(k, c, False) for k, c, _p in reversed(pressed)]
                pressed.clear()

                def next_release(_ok: bool = True, _msg: str = "") -> None:
                    if stuck and self.session_open:
                        self._notify(stuck.pop(0), next_release)
                    else:
                        finish(False, message)

                next_release()

            def step(ok: bool = True, err: str = "") -> None:
                if not ok:
                    log.warning("portal key event failed: %s", err)
                    failed = events[state["i"] - 1]
                    if failed[2]:
                        pressed.append(failed)  # releasing an unpressed key is harmless
                    release_then_fail(_("The remote desktop portal rejected a key event."))
                    return
                if state["i"] > 0:
                    prev = events[state["i"] - 1]
                    if prev[2]:
                        pressed.append(prev)
                    else:
                        pressed[:] = [e for e in pressed if e[:2] != prev[:2]]
                if state["i"] >= len(events):
                    finish(True, "")
                    return
                if not self.session_open:
                    release_then_fail(_("The remote desktop session was closed."))
                    return
                event = events[state["i"]]
                state["i"] += 1
                self._notify(event, step)

            if self._fresh:
                self._fresh = False
                GLib.timeout_add(self.fresh_delay_ms, lambda: (step(), GLib.SOURCE_REMOVE)[1])
            else:
                step()

        self._ensure_session(ready)

    def paste(self, shortcut: str, callback: Callable[[bool, str, str], None]) -> None:
        """Press the paste chord; ``callback(ok, used shortcut, message)``.

        ``auto`` means ctrl-v here (the portal cannot see the focused window).
        """
        used = "ctrl-v" if shortcut == "auto" else shortcut
        try:
            if keysyms.prefer_keycodes(environment.current_xkb_layout()):
                # evdev codes (NotifyKeyboardKeycode expects evdev, not xkb, codes)
                mods, key = keysyms.paste_evdev(used)
                is_keycode = True
            else:
                mods, key = keysyms.paste_keysyms(used)
                is_keycode = False
        except ValueError as exc:
            callback(False, used, str(exc))
            return
        events = [(is_keycode, code, down) for code, down in keysyms.combo_events(mods, key)]
        self.send_events(events, lambda ok, _n, msg: callback(ok, used, msg))

    def type_text(self, text: str, callback: KeysCallback) -> None:
        """Type ``text`` as keysyms; ``callback(ok, characters sent, message)``.

        Characters missing from the active layout are silently dropped by the
        compositor; the count cannot reflect that.
        """
        syms = keysyms.text_to_keysyms(text)
        events: list[KeyEvent] = []
        for sym in syms:
            events += [(False, sym, True), (False, sym, False)]
        self.send_events(events, lambda ok, n, msg: callback(ok, n // 2, msg))
