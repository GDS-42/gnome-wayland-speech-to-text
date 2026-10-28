// SPDX-License-Identifier: GPL-3.0-or-later
// Who may call Shell1? Owning the service's bus name is not enough (any
// client can take it while the service is not running), so the caller's
// process must also live in the service's systemd unit, read from
// /proc/<pid>/cgroup. Verdicts are cached per unique bus name and dropped
// whenever the service name changes owner.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {DAEMON_NAME, DAEMON_UNIT} from './constants.js';

const escapeRegExp = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// gnome-shell / gnome-session launch of the XDG autostart entry.
const AUTOSTART_SCOPE = new RegExp(`^app-gnome-${escapeRegExp(DAEMON_NAME)}-\\d+\\.scope$`);
// systemd-xdg-autostart-generator unit for the same desktop id.
const AUTOSTART_SERVICE = `app-${DAEMON_NAME}@autostart.service`;

/**
 * Decide from the content of /proc/<pid>/cgroup.
 *
 * Only the systemd-managed hierarchies are considered: the unified one
 * ("0::/…") and, on hybrid systems, "name=systemd".
 *
 * @param {string} text file content
 * @returns {{ok: boolean, path: string}}
 */
export function cgroupAllowed(text) {
    let seen = '';
    for (const line of text.split('\n')) {
        const first = line.indexOf(':');
        const second = line.indexOf(':', first + 1);
        if (first < 0 || second < 0)
            continue;
        const controllers = line.slice(first + 1, second);
        if (controllers !== '' && controllers !== 'name=systemd')
            continue;
        const path = line.slice(second + 1).trim();
        const leaf = path.split('/').pop();
        if (leaf === DAEMON_UNIT || leaf === AUTOSTART_SERVICE || AUTOSTART_SCOPE.test(leaf))
            return {ok: true, path};
        seen ||= path;
    }
    return {ok: false, path: seen};
}

export class SenderVerifier {
    constructor() {
        this._cancellable = new Gio.Cancellable();
        this._verdicts = new Map(); // unique name -> {done, ok, waiters}
        this._warned = new Set();
        this._subscription = Gio.DBus.session.signal_subscribe(
            'org.freedesktop.DBus', 'org.freedesktop.DBus', 'NameOwnerChanged',
            '/org/freedesktop/DBus', DAEMON_NAME, Gio.DBusSignalFlags.NONE,
            () => this._verdicts.clear());
    }

    /**
     * Check that `sender` runs inside the Greffier service unit.
     *
     * @param {string} sender unique bus name
     * @param {function(boolean): void} callback verdict
     */
    verify(sender, callback) {
        const cached = this._verdicts.get(sender);
        if (cached?.done) {
            callback(cached.ok);
            return;
        }
        if (cached) {
            cached.waiters.push(callback);
            return;
        }
        const entry = {done: false, ok: false, waiters: [callback]};
        this._verdicts.set(sender, entry);

        const finish = (ok, detail) => {
            entry.done = true;
            entry.ok = ok;
            if (!ok && !this._warned.has(sender) && !this._cancellable.is_cancelled()) {
                this._warned.add(sender);
                console.warn(`Greffier: refusing Shell1 calls from ${sender} (${detail}): ` +
                    `only the Greffier service running as ${DAEMON_UNIT} (or from its ` +
                    'autostart entry) may use the extension. A service started from a ' +
                    'terminal falls back to the RemoteDesktop portal.');
            }
            const waiters = entry.waiters;
            entry.waiters = [];
            for (const cb of waiters)
                cb(ok);
        };

        Gio.DBus.session.call('org.freedesktop.DBus', '/org/freedesktop/DBus',
            'org.freedesktop.DBus', 'GetConnectionUnixProcessID',
            new GLib.Variant('(s)', [sender]), new GLib.VariantType('(u)'),
            Gio.DBusCallFlags.NONE, 1000, this._cancellable, (conn, res) => {
                let pid;
                try {
                    [pid] = conn.call_finish(res).deepUnpack();
                } catch (e) {
                    finish(false, `no process id: ${e.message}`);
                    return;
                }
                const file = Gio.File.new_for_path(`/proc/${pid}/cgroup`);
                file.load_contents_async(this._cancellable, (f, r) => {
                    let contents;
                    try {
                        [, contents] = f.load_contents_finish(r);
                    } catch (e) {
                        finish(false, `pid ${pid}: ${e.message}`);
                        return;
                    }
                    const {ok, path} = cgroupAllowed(new TextDecoder().decode(contents));
                    finish(ok, `pid ${pid}, cgroup ${path || 'unknown'}`);
                });
            });
    }

    destroy() {
        this._cancellable.cancel();
        if (this._subscription) {
            Gio.DBus.session.signal_unsubscribe(this._subscription);
            this._subscription = 0;
        }
        this._verdicts.clear();
    }
}
