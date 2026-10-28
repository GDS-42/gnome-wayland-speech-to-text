// SPDX-License-Identifier: GPL-3.0-or-later
// Key synthesis inside gnome-shell: a Clutter virtual keyboard for chords and
// key-by-key typing, the input method for whole strings, and focused-window
// inspection (terminals paste with Ctrl+Shift+V).

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as KeyboardStatus from 'resource:///org/gnome/shell/ui/status/keyboard.js';

// Linux evdev codes (linux/input-event-codes.h).
const EV_KEY_LEFTCTRL = 29;
const EV_KEY_LEFTSHIFT = 42;
const EV_KEY_V = 47;
const EV_KEY_INSERT = 110;

const UNICODE_KEYSYM_FLAG = 0x01000000;

// Keys sent per main-loop iteration while typing, and the pause between batches.
const TYPE_BATCH = 8;
const TYPE_INTERVAL_MS = 4;

const PASTE_CHORDS = {
    'ctrl-v': {mods: [Clutter.KEY_Control_L], key: Clutter.KEY_v,
        modCodes: [EV_KEY_LEFTCTRL], keyCode: EV_KEY_V},
    'ctrl-shift-v': {mods: [Clutter.KEY_Control_L, Clutter.KEY_Shift_L], key: Clutter.KEY_v,
        modCodes: [EV_KEY_LEFTCTRL, EV_KEY_LEFTSHIFT], keyCode: EV_KEY_V},
    'shift-insert': {mods: [Clutter.KEY_Shift_L], key: Clutter.KEY_Insert,
        modCodes: [EV_KEY_LEFTSHIFT], keyCode: EV_KEY_INSERT},
};

// Lower-case wm_class / application ids that identify terminal emulators.
const TERMINALS = new Set([
    'org.gnome.ptyxis', 'org.gnome.ptyxis.devel', 'ptyxis',
    'org.gnome.console', 'kgx',
    'gnome-terminal', 'gnome-terminal-server', 'org.gnome.terminal',
    'kitty', 'alacritty', 'org.alacritty', 'foot', 'footclient',
    'wezterm', 'org.wezfurlong.wezterm', 'wezterm-gui',
    'konsole', 'org.kde.konsole', 'yakuake', 'org.kde.yakuake',
    'tilix', 'com.gexperts.tilix', 'terminator', 'xterm', 'uxterm', 'urxvt', 'rxvt',
    'com.mitchellh.ghostty', 'ghostty', 'com.raggesilver.blackbox', 'blackbox',
    'io.elementary.terminal', 'xfce4-terminal', 'mate-terminal', 'lxterminal',
    'qterminal', 'sakura', 'st', 'st-256color', 'guake', 'tilda', 'cool-retro-term',
    'contour', 'org.contourterminal.contour', 'rio', 'com.raggesilver.terminal',
    'app.devsuite.ptyxis', 'org.codeberg.dnkl.foot', 'warp', 'dev.warp.warp',
    'hyper', 'tabby', 'terminology', 'deepin-terminal',
]);

// xkb layouts with no Latin letters: the "v" keysym is absent, so the paste
// chord goes out as raw key codes (apps map Ctrl+<V position> to paste).
const NON_LATIN_LAYOUTS = new Set([
    'af', 'am', 'ara', 'bd', 'bg', 'bt', 'by', 'eg', 'et', 'ge', 'gr', 'il', 'in', 'iq',
    'ir', 'kg', 'kh', 'kz', 'la', 'lk', 'ma', 'mk', 'mm', 'mn', 'mv', 'np', 'pk', 'rs',
    'ru', 'sy', 'th', 'tj', 'ua', 'uz',
]);
const REMAPPED_VARIANTS = ['dvorak', 'colemak', 'bepo', 'workman', 'neo', 'bone', 'koy',
    'adnw', 'carpalx', 'norman', 'dvp'];

function normalizeId(id) {
    return (id ?? '').toLowerCase().replace(/\.desktop$/, '');
}

function preferKeycodes() {
    let xkbId = '';
    try {
        xkbId = KeyboardStatus.getInputSourceManager()?.currentSource?.xkbId ?? '';
    } catch (e) {
        return false;
    }
    const [base, variant = ''] = xkbId.toLowerCase().split('+');
    if (REMAPPED_VARIANTS.some(v => variant.includes(v)))
        return false;
    if (base === 'rs' && variant.includes('latin'))
        return false;
    return NON_LATIN_LAYOUTS.has(base);
}

/**
 * Keyval for one code point (`null` for control characters).
 *
 * @param {string} char one character (code point)
 * @returns {number|null}
 */
function keyvalFor(char) {
    if (char === '\n')
        return Clutter.KEY_Return;
    if (char === '\t')
        return Clutter.KEY_Tab;
    const cp = char.codePointAt(0);
    if (cp < 0x20 || (cp >= 0x7f && cp <= 0x9f))
        return null;
    const keyval = Clutter.unicode_to_keysym(cp);
    return keyval || (UNICODE_KEYSYM_FLAG | cp);
}

export class VirtualKeyboard {
    constructor() {
        this._device = null;
        this._typing = null; // {sourceId, resolve, count}
    }

