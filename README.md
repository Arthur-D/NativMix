# NativMix

NativMix is a hardware-based volume mixer for Linux, built with PyQt6. It connects physical Arduino potentiometers via USB to PipeWire/PulseAudio, giving you per-app volume control through real faders. Each channel can be mapped to one or more apps, a hardware device, or the system master. **Virtual Sinks** isolate apps in a dedicated PipeWire null-sink so seek-related volume spikes never reach your ears — the hardware fader controls the sink, the app stays at unity gain inside. New streams are caught and muted instantly (Two-Stage Mute-Catch) before metadata is available, then released at the correct fader level. MIDI CC controllers are supported natively alongside the Arduino, with MIDI-Learn and a built-in virtual MIDI port. The GUI follows your system theme automatically via the XDG Desktop Portal and works on KDE, GNOME, and any XDG-compliant desktop including Wayland.

![NativMix Icon](assets/icon.png)

<div align="center">

| Breeze Theme (Native) | Iridescent Theme |
|:---:|:---:|
| ![Breeze Theme](assets/Breeze.jpg) | ![Iridescent Theme](assets/Iridescent_Lightly_3.jpg) |

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
| **COSMIC Desktop** | ✅ Stable | Tested on Pop!_OS |
| **Raspberry Pi OS** | ❓ Untested | Cannot verify — hardware not available |
| **Windows** | 📋 Planned | — |

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://build.opensuse.org/package/show/home:knoelliX/nativmix)

---

## Installation

→ **[Full Installation Guide](https://github.com/knoellix/NativMix/wiki/Installation)**

**Arch Linux / CachyOS:**
```bash
paru -S nativmix
```

**openSUSE Tumbleweed:**
```bash
sudo zypper addrepo https://download.opensuse.org/repositories/home:/knoelliX/openSUSE_Tumbleweed/ nativmix
sudo zypper refresh && sudo zypper install nativmix
```

---

## Documentation

- [Wiki (EN)](https://github.com/knoellix/NativMix/wiki/)
- [Wiki (DE)](https://github.com/knoellix/NativMix/wiki#nativmix-wiki-deutsch)

---

## License
GPL-3.0 – see [LICENSE](LICENSE) for details.
