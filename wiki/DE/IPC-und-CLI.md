# IPC & CLI-Steuerung

NativMix verfügt über einen integrierten IPC-Server (Inter-Process Communication), der auf einem Unix-Socket (`/tmp/nativmix_ipc_<uid>.sock`) lauscht. So kannst du die laufende Instanz über die Kommandozeile steuern — ideal für globale Hotkeys im Desktop-Environment.

## Verfügbare Befehle

| Befehl | Beschreibung |
| :--- | :--- |
| `--toggle-mute <INDEX>` | Schaltet Mute für Kanal `INDEX` um (0-basiert). |
| `--list-sinks` | Zeigt alle aktiven Virtual Sinks und deren Indizes an. |
| `--list-apps` | Zeigt alle erkannten Audio-Anwendungen an. |
| `--show` | Bringt das bereits laufende Fenster in den Vordergrund. |
| *(keine Argumente)* | Wie `--show`. |

## Beispiele

**Discord auf Kanal 1 stummschalten/aufheben:**
```bash
nativmix --toggle-mute 1
```

**Alle aktiven V-Sinks anzeigen:**
```bash
nativmix --list-sinks
```

**Alle erkannten Apps anzeigen:**
```bash
nativmix --list-apps
```

## Globale Hotkeys

Die Befehle lassen sich als Tastenkürzel im Desktop-Environment einrichten.

**KDE (Systemeinstellungen → Kurzbefehle → Eigene Kurzbefehle):**
```
Befehl: sh -c "/usr/bin/nativmix --toggle-mute 0"
```

**GNOME / Hyprland / Sway** — Keybind auf denselben Befehl setzen.

> [!NOTE]
> Eine zweite NativMix-Instanz mit einem IPC-Befehl verbindet sich mit der laufenden Instanz und beendet sich sofort — es wird kein zweites GUI-Fenster geöffnet.