    _getDevice() {
        if (!this._device) {
            // GNOME 48+ reaches the backend through the stage context;
            // Clutter.get_default_backend() is the GNOME 45-47 way.
            const backend = global.stage?.context?.get_backend?.() ??
                Clutter.get_default_backend();
            const seat = backend.get_default_seat();
            this._device = seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        }
        return this._device;
    }

    _keyval(keyval, pressed) {
        this._getDevice().notify_keyval(GLib.get_monotonic_time(), keyval,
            pressed ? Clutter.KeyState.PRESSED : Clutter.KeyState.RELEASED);
    }

    _keycode(code, pressed) {
        this._getDevice().notify_key(GLib.get_monotonic_time(), code,
            pressed ? Clutter.KeyState.PRESSED : Clutter.KeyState.RELEASED);
    }

    /**
     * Information about the focused window.
     *
     * @returns {{wmClass: string, isTerminal: boolean}}
     */
    focusInfo() {
        const win = global.display.focus_window;
        if (!win)
            return {wmClass: '', isTerminal: false};
        const ids = [
            win.get_wm_class(),
            win.get_wm_class_instance(),
            win.get_gtk_application_id(),
            win.get_sandboxed_app_id(),
        ];
        try {
            ids.push(Shell.WindowTracker.get_default().get_window_app(win)?.get_id());
        } catch (e) {
            // the tracker is best effort
        }
        const isTerminal = ids.some(id => TERMINALS.has(normalizeId(id)));
        const wmClass = win.get_wm_class() || win.get_sandboxed_app_id() ||
            win.get_gtk_application_id() || '';
        return {wmClass, isTerminal};
    }

    /**
     * Press the paste chord in the focused window.
     *
     * @param {string} shortcut auto | ctrl-v | ctrl-shift-v | shift-insert
     * @returns {string} the shortcut actually sent
     */
    paste(shortcut) {
        let used = shortcut;
        if (used === 'auto')
            used = this.focusInfo().isTerminal ? 'ctrl-shift-v' : 'ctrl-v';
        const chord = PASTE_CHORDS[used];
        if (!chord)
            throw new RangeError(`Unknown paste shortcut “${shortcut}”`);

        const useCodes = preferKeycodes();
        const mods = useCodes ? chord.modCodes : chord.mods;
        const key = useCodes ? chord.keyCode : chord.key;
        const send = useCodes ? this._keycode.bind(this) : this._keyval.bind(this);
        const pressed = [];
        try {
            for (const mod of mods) {
                send(mod, true);
                pressed.push(mod);
            }
            send(key, true);
            send(key, false);
        } finally {
            // Never leave a modifier stuck, even if a call above threw.
            for (const mod of pressed.reverse()) {
                try {
                    send(mod, false);
                } catch (e) {
                    logError(e, 'Greffier: cannot release modifier');
                }
            }
        }
        return used;
    }

    /**
     * Type `text`. Uses the input method when a text field has IM focus
     * (any character, any layout), key presses otherwise (only characters
     * of the current layout come out). Resolves with the number of
     * characters sent.
     *
     * @param {string} text the text
     * @returns {Promise<number>}
     */
    typeText(text) {
        this.cancelTyping();
        const normalized = text.replace(/\r\n?/g, '\n');
        const chars = [...normalized];
        const expected = chars.filter(c => keyvalFor(c) !== null).length;

        if (Main.inputMethod?.currentFocus) {
            // Whole runs through the IM; line breaks and tabs as real keys.
            for (const part of normalized.split(/([\n\t])/)) {
                if (part === '\n' || part === '\t') {
                    const kv = keyvalFor(part);
                    this._keyval(kv, true);
                    this._keyval(kv, false);
                } else if (part) {
                    // eslint-disable-next-line no-control-regex
                    const clean = part.replace(/[\u0000-\u001f\u007f-\u009f]/g, '');
                    if (clean)
                        Main.inputMethod.commit(clean);
                }
            }
            return Promise.resolve(expected);
        }

        return new Promise(resolve => {
            const state = {sourceId: 0, resolve, count: 0, index: 0};
            this._typing = state;
            const step = () => {
                const end = Math.min(state.index + TYPE_BATCH, chars.length);
                try {
                    for (; state.index < end; state.index++) {
                        const kv = keyvalFor(chars[state.index]);
                        if (kv === null)
                            continue;
                        this._keyval(kv, true);
                        this._keyval(kv, false);
                        state.count++;
                    }
                } catch (e) {
                    logError(e, 'Greffier: typing failed');
                    state.index = chars.length;
                }
                if (state.index >= chars.length) {
                    state.sourceId = 0;
                    this._typing = null;
                    resolve(state.count);
                    return GLib.SOURCE_REMOVE;
                }
                return GLib.SOURCE_CONTINUE;
            };
            if (step() === GLib.SOURCE_CONTINUE) {
                state.sourceId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, TYPE_INTERVAL_MS, step);
                GLib.Source.set_name_by_id(state.sourceId, '[greffier] typeText');
            }
        });
    }

    cancelTyping() {
        const state = this._typing;
        if (!state)
            return;
        this._typing = null;
        if (state.sourceId)
            GLib.source_remove(state.sourceId);
        state.resolve(state.count);
    }

    destroy() {
        this.cancelTyping();
        // Release anything the device may still hold right away, not at GC.
        this._device?.run_dispose();
        this._device = null;
    }
}
