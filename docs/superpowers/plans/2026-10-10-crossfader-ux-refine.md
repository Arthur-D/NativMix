# Crossfader UX Refine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refine the existing A/B crossfader UI to match the approved design: centered bar width `clamp(n,2,6)×strip`, control name above the slider, **Targets** menu (Apps + Hardware submenu + crossfader control), A/B assignment via right-click on A/B labels by channel name, MIDI Learn only in MIDI edit mode — without changing audio math or profile field schema.

**Architecture:** Pure layout helpers for bar width/eligibility live next to existing `crossfader.py` (or a tiny `gui/crossfader_layout.py`). `CrossfaderBar` gains a control-name label and side-assignment signals; `ChannelWidget` replaces Device/+App with a unified Targets menu and drops strip A/B/control context actions; `MainWindow` centers the bar and gates Learn on edit mode. Backend apply paths stay untouched.

**Tech Stack:** Python 3.10+, PyQt6, existing `ConfigManager` / `ProfileManager` crossfader APIs, pytest.

**Spec:** `docs/superpowers/specs/2026-10-10-crossfader-ux-refine-design.md`

## Global Constraints

- Communicate with maintainer in German; code and comments in English.
- Do **not** bump `__version__` / packaging unless the user explicitly asks.
- Do **not** commit unless the user explicitly asks (fish shell — no bash heredoc; use `git commit -m "..."` or `-F file`).
- Do **not** post GitHub issue comments unless asked.
- Do **not** change `effective_volume` / piecewise gains / PipeWire·WASAPI apply semantics.
- Profile fields unchanged: `crossfader_*` root keys + per-channel `cross_side`.
- Button label: **Targets**. Placeholder when no control: **—**.
- Bar width: `clamp(n_channels, 2, 6) * strip_width`, centered under channel strips.
- Multiple channels per A/B side OK; opposite side + USB control channel ineligible.
- MIDI Learn / Clear on the bar only when MIDI Edit mode is active; cancel learn when edit turns off.
- Device checkbox removed; hardware mode via Targets → Hardware submenu.
- Run `pytest -q` before claiming done; maintainer validates with local install.

## File map

| File | Role |
|------|------|
| `lib/nativmix/audio/crossfader.py` or `lib/nativmix/gui/crossfader_layout.py` | Pure helpers: `crossfader_bar_width(n, strip_w)`, `side_menu_eligibility(...)` |
| `lib/nativmix/gui/crossfader_bar.py` | Control name label; A/B label context menus; Learn gated by `set_midi_edit_mode` |
| `lib/nativmix/gui/main_window.py` | Center/resize bar; Targets menu; remove Device + strip XF context; wire rename → control label; edit-mode → Learn gate |
| `tests/test_crossfader_layout.py` | **New** width + eligibility unit tests |
| `tests/test_crossfader_bar.py` | Extend for label / learn gating if practical |

---

### Task 1: Pure layout + A/B eligibility helpers

**Files:**
- Create or extend: `lib/nativmix/gui/crossfader_layout.py` (prefer GUI package so audio stays math-only)
- Test: `tests/test_crossfader_layout.py`

**Interfaces:**
- Produces:
  - `crossfader_bar_width(num_channels: int, strip_width: int) -> int`
  - `channel_eligible_for_side(side: str, channel_index: int, *, control_index: int | None, sides: dict[int, str]) -> bool`  
    where `sides[i]` is `"none"|"a"|"b"`; `side` is `"a"` or `"b"`
  - Optional: `display_name_for_channel(config, index) -> str` if not already duplicated elsewhere

```python
def crossfader_bar_width(num_channels: int, strip_width: int) -> int:
    n = max(0, int(num_channels))
    w = max(1, int(strip_width))
    return max(2, min(6, n if n > 0 else 2)) * w
```

Note: when `n == 0` treat as 2 strips wide (empty rebuild edge case). When `n == 1`, still min 2 × strip width per spec.

Eligibility:

```python
def channel_eligible_for_side(
    side: str,
    channel_index: int,
    *,
    control_index: int | None,
    sides: dict[int, str],
) -> bool:
    if control_index is not None and channel_index == control_index:
        return False
    other = "b" if side == "a" else "a"
    if (sides.get(channel_index, "none") or "none").lower() == other:
        return False
    return True
```

- [ ] **Step 1: Write failing tests**

