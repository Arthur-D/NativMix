# NativMix

NativMix is a hardware-based volume mixer for Linux, built with PyQt6. It connects physical Arduino potentiometers via USB to PipeWire/PulseAudio, giving you per-app volume control through real faders. Each channel can be mapped to one or more apps, a hardware device, or the system master. **Virtual Sinks** isolate apps in a dedicated PipeWire null-sink so seek-related volume spikes never reach your ears — the hardware fader controls the sink, the app stays at unity gain inside. New streams are caught and muted instantly (Two-Stage Mute-Catch) before metadata is available, then released at the correct fader level. MIDI CC controllers are supported natively alongside the Arduino, with MIDI-Learn and a built-in virtual MIDI port. The GUI follows your system theme automatically via the XDG Desktop Portal and works on KDE, GNOME, and any XDG-compliant desktop including Wayland.

![NativMix Icon](assets/icon.png)

<div align="center">

| Breeze Theme (Native) | Iridescent Theme |
|:---:|:---:|
| ![Breeze Theme](assets/Breeze.jpg) | ![Iridescent Theme](assets/Iridescent_Lightly_3.jpg) |

![Nothing](assets/nothing.jpg)

</div>

---

## Status

> MIDI stability data is still being collected — if you use NativMix with a MIDI controller, feedback is very welcome.
>
> **Note on "Stable":** Unless otherwise noted, "Stable" means the package installs and no obvious errors appear on first use. Only **Arch Linux / CachyOS** is daily-driven and tested in production.

| OS | Status | Notes |
| :--- | :---: | :--- |
| **Arch Linux / CachyOS** | ✅ Stable | AUR package, daily driver |
| **Ubuntu 25.04 / 25.10** | ✅ Stable | OBS package, tested on Pop!_OS |
| **Pop!_OS** | ✅ Stable | COSMIC desktop, GUI tested, no log errors |
| **openSUSE Tumbleweed** | ✅ Stable | OBS package, GUI tested, no log errors |
| **openSUSE Slowroll** | ❓ Untested | OBS package |
| **Fedora 42 / 43** | 🔧 In Progress | OBS package, being worked on |
| **Debian 12 / 13** | 🔧 In Progress | OBS package, untested |
| **Raspberry Pi OS** | ❓ Untested | Cannot verify — hardware not available |
| **Windows 10 / 11** | 🔧 In Progress | Early alpha — installer available, being actively worked on |

| Desktop Environment | Status | Notes |
| :--- | :---: | :--- |
| **KDE Plasma** | ✅ Stable | Wayland + X11, daily driver |
| **COSMIC** | ✅ Stable | Tested on Pop!_OS |
| **GNOME** | 🔧 In Progress | Basic functionality works, some quirks |

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://build.opensuse.org/package/show/home:knoelliX/nativmix)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/knoellix/NativMix)
---

## Installation

→ **[Full Installation Guide](https://github.com/knoellix/NativMix/wiki/EN-Installation)**

**Arch Linux / CachyOS:**
```bash
paru -S nativmix
```

---

## Documentation

- [Wiki (EN)](https://github.com/knoellix/NativMix/wiki/)
- [Wiki (DE)](https://github.com/knoellix/NativMix/wiki/DE-Home)

---

## Update History

**v1.0.9**
- Feat: Compact Mode — top-bar toggle collapses the mixer to fader-only view; window shrinks to fit, fader spacing preserved
- Feat: MIDI Mute-CC — assign any MIDI button/switch to mute-toggle a channel; only CC value 127 triggers (button press), faders are safe
- Feat: Edit MIDI Channel toggle — show/hide per-channel Learn, Mute-CC, and Delete buttons without cluttering the mixer
- Feat: `nativmix --restart` IPC command — fully restarts the running instance (all threads and audio state reloaded)
- Feat: auto-restart after package update — checks installed version every 60 s, restarts automatically on upgrade
- Feat: PipeWire reconnect + V-Sink recovery — after PipeWire restart, re-runs audio audit after 3 s and recreates all V-Sinks
- Perf: event deduplication in PipeWire listener — PipeWire fires one change event per stream property update, causing 20+ redundant callbacks when an app starts; only events with actual volume/mute changes are now processed
- Perf: persistent PulseAudio connection for volume operations — reduces gradual RAM growth
- Perf: window geometry writes debounced to 500 ms — eliminates QSettings spam during window drag
- Fix: V-Sink display name in pavucontrol/Helvum now shows only `NativMix_CH_0` instead of the full flags string
- Fix: SPDX license string in pyproject.toml (setuptools deprecation warning resolved)

**v1.0.8**
- Fix: MIDI input now correctly controls hardware output devices (hardware mode channels were not applying volume)
- Fix: Garbage serial frames after Arduino reconnect (e.g. caused by Steam/games disrupting the USB bus) no longer trigger a spurious channel count reset and GUI rebuild

**v1.0.7**
- Windows: installer (PyInstaller + Inno Setup), early alpha
- Windows: WASAPI audio backend implemented (pycaw), stability being evaluated
- Windows: per-app volume control via Arduino implemented (early alpha)
- Windows: system master volume control via WASAPI (IAudioEndpointVolume)
- Windows: channel mapped to a hardware output device not supported
- Windows: Virtual MIDI Port hidden — not planned (WinMM has no virtual port support)
- Windows: Virtual Sinks not planned
- KDE X11 + GNOME X11: window position no longer jumps to center on show
- Fedora/Nobara: Virtual MIDI Port disabled — platform limitation (portmidi, no ALSA virtual ports)
- MIDI: Circuit Breaker — GUI protected against repeated MIDI backend crashes (disabled after 3 consecutive failures, manual restart available)
- MIDI: automatic recovery with cooldown on transient errors
- Config: corrupted config.json automatically backed up as config.json.bak instead of being silently overwritten
- Stability: various resource leak and error handling fixes (Windows IPC, MIDI port, null-sink timeout)
- About section shows version number

**v1.0.6**
- App pinning and channel renaming
- systemd autostart + XDG config migration
- portmidi fix for Fedora/Nobara
- Rounded corners always active
- Wayland: system shutdown no longer blocked by window

**v1.0.5**
- V-Sink restart stability fix
- Improved Wayland/COSMIC integration
- MIDI auto-recovery on device disconnect

**v1.0.4**
- PipeWire update handling, autostart fix, error handling improvements

**v1.0.3**
- openSUSE packaging
- AUR automation
- App filtering and V-Sink routing improvements

**v1.0.2**
- MIDI sync and mode switching fixes
- UI stability improvements

**v1.0.1**
- Tray icon fix
- "Other Apps" channel visibility

---

## License
GPL-3.0 – see [LICENSE](LICENSE) for details.
