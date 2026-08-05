# AGENTS.md

Guidance for coding agents working in this repository.

## Scope

- Applies to the entire repository.
- Prefer small, focused changes.
- Do not modify unrelated files.

## Project Snapshot

- Language: Python (>=3.10)
- Package root: `/home/runner/work/NativMix/NativMix/lib/nativmix`
- Tests: `/home/runner/work/NativMix/NativMix/tests`
- Build/config: `/home/runner/work/NativMix/NativMix/pyproject.toml`

## Architecture and Safety Rules

- Keep thread boundaries strict: Arduino, MIDI, and Audio run in worker threads.
- Communicate across threads with `pyqtSignal`; do not mutate GUI from worker threads.
- Protect shared `PipeWireManager` state dictionaries with `self._state_lock` (`RLock`).
- Keep cleanup order: `disconnect` -> `stop` -> `wait`.
- Preserve reconnect safeguards (error counters/circuit breaker, exponential backoff).

## PyQt / IPC Gotchas

- Ensure signal wiring is correct on both `.connect()` and `.emit()` paths.
- `QPushButton.clicked` handlers must accept `checked: bool = False`.
- Use slot guards where the codebase already uses them.
- In `QLocalSocket` client code, do not call `shutdown(SHUT_WR)`.
- In `_on_new_connection`, check `bytesAvailable()` before reading.

## Audio Mapping Expectations

- Preserve fallback resolution order:
  `application.name` -> `binary` -> `media.name` -> `"Unknown"`.
- For editable `QComboBox`, debounce input via `QTimer.singleShot(500)` to avoid reconnect storms.

## XDG / Paths

- Keep XDG compliance: use `XDG_RUNTIME_DIR` and `~/.config/nativmix/`.
- Do not introduce new `/tmp/` runtime path logic for application data.

## Validation

Run from repository root `/home/runner/work/NativMix/NativMix`.

Required:

```bash
pytest -q
```

Recommended:

```bash
ruff check lib/
mypy lib/
PYTHONPATH=lib python -m nativmix.main
```

## Coding Standards

- Avoid broad silent exception handling like `except Exception: pass`.
- At minimum, log expected transient failures (e.g., `logger.debug`) where appropriate.
- Reuse existing patterns in touched modules.

## Change Management

- Keep PRs single-purpose.
- Do not bump versions unless explicitly requested/approved.
- Hardware validation and packaging release workflows are maintainer-owned.

## File Orientation

- `lib/nativmix/main.py` - central signal wiring
- `lib/nativmix/audio/manager.py` - PipeWire logic
- `lib/nativmix/hardware/` - Arduino and MIDI workers
- `lib/nativmix/gui/` - PyQt6 UI widgets
- `lib/nativmix/utils/` - config/system helpers
