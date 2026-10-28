# ADR-0003: GNOME custom shortcut for toggle, GlobalShortcuts portal for push-to-talk

**Status:** Accepted · **Date:** 2026-10-28

## Context
Wayland does not let applications grab keys. GNOME offers (1) custom keyboard
shortcuts in `org.gnome.settings-daemon.plugins.media-keys` that run a command
on key *press*, and (2) since GNOME 48 the `org.freedesktop.portal.GlobalShortcuts`
portal, which delivers press *and* release but needs a consent dialog and an
application id (host apps: `app-<id>.service` unit name and/or
`org.freedesktop.host.portal.Registry.Register`).

## Decision
- **Toggle mode (default):** register a GNOME custom shortcut "Greffier"
  (default `<Super><Alt>d`) running `gdbus call … Daemon1.Toggle`.
- **Push-to-talk mode:** use the GlobalShortcuts portal; fall back to the
  custom shortcut (toggle) with an explanation when the portal is missing.
- Never keep both bindings active for the same key.

## Options considered
| Option | Release events | Works on GNOME 45–47 | Visible in Settings | Root needed |
|---|---|---|---|---|
| **Custom shortcut (gsettings)** | no | yes | yes | no |
| **GlobalShortcuts portal** | yes | no (48+) | yes (portal UI) | no |
| evdev (`/dev/input`) | yes | yes | no | `input` group — security risk |
| Shell extension keybinding | no | yes | no | no |

## Trade-off analysis
The custom shortcut is the most reliable, survives daemon crashes (D-Bus
activation restarts the service) and is discoverable in GNOME Settings. Only
push-to-talk needs release events, which justifies the portal. evdev was
rejected: reading every keystroke is unacceptable for a privacy-focused tool.

## Consequences
- The daemon runs as `app-io.github.greffier.Greffier.Daemon.service` and calls
  `Registry.Register` first thing on its bus connection.
- Conflicts with existing shortcuts are detected and reported, not overridden.
