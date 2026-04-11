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

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://build.opensuse.org/package/show/home:knoelliX/nativmix)

---

## Installation

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

**v1.0.11**
- Fix: `--list-sinks` / `--list-apps` IPC gibt jetzt korrekt Daten zurück (`shutdown(SHUT_WR)` vom Client versetzte Qt-Socket in nicht-beschreibbaren Zustand vor der Antwort)
- Fix: gemappte Apps werden nicht mehr von „Other Apps" mitgesteuert (`media.name` zu `pa_fallback` in Volume- und Mute-Pfaden ergänzt)
- Fix: IPC `readyRead` Race Condition bei neuer Verbindung — `bytesAvailable`-Guard verhindert verpasste Events
- Fix: AUR-Deploy-Workflow-Berechtigungen auf Job-Ebene verschoben (Principle of Least Privilege)
- Feat: `GENERIC_PA_NAMES` — erkennt und kennzeichnet anonyme/virtuelle Streams (pid=0, kein Prozess)
- Feat: `spotify-bin` und `brave-bin` zur Binary-Resolver-Map für AUR-Installationen ergänzt
- Feat: Stream-Picker zeigt `[no process — map by name]`-Hinweis für anonyme Streams
- Feat: Feld `anonymous` zur `--list-apps`-Ausgabe hinzugefügt

**v1.0.10**
- Refactor: private Signal-Verbindungen durch öffentliche API ersetzt (`on_midi_connection_changed`, `open_settings`, `on_mapping_changed`)
- Refactor: Arduino-Verbindungs-Handler in benannte Funktion mit Fehlerbehandlung extrahiert
- Refactor: MIDI-Statusfarben als Modul-Konstante extrahiert
- Refactor: Backend-Factory-Lambdas durch benannte Funktionen ersetzt
- Refactor: `trigger_panic()`-Alias aus MidiThread entfernt — `midi_panic_triggered` verbindet direkt auf `restart_midi`
- Fix: Mute-Button-Lambda akzeptiert jetzt `checked=False` passend zum PyQt6-`clicked(bool)`-Signal
- Fix: `_on_add_midi_clicked`-Slot mit `@pyqtSlot(bool)` dekoriert passend zum `clicked`-Signal
- Fix: PyQt6-QWidgets-Imports sortiert (ruff I001)
- Fix: veraltete `/tmp`-Referenz im `paths.py`-Docstring auf `$XDG_RUNTIME_DIR` korrigiert
- Feat: Tray-Menü — „NativMix neu starten"-Eintrag ergänzt (entspricht `nativmix --restart`)

**v1.0.9**
- Feat: Kompakt-Modus — Schalter in der Titelleiste klappt den Mixer auf Fader-Only-Ansicht zusammen; Fenster schrumpft passend, Fader-Abstände bleiben erhalten
- Feat: MIDI Mute-CC — beliebigen MIDI-Button/Schalter dem Mute-Toggle eines Kanals zuweisen; nur CC-Wert 127 löst aus (Tastendruck), Fader sind sicher
- Feat: Kanal-Bearbeitung per Toggle — zeigt/versteckt die per-Kanal-Buttons (Learn, Mute-CC, Löschen) ohne den Mixer zu überladen
- Feat: `nativmix --restart` IPC-Befehl — startet die laufende Instanz vollständig neu (alle Threads und Audio-Zustand werden neu geladen)
- Feat: Auto-Neustart nach Paket-Update — prüft alle 60 s die installierte Version und startet automatisch neu bei einem Upgrade
- Feat: PipeWire-Reconnect + V-Sink-Wiederherstellung — nach einem PipeWire-Neustart wird nach 3 s ein Audio-Audit ausgeführt und alle V-Sinks werden neu erstellt
- Perf: Event-Deduplizierung im PipeWire-Listener — PipeWire sendet pro Stream-Property-Änderung ein separates Change-Event, was beim App-Start 20+ redundante Callbacks erzeugt; nur Events mit tatsächlichen Lautstärke-/Mute-Änderungen werden jetzt verarbeitet
- Perf: Persistente PulseAudio-Verbindung für Volume-Operationen — reduziert schrittweises RAM-Wachstum
- Perf: Fenster-Geometrie-Schreibvorgänge auf 500 ms gedrosselt — eliminiert QSettings-Spam beim Fenster ziehen
- Fix: V-Sink-Anzeigename in pavucontrol/Helvum zeigt nur noch `NativMix_CH_0` statt dem vollständigen Flags-String
- Fix: SPDX-Lizenz-String in pyproject.toml (setuptools-Deprecation-Warnung behoben)
- Fix: MIDI-Kanal-Löschen-Button war durch einen TypeError (fehlender bool-Parameter am Slot) lautlos blockiert
- Fix: Kanal-Bearbeitungs-Modus bleibt jetzt nach dem Hinzufügen oder Löschen eines MIDI-Kanals aktiv
- Fix: Learn-Buttons zeigen „Cancel" während des Wartens; Escape-Taste oder erneutes Klicken bricht MIDI-Learn ohne CC-Zuweisung ab
- Fix: Windows — System-Master-Lautstärke verursachte beim ersten Zugriff einen AttributeError
- Fix: Windows — Audio-Thread konnte nach stop()-Timeout zu einem Zombie werden

