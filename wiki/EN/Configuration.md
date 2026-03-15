# Configuration

## File Location

NativMix follows the XDG Base Directory Specification:

| Type | Path |
| :--- | :--- |
| Config | `~/.config/nativmix/config.json` |
| Logs | `~/.cache/nativmix/logs/nativmix.log` |
| Data | `~/.local/share/nativmix/` |

The config file is created automatically on first launch with sensible defaults.

## Log Rotation

Logs rotate automatically at 5 MB, keeping the last 3 files.
You can open the log folder directly from the NativMix settings panel via **Open Log Folder**.

## Debug Logging

Enable verbose `DEBUG`-level logging from the settings panel. Changes take effect immediately without restarting NativMix.
