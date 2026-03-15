# Konfiguration

## Datei-Speicherorte

NativMix folgt der XDG Base Directory Specification:

| Typ | Pfad |
| :--- | :--- |
| Konfiguration | `~/.config/nativmix/config.json` |
| Logs | `~/.cache/nativmix/logs/nativmix.log` |
| Daten | `~/.local/share/nativmix/` |

Die Konfigurationsdatei wird beim ersten Start automatisch mit sinnvollen Standardwerten erstellt.

## Log-Rotation

Logs rotieren automatisch bei 5 MB, die letzten 3 Dateien werden behalten.
Den Log-Ordner kannst du direkt aus dem NativMix-Einstellungspanel über **Log-Ordner öffnen** aufrufen.

## Debug-Logging

Aktiviere ausführliches `DEBUG`-Level-Logging im Einstellungspanel. Die Änderung wirkt sofort ohne Neustart von NativMix.