**v1.0.8**
- Fix: MIDI-Input steuert jetzt korrekt Hardware-Ausgabegeräte (Hardware-Mode-Kanäle haben die Lautstärke nicht angewendet)
- Fix: Garbage-Serailframes nach Arduino-Reconnect (z.B. durch Steam/Spiele die den USB-Bus kurz stören) lösen keinen falschen Channel-Count-Reset und GUI-Rebuild mehr aus

**v1.0.7**
- Windows: Installer (PyInstaller + Inno Setup), frühe Alpha
- Windows: WASAPI-Audio-Backend implementiert (pycaw), Stabilität wird evaluiert
- Windows: App-Lautstärkeregelung via Arduino implementiert (frühe Alpha)
- Windows: System-Master-Lautstärke via WASAPI (IAudioEndpointVolume)
- Windows: Kanal auf Hardware-Ausgabegerät gemappt nicht unterstützt
- Windows: Virtueller MIDI-Port ausgeblendet — nicht geplant (WinMM hat keine virtuellen Ports)
- Windows: Virtual Sinks nicht geplant
- KDE X11 + GNOME X11: Fensterposition springt nicht mehr zur Mitte
- Fedora/Nobara: Virtueller MIDI-Port deaktiviert — Plattform-Einschränkung (portmidi, kein ALSA Virtual Port)
- MIDI: Circuit Breaker — GUI wird vor wiederholten MIDI-Backend-Abstürzen geschützt (nach 3 aufeinanderfolgenden Fehlern deaktiviert, manueller Neustart möglich)
- MIDI: automatische Wiederherstellung mit Cooldown bei kurzzeitigen Fehlern
- Konfiguration: korrupte config.json wird automatisch als config.json.bak gesichert statt still überschrieben
- Stabilität: diverse Fixes für Resource Leaks und Fehlerbehandlung (Windows IPC, MIDI Port, Null-Sink Timeout)
- About-Bereich zeigt Versionsnummer

**v1.0.6**
- App-Pinning und Kanal-Umbenennung
- systemd-Autostart + XDG-Konfigurationsmigration
- portmidi-Fix für Fedora/Nobara
- Abgerundete Ecken immer aktiv
- Wayland: Systemherunterfahren wird nicht mehr vom Fenster blockiert

**v1.0.5**
- V-Sink-Neustart-Stabilitätsfix
- Verbesserte Wayland/COSMIC-Integration
- MIDI-Wiederherstellung bei Gerätetrennung

**v1.0.4**
- PipeWire-Update-Behandlung, Autostart-Fix, verbesserte Fehlerbehandlung

**v1.0.3**
- openSUSE-Paketierung
- AUR-Automatisierung
- App-Filterung und V-Sink-Routing-Verbesserungen

**v1.0.2**
- MIDI-Sync- und Moduswechsel-Fixes
- UI-Stabilitätsverbesserungen

**v1.0.1**
- Tray-Icon-Fix
- "Andere Apps"-Kanal-Sichtbarkeit

---

## Lizenz
GPL-3.0 – siehe [LICENSE](LICENSE) für Details.
