# MIDI-Steuerung

NativMix hat native MIDI-Unterstützung zur Lautstärkeregelung über beliebige MIDI-CC-Geräte.

## Funktionen

- **MIDI-Learn** — Weise MIDI-CC-Regler oder Fader direkt aus der GUI dynamisch jedem Kanal zu.
- **Virtual MIDI Port** — Ein integriertes virtuelles MIDI-Gerät („NativMix") steht für headless Routing (z.B. via `pw-link`) zur Verfügung — ohne physische Kabel oder Loopbacks.
- **Direkte Integration** — Native Unterstützung für ALSA und USB-MIDI-Controller via `mido` / `python-rtmidi`.
- **Präzise Regelung** — Latenzarme Lautstärkeregelung mit 7-Bit MIDI-Auflösung.

## Eingabe-Modi

NativMix unterstützt drei Eingabe-Modi, die in den Einstellungen konfigurierbar sind:

| Modus | Beschreibung |
| :--- | :--- |
| `usb` | Nur die Arduino-Potentiometer steuern die Lautstärke. |
| `midi_only` | Nur MIDI-CC-Nachrichten steuern die Lautstärke. |
| `hybrid` | Arduino und MIDI steuern die Lautstärke gleichzeitig. |

## MIDI-Learn

1. Öffne das Einstellungspanel eines Kanals.
2. Klicke auf **MIDI-Learn** und bewege den gewünschten Regler/Fader am MIDI-Controller.
3. Die CC-Nummer wird automatisch zugewiesen.

## Virtueller MIDI-Port

Der integrierte virtuelle Port **„NativMix"** ist immer in ALSA/PipeWire verfügbar. Du kannst beliebige MIDI-Quellen via `pw-link` oder einem Patchbay wie **Helvum** oder **Carla** damit verbinden.