```python
# tests/test_crossfader_layout.py
from nativmix.gui.crossfader_layout import crossfader_bar_width, channel_eligible_for_side

def test_bar_width_clamps_2_to_6():
    assert crossfader_bar_width(1, 70) == 140   # min 2
    assert crossfader_bar_width(3, 70) == 210
    assert crossfader_bar_width(6, 70) == 420
    assert crossfader_bar_width(10, 70) == 420  # max 6

def test_eligibility_blocks_control_and_opposite():
    sides = {0: "a", 1: "b", 2: "none"}
    assert channel_eligible_for_side("a", 2, control_index=3, sides=sides) is True
    assert channel_eligible_for_side("a", 3, control_index=3, sides=sides) is False
    assert channel_eligible_for_side("a", 1, control_index=None, sides=sides) is False  # on B
    assert channel_eligible_for_side("b", 0, control_index=None, sides=sides) is False  # on A
    assert channel_eligible_for_side("a", 0, control_index=None, sides=sides) is True   # already A → can uncheck
```

Clarify in implementation: for **already on same side**, menu entry stays enabled/checkable so user can clear. Eligibility for *listing as disabled* vs *toggling* — use:

- `eligible` = not control and not opposite → can appear as enabled checkable  
- already same side → enabled + checked  
- opposite / control → disabled (or omit)

Adjust `channel_eligible_for_side` so same-side returns `True` (toggle off allowed); only control + opposite return `False`. Tests above match that.

- [ ] **Step 2: Run tests — expect FAIL (module missing)**

Run: `pytest tests/test_crossfader_layout.py -v`

- [ ] **Step 3: Implement helpers**

- [ ] **Step 4: Run tests — PASS**

- [ ] **Step 5: Commit only if user asks**

Message suggestion: `Add crossfader bar width and A/B eligibility helpers.`

---

### Task 2: CrossfaderBar — control name, A/B menus, Learn gate

**Files:**
- Modify: `lib/nativmix/gui/crossfader_bar.py`
- Modify: `tests/test_crossfader_bar.py`

**Interfaces:**
- Consumes: `channel_eligible_for_side` from Task 1
- Produces signals / API:
  - `side_assignment_toggled = pyqtSignal(str, int, bool)`  # side "a"|"b", channel_index, checked
  - `set_control_name(self, name: str) -> None`  # `—` when empty
  - `set_midi_edit_mode(self, active: bool) -> None`  # gates Learn/Clear in context menu; cancel learn when False
  - `set_side_menu_model(self, entries: list[tuple[int, str, str]]) -> None`  
    # (channel_index, display_name, current_side "none"|"a"|"b") plus control_index via `set_control_index(int | None)`
  - Keep existing: `position_changed`, `midi_learn_requested`, `midi_clear_requested`, `set_position`, `set_learning`, `set_midi_label`

Layout inside bar:

```
QVBoxLayout:
  QLabel control_name  (center, small font)
  QHBoxLayout: A | slider | B
```

- Right-click on **A** / **B** labels (install event filter or `customContextMenu` on each label) → build checkable menu from model; use eligibility helper; emit `side_assignment_toggled`.
- Right-click on slider / empty area: if `_midi_edit_mode`: Learn/Clear as today; else no Learn actions (tooltip may mention Edit MIDI).
- `set_midi_edit_mode(False)` must call cancel path if `_learning` (emit clear of learning state via `set_learning(False)` and notify MainWindow if needed — MainWindow already tracks `_crossfader_midi_learning`).

- [ ] **Step 1: Extend/adjust unit tests for `set_control_name` and learn gate** (widget-level with qtbot if existing tests already use it)

- [ ] **Step 2: Implement bar changes**

- [ ] **Step 3: `pytest tests/test_crossfader_bar.py tests/test_crossfader_layout.py -q` green**

- [ ] **Step 4: Commit only if user asks**

---

### Task 3: MainWindow — center bar + control name + Learn gate wiring

**Files:**
- Modify: `lib/nativmix/gui/main_window.py` (layout around `_crossfader_bar`, `refresh_crossfader_ui`, rename, edit MIDI toggle, compact resize)

**Interfaces:**
- Consumes: `crossfader_bar_width`, CrossfaderBar new APIs
- Strip width source: use first channel widget `width()` if built, else `_CHANNEL_MIN_WIDTH` (60) or average; recompute on `resizeEvent` / after `_rebuild_channels` / when channel count changes.

Centering approach (keep YAGNI):

