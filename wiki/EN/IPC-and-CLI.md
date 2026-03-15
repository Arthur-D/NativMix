# IPC & CLI Control

NativMix features a built-in IPC (Inter-Process Communication) server listening on a Unix socket (`/tmp/nativmix_ipc_<uid>.sock`). This allows you to control the running instance from the command line — perfect for global hotkeys in your desktop environment.

## Available Commands

| Command | Description |
| :--- | :--- |
| `--toggle-mute <INDEX>` | Toggles mute for channel `INDEX` (0-indexed). |
| `--list-sinks` | Returns a list of all active Virtual Sinks and their indices. |
| `--list-apps` | Returns a list of all detected audio applications. |
| `--show` | Brings the already-running window to the foreground. |
| *(no arguments)* | Same as `--show`. |

## Examples

**Mute/unmute Discord on channel 1:**
```bash
nativmix --toggle-mute 1
```

**List all active V-Sinks:**
```bash
nativmix --list-sinks
```

**List all detected apps:**
```bash
nativmix --list-apps
```

## Global Hotkeys

You can bind any of these commands to keyboard shortcuts in your desktop environment.

**KDE (System Settings → Shortcuts → Custom Shortcuts):**
```
Command: sh -c "/usr/bin/nativmix --toggle-mute 0"
```

**GNOME / Hyprland / Sway** — add a keybind pointing to the same command.

> [!NOTE]
> A second invocation of NativMix with an IPC command connects to the running instance and exits immediately — no second GUI window is opened.
