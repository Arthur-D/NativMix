# Crossfader + MIDI Label/Desync Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship GitHub #38 (MIDI channel rename persists), #39 (fast MIDI fader no longer drops the last CC), and #37 (A/B crossfader with USB control + MIDI Learn) in one release after maintainer hardware validation — no separate patch release.

**Architecture:** Channel `volume` in the profile stays the **base** GUI fader value. Crossfader applies a runtime **gain multiplier** (`effective = base × gain_side(p)`) only in PipeWire/WASAPI apply paths, never by rewriting stored fader positions. Labels and crossfader state live in the **active profile** (`ProfileManager.save_current`), not in `config.json` (which strips `channels` on `ConfigManager.save()`). MIDI CC emits are coalesced so the last value always flushes after throttle.

**Tech Stack:** Python 3.10+, PyQt6, PipeWire/`pulsectl` (`lib/nativmix/audio/manager.py`), WASAPI (`wasapi_manager.py`), MIDI worker (`hardware/midi.py`), profiles under `~/.config/nativmix/profiles/`.

## Global Constraints

- Communicate with maintainer in German; code and comments in English.
- Do **not** bump `__version__` / packaging unless the user explicitly asks.
- Do **not** commit unless the user explicitly asks (fish shell — no bash heredoc commits).
- Do **not** post GitHub issue comments unless asked.
- GUI fader display = base volume only; crossfader must not move vertical strip percentages.
- Exactly one USB (non-MIDI) channel may be crossfader control at a time; MIDI-only strips cannot become the USB control via channel mode — MIDI for the bar is Learn-only via right-click on the horizontal crossfader.
- When crossfader is disabled: all gains → 1.0, re-apply base volumes, unfreeze control channel; keep per-channel `cross_side` for next enable.
- Compact mode: global crossfader row stays visible when feature enabled; include its height in compact resize.
- Crossfader UI: full-width row **below** the channel scroll area, **above** the MIDI/size-grip bottom bar.
- Run `pytest -q` before claiming done; maintainer validates with local `makepkg` / Flatpak install.

## File map

| File | Role |
|------|------|
| `lib/nativmix/gui/main_window.py` | `#38` rename → `save_current`; `#37` crossfader widget, A/B menu, USB control mode UI, compact height |
| `lib/nativmix/utils/config_manager.py` | getters/setters: `cross_side`, crossfader flags, USB control index; channel defaults |
| `lib/nativmix/utils/profile_manager.py` | `default_channels()` + profile-level crossfader fields via `save_current` / load |
| `lib/nativmix/audio/crossfader.py` | **New** pure helpers: `crossfader_gains(p)`, `effective_volume(base, side, p, enabled)` |
| `lib/nativmix/audio/manager.py` | Apply `effective` in poti/MIDI/GUI volume paths; disable cleanup re-apply |
| `lib/nativmix/audio/wasapi_manager.py` | Same effective gain on Windows apply paths |
| `lib/nativmix/hardware/midi.py` | Trailing CC flush; optional crossfader CC map; master-echo suppress hook if needed |
| `lib/nativmix/main.py` | Wire crossfader signals; USB poti → position when control channel; MIDI Learn for bar |
| `lib/nativmix/gui/settings_panel.py` | Enable/disable crossfader feature toggle (profile-backed) |
| `tests/test_crossfader.py` | **New** pure gain + effective volume tests |
| `tests/test_midi_throttle.py` | **New** (or extend existing) trailing CC / coalesce tests |
| `tests/test_profile_manager.py` | Label / cross_side round-trip if useful |

---

### Task 1: #38 — Persist channel labels to the active profile

**Files:**
- Modify: `lib/nativmix/gui/main_window.py` (`ChannelWidget._on_rename`, and any add/remove MIDI path that only calls `config.save()`)
- Test: `tests/test_profile_manager.py` (optional round-trip) or a small GUI-free helper test if rename is wired through a save helper

**Interfaces:**
- Consumes: `ConfigManager.set_channel_label`, `ConfigManager.all_channels()`, `ConfigManager.get_channel_order()`, `ProfileManager.save_current(channels, channel_order)`
- Produces: Rename survives restart because profile JSON is updated

**Bug:** `_on_rename` calls `set_channel_label` + `config.save()`. `ConfigManager.save()` **strips** `channels`, so labels never hit disk. Reorder already uses `profile_manager.save_current(...)` (~lines 1911, 2381).

- [ ] **Step 1: Write a failing persistence check**

