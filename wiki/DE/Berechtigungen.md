# Berechtigungen & Hardware-Zugriff

## Automatisch (Empfohlen)

Für **Arch Linux** und **openSUSE** enthalten die Pakete eine eigene udev-Regel (`99-nativmix-arduino.rules`), die dem aktiven Benutzer automatisch Zugriff über das `uaccess`-Tag gewährt.

Regeln nach manueller Installation neu laden:
```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Manueller Fallback

Falls die automatische Konfiguration fehlschlägt, füge deinen Nutzer den entsprechenden Gruppen hinzu und starte die Sitzung neu:

| Distribution | Befehl |
| :--- | :--- |
| Arch Linux / CachyOS | `sudo usermod -aG uucp,audio $USER` |
| openSUSE | `sudo usermod -aG dialout,audio $USER` |
| Fedora / Ubuntu / Debian | `sudo usermod -aG dialout,audio $USER` |

> [!IMPORTANT]
> Du musst dich einmal ab- und wieder anmelden, damit die Gruppenänderungen wirksam werden.
