# Hardware-Setup (Arduino)

NativMix ist kompatibel mit Standard-**[deej](https://github.com/omriharel/deej)**-Firmware.

## Protokoll

Der Arduino sendet pipe-separierte ADC-Werte (0–1023) als zeilengetrennte Zeichenkette:

```
512|0|1023|256\n
```

Jeder Wert entspricht einem Potentiometer / Kanal.

## Empfohlene Firmware

Für ein optimales Erlebnis und aktuellen Arduino-Code empfehlen wir **[deejHotkey](https://github.com/knoellix/deejHotkey)**. Dieses Repository bietet optimierte Sketches und moderne Alternativen zur ursprünglichen deej-Firmware, speziell angepasst für fortgeschrittene Setups.

## Auto-Erkennung & Hot-Plug

NativMix erkennt `/dev/ttyACM0`, `/dev/ttyUSB0` oder beliebige USB-Seriel-Geräte automatisch. Wird der Arduino getrennt, verbindet sich NativMix nach dem Wiederanstecken selbstständig neu.

## Berechtigungen

Falls NativMix nicht auf den seriellen Port zugreifen kann, siehe [Berechtigungen](Berechtigungen.md).
