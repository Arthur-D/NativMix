# AGENTS.md

Guidance for coding agents working in this repository.

## Scope

- Applies to the entire repository.
- Prefer small, focused changes.
- Do not modify unrelated files.

## Project Snapshot

- Language: Python (>=3.10)
- Package root: `lib/nativmix/`
- Tests: `tests/`
- Build/config: `pyproject.toml`
- GUI: PyQt6; Linux audio: PulseAudio/PipeWire; Windows audio: WASAPI.
- Local MIDI: Mido/python-rtmidi; remote control: AppleMIDI plus TCP mixer-state sync.
- Paths in this guide are relative to the current repository/worktree root, not a fixed CI checkout.

## Architecture and Safety Rules

- Keep hardware I/O and slow audio writes in their workers. Qt state coordinators,
  mixer facades, and receiver authority belong to the owning Qt thread.
- Communicate across threads with `pyqtSignal`; do not mutate GUI from worker threads.
- Protect shared `PipeWireManager` state dictionaries with `self._state_lock` (`RLock`).
  Preserve dedicated locks for other state, such as `_unresolved_lock`.
- Keep cleanup order: `disconnect` -> `stop` -> `wait`, using existing lifecycle methods.
  Stop the volume scheduler before tearing down its audio backend; do not close the
  backend concurrently with a still-running volume write.
- Preserve reconnect safeguards (error counters/circuit breaker, exponential backoff).
- Route local GUI/MIDI volume intents through `VolumeIntentCoordinator` and
  `LatestVolumeScheduler`; preserve coalescing, generation checks, and echo suppression.
- Keep the remote mixer receiver-authoritative. The remote facade must not write
  the sender's local profiles/configuration or apply receiver commands to local audio.

## PyQt / IPC Gotchas

- Ensure signal wiring is correct on both `.connect()` and `.emit()` paths.
- `QPushButton.clicked` handlers must accept `checked: bool = False`.
- Use slot guards where the codebase already uses them.
- Debounce editable port/name inputs with a restartable, single-shot `QTimer`
  at 500 ms, following `SettingsPanel`; independent `QTimer.singleShot()` calls
  per keystroke do not cancel earlier callbacks.
- In `QLocalSocket` client code, do not call `shutdown(SHUT_WR)`.
- In `_on_new_connection`, check `bytesAvailable()` before reading.

## Audio Mapping Expectations

- Preserve display-name fallback resolution order:
  `application.name` -> `application.process.binary` -> `media.name` -> `"Unknown"`.
  This is distinct from target matching, which also uses application IDs,
  process identity, aliases, and backend-specific resolution rules.

## MIDI Expectations

