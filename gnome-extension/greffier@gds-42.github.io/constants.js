// SPDX-License-Identifier: GPL-3.0-or-later
// Identifiers shared with the Python package (greffier/const.py). When
// renaming the project, change APP_ID here too (docs/ARCHITECTURE.md §9).

export const APP_ID = 'io.github.gds_42.Greffier';

export const DAEMON_NAME = `${APP_ID}.Daemon`;
// systemd user unit the service must run in (greffier.const.SYSTEMD_UNIT).
export const DAEMON_UNIT = `app-${DAEMON_NAME}.service`;
export const DAEMON_PATH = '/io/github/gds_42/Greffier/Daemon';
export const DAEMON_IFACE = `${APP_ID}.Daemon1`;

export const SHELL_NAME = `${APP_ID}.Shell`;
export const SHELL_PATH = '/io/github/gds_42/Greffier/Shell';
export const SHELL_IFACE = `${APP_ID}.Shell1`;
// Bumped on incompatible changes of Shell1 (greffier/desktop/shell_client.py).
export const SHELL_API_VERSION = 2;
export const ERROR_MODIFIERS_HELD = `${APP_ID}.Shell.Error.ModifiersHeld`;

export const APP_DESKTOP_ID = `${APP_ID}.desktop`;

// Daemon states (greffier.const.State).
export const State = Object.freeze({
    NO_MODEL: 'no-model',
    LOADING: 'loading',
    IDLE: 'idle',
    RECORDING: 'recording',
    TRANSCRIBING: 'transcribing',
    ERROR: 'error',
});
