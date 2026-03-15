# Virtual Sinks (V-Sinks)

NativMix nutzt dedizierte Software-Ausgabegeräte (Virtual Sinks) in PipeWire, um ein verbreitetes Linux-Audioproblem zu lösen: **Lautstärke-Spikes.**

## Das Problem: Lautstärke-Spikes

Viele Anwendungen (z.B. Webbrowser oder Media Player) setzen ihren internen Stream-Pegel beim Spulen, Vor-/Zurückspringen per Tastatur oder beim Wiederaufnehmen eines „hängenden" Streams kurzzeitig auf 100% zurück. Das führt zu schmerzhaften Vollgas-Spikes, bevor PipeWire oder ein normaler Mixer den richtigen Pegel wieder anlegen kann.

## Die Lösung: Isolation via V-Sinks

Durch einen Virtual Sink als Zwischenschicht entkoppelt NativMix die App vom physischen Ausgang:

- **Signalweg**: `App (fest auf 100% Unity Gain) → V-Sink (gesteuert vom Hardware-Regler) → Physischer Ausgang`
- **Persistenz**: Da die App immer auf 100% innerhalb ihres eigenen „Tunnels" läuft, hat ein seek-bedingter Reset **keinen Einfluss** auf die tatsächliche Ausgabelautstärke.
- **Hardware-Präzision**: Der physische Regler steuert den *Virtual Sink* selbst — eine Lautstärkegrenze, die die App nicht umgehen kann.

## Features & Einschränkungen

| Feature | Beschreibung |
| :--- | :--- |
| **Sicheres Ein-/Ausschalten** | Beim Deaktivieren setzt NativMix zuerst die App-Lautstärke auf den Fader-Wert, bevor PipeWire den Stream nahtlos rettet — ohne Pause oder Unterbrechung der Wiedergabe. |
| **Live-App-Zuweisung** | Wird eine bereits laufende App einem Kanal mit aktivem V-Sink zugewiesen, wird sie sofort hineingerouted — kein Neuerstellen des Sinks oder Neustart der App nötig. |
| **Erstellungssperre** | Während ein neuer V-Sink aufgebaut wird, werden Slider-Eingaben für diesen Kanal kurzzeitig unterdrückt. Erst wenn PipeWire den Sink registriert hat (~50 ms) und der echte Fader-Wert gesetzt wurde, wird die Sperre aufgehoben. Das verhindert, dass einzelne Slider-Ticks versehentlich auf den System-Sink statt auf den neuen V-Sink schreiben. |
| **Isolierungsregeln** | Die Kanäle *System Master* und *Alle anderen Apps* können nicht über V-Sinks geroutet werden (würde Rückkopplungsschleifen oder unkontrolliertes System-Lautstärkeverhalten verursachen). |

## So aktivierst du einen V-Sink

1. Öffne das Einstellungspanel eines Kanals.
2. Aktiviere den **V-Sink**-Schalter.
3. NativMix erstellt den virtuellen Sink und routet die zugewiesenen Apps automatisch.

> [!TIP]
> Nutze den **Panic Button** in den Einstellungen, um sofort alle Apps aus V-Sinks zu evakuieren und die Sinks zu entfernen — hilfreich wenn etwas hängt.