- Prefer RtMidi for hotplug safety. Linux physical ports prefer JACK (including
  PipeWire's JACK interface), with RtMidi/ALSA fallback when JACK is unavailable.
  Keep the selected API stable while ports are open; the virtual input uses ALSA.
- Keep PortMidi disabled in Flatpak; preserve the existing native compatibility
  fallback and its hot-unplug warning.
- Use `utils/midi_ports.py` for stable saved identities and matching raw input/output
  names. Preserve ALSA address normalization and JACK direction/bridge handling.
- Keep MIDI CC scaling linear and preserve controller-origin/feedback suppression
  across reconnects and remote sessions.

## XDG / Paths

- Reuse `utils/paths.py`: respect `XDG_RUNTIME_DIR`, `XDG_CONFIG_HOME`,
  `XDG_DATA_HOME`, and `XDG_CACHE_HOME` on Linux and the existing AppData paths
  on Windows. The default Linux config directory is `~/.config/nativmix/`.
- Do not introduce new `/tmp/` runtime path logic for application data.
  The existing IPC fallback in `get_ipc_socket_path()` is not a pattern to expand.

## Validation

Run from the current repository/worktree root.

Never launch NativMix during automated validation, including a one-off smoke check and especially repeatedly or in a loop. Do not access live PipeWire, PulseAudio, MIDI, audio hardware, discovery/network peers, or portal services. Pytest forces Qt to the `offscreen` platform; do not override it with `xcb`, `wayland`, or another interactive platform. Use mocked/offscreen tests only; live startup and hardware testing are maintainer-owned.

Required for code or test changes:

```bash
pytest -q
```

Recommended:

```bash
ruff check lib/
mypy lib/
```

For documentation-only changes, verify referenced paths and instructions against
the code and run `git diff --check`; rerun tests only when the change affects
runtime behavior, test configuration, or executable examples.

Some transport tests use local loopback sockets. Keep discovery and external
services mocked; never relax the sandbox to contact live peers or hardware.
If tests hang or the environment blocks them, use bounded diagnostics, report
the exact failure/exclusion, and distinguish it from a passing full suite.
Do not silently skip failures or treat existing lint/type errors as newly introduced.

## Coding Standards

- Avoid broad silent exception handling like `except Exception: pass`.
- At minimum, log expected transient failures (e.g., `logger.debug`) where appropriate.
- Reuse existing patterns in touched modules.

## Change Management

- Keep PRs single-purpose.
- Do not bump versions unless explicitly requested/approved.
- Hardware validation and packaging release workflows are maintainer-owned.

## Git Workflow (Codex Only)

This section applies only to Codex sessions. It is not a GitHub Copilot instruction
or a repository-wide automation trigger. Copilot and other agents should follow
their own assigned task workflows and must not duplicate this automation or take
over Codex branches/PRs unless the user explicitly assigns that work to them.
The other sections of this file remain shared guidance for all coding agents.

- After completing a coding task, validate and review its diff, commit only that
  task's changes on a `codex/` branch, push, and create or update a single-purpose
  pull request. Do not include unrelated working-tree changes.
- The only publishing destination is the user's fork, `Arthur-D/NativMix`, with
  PR base `main`. Verify the push URL before publishing and explicitly pass
  `--repo Arthur-D/NativMix` to GitHub CLI PR commands; do not rely on fork defaults.
- Never push to or open pull requests against `knoellix/NativMix` (`upstream`).
  Never force-push or bypass branch protections, required checks, or required reviews.
- Automatically merge the task's PR into the fork's `main` once applicable local
  validation, required checks, and required reviews pass. If checks are pending,
  use GitHub auto-merge where available. Missing CI checks do not substitute for
  applicable local validation; failures, conflicts, and blocked validation must
  be reported instead of bypassed.
- This workflow is standing authorization for task-scoped commit, push, PR creation,
  and merge. Proceed without asking again unless the user changes the scope or an
  actual tool/security approval is required. It does not authorize releases, tags,
  version bumps, or changes to repository permissions/protection settings.
- If the app's Git controls are unavailable, use Git and the authenticated GitHub
  CLI. For HTTPS credential-prompt failures, use the existing `gh auth git-credential`
  helper per command; never expose tokens or change global Git credentials.
- Report the commit, PR URL, validation results, and whether the PR was merged,
  queued for auto-merge, or blocked. Do not claim completion from a push alone.

## File Orientation

- `lib/nativmix/main.py` - central signal wiring
- `lib/nativmix/audio/manager.py` - PulseAudio/PipeWire management and routing
- `lib/nativmix/audio/pipewire_native.py` - native PipeWire node resolution and volume operations
- `lib/nativmix/audio/wasapi_manager.py` - Windows audio backend
- `lib/nativmix/audio/volume_scheduler.py` - asynchronous volume writes and intent coordination
- `lib/nativmix/hardware/` - Arduino/MIDI workers and remote MIDI transport
- `lib/nativmix/remote_sync/` - mixer sync protocol, transport, state, and receiver authority
- `lib/nativmix/gui/` - PyQt6 UI widgets
- `lib/nativmix/gui/mixer_facade.py` - local/remote mixer state and command boundary
- `lib/nativmix/utils/` - config/system helpers
- `tests/conftest.py` - offscreen Qt setup and native MIDI test guard
- `flatpak/`, `packaging/`, `.github/workflows/` - dependency packaging and release builds