Add to `tests/test_profile_manager.py` (or a dedicated small test) that after `save_current` with a labeled channel, reload shows the label:

```python
def test_save_current_persists_channel_label(qtbot, tmp_profiles_dir):
    from nativmix.utils.profile_manager import ProfileManager, default_channels

    pm = ProfileManager(profiles_dir=tmp_profiles_dir)
    pid = pm.create("Label Test", channel_count=2)
    pm.switch(pid)
    channels = default_channels(2)
    channels[0]["label"] = "Kick"
    pm.save_current(channels)
    loaded = pm.load(pid)
    assert loaded["channels"][0]["label"] == "Kick"
```

- [ ] **Step 2: Run test**

Run: `pytest tests/test_profile_manager.py::test_save_current_persists_channel_label -v`  
Expected: PASS (this documents the API). The GUI bug is the missing call site.

- [ ] **Step 3: Fix `_on_rename` to save the profile**

In `ChannelWidget._on_rename`:

```python
def _on_rename(self, new_name: str) -> None:
    self._config.set_channel_label(self._ch, new_name)
    self._ch_label.setText(new_name)
    parent = self.window()
    pm = getattr(parent, "_profile_manager", None)
    if pm is not None:
        pm.save_current(self._config.all_channels(), self._config.get_channel_order())
    else:
        self._config.save()  # fallback; channels still stripped — log once
        logger.warning("Channel rename: no ProfileManager on window; label may not persist")
```

Prefer a cleaner approach if `MainWindow` already exposes a `_persist_channels()` helper used by reorder — **reuse that** instead of duplicating. Grep for `save_current` in `main_window.py` and call the same method from rename (and from add/remove MIDI channel if those only `config.save()`).

- [ ] **Step 4: Manual check list for maintainer**

1. Hybrid/MIDI mode → rename MIDI strip → quit → reopen → label still there.  
2. USB strip rename also persists.

- [ ] **Step 5: Commit only if user asks**

Message suggestion: `Fix MIDI/USB channel rename not persisting to profile (#38).`

---

### Task 2: #39 — MIDI CC coalesce + trailing flush; System Master echo

**Files:**
- Modify: `lib/nativmix/hardware/midi.py` (`_handle_cc` volume path ~624–648)
- Modify: `lib/nativmix/audio/manager.py` (`_on_master_volume_changed` ~2029–2041) — suppress GUI/config overwrite while MIDI is actively writing System Master
- Test: `tests/test_midi_throttle.py` (new) — unit-test coalesce helper if extracted; otherwise document manual hardware test

**Interfaces:**
- Consumes: `_last_vol_emit`, `_last_values`, `midi_volumes_changed`
- Produces: After a burst of CCs within 20 ms, the **final** CC value is always emitted

**Bug:** Throttle `now - last >= 0.02` drops intermediate emits **including the last sample** when the burst ends inside the window.

- [ ] **Step 1: Extract coalesce + flush logic (testable)**

In `midi.py` (or `lib/nativmix/hardware/midi_throttle.py`):

```python
class CcVolumeThrottler:
    """Emit at most every min_interval; always flush the latest pending value."""

    def __init__(self, min_interval: float = 0.02) -> None:
        self.min_interval = min_interval
        self._last_emit: dict[tuple[int, int], float] = {}
        self._pending: dict[tuple[int, int], tuple[int, float]] = {}  # key -> (ch_idx, vol)

    def note(self, key: tuple[int, int], ch_idx: int, vol: float, now: float) -> list[tuple[int, float]] | None:
        """Return mappings to emit now, or None if only pending."""
        self._pending[key] = (ch_idx, vol)
        last = self._last_emit.get(key, 0.0)
        if now - last >= self.min_interval:
            self._last_emit[key] = now
            self._pending.pop(key, None)
            return [(ch_idx, vol)]
        return None

    def flush_due(self, now: float) -> list[tuple[int, float]]:
        out: list[tuple[int, float]] = []
        for key, (ch_idx, vol) in list(self._pending.items()):
            last = self._last_emit.get(key, 0.0)
            if now - last >= self.min_interval:
                self._last_emit[key] = now
                self._pending.pop(key, None)
                out.append((ch_idx, vol))
        return out
```

Wire `_handle_cc` to use `note()`; add a short QTimer / sleep-checked poll in the MIDI read loop (every ~10–20 ms) calling `flush_due` and emitting `midi_volumes_changed` when non-empty. Prefer reusing an existing timer in `MidiThread` if one exists; otherwise a monotonic check inside the read loop is enough (no new QThread).

