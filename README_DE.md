# NativMix (Deutsch)

NativMix ist ein moderner, hardwaregestützter Lautstärkemixer für Linux, entwickelt mit PyQt6. Als leistungsstarke Alternative zu deej verbindet er physische Arduino-Potentiometer über USB mit dem PipeWire/PulseAudio-Stack.

![NativMix Icon](assets/icon.png)

## Funktionen

### 🎚️ Hardware-Integration
- **Physische Regler**: Weise jeden Arduino-Potentiometer einer oder mehreren Anwendungen zu.
- **Kubisches Lautstärke-Mapping**: Die physische Mitte des Potentiometers entspricht ~50% der wahrgenommenen Lautstärke (Gehörkurve).
- **Poti-Invertierung**: Drehe die Richtung eines Potentiometers um (0 = 100%, 1023 = 0%), konfigurierbar pro Kanal oder global.
- **Auto-Erkennung & Hot-Plug**: Erkennt `/dev/ttyACM0`, `/dev/ttyUSB0` oder beliebige USB-Seriel-Geräte automatisch. Verbindet sich nach einem Trennen selbstständig wieder.
- **Auto-Unmute**: Ein stummgeschalteter Kanal wird automatisch wieder laut, wenn der physische Regler deutlich bewegt wird.

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

### 🔌 IPC & CLI-Steuerung
Stummschalten per Kommandozeile – ideal für globale Tastenkombinationen:
```bash
nativmix --toggle-mute 0   # Kanal 0 stummschalten/laut schalten
```
Nutzt einen `QLocalServer`/`QLocalSocket` IPC-Kanal.

### ⚙️ Einstellungen
- **Serielle Port-Auswahl**: Port wählen oder Auto-Erkennung nutzen. Der verbundene Port wird mit ★ markiert.
- **Master-Ausgang**: Wähle das Standard-Wiedergabegerät direkt aus NativMix.
- **Autostart**: Aktivieren/Deaktivieren durch Kopieren/Entfernen der `.desktop`-Datei in `~/.config/autostart/`.
- **Transparenz**: Fenstertransparenz ein-/ausschalten.
- **Panic Button**: Ein-Klick-Reset – evakuiert alle Apps aus V-Sinks, entfernt Sinks, setzt Routing zurück.
- **Debug-Logging**: Aktiviert ausführliches `DEBUG`-Level-Logging dynamisch (wirkt sofort).
- **Log-Ordner öffnen**: Öffnet das Log-Verzeichnis im System-Dateimanager.

### 🗂️ Tray-Icon
- Linksklick oder Doppelklick zum Ein-/Ausblenden des Hauptfensters.
- Rechtsklick zum Beenden.

---

## Installation (Arch Linux / CachyOS)

### via AUR
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
python-pyqt6  python-pulsectl  python-pyserial  python-setproctitle
```

---

## Verwendung

```bash
nativmix                    # Anwendung starten
nativmix --toggle-mute 0   # Kanal 0 stummschalten (für Hotkeys)
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
Logs werden nach `~/.local/share/nativmix/logs/nativmix.log` geschrieben (rotierend, max. 5 MB, 3 Backups).

---

## Lizenz

GPL-3.0 – siehe [LICENSE](LICENSE) für Details.
