# ADR-0008: Feedback through sounds, notifications and an optional Shell extension

**Status:** Accepted · **Date:** 2026-10-28

## Context
The user must know when the microphone is listening. On Wayland a regular
window cannot stay on top or be positioned, and — critically — showing one
steals keyboard focus, so the text would be pasted into Greffier itself.

## Decision
- Short, soft start/stop/cancel/error sounds (configurable).
- GNOME notifications for errors, finished downloads, updates and the
  copy-only fallback (never for each dictation unless enabled).
- The optional **GNOME Shell extension** adds a top-bar indicator bound to the
  daemon state and a non-reactive OSD pill with a level meter while recording.
  It also provides the best injection backend (ADR-0004).
- The GUI window is for settings, models and history, never for live feedback.

## Consequences
- The extension must follow extensions.gnome.org rules and be ported for each
  GNOME release (ESM, GNOME 45+).
- Without the extension, feedback is sound + GNOME's own microphone indicator.
