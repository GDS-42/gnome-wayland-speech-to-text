"""Project-wide identifiers.

Every D-Bus name, object path, desktop-file id and systemd unit is derived from
APP_ID so that forks only have to change it in one place (plus the static data
files listed in docs/ARCHITECTURE.md, "Renaming the project").
"""

from enum import StrEnum

from greffier import __version__

VERSION = __version__
APP_NAME = "Greffier"
APP_ID = "io.github.greffier.Greffier"  # GUI application id / main .desktop file

# Background service (owns the dictation pipeline).
DAEMON_ID = f"{APP_ID}.Daemon"
DBUS_NAME = DAEMON_ID
DBUS_PATH = "/io/github/greffier/Greffier/Daemon"
DBUS_IFACE = f"{APP_ID}.Daemon1"

# Interface exported by the optional GNOME Shell extension.
SHELL_DBUS_NAME = f"{APP_ID}.Shell"
SHELL_DBUS_PATH = "/io/github/greffier/Greffier/Shell"
SHELL_DBUS_IFACE = f"{APP_ID}.Shell1"
EXTENSION_UUID = "greffier@greffier.github.io"

# xdg-desktop-portal derives the app id of unsandboxed apps from the systemd
# unit name, which must follow the "app-<APP_ID>.service" convention.
SYSTEMD_UNIT = f"app-{DAEMON_ID}.service"

PROJECT_URL = "https://github.com/greffier/greffier"
DEFAULT_CATALOG_URL = (
    "https://raw.githubusercontent.com/greffier/greffier/main/greffier/data/catalog.json"
)

SAMPLE_RATE = 16000  # every engine consumes 16 kHz mono float32


class State(StrEnum):
    """Dictation pipeline state, as published on D-Bus (property ``State``)."""

    NO_MODEL = "no-model"  # nothing usable is installed / selected
    LOADING = "loading"  # model is being loaded in memory
    IDLE = "idle"  # ready, waiting for the shortcut
    RECORDING = "recording"  # microphone open
    TRANSCRIBING = "transcribing"  # audio captured, engine running
    ERROR = "error"  # last action failed; message in StateChanged detail
