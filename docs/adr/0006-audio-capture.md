# ADR-0006: Microphone capture with GStreamer (PipeWire)

**Status:** Accepted · **Date:** 2026-10-28

## Context
Fedora uses PipeWire. We need 16 kHz mono float32, device selection, a level
meter, and no extra native dependency that pip cannot provide.

## Decision
GStreamer through PyGObject: `autoaudiosrc` (or the selected `Gst.Device`) →
`audioconvert` → `audioresample` → `appsink`. Devices come from
`Gst.DeviceMonitor`. The microphone is opened only while dictating, so GNOME's
microphone privacy indicator reflects reality.

## Options considered
| Option | Extra deps on Fedora | Device listing | Notes |
|---|---|---|---|
| **GStreamer (chosen)** | none (Workstation default) | yes | also plays the feedback sounds |
| sounddevice/PortAudio | `portaudio` + pip wheel | yes | another audio stack to debug |
| `pw-record` subprocess | none | no | raw format handling, process churn |

## Consequences
- `GREFFIER_AUDIO_SOURCE` lets tests replace the microphone with any
  GStreamer source (file, test tone).
- Opening the source costs ~50–100 ms; the start sound is played once it runs.