- [ ] **Step 2: Unit test the throttler**

```python
# tests/test_midi_throttle.py
def test_throttler_emits_trailing_value():
    from nativmix.hardware.midi_throttle import CcVolumeThrottler  # or wherever placed

    t = CcVolumeThrottler(0.02)
    assert t.note((0, 7), 1, 0.1, 0.0) == [(1, 0.1)]
    assert t.note((0, 7), 1, 0.5, 0.005) is None
    assert t.note((0, 7), 1, 0.9, 0.010) is None
    assert t.flush_due(0.025) == [(1, 0.9)]
    assert t.flush_due(0.030) == []
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_midi_throttle.py -v`  
Expected: PASS

- [ ] **Step 4: System Master echo suppress**

When MIDI (or USB) applies volume to a channel that has `"System Master"`, set a short suppress window (e.g. 150–300 ms) so `_on_master_volume_changed` does not push sink feedback into `_poti_volumes` / GUI and fight the controller.

Minimal approach in `PipeWireManager`:

```python
# on MIDI/GUI apply to System Master channel:
self._master_write_suppress_until = time.monotonic() + 0.25

def _on_master_volume_changed(self, volume: float, muted: bool) -> None:
    if time.monotonic() < getattr(self, "_master_write_suppress_until", 0.0):
        return
    # ... existing loop ...
```

Mirror on WASAPI if an equivalent master poll exists.

- [ ] **Step 5: Maintainer smoke**

Fast MIDI fader sweep → GUI and audio end on the **same** final value; System Master channel no longer snaps back mid-move.

- [ ] **Step 6: Commit only if user asks**

Message suggestion: `Fix MIDI fader desync by flushing trailing CC and suppressing master echo (#39).`

---

### Task 3: Crossfader math module (pure)

**Files:**
- Create: `lib/nativmix/audio/crossfader.py`
- Test: `tests/test_crossfader.py`

**Interfaces:**
- Produces:
  - `crossfader_gains(position: float) -> tuple[float, float]`  # (gain_a, gain_b), position clamped to [0, 1]
  - `side_gain(side: str | None, position: float, enabled: bool) -> float`  # "none"|"a"|"b"|None → 1.0 if disabled/none
  - `effective_volume(base: float, side: str | None, position: float, enabled: bool) -> float`

**Curve (piecewise linear, center = both full):**

- `p ≤ 0.5`: `gain_A = 1.0`, `gain_B = 2*p`
- `p > 0.5`: `gain_A = 2*(1-p)`, `gain_B = 1.0`
- `enabled=False` or `side` in `(None, "none", "")`: gain `1.0`

- [ ] **Step 1: Failing tests**

```python
# tests/test_crossfader.py
from nativmix.audio.crossfader import crossfader_gains, effective_volume

def test_gains_left_center_right():
    assert crossfader_gains(0.0) == (1.0, 0.0)
    ga, gb = crossfader_gains(0.5)
    assert ga == 1.0 and gb == 1.0
    assert crossfader_gains(1.0) == (0.0, 1.0)

def test_effective_keeps_base_when_disabled():
    assert effective_volume(0.8, "a", 1.0, enabled=False) == 0.8

def test_effective_silences_a_when_full_b():
    assert effective_volume(0.8, "a", 1.0, enabled=True) == 0.0
    assert effective_volume(0.8, "b", 1.0, enabled=True) == 0.8
```

- [ ] **Step 2: Run — expect FAIL (module missing)**

Run: `pytest tests/test_crossfader.py -v`

- [ ] **Step 3: Implement `crossfader.py`**

```python
from __future__ import annotations

def crossfader_gains(position: float) -> tuple[float, float]:
    p = max(0.0, min(1.0, float(position)))
    if p <= 0.5:
        return 1.0, 2.0 * p
    return 2.0 * (1.0 - p), 1.0

def side_gain(side: str | None, position: float, enabled: bool) -> float:
    if not enabled:
        return 1.0
    key = (side or "none").lower()
    if key in ("", "none"):
        return 1.0
    ga, gb = crossfader_gains(position)
    if key == "a":
        return ga
    if key == "b":
        return gb
    return 1.0

def effective_volume(base: float, side: str | None, position: float, enabled: bool) -> float:
    return max(0.0, min(1.0, float(base) * side_gain(side, position, enabled)))
```

- [ ] **Step 4: Run tests — PASS**

- [ ] **Step 5: Commit only if user asks**

