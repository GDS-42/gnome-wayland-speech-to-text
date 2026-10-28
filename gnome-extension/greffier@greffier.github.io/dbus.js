// SPDX-License-Identifier: GPL-3.0-or-later
// io.github.greffier.Greffier.Shell1 (greffier/data/shell-interface.xml),
// exported on gnome-shell's own session bus connection (the service checks
// that the owner of our name is also the owner of org.gnome.Shell).
//
// A caller must (1) currently own the Greffier service name and (2) run in
// the service's systemd unit (see trust.js); anything else gets AccessDenied.
// The clipboard can be written, never read, through this interface.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {ERROR_MODIFIERS_HELD, SHELL_API_VERSION, SHELL_IFACE, SHELL_NAME, SHELL_PATH}
    from './constants.js';
import {ModifiersHeldError, Paster} from './paste.js';
import {SenderVerifier} from './trust.js';

// Keep in sync with greffier/data/shell-interface.xml.
const SHELL_XML = `
<node>
  <interface name="${SHELL_IFACE}">
    <method name="SetClipboard">
      <arg name="text" type="s" direction="in"/>
    </method>
    <method name="PasteText">
      <arg name="text" type="s" direction="in"/>
      <arg name="shortcut" type="s" direction="in"/>
      <arg name="restore" type="b" direction="in"/>
      <arg name="used" type="s" direction="out"/>
    </method>
    <method name="TypeText">
      <arg name="text" type="s" direction="in"/>
      <arg name="typed" type="u" direction="out"/>
    </method>
    <method name="GetFocusInfo">
      <arg name="wm_class" type="s" direction="out"/>
      <arg name="is_terminal" type="b" direction="out"/>
    </method>
    <property name="Version" type="u" access="read"/>
  </interface>
</node>`;

const ACCESS_DENIED = 'org.freedesktop.DBus.Error.AccessDenied';
const INVALID_ARGS = 'org.freedesktop.DBus.Error.InvalidArgs';
const FAILED = 'org.freedesktop.DBus.Error.Failed';

function errorName(e) {
    if (e instanceof ModifiersHeldError)
        return ERROR_MODIFIERS_HELD;
    if (e instanceof RangeError)
        return INVALID_ARGS;
    return FAILED;
}

export class ShellService {
    /**
     * @param {import('./keyboard.js').VirtualKeyboard} keyboard key synthesis
     * @param {import('./daemon.js').DaemonClient} daemon the service watcher
     */
    constructor(keyboard, daemon) {
        this._keyboard = keyboard;
        this._daemon = daemon;
        this._impl = null;
        this._ownerId = 0;
        this._verifier = null;
        this._paster = null;
        this._destroyed = false;
    }

    start() {
        this._verifier = new SenderVerifier();
        this._paster = new Paster(this._keyboard);
        this._impl = Gio.DBusExportedObject.wrapJSObject(SHELL_XML, this);
        this._impl.export(Gio.DBus.session, SHELL_PATH);
        this._ownerId = Gio.bus_own_name_on_connection(Gio.DBus.session, SHELL_NAME,
            Gio.BusNameOwnerFlags.NONE, null, null);
    }

    // -- property ------------------------------------------------------------
    get Version() {
        return SHELL_API_VERSION;
    }

    // -- authorization -------------------------------------------------------
    /**
     * Run `handler` (which returns a value, a Promise or undefined) for an
     * authorised caller and send its result with `outSignature`.
     *
     * @param {Gio.DBusMethodInvocation} invocation the call
     * @param {string|null} outSignature reply signature, e.g. '(s)'
     * @param {function(): any} handler the method body
     */
    _serve(invocation, outSignature, handler) {
        const sender = invocation.get_sender();
        const deny = () => invocation.return_dbus_error(ACCESS_DENIED,
            'Only the Greffier service may use this interface');
        const run = async () => {
            if (this._destroyed) {
                invocation.return_dbus_error(FAILED, 'Greffier extension disabled');
                return;
            }
            try {
                const value = await handler();
                invocation.return_value(outSignature ? new GLib.Variant(outSignature, value)
                    : null);
            } catch (e) {
                invocation.return_dbus_error(errorName(e), e.message);
            }
        };
        const checkProcess = () => {
            if (this._destroyed) {
                deny();
                return;
            }
            this._verifier.verify(sender, ok => (ok ? run() : deny()));
        };
        if (!sender) {
            deny();
        } else if (sender === this._daemon?.owner) {
            checkProcess();
        } else {
            // The cached owner may lag behind a service that just started.
            this._daemon?.queryOwner(owner => (owner === sender ? checkProcess() : deny()));
        }
    }

    // -- methods -------------------------------------------------------------
    SetClipboardAsync([text], invocation) {
        this._serve(invocation, null, () => this._paster.setClipboard(text));
    }

    PasteTextAsync([text, shortcut, restore], invocation) {
        this._serve(invocation, '(s)', async () => [
            await this._paster.pasteText(text, shortcut, restore)]);
    }

    TypeTextAsync([text], invocation) {
        this._serve(invocation, '(u)', async () => {
            // Keys typed while the user still holds Super/Alt would be shortcuts.
            await this._paster.waitForModifiers();
            return [await this._keyboard.typeText(text)];
        });
    }

    GetFocusInfoAsync(_params, invocation) {
        this._serve(invocation, '(sb)', () => {
            const {wmClass, isTerminal} = this._keyboard.focusInfo();
            return [wmClass, isTerminal];
        });
    }

    destroy() {
        this._destroyed = true;
        if (this._ownerId) {
            Gio.bus_unown_name(this._ownerId);
            this._ownerId = 0;
        }
        if (this._impl) {
            this._impl.unexport();
            this._impl = null;
        }
        this._paster?.destroy();
        this._paster = null;
        this._verifier?.destroy();
        this._verifier = null;
        this._keyboard = null;
        this._daemon = null;
    }
}
