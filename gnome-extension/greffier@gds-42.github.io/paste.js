// SPDX-License-Identifier: GPL-3.0-or-later
// Paste through the clipboard without ever exposing the clipboard content on
// D-Bus: save the previous text here, set CLIPBOARD+PRIMARY, wait for the
// user's own modifier keys to be released, press the paste chord, and put
// the previous text back once the target application had time to read it.
// Every clipboard operation goes through one promise chain, so a restoration
// can never overwrite the next dictation.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import St from 'gi://St';

import {checkShortcut} from './keyboard.js';

const SETTLE_MS = 30; // let the new selection reach clients
const RESTORE_DELAY_MS = 400; // time given to the application to read it
const CLIPBOARD_READ_TIMEOUT_MS = 1000;
const MODIFIER_POLL_MS = 20;
const MODIFIER_WAIT_MS = 1000;

const HELD_MODIFIERS = Clutter.ModifierType.SHIFT_MASK | Clutter.ModifierType.CONTROL_MASK |
    Clutter.ModifierType.MOD1_MASK | Clutter.ModifierType.SUPER_MASK |
    Clutter.ModifierType.MOD4_MASK | Clutter.ModifierType.HYPER_MASK |
    Clutter.ModifierType.META_MASK;

export class ModifiersHeldError extends Error {
    constructor() {
        super('Modifier keys are still held down');
        this.name = 'ModifiersHeldError';
    }
}

class DisabledError extends Error {}

export class Paster {
    /**
     * @param {import('./keyboard.js').VirtualKeyboard} keyboard key synthesis
     */
    constructor(keyboard) {
        this._keyboard = keyboard;
        this._chain = Promise.resolve();
        this._pending = null; // {text, saved, due, timerId}
        this._sleepers = new Map(); // source id -> reject
        this._destroyed = false;
    }

    // -- helpers ---------------------------------------------------------------
    _sleep(ms) {
        if (this._destroyed)
            return Promise.reject(new DisabledError('disabled'));
        return new Promise((resolve, reject) => {
            const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, Math.max(0, Math.round(ms)), () => {
                this._sleepers.delete(id);
                resolve();
                return GLib.SOURCE_REMOVE;
            });
            GLib.Source.set_name_by_id(id, '[greffier] paste delay');
            this._sleepers.set(id, reject);
        });
    }

    _enqueue(fn) {
        const run = () => {
            if (this._destroyed)
                throw new DisabledError('disabled');
            return fn();
        };
        const result = this._chain.then(run, run);
        this._chain = result.catch(() => {});
        return result;
    }

    _readClipboard() {
        return new Promise(resolve => {
            let done = false;
            const finish = value => {
                if (done)
                    return;
                done = true;
                if (this._sleepers.has(guard)) {
                    this._sleepers.delete(guard);
                    GLib.source_remove(guard);
                }
                resolve(typeof value === 'string' ? value : null);
            };
            // A misbehaving selection owner must not block us.
            const guard = GLib.timeout_add(GLib.PRIORITY_DEFAULT, CLIPBOARD_READ_TIMEOUT_MS, () => {
                this._sleepers.delete(guard);
                finish(null);
                return GLib.SOURCE_REMOVE;
            });
            this._sleepers.set(guard, () => finish(null));
            St.Clipboard.get_default().get_text(St.ClipboardType.CLIPBOARD,
                (_clipboard, text) => finish(text));
        });
    }

    _setClipboard(text, primary = true) {
        const clipboard = St.Clipboard.get_default();
        clipboard.set_text(St.ClipboardType.CLIPBOARD, text);
        if (primary)
            clipboard.set_text(St.ClipboardType.PRIMARY, text);
    }

    _takePending() {
        const pending = this._pending;
        this._pending = null;
        if (pending?.timerId) {
            GLib.source_remove(pending.timerId);
            pending.timerId = 0;
        }
        return pending;
    }

    _schedulePending(text, saved) {
        const pending = {text, saved, due: GLib.get_monotonic_time() + RESTORE_DELAY_MS * 1000};
        pending.timerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, RESTORE_DELAY_MS, () => {
            pending.timerId = 0;
            if (this._pending === pending) {
                this._pending = null;
                this._enqueue(() => this._restore(pending)).catch(() => {});
            }
            return GLib.SOURCE_REMOVE;
        });
        GLib.Source.set_name_by_id(pending.timerId, '[greffier] clipboard restore');
        this._pending = pending;
    }

    async _restore(pending) {
        if (pending.saved === null)
            return;
        const current = await this._readClipboard();
        if (current === pending.text)
            this._setClipboard(pending.saved, false); // never touch a changed clipboard
    }

    /**
     * Wait until the user released every modifier (they may still hold the
     * dictation shortcut), at most MODIFIER_WAIT_MS.
     */
    async waitForModifiers() {
        const deadline = GLib.get_monotonic_time() + MODIFIER_WAIT_MS * 1000;
        for (;;) {
            const [, , mods] = global.get_pointer();
            if ((mods & HELD_MODIFIERS) === 0)
                return;
            if (GLib.get_monotonic_time() >= deadline)
                throw new ModifiersHeldError();
            // eslint-disable-next-line no-await-in-loop
            await this._sleep(MODIFIER_POLL_MS);
        }
    }

    // -- public ------------------------------------------------------------------
    /**
     * @param {string} text the text to paste
     * @param {string} shortcut auto | ctrl-v | ctrl-shift-v | shift-insert
     * @param {boolean} restore put the previous clipboard text back
     * @returns {Promise<string>} the shortcut used
     */
    pasteText(text, shortcut, restore) {
        checkShortcut(shortcut); // InvalidArgs before anything happens
        return this._enqueue(async () => {
            let saved = null;
            const pending = this._takePending();
            if (pending) {
                // Let the application read the previous dictation first.
                const wait = (pending.due - GLib.get_monotonic_time()) / 1000;
                if (wait > 0)
                    await this._sleep(wait);
                if (restore) {
                    // Still ours: the original text stays the one to restore;
                    // changed meanwhile: the user's new copy is.
                    const current = await this._readClipboard();
                    saved = current === pending.text ? pending.saved : current;
                }
            } else if (restore) {
                saved = await this._readClipboard();
            }

            this._setClipboard(text);
            await this._sleep(SETTLE_MS);
            await this.waitForModifiers(); // on failure the text stays in the clipboard
            if (this._destroyed)
                throw new DisabledError('disabled');
            const used = this._keyboard.paste(shortcut);
            this._schedulePending(text, saved !== null && saved !== text ? saved : null);
            return used;
        });
    }

    /**
     * Copy-only mode: set the clipboard, cancelling any pending restoration.
     *
     * @param {string} text the text
     * @returns {Promise<void>}
     */
    setClipboard(text) {
        return this._enqueue(() => {
            this._takePending();
            this._setClipboard(text);
        });
    }

    destroy() {
        this._destroyed = true;
        this._takePending();
        const sleepers = [...this._sleepers];
        this._sleepers.clear();
        for (const [id, reject] of sleepers) {
            GLib.source_remove(id);
            reject(new DisabledError('disabled'));
        }
        this._keyboard = null;
    }
}
