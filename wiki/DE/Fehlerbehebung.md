# Fehlerbehebung

## Arduino / Serieller Port

**NativMix erkennt den Arduino nicht**
- Prüfe ob das Gerät unter `/dev/` erscheint (`ls /dev/ttyACM* /dev/ttyUSB*`).
- Stelle sicher, dass dein Benutzer Zugriff auf den Port hat — siehe [Berechtigungen](Berechtigungen.md).
- Wähle den Port manuell im NativMix-Einstellungspanel.

**Serieller Port trennt sich häufig**
- Meist ein USB-Kabel- oder Stromproblem. Die Hot-Plug-Wiederverbindung ist automatisch.

---

## Audio / PipeWire

**App-Lautstärke wird nicht gesteuert**
- Stelle sicher, dass die App unter `nativmix --list-apps` aufgeführt ist.
- Einige Electron/Chromium-Apps (Discord, Spotify) erscheinen möglicherweise unter einem anderen Stream-Namen. NativMix löst diese via `/proc` auf, was beim Stream-Start einen Moment dauern kann.

**Lautstärke-Spikes beim Spulen oder Vor-/Zurückspringen**
- Aktiviere einen **V-Sink** für diesen Kanal — siehe [Virtual Sinks](Virtual-Sinks.md).

**App ist stummgeschaltet und wird nicht wieder laut**
- Bewege den physischen Regler deutlich (>5% von der Position beim Stummschalten) — das löst den Auto-Unmute aus.
- Oder klicke den Mute-Button im NativMix-GUI.

---

## Virtual Sinks

**App ist nach dem Aktivieren eines V-Sinks stumm**
- Kurz warten — NativMix braucht ~50 ms, bis PipeWire den neuen Sink registriert hat und der Fader-Wert gesetzt wird.
- Hilft das nicht, nutze den **Panic Button** in den Einstellungen, um das gesamte V-Sink-Routing zurückzusetzen, und aktiviere dann erneut.

**System-Lautstärke ändert sich beim Bewegen eines Sliders nach V-Sink-Erstellung**
- Das war ein bekanntes Problem, das behoben wurde. Stelle sicher, dass du die neueste Version verwendest.

---

## Logs

Aktiviere **Debug-Logging** im Einstellungspanel und prüfe die Log-Datei unter `~/.cache/nativmix/logs/nativmix.log` für detaillierte Ausgaben.
