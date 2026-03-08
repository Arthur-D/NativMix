# NativMix (Deutsch)

NativMix ist ein moderner, hardwaregestützter Lautstärkemixer für Linux, entwickelt mit PyQt6. Als leistungsstarke Alternative zu deej verbindet er physische Arduino-Potentiometer über USB mit dem PipeWire/PulseAudio-Stack.

![NativMix Icon](assets/icon.png)

## Screenshots

| Breeze Theme (Native) | Iridescent Theme |
| :---: | :---: |
| ![Breeze](assets/Breeze.jpg) | ![Iridescent](assets/Iridescent_Lightly_3.jpg) |

## Funktionen

### 🎚️ Hardware-Integration
- **[deej](https://github.com/omriharel/deej) Kompatibilität**: Vollständig kompatibel mit dem Standard-deej-Arduino-Protokoll.
- **Verbesserte Firmware**: Für ein optimales Erlebnis und aktuellen Arduino-Code empfehlen wir **[deejHotkey](https://github.com/knoellix/deejHotkey)**. Dieses Repository bietet optimierte Sketches und moderne Alternativen zur ursprünglichen deej-Firmware, speziell angepasst für fortgeschrittene Setups.
- **Physische Regler**: Weise jeden Arduino-Potentiometer einer oder mehreren Anwendungen zu.
- **Einstellbare Lautstärkekurve**: Anpassbare Fader-Kurve (Linear bis Kubisch) für ein natürliches Lautstärkeempfinden.
- **Poti-Invertierung**: Drehe die Richtung eines Potentiometers um (0 = 100%, 1023 = 0%), konfigurierbar pro Kanal oder global.
- **Auto-Erkennung & Hot-Plug**: Erkennt `/dev/ttyACM0`, `/dev/ttyUSB0` oder beliebige USB-Seriel-Geräte automatisch. Verbindet sich nach einem Trennen selbstständig wieder.
- **Auto-Unmute**: Ein stummgeschalteter Kanal wird automatisch wieder laut, wenn der physische Regler deutlich bewegt wird.
- **🎹 MIDI-Steuerung**:
  - **MIDI-Learn**: Weise MIDI-CC-Regler oder Fader dynamisch jedem Kanal zu.
  - **Direkte Integration**: Native Unterstützung für ALSA und USB-MIDI-Controller via `mido`.
  - **Präzise Regelung**: Latenzarme Lautstärkeregelung mit 7-Bit MIDI-Auflösung.

### 🔊 Audio-Routing
- **App-Modus**: Steuere die Lautstärke einzelner Anwendungs-Audiostreams direkt.
- **Multi-App-Gruppierung**: Weise mehrere Anwendungen einem einzigen Regler zu.
- **System Master**: Ein Kanal kann die Master-Lautstärke und den Mute-Status des Standard-Ausgabegeräts steuern.
- **Alle anderen Apps**: Ein Kanal steuert alle nicht explizit zugewiesenen Audiostreams als Gruppe.
- **Geräte-Modus**: Steuere die Lautstärke physikalischer Audio-Geräte direkt (Lautsprecher, Kopfhörer, Mikrofone).

### 🔁 Pro-Routing: Virtual Sinks (V-Sinks)
Ein virtueller Audio-Sink ist ein dediziertes Software-Ausgabegerät in PipeWire.
- Wenn aktiviert, wird Audio geroutet: `App → V-Sink → Physischer Ausgang`.
- Der Hardware-Regler steuert die Lautstärke des V-Sinks; die App spielt intern auf 100% (Unity Gain).
- **Sicheres Ein-/Ausschalten**: Beim Deaktivieren setzt NativMix zuerst die App-Lautstärke auf den Fader-Wert, bevor PipeWire den Stream nahtlos rettet – ohne Pause oder Unterbrechung der Wiedergabe.
- **Isolierungsregeln**: System Master und „Alle anderen Apps" dürfen keine V-Sinks verwenden.

### 🛡️ Mute-Catch Reflex-System (Regel 11)
Verhindert 100%-Lautstärke-Knalls wenn neue Audiostreams starten:
- **Stufe 1 (Reflex)**: Schaltet jeden neuen Stream sofort stumm, bevor Metadaten verfügbar sind.
- **Stufe 2 (Auflösung)**: Sobald der App-Name ermittelt wurde, wird die gespeicherte Regler-Lautstärke gesetzt und der Stream wieder laut geschaltet.

### 🧠 Intelligente App-Erkennung
- Identifiziert sandboxed Electron/Chromium-Apps (Discord, Spotify, Chrome) durch Auslesen von `/proc/<PID>/cmdline` und Durchlaufen des Prozessbaums.
- Handhabt generische Stream-Namen wie „Chromium" oder „WEBRTC Voice Engine" korrekt.

### 🎨 Natives System-Theming
- Liest Dark/Light-Mode und Akzentfarbe über das **XDG Desktop Portal** (`org.freedesktop.portal.Settings`) – funktioniert auf KDE, GNOME und allen XDG-konformen Desktops.
- Style-Priorität: **Kvantum** → **Breeze** → **Fusion** (mit dunkler Fallback-Palette).
- Regler-Farben nutzen die System-Akzentfarbe via `QPalette`.
- **Transparenz**: Optionaler halbtransparenter Fensterhintergrund.

### 🖥️ Native Wayland-Integration
- Prozesstitel wird via `setproctitle` gesetzt (korrekter Name in `htop` etc.).
- Fenster/Icon wird über `app.setDesktopFileName()` mit der `.desktop`-Datei verknüpft.
- Kein X11-Window-Scraping (`wmctrl`, `xdotool`).

### 🔌 IPC & CLI-Steuerung (Globale Hotkeys)

NativMix verfügt über einen integrierten IPC-Server (Inter-Process Communication). Dies ermöglicht es Ihnen, die laufende Instanz von NativMix über die Kommandozeile zu steuern, ohne eine zweite GUI zu starten oder die Hardware-Kommunikation zu unterbrechen.

Dies ist ideal, um Medientasten der Tastatur oder benutzerdefinierte Makros mit NativMix-Aktionen zu verknüpfen.

#### Stummschalten-Befehl
Um den Stummschaltungszustand eines bestimmten Kanals umzuschalten (z. B. Ihr Mikrofon oder Discord auf Kanal 0):

```bash
sh -c "/usr/bin/nativmix --toggle-mute 0"
```
Nutzt einen reinen Python-Socket IPC-Kanal für zuverlässige Ausführung auch in Headless-Shortcut-Umgebungen.

### ⚙️ Einstellungen
- **Serielle Port-Auswahl**: Port wählen oder Auto-Erkennung nutzen. Der verbundene Port wird mit ★ markiert.
- **Master-Ausgang**: Wähle das Standard-Wiedergabegerät direkt aus NativMix.
- **Fader Curve Intensity**: Slider zur Anpassung der Lautstärkekurve (Linear » Quadratisch » Kubisch).
- **Don't hide**: Wenn aktiviert, wird das Fenster beim Schließen (X) nur in den System-Tray minimiert. Der Hintergrunddienst bleibt aktiv.
- **Autostart**: Aktivieren/Deaktivieren durch Kopieren/Entfernen der `.desktop`-Datei in `~/.config/autostart/`.
- **Transparenz**: Fenstertransparenz ein-/ausschalten.
- **Panic Button**: Ein-Klick-Reset – evakuiert alle Apps aus V-Sinks, entfernt Sinks, setzt Routing zurück.
- **Debug-Logging**: Aktiviert ausführliches `DEBUG`-Level-Logging dynamisch (wirkt sofort).
- **Log-Ordner öffnen**: Öffnet das Log-Verzeichnis im System-Dateimanager.

### 🗂️ Tray-Icon
- Linksklick oder Doppelklick zum Ein-/Ausblenden des Hauptfensters.
- Rechtsklick zum Beenden.

---

### Unterstützte Betriebssysteme

| Betriebssystem | Status | Installationsmethode |
| :--- | :--- | :--- |
| **Arch Linux / CachyOS** | Stabil | `paru -S nativmix` (AUR) |
| **Ubuntu / Debian / Mint / Pop!_OS** | Stabil | `.deb` über [OBS Repository](https://download.opensuse.org/repositories/home:/knoelliX/) |
| **Fedora / openSUSE Tumbleweed** | Stabil | `.rpm` über [OBS Repository](https://download.opensuse.org/repositories/home:/knoelliX/) |
| **Raspberry Pi OS (Raspbian)** | Stabil | `.deb` über [OBS Repository](https://download.opensuse.org/repositories/home:/knoelliX/) |
| **SteamOS** | In Arbeit | Manuelle Installation (Flatpak folgt bald) |
| **Winndoof** | Geplant | Nativer Installer folgt bald |

> **Hinweis für Nutzer von Ubuntu / Fedora / Debian / openSUSE:**
> Klicke im obigen Link zum [OBS Repository](https://download.opensuse.org/repositories/home:/knoelliX/) einfach auf deine Distribution. Dort findest du die genauen Terminal-Befehle, um den GPG-Schlüssel hinzuzufügen und NativMix sicher über deinen normalen Paketmanager (`apt`, `dnf` oder `zypper`) zu installieren. So ist sichergestellt, dass du zukünftige Updates ganz automatisch erhältst!
---

## Installation

### Arch Linux / CachyOS (via AUR)

```bash
paru -S nativmix-git
```

### Manuelle Installation
```bash
git clone https://github.com/your-user/nativmix.git
cd nativmix
makepkg -si
```

### Abhängigkeiten
```
python-pyqt6  python-pulsectl  python-pyserial  python-setproctitle  python-mido  python-rtmidi
```

---

## Verwendung

```bash
nativmix                                      # Anwendung starten
sh -c "/usr/bin/nativmix --toggle-mute 0"    # Kanal 0 stummschalten (für Hotkeys)
```

---

## Hardware-Setup (Arduino)

NativMix ist kompatibel mit Standard-**deej**-Firmware. Der Arduino sendet pipe-separierte ADC-Werte (0–1023) als zeilengetrennte Zeichenkette:

```
512|0|1023|256\n
```

Beliebig viele Kanäle werden unterstützt. NativMix passt sich dynamisch an wenn sich die Kanal-Anzahl ändert.

---

## Konfiguration

Einstellungen werden in `~/.config/nativmix/config.json` gespeichert (XDG-Standard).
Daten und Logs werden nach `~/.local/share/nativmix/` und `~/.cache/nativmix/logs/nativmix.log` geschrieben.

---

## Lizenz

GPL-3.0 – siehe [LICENSE](LICENSE) für Details.
