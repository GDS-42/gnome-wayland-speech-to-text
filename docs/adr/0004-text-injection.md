# ADR-0004: Paste through the clipboard by default; composable keyboard/clipboard backends

**Status:** Accepted · **Date:** 2026-10-28

## Context
Wayland clients cannot type into other windows. French text is full of
characters (é, è, ê, ç, œ, « ») that are not directly on every layout: GNOME's
virtual keyboard (`notify_keyval`) only finds keysyms present in the current
layout, and `ydotool type` assumes a US layout (AZERTY users get garbage).
Setting the clipboard needs either keyboard focus (wl-copy's hack on GNOME) or
running inside gnome-shell.

## Decision
Split injection into a **clipboard backend** (Shell extension `St.Clipboard` →
`wl-copy`) and a **keyboard backend** (Shell extension virtual keyboard →
RemoteDesktop portal → `ydotool`). Default method `paste`: save the text
clipboard, set CLIPBOARD and PRIMARY, press the paste shortcut (Ctrl+Shift+V in
terminals when the extension can tell, Ctrl+V otherwise), restore the previous
text. `type` (key by key) and `clipboard` (copy + notification) are options.
When no keyboard backend works the text is still copied — never lost.

## Options considered
| Option | Accents / any layout | Needs | Side effects |
|---|---|---|---|
| **Clipboard + paste (chosen)** | yes | extension or wl-copy + a key backend | clipboard briefly replaced (restored) |
| Keysym typing | partial | extension or portal | slow for long text, fails on missing keysyms |
| ydotool type | no (US only) | ydotoold running as root | layout bugs |
| IBus engine committing text | yes | switching input source | intrusive, conflicts with user IMs |

## Trade-off analysis
Paste is instant for long dictations and layout-independent. The Shell
extension makes it invisible (no focus change); the portal path needs one
consent dialog (persisted with a restore token) and shows GNOME's remote-access
indicator briefly, so its session is closed after a few seconds of inactivity.

## Consequences
- Non-text clipboard content (images) cannot be restored after a paste — documented.
- Terminals without the extension need `paste_shortcut = "ctrl-shift-v"`.
