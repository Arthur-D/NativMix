# NativMix Profile System — Design Spec
Date: 2026-05-05

## Overview

A profile stores the channel-level configuration of the mixer (names, app assignments,
MIDI CC mappings, optionally fader positions). Global settings (port, baud rate, input
mode) are hardware-specific and remain in `config.json`. Multiple profiles can coexist;
switching is instant via UI dropdown, IPC command, or MIDI CC.

---

## 1. File Structure

```
~/.config/nativmix/
  config.json                  # global settings only (no channels[])
  profiles/
    profile-1.json
    profile-2.json
    profile-voice-chat.json
    ...
```

### config.json (v7, stripped of channel data)

```json
{
  "version": 7,
  "active_profile": "profile-1",
  "hardware": {
    "port": null,
    "auto_search_device": true,
    "num_channels": 7,
    "input_mode": "usb",
    "midi_device": "",
    "midi_channel_count": 0,
    "baud_rate": 9600
  },
  "settings": {
    "threshold": 0.01,
    "transparency": true,
    "compact_mode": false,
    "stay_open": false,
    "show_invert_option": false,
    "debug_logging": false,
    "profile_midi_next_cc": null,
    "profile_midi_prev_cc": null
  }
}
```

`profile_midi_next_cc` and `profile_midi_prev_cc` are global (apply across all profiles).

### Profile file format

```json
{
  "id": "profile-1",
  "name": "Profile 1",
  "channel_count": 7,
  "restore_fader_positions": false,
  "midi_switch_cc": null,
  "channels": [
    {
      "index": 0,
      "label": "",
      "is_midi": false,
      "app_names": ["spotify"],
      "midi_cc": null,
      "midi_mute_cc": null,
      "inverted": false,
      "v_sink": false,
      "mode": "app",
      "hardware_id": null,
      "volume": 0.8
    }
  ]
}
```

`volume` is always saved (as last known state). Whether it is **applied** on load
depends on `restore_fader_positions`.

---

## 2. Migration (Config v6 → v7)

Triggered automatically on startup when `config.version < 7`:

1. Read `channels[]` from `config.json`
2. Create `~/.config/nativmix/profiles/profile-1.json` with:
   - `name: "Profile 1"`, `channel_count` from hardware.num_channels
   - `restore_fader_positions: false`
   - `channels[]` copied verbatim from old config
3. Remove `channels[]`, `settings.invert_map`, `settings.v_sink_map` from `config.json`
   (these are now per-profile as `inverted`/`v_sink` on each channel)
4. Set `active_profile: "profile-1"`
5. Set `version: 7`, save

---

## 3. Auto-Profile Creation

Triggered on **startup** or **port change** only — never on manual profile switch.

Rule: if `hardware.num_channels < active_profile.channel_count` → hardware cannot
serve this profile → auto-create a new profile.

Hardware with **more** channels than the profile: extra channels are simply unassigned.
No new profile is created.

Auto-created profiles are named "Profile N" (next available number) and use
`restore_fader_positions: false`.

---

## 4. ProfileManager

New class: `lib/nativmix/utils/profile_manager.py`

```python
class ProfileManager(QObject):
    profile_changed = pyqtSignal(str)    # profile_id after every switch
    profile_list_changed = pyqtSignal()  # after create / rename / delete

    # Properties
    active_profile_id: str
    active_profile: dict

    # Methods
    list_profiles() -> list[dict]        # [{id, name, channel_count}, ...]
    load(profile_id) -> dict             # load without activating
    switch(profile_id) -> None           # activate + emit profile_changed
    switch_next() -> None
    switch_prev() -> None
    create(name: str, channel_count: int) -> str   # returns new id
    rename(profile_id: str, name: str) -> None
    delete(profile_id: str) -> None      # cannot delete last profile
    save_current(channels: list[dict]) -> None  # write current state back
```

### Profile ID generation

IDs are stable slugs independent of the name: `profile-1`, `profile-2`, `profile-3`, …
(next available integer, never reused). Renaming a profile does not change its ID.

### Integration in main.py

```
profile_manager.profile_changed → config.apply_profile(dict) → settings_changed.emit()
```

`ConfigManager.apply_profile(profile: dict)` replaces the in-memory channel data and
`invert_map` / `v_sink_map` (derived from channel flags) without touching hardware
settings. Does not save to disk — profile file is the source of truth for channel data.

If `profile.restore_fader_positions == True`:
- volumes from the profile are immediately applied via `backend.apply_volumes()`
- `arduino.set_takeover_pending(set(range(channel_count)))` is called

### Auto-save of channel changes back to the profile

