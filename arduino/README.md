# NativMix Arduino Examples

> **Status:** These examples are shipped as reference implementations and are **not
> fully tested** in daily use yet. Hardware wiring, MIDI CC assignments, and LED
> feedback should be verified on your setup before relying on them in production.

## nativmix_midi_controller.ino

Example MIDI controller for NativMix with RGB LED feedback.

### Required Hardware

| Component | Qty | Notes |
|---|---|---|
| SparkFun Pro Micro (ATmega32u4) | 1 | Native USB-MIDI — Leonardo works too |
| Potentiometer / fader 10 kΩ | 4 | Linear taper |
| Momentary push button | 6 | 4× mute, 2× profile |
| Toggle switch | 1 | Direct profile activate |
| WS2812B RGB LED | 6 | Chainable, single data wire |
| 330 Ω resistor | 1 | In series on LED data line |
| 100 µF capacitor | 1 | Across LED power rail |

### Libraries (Arduino Library Manager)

- **MIDIUSB** — USB-MIDI for ATmega32u4 boards
- **Adafruit NeoPixel** — WS2812B control

### Pin Assignment

| Pin | Function |
|---|---|
| A0–A3 | Fader 1–4 |
| D2–D5 | Mute button 1–4 |
| D6 | Profile button: prev |
| D7 | Profile button: next |
| D8 | Profile switch (direct activate) |
| D9 | WS2812B data |

### MIDI Protocol

#### Sent by the controller

| CC | Value | Function |
|---|---|---|
| 1–4 | 0–127 | Fader volume (channel 1–4) |
| 5–8 | 127 | Mute toggle (channel 1–4) |
| 9 | 127 | Profile: previous |
| 10 | 127 | Profile: next |
| 11 | 127 | Profile: direct activate (switch) |

Assign these CCs in NativMix via MIDI-Learn.

#### Received from NativMix (LED feedback)

| CC | Value | Function |
|---|---|---|
| 32–37 | 0–127 | LED 0–5 color — hue encoding (see below) |
| 38 | 0–127 | Global LED brightness (optional) |

**Hue encoding** — CC value maps to color:

| Value | Color | Meaning (suggested) |
|---|---|---|
| 0 | Red | Muted / error |
| 21 | Orange | Warning |
| 42 | Green | Active / unmuted |
| 63 | Yellow | Fader takeover active |
| 85 | Blue | Idle / profile button |
| 106 | Purple | — |

The hue wraps around — 127 is back to red. Full saturation and brightness
are fixed in the firmware; use CC 38 to adjust brightness globally.

### USB Device Name

To make the controller show up as "NativMix Controller" in NativMix and
in system MIDI device lists, set the USB descriptor in the SparkFun Pro
Micro core (`boards.txt`):

```
promicro.build.usb_product="NativMix Controller"
promicro.build.usb_manufacturer="knoelliX"
```

NativMix can then match the device by name automatically.

### Extensibility

The protocol is intentionally open-ended:

- **More LEDs**: extend CC 32+ range, no firmware changes needed on the NativMix side
- **Displays**: MIDI SysEx is planned for streaming text data (app names,
  profile names, volume values) to attached OLED/LCD displays
- **More buttons**: any unused CC numbers can be assigned in NativMix via MIDI-Learn

### Planned: bidirectional MIDI fader sync

NativMix currently receives MIDI CC from controllers (one-way). Outbound fader
position sync (NativMix → controller) is planned and will be **opt-in via the
settings panel** so users can disable it when not needed.

Design notes for implementation:

- **Feedback-loop protection:** when NativMix sends a CC to move a physical
  fader, the returning CC must not re-apply volume (similar to Arduino fader
  takeover / `--vol` IPC takeover).
- **Settings toggle:** global enable/disable in the settings panel; default off
  until verified on real hardware.
- **Send targets:** same MIDI device as input where possible; on Linux with
  rtmidi, the existing virtual port may be used — Fedora/portmidi has no
  virtual port (platform limitation already documented in the main README).
- **Throttling / deadband:** do not spam CC on every GUI tick; send only on
  meaningful volume changes (profile load, IPC `--vol`, mute/unmute, external
  volume events).
- **Learn mode:** outbound sync must pause while MIDI-Learn is active on a
  channel.
- **Config:** persist the toggle in `config.json` (settings-owned field only).