---

### Task 4: Profile / config fields for crossfader

**Files:**
- Modify: `lib/nativmix/utils/profile_manager.py` (`default_channels`, profile dict shape)
- Modify: `lib/nativmix/utils/config_manager.py` (`_channel` defaults, getters/setters; load from profile via existing `apply_profile`)

**Profile-level fields** (on the profile JSON root, next to `channels`):

```json
{
  "crossfader_enabled": false,
  "crossfader_position": 0.5,
  "crossfader_usb_channel_index": null,
  "crossfader_midi_cc": null,
  "crossfader_midi_channel": 0
}
```

**Per-channel field** in each channel dict:

```json
"cross_side": "none"
```

Valid: `"none" | "a" | "b"`. USB control channel must force `"none"` and clear apps when assigned as control.

- [ ] **Step 1: Extend `default_channels` and profile create defaults**

Add `"cross_side": "none"` to `default_channels()` entries. When creating/saving profiles, default missing crossfader root keys as above.

- [ ] **Step 2: ConfigManager API**

```python
def get_cross_side(self, index: int) -> str: ...
def set_cross_side(self, index: int, side: str) -> None: ...  # normalize to none|a|b

def get_crossfader_enabled(self) -> bool: ...
def set_crossfader_enabled(self, enabled: bool) -> None: ...
def get_crossfader_position(self) -> float: ...
def set_crossfader_position(self, position: float) -> None: ...
def get_crossfader_usb_channel_index(self) -> int | None: ...
def set_crossfader_usb_channel_index(self, index: int | None) -> None: ...
# MIDI Learn for bar
def get_crossfader_midi_binding(self) -> tuple[int | None, int]: ...  # (cc, midi_ch)
def set_crossfader_midi_binding(self, cc: int | None, midi_channel: int = 0) -> None: ...
```

Store profile-level keys either:
- on `ConfigManager` in-memory mirror loaded from the active profile (same pattern as channels), **or**
- only on `ProfileManager` + MainWindow reads/writes via `save_current` extended to accept optional kwargs.

**Preferred:** extend `save_current` / profile load so MainWindow keeps crossfader state in sync:

```python
def save_current(
    self,
    channels: list[dict],
    channel_order: list[int] | None = None,
    *,
    crossfader: dict[str, Any] | None = None,
) -> None:
    ...
    if crossfader is not None:
        profile.update(crossfader)
```

And on profile apply (`ConfigManager.apply_profile` / MainWindow profile switch), copy those keys into a small in-memory dict on ConfigManager **or** a dedicated `CrossfaderState` object owned by MainWindow + backends.

- [ ] **Step 3: Tests for defaults and clamp**

```python
def test_default_channel_has_cross_side():
    from nativmix.utils.profile_manager import default_channels
    assert default_channels(1)[0]["cross_side"] == "none"
```

- [ ] **Step 4: Commit only if user asks**

---

### Task 5: Apply effective volume in audio backends

**Files:**
- Modify: `lib/nativmix/audio/manager.py` — `apply_poti_volumes`, `apply_midi_volumes`, `set_channel_volume`
- Modify: `lib/nativmix/audio/wasapi_manager.py` — matching volume apply entry points

**Interfaces:**
- Consumes: `effective_volume`, config `cross_side` / position / enabled
- Produces: PipeWire/WASAPI hear attenuated audio; `_config.set_channel_volume` and GUI still store **base**

- [ ] **Step 1: Helper on manager**

```python
def _effective_for_channel(self, channel: int, base: float) -> float:
    from nativmix.audio.crossfader import effective_volume
    # skip gain for the USB crossfader control channel (it drives position, not mix)
    if self._config.get_crossfader_usb_channel_index() == channel:
        return 0.0  # or do not apply apps — control channel has no apps
    return effective_volume(
        base,
        self._config.get_cross_side(channel),
        self._config.get_crossfader_position(),
        self._config.get_crossfader_enabled(),
    )
```

- [ ] **Step 2: Use `eff = self._effective_for_channel(channel, volume)` before `_should_apply_volume` / `_apply_*`**

Keep `self._poti_volumes[channel] = volume` and `set_channel_volume` persistence as **base**. Emit `channel_volume_changed` with **base** so GUI stays at 80%.

- [ ] **Step 3: Re-apply on crossfader move / disable**

Add `reapply_all_channel_volumes()` that for each channel applies `_effective_for_channel(ch, get_channel_volume(ch))` without changing stored bases. Call from:
- position changed
- enabled → false (cleanup)
- `cross_side` changed
- profile switch

