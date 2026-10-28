# ADR-0007: User-level install script with a venv; Flatpak later

**Status:** Accepted · **Date:** 2026-10-28

## Context
sherpa-onnx and numpy come from PyPI; GTK, libadwaita, GStreamer and PyGObject
come from Fedora. The Shell extension and the GNOME custom shortcut live on the
host. The tool must start at login.

## Decision
`packaging/install.sh` installs per user: missing Fedora packages via `dnf`
(with consent), a venv with `--system-site-packages` in
`~/.local/share/greffier/venv`, launchers in `~/.local/bin`, desktop files,
icons, metainfo, a D-Bus activation file, the systemd user unit
(`WantedBy=graphical-session.target`) and the extension. `uninstall.sh`
reverses everything (`--purge` also removes models and settings).

## Options considered
| Option | Effort now | Fit |
|---|---|---|
| **Install script + venv (chosen)** | low | full host integration, easy upgrades (`git pull && ./packaging/install.sh`) |
| Flatpak (Flathub) | high | ideal for GUI distribution, but the extension and host shortcut stay outside the sandbox; injection backends limited |
| RPM/COPR | medium | sherpa-onnx is not packaged in Fedora; vendoring wheels conflicts with packaging guidelines |

## Consequences
- Flatpak packaging of the GUI + daemon (GlobalShortcuts/RemoteDesktop portals,
  Background portal autostart) is the next distribution milestone; the
  extension would be published on extensions.gnome.org.
