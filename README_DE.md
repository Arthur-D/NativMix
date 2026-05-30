# NativMix (Deutsch)

NativMix ist ein hardwaregestützter Lautstärkemixer für Linux, entwickelt mit PyQt6. Er verbindet physische Arduino-Potentiometer über USB mit PipeWire/PulseAudio und ermöglicht die Lautstärkeregelung einzelner Apps über echte Regler. Jeder Kanal lässt sich einer oder mehreren Apps, einem Gerät oder dem System-Master zuweisen. **Virtual Sinks** isolieren Apps in einem eigenen PipeWire Null-Sink — seek-bedingte Lautstärke-Spikes erreichen deine Lautsprecher nie mehr, weil der Regler den Sink steuert und die App intern auf Unity Gain läuft. Neue Streams werden sofort stumm geschaltet (Two-Stage Mute-Catch), bevor Metadaten verfügbar sind, und dann auf dem richtigen Fader-Pegel freigegeben. MIDI-CC-Controller werden nativ neben dem Arduino unterstützt, mit MIDI-Learn und einem integrierten virtuellen MIDI-Port. Die GUI passt sich automatisch ans System-Theme an (via XDG Desktop Portal) und funktioniert auf KDE, GNOME und allen XDG-konformen Desktops einschließlich Wayland.

![NativMix Icon](assets/icon.png)

<div align="center">

| Breeze Theme (Native) | Iridescent Theme |
|:---:|:---:|
| ![Breeze Theme](assets/Breeze.jpg) | ![Iridescent Theme](assets/Iridescent_Lightly_3.jpg) |

![Nothing](assets/nothing.jpg)

</div>

---

## Status

> Daten zur MIDI-Stabilität werden noch gesammelt — Feedback von MIDI-Nutzern ist sehr willkommen.
>
> **Hinweis zu "Stabil":** Sofern nicht anders angegeben bedeutet "Stabil", dass das Paket installiert und beim ersten Start keine offensichtlichen Fehler auftreten. Nur **Arch Linux / CachyOS** wird täglich genutzt und produktiv getestet.

| Betriebssystem | Status | Hinweis |
| :--- | :---: | :--- |
| **Arch Linux / CachyOS** | ✅ Stabil | AUR-Paket, täglich genutzt |
| **Ubuntu 25.04 / 25.10** | ✅ Stabil | OBS-Paket, getestet auf Pop!_OS |
| **Ubuntu 24.04 / 24.10** | ✅ Stabil | OBS-Paket |
| **Linux Mint 22** | ✅ Stabil | Nutzt Ubuntu-24.04-OBS-Paket |
| **Pop!_OS** | ✅ Stabil | COSMIC Desktop, GUI getestet, keine Log-Fehler |
| **openSUSE Tumbleweed** | ✅ Stabil | OBS-Paket, GUI getestet, keine Log-Fehler |
| **openSUSE Slowroll** | ❓ Ungetestet | OBS-Paket |
| **Fedora 42 / 43** | ✅ Stabil | OBS-Paket, Grundfunktionen getestet — nutzt portmidi statt rtmidi (kein virtueller MIDI-Port) |
| **Debian 12 / 13** | ✅ Stabil | OBS-Paket — basierend auf Ubuntu-Kompatibilität |
| **Raspberry Pi OS** | ❓ Ungetestet | Kann nicht verifiziert werden — Hardware nicht verfügbar |
| **Windows 10 / 11** | 🔧 In Arbeit | Frühe Alpha — Installer verfügbar, wird aktiv entwickelt |

> **Windows-Tester gesucht!** Das Windows-Backend funktioniert in ersten Tests, hat aber noch kein echtes Alltagsfeedback bekommen.
> Wer NativMix auf Windows ein paar Tage im Alltag nutzt — ich freue mich über Rückmeldungen: Abstürze, Eigenheiten, alles was auffällt.
> Bitte einfach ein Issue öffnen oder kommentieren: [GitHub Issues](https://github.com/knoellix/NativMix/issues)

| Desktop-Umgebung | Status | Hinweis |
| :--- | :---: | :--- |
| **KDE Plasma** | ✅ Stabil | Wayland + X11, täglich genutzt |
| **COSMIC** | ✅ Stabil | Getestet auf Pop!_OS |
| **GNOME** | ✅ Stabil | Grundfunktionen bestätigt — wer es täglich nutzt, über Feedback freue ich mich! |

> **Fedora & GNOME — Feedback willkommen!** Fedora nutzt portmidi statt rtmidi — virtueller MIDI-Port ist dort nicht verfügbar.
> Wenn bei dir alles funktioniert (GUI, Fader, MIDI), kurze Rückmeldung wäre super: [GitHub Issues](https://github.com/knoellix/NativMix/issues)

---

## Installation

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://software.opensuse.org/download.html?project=home%3AknoelliX&package=nativmix)

→ **[Vollständige Installationsanleitung](https://github.com/knoellix/NativMix/wiki/DE-Installation)**

**Arch Linux / CachyOS:**
```bash
paru -S nativmix
```

---

## Dokumentation

- [Wiki (EN)](https://github.com/knoellix/NativMix/wiki/)
- [Wiki (DE)](https://github.com/knoellix/NativMix/wiki/DE-Home)

---

## Update-Verlauf

**v1.0.14**
- Fix: Redundante Pulse-Volume-Writes werden übersprungen — verhindert FIFO-Anstieg in GNOME Shell bei Systemlautstärke (behebt #19)
- Fix: Persistente Pulse-Verbindung wird für Hardware-Lautstärke bei Arduino/MIDI-Ticks wiederverwendet
- Fix: Transiente Arduino-Reconnect-Frames mit falscher Kanalanzahl werden verworfen
- Fix: Fader-Slider zeigen gespeicherte Positionen nach Tray-Schließen und Neuöffnen; Audio war bereits korrekt (behebt #17)

→ [Vollständiger Changelog](CHANGELOG.md)

---

## Lizenz
GPL-3.0 – siehe [LICENSE](LICENSE) für Details.
