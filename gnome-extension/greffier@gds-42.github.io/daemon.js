// SPDX-License-Identifier: GPL-3.0-or-later
// Client of the Greffier service (io.github.gds_42.Greffier.Daemon1):
// tracks its bus name owner, its State/ActiveModel properties and its
// StateChanged/Level/Transcribed signals.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {EventEmitter} from 'resource:///org/gnome/shell/misc/signals.js';

import {DAEMON_IFACE, DAEMON_NAME, DAEMON_PATH, State} from './constants.js';

// The subset of daemon-interface.xml the extension uses.
const DAEMON_XML = `
<node>
  <interface name="${DAEMON_IFACE}">
    <method name="Toggle"/>
    <method name="Cancel"/>
    <signal name="StateChanged">
      <arg name="state" type="s"/>
      <arg name="detail" type="s"/>
    </signal>
    <signal name="Level">
      <arg name="level" type="d"/>
    </signal>
    <signal name="Transcribed">
      <arg name="text" type="s"/>
      <arg name="meta" type="s"/>
    </signal>
    <property name="State" type="s" access="read"/>
    <property name="ActiveModel" type="s" access="read"/>
  </interface>
</node>`;

const KNOWN_STATES = new Set(Object.values(State));

/**
 * Signals:
 * - 'changed' (): running/state/detail/activeModel changed
 * - 'level' (level: number)
 * - 'transcribed' (text: string, meta: object)
 */
export class DaemonClient extends EventEmitter {
    constructor() {
        super();
        this._proxy = null;
        this._cancellable = null;
        this._handlerIds = [];
        this.state = '';
        this.detail = '';
        this.activeModel = '';
    }

    start() {
        if (this._cancellable)
            return;
        this._cancellable = new Gio.Cancellable();
        const info = Gio.DBusNodeInfo.new_for_xml(DAEMON_XML).lookup_interface(DAEMON_IFACE);
        Gio.DBusProxy.new(
            Gio.DBus.session,
            // Never start the service just because the shell started.
            Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            info, DAEMON_NAME, DAEMON_PATH, DAEMON_IFACE,
            this._cancellable,
            (source, res) => {
                try {
                    this._proxy = Gio.DBusProxy.new_finish(res);
                } catch (e) {
                    if (!e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                        logError(e, 'Greffier: cannot create the service proxy');
                    return;
                }
                this._handlerIds = [
                    this._proxy.connect('notify::g-name-owner', () => this._sync()),
                    this._proxy.connect('g-properties-changed', () => this._sync()),
                    this._proxy.connect('g-signal', this._onSignal.bind(this)),
                ];
                this._sync();
            });
    }

    /** Unique bus name of the running service, or null. */
    get owner() {
        return this._proxy?.get_name_owner() ?? null;
    }

    get running() {
        return this.owner !== null;
    }

    _sync() {
        if (this.running) {
            const state = this._proxy.get_cached_property('State')?.unpack() ?? '';
            if (state && state !== this.state) {
                this.state = state;
                if (state !== State.ERROR)
                    this.detail = '';
            }
            this.activeModel = this._proxy.get_cached_property('ActiveModel')?.unpack() ?? '';
        } else {
            this.state = '';
            this.detail = '';
            this.activeModel = '';
        }
        this.emit('changed');
    }

    _onSignal(proxy, sender, name, params) {
        switch (name) {
        case 'StateChanged': {
            const [state, detail] = params.deepUnpack();
            if (state) {
                if (!KNOWN_STATES.has(state))
                    console.debug(`Greffier: unknown state ${state}`);
                this.state = state;
                this.detail = detail;
                this.emit('changed');
            }
            break;
        }
        case 'Level': {
            const [level] = params.deepUnpack();
            this.emit('level', Math.max(0, Math.min(1, level)));
            break;
        }
        case 'Transcribed': {
            const [text, metaJson] = params.deepUnpack();
            let meta = {};
            try {
                meta = JSON.parse(metaJson);
            } catch (e) {
                // malformed meta: ignore it
            }
            this.emit('transcribed', text, meta);
            break;
        }
        }
    }

    _call(method) {
        // Through the bus (not the proxy) so that D-Bus activation can start
        // the service when it is not running.
        Gio.DBus.session.call(DAEMON_NAME, DAEMON_PATH, DAEMON_IFACE, method, null, null,
            Gio.DBusCallFlags.NONE, -1, this._cancellable, (conn, res) => {
                try {
                    conn.call_finish(res);
                } catch (e) {
                    if (!e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                        logError(e, `Greffier: ${method} failed`);
                }
            });
    }

    toggle() {
        this._call('Toggle');
    }

    cancel() {
        this._call('Cancel');
    }

    /**
     * Ask the bus who owns the service name right now (callback(owner|null)).
     * Used when the cached owner might lag behind a just-started service.
     *
     * @param {function(string|null): void} callback receives the owner
     */
    queryOwner(callback) {
        Gio.DBus.session.call('org.freedesktop.DBus', '/org/freedesktop/DBus',
            'org.freedesktop.DBus', 'GetNameOwner', new GLib.Variant('(s)', [DAEMON_NAME]),
            new GLib.VariantType('(s)'), Gio.DBusCallFlags.NONE, 1000, this._cancellable,
            (conn, res) => {
                let owner = null;
                try {
                    [owner] = conn.call_finish(res).deepUnpack();
                } catch (e) {
                    owner = null;
                }
                callback(owner);
            });
    }

    destroy() {
        this._cancellable?.cancel();
        this._cancellable = null;
        if (this._proxy) {
            for (const id of this._handlerIds)
                this._proxy.disconnect(id);
        }
        this._handlerIds = [];
        this._proxy = null;
        this.disconnectAll();
    }
}
