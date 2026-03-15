# Features

## Hardware Integration

- **deej Compatibility** — Fully compatible with the standard deej Arduino protocol.
- **Physical Slider Control** — Map each Arduino potentiometer to one or more audio applications.
- **Volume Curve Intensity** — Adjustable fader curve shape (Linear to Cubic) for natural volume perception.
- **Per-Channel Inversion** — Flip the direction of any potentiometer (0 = 100%, 1023 = 0%), configurable per channel or globally.
- **Auto-Detect & Hot-Plug** — Automatically detects any USB-serial device. Reconnects automatically if the Arduino is unplugged.
- **Auto-Unmute on Move** — A muted channel unmutes automatically when the physical slider is significantly moved.

## Audio Routing

- **App Mode** — Directly control the volume of individual application audio streams.
- **Multi-App Grouping** — Assign multiple applications to a single slider.
- **System Master** — One channel can control the system master volume and mute state of the default output device.
- **Other Apps** — Assign a channel to control all unmapped audio streams as a group.
- **Hardware Mode** — Directly control the volume of physical audio devices (speakers, headphones, or microphones).

## Mute-Catch Reflex System

Prevents 100% volume "audio blasts" when new audio streams start:

- **Stage 1 (Reflex)** — Immediately mutes any new audio stream before metadata is available.
- **Stage 2 (Resolution)** — Once the app name is resolved, applies the saved slider volume and unmutes.

## Intelligent App Resolution

- Identifies sandboxed Electron/Chromium apps (Discord, Spotify, Chrome) by parsing `/proc/<PID>/cmdline` and traversing the process tree.
- Handles generic stream names like "Chromium" or "WEBRTC Voice Engine" correctly.

## Native System Theming

- Reads dark/light mode and accent color via the **XDG Desktop Portal** (`org.freedesktop.portal.Settings`) — works on KDE, GNOME, and all XDG-compliant desktops.
- Style priority: **Kvantum** → **Breeze** → **Fusion** (with a dark fallback palette).
- Fader colors use the system highlight/accent color via `QPalette`.
- Optional translucent window background.

## Native Wayland Integration

- Process title set via `setproctitle` (correct name in `htop`, system monitors).
- Window/icon linked to `.desktop` file via `app.setDesktopFileName()`.
- No X11 window scraping (`wmctrl`, `xdotool`).

## Settings Panel

| Setting | Description |
| :--- | :--- |
| Serial Port Selection | Choose port or leave on auto-detect. Connected port is marked with ★. |
| Master Output Selector | Choose the default playback device directly from NativMix. |
| Fader Curve Intensity | Adjust the volume curve (Linear » Quadratic » Cubic). |
| Don't hide | Closing the window (X) only hides it to the system tray. |
| Autostart | Toggle autostart on boot via `~/.config/autostart/`. |
| Transparency | Toggle window translucency. |
| Panic Button | One-click reset — evacuates all apps from V-Sinks, destroys sinks, resets routing. |
| Debug Logging | Enable verbose `DEBUG`-level logging (takes effect immediately). |
| Open Log Folder | Opens the log directory in the system file manager. |

## Tray Icon

- Left-click or double-click to show/hide the main window.
- Right-click for Quick-Quit.
