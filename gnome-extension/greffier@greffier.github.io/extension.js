// SPDX-License-Identifier: GPL-3.0-or-later
// Greffier — GNOME Shell companion of the Greffier voice dictation app.
//
// * exports io.github.greffier.Greffier.Shell1 so that the service can set
//   the clipboard and press keys without stealing focus (Wayland);
// * shows a top-bar indicator bound to the service state;
// * shows a non-interactive overlay while recording/transcribing.
//
// Nothing is created before enable(), and disable() releases everything
// (bus name, exported object, actors, signal handlers, timeouts, virtual
// keyboard). The extension is disabled on the lock screen, so text can
// never be injected there.

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {DaemonClient} from './daemon.js';
import {ShellService} from './dbus.js';
import {Indicator} from './indicator.js';
import {VirtualKeyboard} from './keyboard.js';
import {RecordingOsd} from './osd.js';

export default class GreffierExtension extends Extension {
    enable() {
        this._daemon = new DaemonClient();
        this._keyboard = new VirtualKeyboard();
        this._service = new ShellService(this._keyboard, this._daemon);
        this._service.start();

        this._indicator = new Indicator(this._daemon);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
        this._osd = new RecordingOsd(this._daemon);

        this._daemon.start();
    }

    disable() {
        this._osd?.destroy();
        this._osd = null;
        this._indicator?.destroy();
        this._indicator = null;
        this._service?.destroy();
        this._service = null;
        this._keyboard?.destroy();
        this._keyboard = null;
        this._daemon?.destroy();
        this._daemon = null;
    }
}