- [ ] **Step 4: pytest still green for existing suite; add one manager-level test with mocks if feasible**

Run: `pytest -q`

---

### Task 6: Crossfader GUI (global bar + A/B + USB control)

**Files:**
- Modify: `lib/nativmix/gui/main_window.py` — layout, `ChannelWidget` context menu / mode, compact resize
- Modify: `lib/nativmix/gui/settings_panel.py` — enable checkbox
- Modify: `lib/nativmix/main.py` — signal wiring (USB poti → position, MIDI Learn)

**Layout (root):**

```
top_bar
settings_panel
channel_scroll
crossfader_row   ← QSlider horizontal + "A" / "B" labels; hide if disabled
bottom_bar (MIDI buttons + size grip)
```

- [ ] **Step 1: `CrossfaderBar` widget (can live in `main_window.py` or `gui/crossfader_bar.py`)**

- Horizontal `QSlider` range 0–1000 mapped to position 0.0–1.0  
- Right-click context: Learn MIDI / Clear MIDI  
- Emit `position_changed(float)`, `midi_learn_requested()`, `midi_clear_requested()`  
- Visible only when `crossfader_enabled`

- [ ] **Step 2: Compact mode**

In `_on_compact_toggled` / `_do_compact_resize`, add crossfader row height when visible:

```python
xf_h = self._crossfader_bar.height() if self._crossfader_bar.isVisible() else 0
h = m.top() + top_h + sp + ch_h + (sp if xf_h else 0) + xf_h + m.bottom()
```

- [ ] **Step 3: A/B assignment UI**

On each non-control channel: context menu on channel label (or small control):

- Cross group: None / A / B  
- Persist via `set_cross_side` + `save_current`  
- Hide / disable when crossfader feature off (optional: still show but greyed)

- [ ] **Step 4: USB control channel**

Exclusive assignment (picker or mode entry like app assignment):

- Label e.g. **“Crossfader control”**  
- Clear `app_names`; set `cross_side` to `none`  
- Vertical slider: greyed, fixed display **0%**, ignore poti for channel volume — poti updates **crossfader_position** instead  
- Only one USB (non-`is_midi`) channel; assigning another clears the previous  
- On feature disable: restore normal strip (slider active, apps assignable again); index → `None`

- [ ] **Step 5: Settings toggle**

`SettingsPanel`: checkbox “Enable A/B crossfader” → enables bar + A/B menus; on uncheck call backend `reapply_all` with gains 1.0.

- [ ] **Step 6: Wire in `main.py`**

- Arduino/USB volume for control index → `set_crossfader_position` + reapply (do not call normal channel volume for that index)  
- MIDI CC for crossfader binding → position  
- Learn handshake for bar (reuse existing `midi_cc_received` Learn pattern from channel Learn buttons)

- [ ] **Step 7: Maintainer hardware checklist**

1. Enable crossfader → bar at bottom in normal + compact.  
2. Channel A @ 80%, assign side A; move crossfader full B → silence A, GUI still 80%.  
3. Center → A audible again at 80%.  
4. Disable feature → audio = bases; control strip unfrozen.  
5. USB poti on control channel moves bar only.  
6. Right-click Learn on bar binds MIDI CC.  
7. Profile switch restores position / sides / binding.

---

### Task 7: Verification gate

- [ ] **Step 1: `pytest -q`** — all green  
- [ ] **Step 2: Optional `ruff check lib/` / `mypy lib/`** if time  
- [ ] **Step 3: No version bump** unless user requests release  
- [ ] **Step 4: User local install + hardware report** before any release tagging

---

## Spec coverage checklist

| Requirement | Task |
|-------------|------|
| #38 label → profile | Task 1 |
| #39 trailing MIDI CC | Task 2 |
| #39 System Master fight | Task 2 |
| Base vs effective volume | Tasks 3–5 |
| Disable cleanup (gain 1 + reapply) | Tasks 5–6 |
| A/B per channel | Task 6 |
| One USB control channel | Task 6 |
| MIDI Learn only on bar | Task 6 |
| Bar bottom + compact visible | Task 6 |
| Profile persistence | Task 4 |
| WASAPI parity | Task 5 |

## Out of scope

- Version bump / GitHub release  
- Adjacent channel link / Easy Effects  
- Assigning MIDI-only strip as USB-style crossfader control via channel mode  
- Placing the horizontal crossfader between fader and app list per strip
