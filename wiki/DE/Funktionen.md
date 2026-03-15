# Funktionen

## Hardware-Integration

- **deej-Kompatibilität** — Vollständig kompatibel mit dem Standard-deej-Arduino-Protokoll.
- **Physische Regler** — Weise jeden Arduino-Potentiometer einer oder mehreren Anwendungen zu.
- **Einstellbare Lautstärkekurve** — Anpassbare Fader-Kurve (Linear bis Kubisch) für ein natürliches Lautstärkeempfinden.
- **Poti-Invertierung** — Drehe die Richtung eines Potentiometers um (0 = 100%, 1023 = 0%), konfigurierbar pro Kanal oder global.
- **Auto-Erkennung & Hot-Plug** — Erkennt beliebige USB-Seriel-Geräte automatisch und verbindet sich nach einem Trennen selbstständig wieder.
- **Auto-Unmute** — Ein stummgeschalteter Kanal wird automatisch wieder laut, wenn der physische Regler deutlich bewegt wird.

## Audio-Routing

- **App-Modus** — Steuere die Lautstärke einzelner Anwendungs-Audiostreams direkt.
- **Multi-App-Gruppierung** — Weise mehrere Anwendungen einem einzigen Regler zu.
- **System Master** — Ein Kanal kann die Master-Lautstärke und den Mute-Status des Standard-Ausgabegeräts steuern.
- **Alle anderen Apps** — Ein Kanal steuert alle nicht explizit zugewiesenen Audiostreams als Gruppe.
- **Geräte-Modus** — Steuere die Lautstärke physikalischer Audio-Geräte direkt (Lautsprecher, Kopfhörer, Mikrofone).

## Mute-Catch Reflex-System

Verhindert 100%-Lautstärke-Knalls, wenn neue Audiostreams starten:

- **Stufe 1 (Reflex)** — Schaltet jeden neuen Stream sofort stumm, bevor Metadaten verfügbar sind.
- **Stufe 2 (Auflösung)** — Sobald der App-Name ermittelt wurde, wird die gespeicherte Regler-Lautstärke gesetzt und der Stream wieder laut geschaltet.

## Intelligente App-Erkennung

- Identifiziert sandboxed Electron/Chromium-Apps (Discord, Spotify, Chrome) durch Auslesen von `/proc/<PID>/cmdline` und Durchlaufen des Prozessbaums.
- Handhabt generische Stream-Namen wie „Chromium" oder „WEBRTC Voice Engine" korrekt.

## Natives System-Theming

- Liest Dark/Light-Mode und Akzentfarbe über das **XDG Desktop Portal** (`org.freedesktop.portal.Settings`) — funktioniert auf KDE, GNOME und allen XDG-konformen Desktops.
- Style-Priorität: **Kvantum** → **Breeze** → **Fusion** (mit dunkler Fallback-Palette).
- Regler-Farben nutzen die System-Akzentfarbe via `QPalette`.
- Optionaler halbtransparenter Fensterhintergrund.

## Native Wayland-Integration

- Prozesstitel wird via `setproctitle` gesetzt (korrekter Name in `htop` etc.).
- Fenster/Icon wird über `app.setDesktopFileName()` mit der `.desktop`-Datei verknüpft.
- Kein X11-Window-Scraping (`wmctrl`, `xdotool`).

## Einstellungen

| Einstellung | Beschreibung |
| :--- | :--- |
| Serielle Port-Auswahl | Port wählen oder Auto-Erkennung nutzen. Der verbundene Port wird mit ★ markiert. |
| Master-Ausgang | Wähle das Standard-Wiedergabegerät direkt aus NativMix. |
| Fader Curve Intensity | Anpassung der Lautstärkekurve (Linear » Quadratisch » Kubisch). |
| Don't hide | Beim Schließen (X) wird das Fenster nur in den System-Tray minimiert. |
| Autostart | Aktivieren/Deaktivieren via `~/.config/autostart/`. |
| Transparenz | Fenstertransparenz ein-/ausschalten. |
| Panic Button | Ein-Klick-Reset — evakuiert alle Apps aus V-Sinks, entfernt Sinks, setzt Routing zurück. |
| Debug-Logging | Aktiviert ausführliches `DEBUG`-Level-Logging (wirkt sofort). |
| Log-Ordner öffnen | Öffnet das Log-Verzeichnis im System-Dateimanager. |

## Tray-Icon

- Linksklick oder Doppelklick zum Ein-/Ausblenden des Hauptfensters.
- Rechtsklick zum Beenden.
