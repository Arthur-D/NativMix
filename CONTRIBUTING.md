# Contributing to NativMix

Thanks for contributing to NativMix.

This guide is for humans (not agents) and covers the practical workflow for issues, fixes, and pull requests.

## 1) Before You Start

- Create your own branch from `main`.
- Keep changes small and focused (one topic per PR).
- For larger refactors, open an issue first or extend an existing one with context.

## 2) Important Project Rules

### Threading and Architecture

- There are three worker threads (Arduino, MIDI, Audio).
- Communication between threads and the GUI must use `pyqtSignal`.
- Do not access GUI state directly from worker threads.
- Protect shared state dictionaries in `PipeWireManager` with `self._state_lock` (`RLock`).

### Stability and Recovery

- Keep MIDI reconnect behavior protected by limits and error counters (circuit breaker).
- Use exponential backoff for reconnect errors.
- Keep cleanup order: `disconnect` -> `stop` -> `wait`.

### Critical Gotchas

- For PyQt signals, ensure both `.connect()` and `.emit()` are correctly wired.
- `QPushButton.clicked` slots must accept `checked: bool = False`.
- In GUI slots, use the existing slot guard where applicable.
- In `QLocalSocket` client code, do not call `shutdown(SHUT_WR)`.
- In `_on_new_connection`, check `bytesAvailable()` first to avoid race conditions.
- Keep audio fallback order: `application.name` -> `binary` -> `media.name` -> `"Unknown"`.
- For editable `QComboBox`, debounce text input with `QTimer.singleShot(500)` to avoid reconnect storms.

## 3) Local Checks (Before PR)

Please run tests from the `tests/` folder locally:

```bash
pytest -q
```

### How to Run Tests

Use `pytest` for tests in the `tests/` folder:

```bash
# all tests
pytest -q

# one test file
pytest -q tests/test_config_migration.py

# one specific test
pytest -q tests/test_config_migration.py::test_migrate_v6_to_v7_preserves_channels
```

Optional additional checks (recommended, but not required for every contributor):

```bash
PYTHONPATH=lib python -m nativmix.main
ruff check lib/
mypy lib/
```

Additional rules:

- Do not use `except Exception: pass` (at minimum, log meaningfully, e.g. `logger.debug`).
- Keep XDG compliance: use `XDG_RUNTIME_DIR` and `~/.config/nativmix/`; do not introduce new `/tmp/` path logic.

### Recommended Validation Order

For best signal and reproducibility, use this order:

1. Run test-suite checks first: `pytest -q`.
2. Optionally run static checks (`ruff`, `mypy`) when available in your setup.
3. Then run build/install as your integration step.
4. Finally validate on real hardware before merging.

## 4) PR Workflow

- Open PRs against `main`.
- In the PR description, briefly include:
  - Problem / goal
  - Solution approach
  - What you tested locally
- For review feedback, push follow-up commits to the same branch.

## 5) Releases and Versions

- Only bump versions when explicitly approved by a maintainer.
- Hardware tests and packaging install steps (e.g. `makepkg`) are maintainer-owned tasks.
- If a version bump is approved, all required version locations must be updated consistently.

## 6) Project Structure (Orientation)

- `lib/nativmix/main.py` - central signal wiring
- `lib/nativmix/audio/manager.py` - PipeWire logic
- `lib/nativmix/hardware/` - Arduino and MIDI threads
- `lib/nativmix/gui/` - PyQt6 widgets (ideally without business logic)
- `lib/nativmix/utils/` - config and system helpers