```python
# Wrap bar in a QWidget with QHBoxLayout: stretch | bar | stretch
# Or setMaximumWidth + AlignHCenter in the root layout item
self._crossfader_row = QWidget()
row = QHBoxLayout(self._crossfader_row)
row.setContentsMargins(0, 0, 0, 0)
row.addStretch(1)
row.addWidget(self._crossfader_bar)
row.addStretch(1)
root.addWidget(self._crossfader_row)  # instead of adding bar directly
```

On refresh:

```python
n = len(self._channels)
w = self._channels[0].width() if self._channels else _CHANNEL_MIN_WIDTH
self._crossfader_bar.setFixedWidth(crossfader_bar_width(n, w))
# update control name + side menu model from config
```

Wire:

- `side_assignment_toggled` → `set_cross_side` / clear to none → persist → `reapply_all_channel_volumes` → refresh menus
- Rename (`_on_rename` / after persist): if renamed channel is control → `set_control_name(...)`
- `_on_edit_midi_toggled`: `self._crossfader_bar.set_midi_edit_mode(checked)`; if not checked cancel `_crossfader_midi_learning`
- Compact resize: still include `_crossfader_row` height when visible

- [ ] **Step 1: Implement centering + refresh helpers**

- [ ] **Step 2: Manual smoke checklist note in report** (2 / 6 / >6 channels)

- [ ] **Step 3: `pytest -q` green**

- [ ] **Step 4: Commit only if user asks**

---

### Task 4: Targets menu + remove Device checkbox + strip XF context

**Files:**
- Modify: `lib/nativmix/gui/main_window.py` (`ChannelWidget`: `_add_btn`, `_mode_cb`, `_open_picker`, `_apply_mode_ui`, `contextMenuEvent`, `apply_crossfader_role`)

**Interfaces:**
- Button text always **Targets** (compact still hides app list + Targets button as today).
- `_open_picker` builds one `QMenu`:
  1. Disabled header `Apps` then app actions (reuse `_open_stream_picker` candidate logic; picking an app calls existing assign path **and** forces app mode: `set_channel_mode(..., "app")`, clear `hardware_id` — same as unchecking Device today).
  2. Submenu `Hardware` — reuse `_open_hw_picker` contents; picking hardware forces hardware mode (clear apps, disable V-Sink if needed) via existing `_on_hw_picked` / mode helpers.
  3. If `get_crossfader_enabled()`: separator + `Use this fader as crossfader control` (checkable) / `Clear crossfader control` when this strip is control. MIDI-only strips: hide or disable control assignment (spec: USB control — keep existing rule that only non-MIDI can be control).

Remove:

- `_mode_cb` widget from layout and all visibility toggles in compact/restore.
- Strip `contextMenuEvent` crossfader group / set-as-control (Windows mute-hotkey context on mute button stays).

`apply_crossfader_role`: stop referencing `_mode_cb`; keep Targets/`_add_btn` disabled while control.

- [ ] **Step 1: Implement Targets menu**

- [ ] **Step 2: Remove Device checkbox**

- [ ] **Step 3: Remove obsolete strip XF context menu**

- [ ] **Step 4: `pytest -q` + quick offscreen smoke if feasible**

- [ ] **Step 5: Commit only if user asks**

---

### Task 5: Verification gate

- [ ] **Step 1: `pytest -q` — all green**
- [ ] **Step 2: Optional `ruff check lib/` if available**
- [ ] **Step 3: Confirm no version bump**
- [ ] **Step 4: Maintainer hardware checklist**
  1. Bar centered; width grows with strips until 6, then stays 6-wide centered with more strips.
  2. Control name above bar; rename updates it.
  3. Targets: Apps + Hardware submenu + crossfader control; Device checkbox gone.
  4. Right-click A/B assigns by label; opposite side disabled; multi same side OK.
  5. MIDI Learn on bar only with Edit MIDI on; off cancels learn.
  6. Profile switch restores control index, sides, position; label matches.

---

## Spec coverage

| Spec item | Task |
|-----------|------|
| Width clamp 2–6, centered | 1, 3 |
| Control name above bar | 2, 3 |
| A/B right-click by name, multi, opposite blocked | 1, 2, 3 |
| Targets menu + Hardware submenu | 4 |
| Remove Device checkbox | 4 |
| Control assign in Targets; strip grey | 4 |
| Learn only in MIDI edit | 2, 3 |
| Profile fields unchanged | (no schema task) |
| Audio unchanged | (explicit non-touch) |

## Out of scope

- Version bump / GitHub release
- Changing crossfader curve or backend gain apply
- Full-width bar
