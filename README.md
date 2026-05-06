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
| **Ubuntu 24.04 / 24.10** | ✅ Stable | OBS package |
| **Linux Mint 22** | ✅ Stable | Uses Ubuntu 24.04 OBS package |
| **Pop!_OS** | ✅ Stable | COSMIC desktop, GUI tested, no log errors |
| **openSUSE Tumbleweed** | ✅ Stable | OBS package, GUI tested, no log errors |
| **openSUSE Slowroll** | ❓ Untested | OBS package |
| **Fedora 42 / 43** | ✅ Stable | OBS package, core functions tested — uses portmidi instead of rtmidi (no virtual MIDI port) |
| **Debian 12 / 13** | ✅ Stable | OBS package — based on Ubuntu compatibility |
| **Raspberry Pi OS** | ❓ Untested | Cannot verify — hardware not available |
| **Windows 10 / 11** | 🔧 In Progress | Early alpha — installer available, being actively worked on |

> **Windows testers wanted!** The Windows backend works in basic testing but hasn't seen real daily-use feedback yet.
> If you run NativMix on Windows for more than a day or two, I'd love to hear how it holds up — crashes, quirks, anything unexpected.
> Please open an issue or leave a comment: [GitHub Issues](https://github.com/knoellix/NativMix/issues)

| Desktop Environment | Status | Notes |
| :--- | :---: | :--- |
| **KDE Plasma** | ✅ Stable | Wayland + X11, daily driver |
| **COSMIC** | ✅ Stable | Tested on Pop!_OS |
| **GNOME** | ✅ Stable | Basic functionality confirmed — if you use it daily, feedback is welcome! |

> **Fedora & GNOME feedback welcome!** Fedora uses portmidi instead of rtmidi — the virtual MIDI port is not available there.
> If everything works for you (GUI, faders, MIDI), a quick note would be great: [GitHub Issues](https://github.com/knoellix/NativMix/issues)

---

## Installation

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://software.opensuse.org/download.html?project=home%3AknoelliX&package=nativmix)

→ **[Full Installation Guide](https://github.com/knoellix/NativMix/wiki/EN-Installation)**

**Arch Linux / CachyOS:**
```bash
paru -S nativmix
```

---

## Documentation

- [Wiki (EN)](https://github.com/knoellix/NativMix/wiki/)
- [Wiki (DE)](https://github.com/knoellix/NativMix/wiki/DE-Home)
 
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/knoellix/NativMix)

---

## Update History

**v1.0.13**
- Feat: Profile system — per-profile channel configuration stored in `~/.config/nativmix/profiles/`; switch via top-bar dropdown, IPC (`--profile next/prev/name`) or MIDI CC
- Feat: Config v6→v7 migration — channel data moves from `config.json` into individual profile files; existing configs are migrated automatically
- Feat: Fader takeover — when switching to a profile with saved fader positions, hardware input is suppressed per channel until the first movement is detected
- Feat: MIDI CC profile switching — global next/prev CC + per-profile direct CC, all learnable via settings panel
- Feat: IPC `--profile next/prev/name` command
- Feat: Profile dropdown in top bar with inline rename (debounced) and add button
- Feat: Settings panel profile section — "Fader-Positionen laden" checkbox + collapsible MIDI profile switch section
- Fix: Channel count stability counter (3 consecutive frames) prevents oscillation on USB reconnect (fixes #13)
- Fix: `_inv_flags` sync in `_adapt_channels` prevents IndexError on channel resize
- Fix: MIDI mute-CC bindings preserved across channel count changes (fixes #14)

**v1.0.12**
- Feat: Auto-discover Device toggle — disable automatic port scanning when a specific port is configured; prevents connecting to the wrong device in multi-device setups (e.g. Arduino + ESP32 on the same system), fixing slider app assignments being reset after every reboot (thanks [@DrKartoffel1](https://github.com/DrKartoffel1)!)
- Feat: Port selector is now editable — supports manual entry and symlinked device paths (e.g. `/dev/deej`) (thanks [@DrKartoffel1](https://github.com/DrKartoffel1)!)
- Feat: Config v5→v6 migration — existing users with a port already configured automatically get auto-discovery disabled (thanks [@DrKartoffel1](https://github.com/DrKartoffel1)!)
- Fix: debounce port text input (500 ms QTimer) — prevents reconnect storm while typing a port path
- Fix: `hardware_port` setter no longer silently overwrites the auto-discover checkbox state
- Fix: IPC client socket uses context manager — cleaner resource cleanup
- Fix: `os.unlink` on stale socket wrapped in `try/except` — handles race condition and permission errors

**v1.0.11**
- Fix: `--list-sinks` / `--list-apps` IPC now correctly returns data (client `shutdown(SHUT_WR)` caused Qt socket to enter non-writable state before response was sent)
- Fix: mapped apps no longer controlled by "Other Apps" channel (`media.name` added to `pa_fallback` in volume and mute paths)
- Fix: IPC `readyRead` race condition on new connection — `bytesAvailable` guard prevents missed events
- Fix: AUR deploy workflow permissions moved to job level (Principle of Least Privilege)
- Feat: `GENERIC_PA_NAMES` — detects and labels anonymous/virtual streams (pid=0, no process)
- Feat: `spotify-bin` and `brave-bin` added to binary resolver map for AUR installs
- Feat: stream picker shows `[no process — map by name]` hint for anonymous streams
- Feat: `anonymous` field added to `--list-apps` output

**v1.0.10**
- Refactor: private signal connections replaced with public API (`on_midi_connection_changed`, `open_settings`, `on_mapping_changed`)
- Refactor: Arduino connection handler extracted to named function with error guard
- Refactor: MIDI status colors extracted to module-level constant
- Refactor: backend factory lambdas replaced with named functions
- Refactor: `trigger_panic()` alias removed from MidiThread — `midi_panic_triggered` connects to `restart_midi` directly
- Fix: mute button lambda now accepts `checked=False` to match PyQt6 `clicked(bool)` signal
- Fix: `_on_add_midi_clicked` slot decorated with `@pyqtSlot(bool)` to match `clicked` signal
- Fix: sorted PyQt6 QWidgets imports (ruff I001)
- Fix: stale `/tmp` reference in `paths.py` docstring corrected to `$XDG_RUNTIME_DIR`
- Feat: Tray menu — added "Restart NativMix" entry (same as `nativmix --restart`)

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
- Fix: MIDI channel Delete button was silently swallowed by a TypeError (missing bool parameter on clicked slot)
- Fix: Edit MIDI Channel mode now stays active after adding or deleting a MIDI channel
- Fix: Learn buttons show "Cancel" while waiting; pressing Escape or clicking again cancels MIDI learn without assigning a CC
- Fix: Windows — System Master volume caused AttributeError on first use (endpoint reference was never initialized)
- Fix: Windows — audio thread could become a zombie after stop() timeout; now properly terminated and disconnected

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
