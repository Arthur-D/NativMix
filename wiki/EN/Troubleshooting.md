# Troubleshooting

## Arduino / Serial Port

**NativMix does not detect the Arduino**
- Check that the device appears in `/dev/` (`ls /dev/ttyACM* /dev/ttyUSB*`).
- Make sure your user has permission to access the port — see [Permissions](Permissions.md).
- Try selecting the port manually in the NativMix settings panel.

**Serial port disconnects frequently**
- This is usually a USB cable or power issue. Hot-Plug reconnection is automatic.

---

## Audio / PipeWire

**App volume is not being controlled**
- Make sure the app is listed under **List Apps** (`nativmix --list-apps`).
- Some Electron/Chromium apps (Discord, Spotify) may appear under a different stream name. NativMix resolves these via `/proc`, but it may take a moment on stream start.

**Volume spikes when seeking or rewinding**
- Enable a **V-Sink** for that channel — see [Virtual Sinks](Virtual-Sinks.md).

**App is muted and does not unmute**
- Move the physical slider significantly (>5% from the position when it was muted) to trigger auto-unmute.
- Or click the mute button in the NativMix GUI.

---

## Virtual Sinks

**App is silent after enabling a V-Sink**
- Wait a moment — NativMix needs ~50 ms for PipeWire to register the new sink before applying the fader volume.
- If the issue persists, use the **Panic Button** in settings to reset all V-Sink routing, then re-enable.

**System volume changes when moving a slider after V-Sink creation**
- This was a known issue that is fixed. Make sure you are running the latest version.

---

## Logs

Enable **Debug Logging** in the settings panel and check the log file at `~/.cache/nativmix/logs/nativmix.log` for detailed output.
