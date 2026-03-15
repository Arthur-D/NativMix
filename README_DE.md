# NativMix (Deutsch)

NativMix ist ein hardwaregestützter Lautstärkemixer für Linux, entwickelt mit PyQt6. Er verbindet physische Arduino-Potentiometer über USB mit PipeWire/PulseAudio und ermöglicht die Lautstärkeregelung einzelner Apps über echte Regler. Jeder Kanal lässt sich einer oder mehreren Apps, einem Gerät oder dem System-Master zuweisen. **Virtual Sinks** isolieren Apps in einem eigenen PipeWire Null-Sink — seek-bedingte Lautstärke-Spikes erreichen deine Lautsprecher nie mehr, weil der Regler den Sink steuert und die App intern auf Unity Gain läuft. Neue Streams werden sofort stumm geschaltet (Two-Stage Mute-Catch), bevor Metadaten verfügbar sind, und dann auf dem richtigen Fader-Pegel freigegeben. MIDI-CC-Controller werden nativ neben dem Arduino unterstützt, mit MIDI-Learn und einem integrierten virtuellen MIDI-Port. Die GUI passt sich automatisch ans System-Theme an (via XDG Desktop Portal) und funktioniert auf KDE, GNOME und allen XDG-konformen Desktops einschließlich Wayland.

![NativMix Icon](assets/icon.png)

<div align="center">

| Breeze Theme (Native) | Iridescent Theme |
|:---:|:---:|
| ![Breeze Theme](assets/Breeze.jpg) | ![Iridescent Theme](assets/Iridescent_Lightly_3.jpg) |

</div>

---

## Status

> Aktuell wird an der **COSMIC Launcher Kompatibilität** und verbesserter Stabilität auf verschiedenen Distributionen gearbeitet. Daten zur MIDI-Stabilität werden noch gesammelt — Feedback von MIDI-Nutzern ist sehr willkommen.

| Betriebssystem | Status | Hinweis |
| :--- | :---: | :--- |
| **Arch Linux / CachyOS** | ✅ Stabil | AUR-Paket |
| **openSUSE Tumbleweed** | ✅ Stabil | OBS-Paket |
| **openSUSE Slowroll** | ✅ Stabil | OBS-Paket |
| **Fedora 42 / 43** | 🧪 Testing | OBS-Paket, Feedback willkommen |
| **Ubuntu 25.04 / 25.10** | 🧪 Testing | OBS-Paket, nicht getestet |
| **Debian 12 / 13** | 🧪 Testing | OBS-Paket, nicht getestet |
| **COSMIC Desktop** | 🔧 In Arbeit | Launcher-Kompatibilität WIP |
| **SteamOS / Windows** | 📋 Geplant | — |

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://build.opensuse.org/package/show/home:knoelliX/nativmix)

---

## Installation

→ **[Vollständige Installationsanleitung](wiki/DE/Installation.md)**

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

## Dokumentation

- [Wiki (EN)](wiki/EN/Home.md)
- [Wiki (DE)](wiki/DE/Home.md)

---

## Lizenz
GPL-3.0 – siehe [LICENSE](LICENSE) für Details.
