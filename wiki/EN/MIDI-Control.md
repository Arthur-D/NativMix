# MIDI Control

NativMix has native MIDI support for controlling channel volumes via any MIDI CC device.

## Features

- **MIDI-Learn** — Dynamically assign MIDI CC knobs/faders to any channel directly from the GUI.
- **Virtual MIDI Port** — A built-in virtual MIDI device ("NativMix") is available for headless routing (e.g., via `pw-link`) without needing physical cables or loopbacks.
- **Direct Integration** — Native support for ALSA and USB-MIDI controllers via `mido` / `python-rtmidi`.
- **High Precision** — Low-latency volume control with 7-bit MIDI resolution.

## Input Modes

NativMix supports three input modes configurable in settings:

| Mode | Description |
| :--- | :--- |
| `usb` | Only the Arduino potentiometers control volume. |
| `midi_only` | Only MIDI CC messages control volume. |
| `hybrid` | Both Arduino and MIDI control volume simultaneously. |

## MIDI-Learn

1. Open the settings panel for a channel.
2. Click **MIDI-Learn** and move the desired knob/fader on your MIDI controller.
3. The CC number is assigned automatically.

## Virtual MIDI Port

The built-in virtual port named **"NativMix"** is always available in ALSA/PipeWire. You can connect any MIDI source to it via `pw-link` or a patchbay like **Helvum** or **Carla**.
