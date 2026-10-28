// SPDX-License-Identifier: GPL-3.0-or-later
// Recording overlay: a small pill at the bottom centre of the primary monitor
// with level bars and the elapsed time, then "Transcribing…". It is never
// reactive and never takes focus, so it cannot disturb the target window.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {State} from './constants.js';
import {_} from './i18n.js';

const BAR_COUNT = 14;
const BAR_MIN = 3;
const BAR_MAX = 22;
const BOTTOM_MARGIN = 96; // clears the dash in the overview
const FADE_MS = 160;
const NOTICE_MS = 2500;

function formatElapsed(seconds) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${String(s).padStart(2, '0')}`;
}

export class RecordingOsd {
    /**
     * @param {import('./daemon.js').DaemonClient} daemon service client
     */
    constructor(daemon) {
        this._daemon = daemon;
        this._mode = null; // null | 'recording' | 'transcribing' | 'notice'
        this._levels = new Array(BAR_COUNT).fill(0);
        this._startTime = 0;
        this._clockId = 0;
        this._noticeId = 0;
        this._hiding = false;

        this._box = new St.BoxLayout({
            style_class: 'greffier-osd',
            reactive: false,
            can_focus: false,
            track_hover: false,
            visible: false,
            opacity: 0,
        });
        this._dot = new St.Widget({
            style_class: 'greffier-osd-dot',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._bars = new St.BoxLayout({
            style_class: 'greffier-osd-bars',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._barActors = [];
        for (let i = 0; i < BAR_COUNT; i++) {
            const bar = new St.Widget({
                style_class: 'greffier-osd-bar',
                y_align: Clutter.ActorAlign.CENTER,
            });
            this._bars.add_child(bar);
            this._barActors.push(bar);
        }
        this._label = new St.Label({
            style_class: 'greffier-osd-label',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._box.add_child(this._dot);
        this._box.add_child(this._bars);
        this._box.add_child(this._label);

        Main.layoutManager.addTopChrome(this._box, {affectsInputRegion: false});
        this._box.connectObject('notify::width', () => this._reposition(), this);
        Main.layoutManager.connectObject('monitors-changed', () => this._reposition(), this);
        this._daemon.connectObject(
            'changed', () => this._sync(),
            'level', (_d, level) => this._onLevel(level),
            'transcribed', (_d, text, meta) => this._onTranscribed(text, meta),
            this);
        this._sync();
    }

    _scale() {
        return St.ThemeContext.get_for_stage(global.stage).scale_factor;
    }

    _reposition() {
        const monitor = Main.layoutManager.primaryMonitor;
        if (!monitor || !this._box.visible)
            return;
        const [, natWidth] = this._box.get_preferred_width(-1);
        const [, natHeight] = this._box.get_preferred_height(natWidth);
        this._box.set_position(
            Math.round(monitor.x + (monitor.width - natWidth) / 2),
            Math.round(monitor.y + monitor.height - natHeight - BOTTOM_MARGIN * this._scale()));
    }

    _sync() {
        const daemon = this._daemon;
        if (daemon.running && daemon.state === State.RECORDING)
            this._showRecording();
        else if (daemon.running && daemon.state === State.TRANSCRIBING)
            this._showTranscribing();
        else if (this._mode !== 'notice')
            this._hide();
    }

    _showRecording() {
        if (this._mode !== 'recording') {
            this._mode = 'recording';
            this._clearNotice();
            this._levels.fill(0);
            this._startTime = GLib.get_monotonic_time();
            this._box.remove_style_class_name('greffier-osd-busy');
            this._dot.show();
            this._bars.show();
            this._setPulse(false);
            this._updateBars();
            this._tick();
            this._stopClock();
            this._clockId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
                this._tick();
                return GLib.SOURCE_CONTINUE;
            });
            GLib.Source.set_name_by_id(this._clockId, '[greffier] osd clock');
        }
        this._fadeIn();
    }

    _showTranscribing() {
        if (this._mode !== 'transcribing') {
            this._mode = 'transcribing';
            this._clearNotice();
            this._stopClock();
            this._box.add_style_class_name('greffier-osd-busy');
            this._bars.hide();
            this._dot.show();
            this._label.text = _('Transcribing…');
            this._setPulse(true);
        }
        this._fadeIn();
    }

    _onTranscribed(_text, meta) {
        // Text left in the clipboard (copy-only method or no keyboard
        // backend): tell the user how to get it.
        if (meta?.partial || meta?.method !== 'clipboard')
            return;
        if (this._daemon.state === State.RECORDING)
            return;
        this._mode = 'notice';
        this._stopClock();
        this._setPulse(false);
        this._box.remove_style_class_name('greffier-osd-busy');
        this._bars.hide();
        this._dot.hide();
        this._label.text = _('Copied: press Ctrl+V to paste');
        this._fadeIn();
        this._clearNotice();
        this._noticeId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, NOTICE_MS, () => {
            this._noticeId = 0;
            this._mode = null;
            this._sync();
            return GLib.SOURCE_REMOVE;
        });
        GLib.Source.set_name_by_id(this._noticeId, '[greffier] osd notice');
    }

    _clearNotice() {
        if (this._noticeId) {
            GLib.source_remove(this._noticeId);
            this._noticeId = 0;
        }
    }

    _tick() {
        const seconds = (GLib.get_monotonic_time() - this._startTime) / 1e6;
        this._label.text = formatElapsed(seconds);
    }

    _stopClock() {
        if (this._clockId) {
            GLib.source_remove(this._clockId);
            this._clockId = 0;
        }
    }

    _onLevel(level) {
        if (this._mode !== 'recording')
            return;
        this._levels.shift();
        this._levels.push(Math.min(1, Math.pow(level, 0.6)));
        this._updateBars();
    }

    _updateBars() {
        const scale = this._scale();
        for (let i = 0; i < BAR_COUNT; i++) {
            const h = BAR_MIN + (BAR_MAX - BAR_MIN) * this._levels[i];
            this._barActors[i].height = Math.round(h * scale);
        }
    }

    _setPulse(enabled) {
        this._dot.remove_all_transitions();
        this._dot.opacity = 255;
        if (enabled && St.Settings.get().enable_animations) {
            this._dot.ease({
                opacity: 60,
                duration: 600,
                mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
                repeatCount: -1,
                autoReverse: true,
            });
        }
    }

    _fadeIn() {
        this._hiding = false;
        if (!this._box.visible) {
            this._box.opacity = 0;
            this._box.show();
        }
        this._reposition();
        this._box.remove_transition('opacity');
        this._box.ease({
            opacity: 255,
            duration: FADE_MS,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD,
        });
    }

    _hide() {
        this._mode = null;
        this._stopClock();
        this._clearNotice();
        this._setPulse(false);
        if (!this._box.visible || this._hiding)
            return;
        this._hiding = true;
        this._box.remove_transition('opacity');
        this._box.ease({
            opacity: 0,
            duration: FADE_MS,
            mode: Clutter.AnimationMode.EASE_IN_QUAD,
            onStopped: () => {
                if (this._hiding) {
                    this._hiding = false;
                    this._box?.hide();
                }
            },
        });
    }

    destroy() {
        this._stopClock();
        this._clearNotice();
        this._daemon?.disconnectObject(this);
        Main.layoutManager.disconnectObject(this);
        if (this._box) {
            this._box.remove_all_transitions();
            this._dot.remove_all_transitions();
            this._box.disconnectObject(this);
            Main.layoutManager.removeChrome(this._box);
            this._box.destroy();
        }
        this._box = null;
        this._daemon = null;
    }
}
