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

> Currently working on **COSMIC launcher compatibility** and improving stability across different distributions. MIDI stability data is still being collected — if you use NativMix with a MIDI controller, feedback is very welcome.

| OS | Status | Notes |
| :--- | :---: | :--- |
| **Arch Linux / CachyOS** | ✅ Stable | AUR package |
| **openSUSE Tumbleweed** | 🔧 In Progress | OBS package |
| **openSUSE Slowroll** | 🔧 In Progress| OBS package |
| **Fedora 42 / 43** | 🧪 Testing | OBS package, feedback welcome |
| **Ubuntu 25.04 / 25.10** | 🔧 In Progress | OBS package, untested |
| **Debian 12 / 13** | 🧪 Testing | OBS package, untested |
| **COSMIC Desktop** | 🔧 In Progress | Launcher compatibility WIP |
| **Windows** | 📋 Planned | — |

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://build.opensuse.org/package/show/home:knoelliX/nativmix)

---

## Installation

→ **[Full Installation Guide](wiki/EN/Installation.md)**

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

- [Wiki (EN)](wiki/EN/Home.md)
- [Wiki (DE)](wiki/DE/Home.md)

---

## License
GPL-3.0 – see [LICENSE](LICENSE) for details.
