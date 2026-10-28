// SPDX-License-Identifier: GPL-3.0-or-later
// Top-bar indicator bound to the service State: microphone icon, menu with
// start/stop, status, active model, "Open Greffier" and "Preferences".

import Clutter from 'gi://Clutter';
import GObject from 'gi://GObject';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {APP_DESKTOP_ID, State} from './constants.js';
import {_, format} from './i18n.js';

const ICON_MIC = 'audio-input-microphone-symbolic';
const ICON_MIC_OFF = 'microphone-disabled-symbolic';
const ICON_WARNING = 'dialog-warning-symbolic';

const STATE_STYLE = {
    [State.IDLE]: {icon: ICON_MIC, style: ''},
    [State.RECORDING]: {icon: ICON_MIC, style: 'greffier-recording'},
    [State.TRANSCRIBING]: {icon: ICON_MIC, style: 'greffier-busy', pulse: true},
    [State.LOADING]: {icon: ICON_MIC, style: 'greffier-busy', pulse: true},
    [State.ERROR]: {icon: ICON_MIC_OFF, style: 'greffier-error'},
    [State.NO_MODEL]: {icon: ICON_WARNING, style: 'greffier-warning'},
};
const ALL_STYLES = ['greffier-recording', 'greffier-busy', 'greffier-error', 'greffier-warning',
    'greffier-absent'];

function statusText(daemon) {
    if (!daemon.running)
        return _('Service not running');
    switch (daemon.state) {
    case State.IDLE:
        return _('Ready');
    case State.RECORDING:
        return _('Recording…');
    case State.TRANSCRIBING:
        return _('Transcribing…');
    case State.LOADING:
        return _('Loading the speech model…');
    case State.NO_MODEL:
        return _('No speech model installed');
    case State.ERROR:
        return daemon.detail ? `${_('Error')} — ${daemon.detail}` : _('Error');
    default:
        return daemon.detail || _('Ready');
    }
}

export const Indicator = GObject.registerClass({
    GTypeName: 'GreffierIndicator',
}, class GreffierIndicator extends PanelMenu.Button {
    /**
     * @param {import('./daemon.js').DaemonClient} daemon service client
     */
    _init(daemon) {
        super._init(0.5, _('Dictation'), false);
        this._daemon = daemon;
        this._level = 0;
        this._pulsing = false;

        this._icon = new St.Icon({
            icon_name: ICON_MIC,
            style_class: 'system-status-icon greffier-indicator-icon',
        });
        this.add_child(this._icon);

        this._statusItem = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        this._statusItem.label.add_style_class_name('greffier-menu-status');
        this.menu.addMenuItem(this._statusItem);
        this._modelItem = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        this._modelItem.label.add_style_class_name('greffier-menu-model');
        this.menu.addMenuItem(this._modelItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._toggleItem = this.menu.addAction(_('Start dictation'), () => this._daemon.toggle());
        this._cancelItem = this.menu.addAction(_('Cancel'), () => this._daemon.cancel());
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._openItem = this.menu.addAction(_('Open Greffier'), () => this._launch(null));
        this._prefsItem = this.menu.addAction(_('Preferences'), () => this._launch('preferences'));

        this._daemon.connectObject(
            'changed', () => this._sync(),
            'level', (_d, level) => this._onLevel(level),
            this);
        this.menu.connectObject('open-state-changed', (_m, open) => {
            if (open)
                this._syncApp();
        }, this);
        this._sync();
        this._syncApp();
    }

    _app() {
        return Shell.AppSystem.get_default().lookup_app(APP_DESKTOP_ID);
    }

    _syncApp() {
        const app = this._app();
        this._openItem.visible = app !== null;
        const actions = app?.get_app_info()?.list_actions() ?? [];
        // Without a "preferences" desktop action, Preferences just opens the app.
        this._prefsItem.visible = app !== null;
        this._prefsAction = actions.includes('preferences') ? 'preferences' : null;
    }

    _launch(action) {
        const app = this._app();
        if (!app)
            return;
        if (action && this._prefsAction)
            app.launch_action(this._prefsAction, global.get_current_time(), -1);
        else
            app.activate();
    }

    _sync() {
        const daemon = this._daemon;
        const running = daemon.running;
        const spec = (running && STATE_STYLE[daemon.state]) || STATE_STYLE[State.IDLE];

        this._icon.icon_name = spec.icon;
        for (const style of ALL_STYLES)
            this._icon.remove_style_class_name(style);
        if (!running)
            this._icon.add_style_class_name('greffier-absent');
        else if (spec.style)
            this._icon.add_style_class_name(spec.style);

        this._setPulse(running && Boolean(spec.pulse));
        if (!(running && daemon.state === State.RECORDING))
            this._icon.opacity = running ? 255 : 128;

        const recording = running && daemon.state === State.RECORDING;
        this._toggleItem.label.text = recording ? _('Stop dictation') : _('Start dictation');
        this._toggleItem.setSensitive(!running ||
            [State.IDLE, State.RECORDING, State.ERROR, State.LOADING].includes(daemon.state));
        this._cancelItem.visible = recording;
        this._statusItem.label.text = statusText(daemon);
        this._modelItem.visible = running;
        this._modelItem.label.text = daemon.activeModel
            ? format('Model: %s', daemon.activeModel) : _('No model selected');
    }

    _onLevel(level) {
        if (this._daemon.state !== State.RECORDING)
            return;
        // Subtle pulse: opacity follows a smoothed microphone level.
        this._level = Math.max(level, this._level * 0.6);
        this._icon.opacity = Math.round(150 + 105 * Math.min(1, Math.sqrt(this._level)));
    }

    _setPulse(enabled) {
        const animate = enabled && St.Settings.get().enable_animations;
        if (animate === this._pulsing)
            return;
        this._pulsing = animate;
        this._icon.remove_all_transitions();
        if (animate) {
            this._icon.opacity = 255;
            this._icon.ease({
                opacity: 90,
                duration: 700,
                mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
                repeatCount: -1,
                autoReverse: true,
            });
        } else {
            this._icon.opacity = enabled ? 160 : 255;
        }
    }

    _onDestroy() {
        this._icon.remove_all_transitions();
        this._daemon?.disconnectObject(this);
        this.menu?.disconnectObject(this);
        this._daemon = null;
        super._onDestroy();
    }
});
