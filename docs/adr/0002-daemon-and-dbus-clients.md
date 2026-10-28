# ADR-0002: A D-Bus daemon owns the pipeline; GUI and CLI are clients

**Status:** Accepted · **Date:** 2026-10-28

## Context
Dictation must work from login, with no window open, triggered by a global
shortcut. A model takes 0.5–3.5 GB of RAM and seconds to load, so it must stay
resident. Users also want a polished GUI, and power users a CLI.

## Decision
`greffier-daemon` is a `Gio.Application` (GLib main loop, no GTK) started by a
systemd user unit and D-Bus-activatable. It is the single owner and writer of
configuration, models, microphone, engine and history, and exposes
`io.github.greffier.Greffier.Daemon1` (`greffier/data/daemon-interface.xml`).
The GTK4/libadwaita GUI and `greffierctl` are thin clients. Structured payloads
are JSON strings so the API evolves without changing D-Bus signatures.

## Options considered
| Option | Complexity | RAM when idle | Robustness |
|---|---|---|---|
| **Daemon + D-Bus clients (chosen)** | Medium | model + ~40 MB | GUI crash never stops dictation |
| Single GTK process kept in background | Low | model + ~120 MB (GTK, Adwaita) | a UI bug can kill dictation |
| CLI tool spawned per dictation | Low | 0 | seconds of model load per dictation |

## Trade-off analysis
The daemon costs an IPC contract, but D-Bus is native to GNOME, gives free
activation (`gdbus call … Toggle` from a GNOME shortcut, no Python start-up on
the hot path) and lets the Shell extension, the GUI and scripts share one
source of truth.

## Consequences
- All long work is asynchronous: one ordered worker thread owns the engine
  (load → transcribe ordering without locks), downloads run in their own threads.
- The GUI must cope with the service being absent (banner + "Start").
- `Deps` injection makes the controller fully testable with fakes
  (`tests/test_daemon_controller.py`).
