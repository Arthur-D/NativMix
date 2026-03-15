# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

NativMix is a hardware-based volume mixer for Linux (PyQt6 + PipeWire/PulseAudio). It connects physical Arduino potentiometers via USB to the audio stack, with MIDI support, virtual sinks (V-Sinks), and a system tray GUI.

## Development Commands

```bash
# Run locally (from repo root)
python -m nativmix.main          # requires lib/ on PYTHONPATH, or:
PYTHONPATH=lib python -m nativmix.main

# Install in editable mode (recommended for development)
pip install -e ".[dev]"

# Lint
ruff check lib/

# Type check
mypy lib/

# Run tests
pytest

# Build wheel (for AUR packaging)
python -m build --wheel --no-isolation
```

The source package is under `lib/` — `pyproject.toml` sets `[tool.setuptools.packages.find] where = ["lib"]`.

## Architecture

### Thread Model
Three background `QThread` workers communicate exclusively via `pyqtSignal`:

- **`ArduinoThread`** (`hardware/arduino.py`) — reads pipe-separated ADC values (0–1023) from serial USB. Emits `volumes_changed` and `channel_count_changed`.
- **`MidiThread`** (`hardware/midi.py`) — listens to MIDI CC messages via `mido`/`python-rtmidi`. Emits `midi_volumes_changed` and `midi_cc_received`.
- **`PipeWireManager`** (`audio/manager.py`) — listens to PipeWire/PulseAudio events via `pulsectl`. Runs a `pulseaudio` event loop in its thread. Emits `mute_state_changed`, `audit_finished`, `channel_volume_changed`.

All signal wiring between threads and the GUI lives in `main.py:main()`.

### Signal Flow
```
ArduinoThread.volumes_changed  →  PipeWireManager.apply_poti_volumes
                               →  MainWindow.on_volumes_changed
MidiThread.midi_volumes_changed →  PipeWireManager.apply_midi_volumes
PipeWireManager.mute_state_changed → MainWindow.on_mute_state_changed
```

### Audio Backend
`AudioBackendBase` (`audio/base.py`) is the abstract interface. `PipeWireManager` (`audio/manager.py`) is the Linux implementation using `pulsectl`. A Windows WASAPI backend is planned but not implemented.

**Two-Stage Mute-Catch**: On `new` stream event → immediately mute (Stage 1 Reflex). On `change` event → resolve app name via PID, apply volume, unmute (Stage 2 Resolution). This prevents volume blasts on stream start.

### Virtual Sinks (V-Sinks)
When a channel has `v_sink_map[i] = True`, NativMix creates a PipeWire null-sink and routes the app through it (`App → V-Sink → Physical Output`). The V-Sink volume is controlled by the hardware fader; the app is held at 100% inside the sink. `utils/routing.py` handles `pw-link` / `pw-dump` calls for port discovery and link management.

### Configuration
`ConfigManager` (`utils/config_manager.py`) reads/writes `~/.config/nativmix/config.json` (schema version 5). Emits `settings_changed` and `mapping_changed` signals so threads and the backend react without restart.

### App Name Resolution
`utils/proc_resolver.py` handles Electron/Chromium/Flatpak PID-to-app-name resolution by reading `/proc/<PID>/cmdline` and walking the PPID tree. Resolution order: binary name → `--user-data-dir` profile → `--app-id` → Flatpak info → PPID walk.

### GUI
- **`MainWindow`** (`gui/main_window.py`) — zero QSS, zero manual colors. Uses only `QApplication.style()` / `QPalette`. Theme auto-adapts via `paletteChanged`.
- **`SettingsPanel`** (`gui/settings_panel.py`) — collapsible settings drawer inside the main window.
- **`TrayIcon`** (`gui/tray_icon.py`) — system tray; left-click shows/hides window; right-click quits.
- **`theme.py`** — reads dark/light mode and accent color via XDG Desktop Portal D-Bus (`org.freedesktop.portal.Settings`).

Style priority at startup: **Kvantum** → **Breeze** → **Fusion** (with a dark fallback palette for Fusion).

### IPC
`IpcServer` in `main.py` listens on `/tmp/nativmix_ipc_<uid>.sock` (Qt `QLocalServer`). A second invocation with `--toggle-mute N`, `--list-sinks`, `--list-apps`, or `--show` connects to the running instance and exits.

### Paths
`utils/paths.py` provides all XDG-standard path helpers:
- Config: `~/.config/nativmix/config.json`
- Logs: `~/.cache/nativmix/logs/nativmix.log` (rotating, 5 MB × 3)
- Data: `~/.local/share/nativmix/`
- Assets: project root `assets/` (dev) or `/usr/share/nativmix/assets/` (installed)

## Packaging

- **AUR**: `packaging/aur/PKGBUILD` — builds from GitHub release tarball via `python -m build`.
- **Debian**: `packaging/debian/` — OBS-based.
- **openSUSE**: `packaging/suse/` — OBS-based.
- Version is in `pyproject.toml` and must be updated consistently with PKGBUILD.

## Key Design Rules (from source docstrings)

- Rule 2: Electron/Chromium PID hack — resolve real app names via `/proc`
- Rule 4: XDG Desktop Portal for theming (no DE-specific paths)
- Rule 9: All hardware threads use `QThread` + signals only (no direct GUI calls)
- Rule 10: Serial `SerialException` caught; reconnection loop for hot-plug
- Rule 11: Two-Stage Mute-Catch for new audio streams
- Rule 13: Starts without Arduino (dummy/headless mode — CI-safe)
- Rule 14: XDG-standard config path on Linux