When the user changes an app assignment, channel name, or any other channel-level
setting during a session, that change must be persisted to the active profile file —
not to `config.json`.

`ProfileManager` listens to `config.mapping_changed` and `config.settings_changed`.
On any such event it calls `save_current(config.all_channels())` to write the updated
channel data back to the active profile file.

`ConfigManager.save()` is changed to write only global settings (hardware block +
settings block + active_profile). The `channels[]` key is never written to `config.json`
after migration.

---

## 5. Fader Takeover

Only relevant when `restore_fader_positions: true`.

`ArduinoThread` gets a new field `_takeover_pending: set[int]`.

Behaviour in `_process_line` / volume update path:
- If channel index is in `_takeover_pending`: suppress the hardware volume (do not emit
  for that channel) until the first movement is detected (existing threshold mechanism
  fires → movement confirmed → remove from `_takeover_pending`)
- Once removed: channel behaves normally, hardware fader is the authority

Public method: `set_takeover_pending(channels: set[int]) -> None`

Loaded fader values are **temporary starting points**. They are not re-applied after the
takeover is cleared. `save_current()` always writes the actual current volumes, not the
originally loaded ones.

For motorized faders: the fader physically moves to the saved position, so hardware and
profile are in sync from the start — takeover clears immediately on first hardware report.

---

## 6. UI

### Top Bar

```
[ Settings ]  [ Profile 1 ▼ ] [+]       [ Compact ] [ Don't Close ]
```

- `QComboBox` (editable) listing all profiles by name, ordered by creation
- Selecting a different entry calls `profile_manager.switch()`
- Typing in the combo + Enter/focus-loss → `profile_manager.rename()` with 500 ms
  debounce (same pattern as port selector)
- `[+]` button: creates "Profile N", switches to it, combo enters edit mode immediately

### Settings Panel — Profile Section

New collapsible section "Profile" (always visible, not hidden behind a mode):

```
☐ Fader-Positionen laden beim Wechsel
```

Checkbox maps to `active_profile.restore_fader_positions`. Saved on change.

### Settings Panel — MIDI Profile Switch (collapsible, default collapsed)

```
▶ Profil-Umschaltung (MIDI)
  Nächstes Profil:        [ CC 20 ] [Learn] [✕]
  Vorheriges Profil:      [ CC 21 ] [Learn] [✕]
  Dieses Profil direkt:   [ CC -- ] [Learn] [✕]
```

- "Nächstes" / "Vorheriges" store to `settings.profile_midi_next_cc` /
  `settings.profile_midi_prev_cc` in `config.json` (global)
- "Dieses Profil direkt" stores to `active_profile.midi_switch_cc` (per-profile)
- Learn uses the existing MIDI-Learn mechanism (listen for next CC event)

---

## 7. IPC

New CLI arguments:

```bash
nativmix --profile next
nativmix --profile prev
nativmix --profile "Voice Chat"     # match by name (case-insensitive)
```

IPC wire format: `profile:next`, `profile:prev`, `profile:<name>`

New signal on `IPCServer`: `profile_switch_requested = pyqtSignal(str)` (carries the
raw argument string). Handler in `main.py` resolves name → id and calls
`profile_manager.switch()` or `switch_next()` / `switch_prev()`.

---

## 8. MIDI Profile Switching (runtime)

`MidiThread` receives two new global CC numbers (`next_cc`, `prev_cc`) and a map
`{cc: profile_id}` for direct switches. All set via:

```python
midi.set_profile_ccs(next_cc, prev_cc, direct_map)
```

Called from `_on_settings_changed()` in `main.py` (same pattern as existing
`update_mute_mappings`).

New signal: `MidiThread.profile_switch_requested = pyqtSignal(str)` — carries
`"next"`, `"prev"`, or a profile id. Connected to the same handler as IPC.

---

## 9. Data Flow on Profile Switch

```
user selects profile in dropdown
  → profile_manager.switch("profile-2")
    → loads profile-2.json
    → config.apply_profile(profile_dict)        # replaces channel data in memory
    → if restore_fader_positions:
        backend.apply_volumes(saved_volumes)
        arduino.set_takeover_pending({0,1,2,...})
    → settings_changed.emit()                   # existing handlers update Arduino + MIDI
    → profile_changed.emit("profile-2")         # UI updates combo selection
    → config.active_profile = "profile-2"
    → config.save()                             # only global config, not channel data
```

---

## 10. What is NOT in scope

- Profile import/export UI (can be done manually by copying files)
- Profile reorder via drag-and-drop
- Per-profile hardware settings (port, baud rate, input mode)
- Cloud sync
- Profile templates
