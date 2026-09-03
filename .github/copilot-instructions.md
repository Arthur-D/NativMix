# Copilot instructions for NativMix

## Build, test, and lint

Run commands from the repository root. The package uses a `lib/` layout and requires Python 3.10+.

```bash
# Development install
python -m pip install -e ".[dev]"

# Build a wheel from pyproject.toml
python -m pip wheel . --no-deps --wheel-dir dist

# Full test suite
pytest -q

# One test file
pytest -q tests/test_config_migration.py

# One test
pytest -q tests/test_config_migration.py::test_migrate_v6_to_v7_preserves_channels

# Static checks
ruff check lib/
mypy lib/
```

Ruff targets Python 3.10 with a 120-character line limit. Pre-commit runs Ruff fixes and Ruff formatting. Tests add `lib/` to `sys.path` in `tests/conftest.py`; use temporary config/profile paths and mock external audio or hardware interfaces rather than requiring a live device.

Never launch NativMix during automated validation, including a one-off smoke check and especially repeatedly or in a loop. Do not access live PipeWire, PulseAudio, MIDI, audio hardware, discovery/network peers, or portal services. Use mocked/offscreen tests only; live startup and hardware testing are maintainer-owned.

## Architecture

- `lib/nativmix/main.py` is the composition root. It selects `PipeWireManager` on Linux or `WasapiManager` on Windows, constructs config, profile, hardware, GUI, tray, and IPC objects, wires their signals, then starts workers only after signal connections are complete.
- Arduino, MIDI, and audio processing are worker-thread concerns. Hardware workers normalize physical input into channel volumes; the audio backend applies those volumes using mappings from `ConfigManager`; backend signals feed status, mute, and volume changes back to the GUI. Cross-thread communication must use `pyqtSignal`, never direct GUI access.
- `ConfigManager` owns global hardware/settings state and live change signals. `ProfileManager` owns per-profile channel data such as mappings, MIDI CCs, labels, virtual-sink flags, and saved fader positions. Profile normalization and reconciliation preserve stable sequential channel identities and repair malformed or legacy data deterministically.
- Linux audio uses two complementary layers. `PipeWireManager` uses pulsectl/PipeWire's PulseAudio compatibility layer for event subscription, stream enumeration, fallback writes, and virtual-sink management. `audio/pipewire_native.py` inventories the native graph with `pw-dump` and prefers `pw-cli`/`wpctl` writes before falling back to PulseAudio-compatible writes. Windows implements the same high-level backend contract through WASAPI polling, but does not support virtual sinks.
- New streams use a two-stage mute-catch: mute immediately on the initial event, then resolve metadata, apply the mapped channel volume, and unmute. Routing ownership (`nativmix`, `easyeffects`, or `none`) determines whether NativMix may create routes or virtual sinks.
- The GUI is PyQt6 and largely delegates state and audio behavior to config/backend objects. Startup is coordinated so the window is shown only after the initial audio audit, while `QLocalServer` IPC handles commands sent to an existing process.

## Repository-specific conventions

- Preserve exactly the Arduino, MIDI, and audio worker boundaries. Connect signals before starting workers, and retain cleanup order: `disconnect` -> `stop` -> `wait`.
- Protect shared `PipeWireManager` dictionaries and snapshots with its `self._state_lock` (`RLock`). Preserve reconnect circuit breakers, error counters, and exponential backoff.
- Keep both sides of Qt signal changes synchronized: update every relevant `.emit()` and `.connect()` path. `QPushButton.clicked` handlers accept `checked: bool = False`; use the existing `_slot_guard` pattern for GUI slots.
- `ConfigManager` mutations occur on the main thread and each setter changes only its own field. When changing persisted schema, add a forward migration, update `CONFIG_VERSION`, preserve channel reconciliation invariants, and cover migration with temporary config/profile files.
- Preserve audio display-name fallback order: `application.name` -> process binary -> `media.name` -> `"Unknown"`. Native PipeWire matching prioritizes cached stable IDs, then normalized exact binary/app/node/media fields, with contains matching only as a last resort. Unresolved targets remain configured so they reconnect when streams reappear.
- Respect routing ownership. In host mode, `move_stream_to_vsink()` is the authoritative `pactl move-sink-input` call site; do not add competing direct routing calls. Keep Flatpak/PW-only paths free of unsupported PulseAudio subprocess fallbacks.
- Keep virtual-sink and stream safety behavior intact, including immediate mute-catch and safe gain application. Special mappings `System Master` and `Other Apps` have exclusive routing semantics and cannot be mixed with regular app assignments.
- Preserve XDG and platform integration: config/data/cache paths go through `utils/paths.py`, IPC uses `XDG_RUNTIME_DIR`, autostart is user-space only, and theming uses Qt/XDG Desktop Portal data. Do not add window scraping, desktop-specific theme paths, new `/tmp/` application-data paths, `sudo`, or runtime writes under `/etc/xdg/autostart/`.
- Editable `QComboBox` changes are debounced with `QTimer.singleShot(500)`; persist and emit from the timeout handler to avoid reconnect storms. In `QLocalSocket` client code, do not call `shutdown(SHUT_WR)`; `_on_new_connection` must check `bytesAvailable()` before waiting for `readyRead`.
- Source and code comments are English. Do not silently swallow broad exceptions; expected transient hardware/audio failures should at least log at debug level, while Qt slot guards log the full exception.
- Do not bump versions unless explicitly requested. Hardware validation and packaging/install release checks are maintainer-owned.
