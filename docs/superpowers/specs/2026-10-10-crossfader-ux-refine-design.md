# Crossfader UX refine — Design

**Date:** 2026-10-10  
**Status:** Approved (approach B) — implementation plan: `docs/superpowers/plans/2026-10-10-crossfader-ux-refine.md`  

**Builds on:** `docs/superpowers/plans/2026-10-10-crossfader-midi-fixes.md` (already on `main`)  
**Approach:** B (agreed)

## Goal

Refine the shipped A/B crossfader UI so it matches hardware-test feedback: tighter bar geometry, clearer assignment UX (Targets menu + A/B-by-name), and MIDI Learn only in MIDI edit mode — without changing the audio model (`base × gain`, profile persistence).

## Non-goals

- Version bump / release (unless asked later)
- Changing the piecewise crossfader curve or effective-volume apply logic
- Per-strip duplicate crossfaders
- Reintroducing a full-window-width crossfader row

## Current vs target

| Area | Current (on main) | Target |
|------|-------------------|--------|
| Bar width / place | Full window width under channel scroll | Width = `clamp(n_channels, 2, 6) × strip_width`, centered under the channel strip area |
| Control identity | Tooltip / frozen strip only | Channel **label** shown **above** the slider; updates on rename, assign, clear, profile switch |
| A/B assign | Right-click channel strip → Crossfader group | Right-click **A** or **B** label → checklist of channel labels |
| Multi A/B | `cross_side` none/a/b per channel | Same; multiple per side OK; opposite side + control channel unavailable |
| + button | `+ App` / `+ Device` + separate Device checkbox | **Targets** menu: Apps · Hardware submenu · Crossfader control |
| Device checkbox | Visible under strip | **Removed**; hardware mode entered via Targets → Hardware |
| MIDI Learn on bar | Always via right-click | Only when **MIDI Edit** mode is active |

## Layout

```
[ top bar / settings … ]
[ channel strips ………………… ]   ← scroll area
      [ ControlLabel ]            ← name of USB control channel (or placeholder)
      [ A ════●════ B ]           ← width clamp(2..6)×strip, centered under strips
[ bottom: MIDI buttons / grip ]
```

### Width formula

- Let `n` = number of visible channel strips (same count as `config.num_channels` / rebuilt strips).
- Let `w` = current strip width (or fixed channel min/typical width used for layout).
- Bar content width = `clamp(n, 2, 6) * w`.
- Horizontally center within the channel container / window content area under the strips (not stretched to window edges).
- Compact mode: bar still visible when crossfader enabled; include bar height in compact resize (unchanged requirement).

### Control name label

- Source: `get_channel_label(control_idx)` or default `CH {i+1}` / `MIDI {i+1}`.
- Empty / no control: show a short placeholder (e.g. `—` or `No control`) — not a fake channel name.
- Refresh when: assign/clear control, rename of that channel, rebuild channels, profile apply.

## Targets menu (per strip)

Button text: **Targets** (replaces `+ App` / `+ Device`).

Suggested structure:

1. **Apps** (section header, disabled)  
   - Same app candidates as today’s stream picker (incl. System Master / Other Apps rules).  
   - Selecting an app implies **app mode** (clear hardware assignment).

2. **Hardware** (submenu)  
   - Outputs / Inputs as today.  
   - Choosing hardware implies **hardware mode** (clear apps, same side effects as current Device toggle).  
   - Clear / uncheck current device as today.

3. **Crossfader** (section or submenu, only when feature enabled)  
   - **Use this fader as crossfader control** (checkable; exclusive — assigning this strip clears previous control).  
   - **Clear crossfader control** (only if this strip is the current control).  
   - Control strip: greyed, frozen fader display, no apps; poti drives position (existing behavior).

### Device checkbox

- Remove `Device` checkbox from the strip toggles layout.
- Invert / V-Sink remain as today (V-Sink still app-mode only).

## A/B assignment UI

- Remove channel-strip context menu entries for “Crossfader group” / Set-as-control (control moves to Targets; group moves to A/B labels).
- Right-click **A** label → menu of all eligible channels by **display name**, checkable; toggling sets `cross_side` to `a` or `none`.
- Right-click **B** label → same for `b`.
- Rules for each entry:
  - Control channel: disabled / hidden.
  - Channel already on the **other** side: disabled (cannot be A and B).
  - Multiple channels on the same side: allowed.
- Persist via existing `set_cross_side` + `persist_active_profile_channels` / profile save.

## MIDI Learn

- Crossfader bar context menu shows **Learn / Clear** only when MainWindow MIDI edit mode is on (`_edit_midi_btn` checked / equivalent flag).
- When edit mode turns off: cancel any in-progress crossfader learn.
- When edit mode is off: right-click may still show a disabled hint or no Learn actions (prefer: no Learn/Clear actions; optional short tooltip “Enable Edit MIDI Channel to learn”).

## Persistence (unchanged model)

Profile root (already present):

- `crossfader_enabled`, `crossfader_position`, `crossfader_usb_channel_index`
- `crossfader_midi_cc`, `crossfader_midi_channel`

Per channel:

- `cross_side`: `none` | `a` | `b`

Display name above the bar is **not** stored separately — always derived from the control channel’s label.

## Audio / backend

No change to:

- `effective = base × side_gain`
- USB control skip in apply paths
- `reapply_all_channel_volumes` on position / side / enable / profile

## Testing (implementation phase)

- Unit: width helper `clamp(n, 2, 6) * strip_w` and eligibility for A/B menus (control / opposite side).
- GUI smoke: Targets opens Apps + Hardware submenu + Crossfader; Device checkbox gone; Learn gated by edit mode; label above bar updates on rename.
- Maintainer hardware: poti → position, multi A/B, compact centering with 2 / 6 / >6 channels.

## Open polish (non-blocking)

- Exact placeholder string when no control assigned.
- Whether “Crossfader” in Targets is a flat section vs submenu (flat section preferred for one action).
- Whether strip right-click keeps mute-hotkey / other non-crossfader actions only.
