// SPDX-License-Identifier: GPL-3.0-or-later
// io.github.greffier.Greffier.Shell1 (greffier/data/shell-interface.xml),
// exported on the session bus. Only the current owner of the Greffier
// service name may call it: anything else gets AccessDenied.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import {SHELL_API_VERSION, SHELL_IFACE, SHELL_NAME, SHELL_PATH} from './constants.js';

// Keep in sync with greffier/data/shell-interface.xml.
const SHELL_XML = `
<node>
  <interface name="${SHELL_IFACE}">
    <method name="SetClipboard">
      <arg name="text" type="s" direction="in"/>
    </method>
    <method name="GetClipboard">
      <arg name="has_text" type="b" direction="out"/>
      <arg name="text" type="s" direction="out"/>
    </method>
    <method name="Paste">
      <arg name="shortcut" type="s" direction="in"/>
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
const CLIPBOARD_READ_TIMEOUT_MS = 1000;

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
        this._timeouts = new Set();
        this._destroyed = false;
    }

    start() {
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
    _authorize(invocation, handler) {
        const sender = invocation.get_sender();
        const run = () => {
            if (this._destroyed) {
                invocation.return_dbus_error(FAILED, 'Greffier extension disabled');
                return;
            }
            try {
                handler();
            } catch (e) {
                const name = e instanceof RangeError ? INVALID_ARGS : FAILED;
                invocation.return_dbus_error(name, e.message);
            }
        };
        if (sender && sender === this._daemon.owner) {
            run();
            return;
        }
        // The cached owner may lag behind a service that just started:
        // ask the bus before refusing.
        this._daemon.queryOwner(owner => {
            if (sender && owner === sender)
                run();
            else
                invocation.return_dbus_error(ACCESS_DENIED,
                    'Only the Greffier service may use this interface');
        });
    }

    _addTimeout(ms, fn) {
        const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._timeouts.delete(id);
            fn();
            return GLib.SOURCE_REMOVE;
        });
        GLib.Source.set_name_by_id(id, '[greffier] dbus timeout');
        this._timeouts.add(id);
        return id;
    }

    _removeTimeout(id) {
        if (this._timeouts.delete(id))
            GLib.source_remove(id);
    }

    // -- methods -------------------------------------------------------------
    SetClipboardAsync([text], invocation) {
        this._authorize(invocation, () => {
            const clipboard = St.Clipboard.get_default();
            clipboard.set_text(St.ClipboardType.CLIPBOARD, text);
            clipboard.set_text(St.ClipboardType.PRIMARY, text);
            invocation.return_value(null);
        });
    }

    GetClipboardAsync(_params, invocation) {
        this._authorize(invocation, () => {
            let answered = false;
            const reply = value => {
                if (answered)
                    return;
                answered = true;
                this._removeTimeout(timeoutId);
                const hasText = typeof value === 'string';
                invocation.return_value(new GLib.Variant('(bs)', [hasText, hasText ? value : '']));
            };
            // A misbehaving clipboard owner must not leave the caller hanging.
            const timeoutId = this._addTimeout(CLIPBOARD_READ_TIMEOUT_MS, () => reply(null));
            St.Clipboard.get_default().get_text(St.ClipboardType.CLIPBOARD,
                (_clipboard, text) => reply(text ?? null));
        });
    }

    PasteAsync([shortcut], invocation) {
        this._authorize(invocation, () => {
            const used = this._keyboard.paste(shortcut);
            invocation.return_value(new GLib.Variant('(s)', [used]));
        });
    }

    TypeTextAsync([text], invocation) {
        this._authorize(invocation, () => {
            this._keyboard.typeText(text).then(count => {
                invocation.return_value(new GLib.Variant('(u)', [count]));
            }).catch(e => {
                invocation.return_dbus_error(FAILED, e.message);
            });
        });
    }

    GetFocusInfoAsync(_params, invocation) {
        this._authorize(invocation, () => {
            const {wmClass, isTerminal} = this._keyboard.focusInfo();
            invocation.return_value(new GLib.Variant('(sb)', [wmClass, isTerminal]));
        });
    }

    destroy() {
        this._destroyed = true;
        for (const id of this._timeouts)
            GLib.source_remove(id);
        this._timeouts.clear();
        if (this._ownerId) {
            Gio.bus_unown_name(this._ownerId);
            this._ownerId = 0;
        }
        if (this._impl) {
            this._impl.unexport();
            this._impl = null;
        }
        this._keyboard = null;
        this._daemon = null;
    }
}
