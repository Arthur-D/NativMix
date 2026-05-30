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

→ **[Full Installation Guide](https://github.com/knoellix/NativMix/wiki/EN-Installation)**

**Arch Linux / CachyOS:**
```bash
paru -S nativmix
```

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://software.opensuse.org/download.html?project=home%3AknoelliX&package=nativmix)

---

## Documentation

- [Wiki (EN)](https://github.com/knoellix/NativMix/wiki/)
- [Wiki (DE)](https://github.com/knoellix/NativMix/wiki/DE-Home)
 
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/knoellix/NativMix)

---

## Update History

**v1.0.14**
- Fix: Skip redundant Pulse volume writes to prevent GNOME Shell FIFO accumulation when adjusting system volume (fixes #19)
- Fix: Reuse persistent Pulse connection for hardware volume during Arduino/MIDI ticks
- Fix: Discard transient Arduino reconnect frames with mismatched channel counts
- Fix: Volume sliders show saved positions after tray close and reopen; audio was already correct (fixes #17)

→ [Full changelog](CHANGELOG.md)

---

## License
GPL-3.0 – see [LICENSE](LICENSE) for details.
