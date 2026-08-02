import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import make_profile, write_profile  # noqa: E402

from nativmix.utils.profile_manager import _coerce_channel_count  # noqa: E402


def _make_manager(profiles_dir: Path):
    from nativmix.utils.profile_manager import ProfileManager
    return ProfileManager(profiles_dir=profiles_dir)


@pytest.mark.parametrize(
    ("value", "fallback", "expected"),
    [
        (None, 7, 7),
        (-3, 7, 7),
        ("12", 7, 12),
        ("bad", 7, 7),
        (0, 7, 0),
        (5, 7, 5),
    ],
)
def test_coerce_channel_count(value, fallback, expected):
    assert _coerce_channel_count(value, fallback) == expected


# ── list_profiles ────────────────────────────────────────────────────────────

def test_list_profiles_empty(qtbot, tmp_profiles_dir):
    pm = _make_manager(tmp_profiles_dir)
    assert pm.list_profiles() == []


def test_list_profiles_returns_sorted(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-2", "B", 5))
    write_profile(tmp_profiles_dir, make_profile("profile-1", "A", 5))
    pm = _make_manager(tmp_profiles_dir)
    names = [p["name"] for p in pm.list_profiles()]
    assert names == ["A", "B"]


# ── load ─────────────────────────────────────────────────────────────────────

def test_load_returns_profile_dict(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", channel_count=7))
    pm = _make_manager(tmp_profiles_dir)
    p = pm.load("profile-1")
    assert p["channel_count"] == 7
    assert p["name"] == "Profile 1"


def test_load_missing_raises(qtbot, tmp_profiles_dir):
    pm = _make_manager(tmp_profiles_dir)
    with pytest.raises(FileNotFoundError):
        pm.load("profile-99")


def test_load_reconciles_channel_count_when_too_high(tmp_profiles_dir):
    """When channels are truncated, load() pads back to canonical channel_count."""
    profile = make_profile("profile-1", channel_count=10)  # says 10 channels
    # But only write 5 channels
    profile["channels"] = profile["channels"][:5]
    write_profile(tmp_profiles_dir, profile)
    pm = _make_manager(tmp_profiles_dir)
    loaded = pm.load("profile-1")
    assert loaded["channel_count"] == 10
    assert len(loaded["channels"]) == 10
    assert loaded["channels"][:5] == profile["channels"][:5]


def test_load_repair_invalid_channel_count_value(tmp_profiles_dir):
    profile = make_profile("profile-1", channel_count=5)
    profile["channel_count"] = "invalid"
    write_profile(tmp_profiles_dir, profile)
    pm = _make_manager(tmp_profiles_dir)
    loaded = pm.load("profile-1")
    assert loaded["channel_count"] == 5
    assert len(loaded["channels"]) == 5


def test_load_reconciles_channel_count_when_too_low(tmp_profiles_dir):
    """channel_count is canonical: expanded channel payload is clamped."""
    profile = make_profile("profile-1", channel_count=2)
    # Write 5 channels despite channel_count=2
    profile["channels"] = make_profile("profile-1", channel_count=5)["channels"]
    write_profile(tmp_profiles_dir, profile)
    pm = _make_manager(tmp_profiles_dir)
    loaded = pm.load("profile-1")
    assert loaded["channel_count"] == 2
    assert len(loaded["channels"]) == 2


def test_load_reconcile_persists_fix_to_disk(tmp_profiles_dir):
    """After reconciling a mismatch, load() writes the corrected channel_count
    back to disk so subsequent calls see a consistent file."""
    import json
    profile = make_profile("profile-1", channel_count=10)
    profile["channels"] = profile["channels"][:5]
    write_profile(tmp_profiles_dir, profile)
    pm = _make_manager(tmp_profiles_dir)
    pm.load("profile-1")  # triggers reconciliation + disk write
    # Re-read raw file — should now have the corrected count
    raw = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    assert raw["channel_count"] == 10
    assert len(raw["channels"]) == 10


def test_load_reconcile_warning_not_duplicated(tmp_profiles_dir, caplog):
    """After the first load() reconciles a mismatch and saves the fix,
    a second load() must NOT emit another WARNING for the same profile."""
    import logging
    profile = make_profile("profile-1", channel_count=17)
    profile["channels"] = make_profile("profile-1", channel_count=30)["channels"]
    write_profile(tmp_profiles_dir, profile)
    pm = _make_manager(tmp_profiles_dir)

    with caplog.at_level(logging.WARNING, logger="nativmix.utils.profile_manager"):
        pm.load("profile-1")  # first load — reconciles + saves
        caplog.clear()
        pm.load("profile-1")  # second load — file is now consistent

    reconcile_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "reconcil" in r.message
    ]
    assert reconcile_warnings == [], (
        "No reconcile warning should be emitted on the second load() after fix was persisted"
    )


def test_load_repairs_duplicate_channel_indexes_and_preserves_mappings(tmp_profiles_dir):
    """Duplicate channel entries are collapsed by stable index without dropping mappings."""
    import json
    profile = make_profile("profile-1", channel_count=4)
    profile["channels"][1]["app_names"] = ["Spotify"]
    profile["channels"][1]["midi_cc"] = 12
    duplicate = dict(profile["channels"][1])
    duplicate["app_names"] = ["Firefox"]
    duplicate["midi_cc"] = None
    duplicate["midi_mute_cc"] = 21
    profile["channels"].append(duplicate)
    # Canonical count stays 4 even though payload contains one duplicate row.
    profile["channel_count"] = 4
    write_profile(tmp_profiles_dir, profile)

    pm = _make_manager(tmp_profiles_dir)
    loaded = pm.load("profile-1")

    assert len(loaded["channels"]) == 4
    assert loaded["channel_count"] == 4
    ch1 = loaded["channels"][1]
    assert ch1["app_names"] == ["Spotify", "Firefox"]
    assert ch1["midi_cc"] == 12
    assert ch1["midi_mute_cc"] == 21

    raw = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    assert len(raw["channels"]) == 4
    assert raw["channel_count"] == 4


def test_load_repairs_polluted_expanded_channels_without_upward_reconcile(tmp_profiles_dir):
    """Polluted expanded channel payload is shrunk to canonical count and persisted."""
    profile = make_profile("profile-4", channel_count=17)
    profile["channels"][16]["label"] = "Keep Me"
    profile["channels"][16]["midi_cc"] = 42
    inflated_tail = make_profile("profile-4", channel_count=47)["channels"][17:]
    for offset, ch in enumerate(inflated_tail, start=17):
        ch["index"] = offset
        ch["label"] = f"stale-{offset}"
    profile["channels"].extend(inflated_tail)
    write_profile(tmp_profiles_dir, profile)

    pm = _make_manager(tmp_profiles_dir)
    loaded = pm.load("profile-4")

    assert loaded["channel_count"] == 17
    assert len(loaded["channels"]) == 17
    assert loaded["channels"][16]["label"] == "Keep Me"
    assert loaded["channels"][16]["midi_cc"] == 42
    assert all(ch.get("label") != "stale-17" for ch in loaded["channels"])

    raw = json.loads((tmp_profiles_dir / "profile-4.json").read_text())
    assert raw["channel_count"] == 17
    assert len(raw["channels"]) == 17


# ── create ────────────────────────────────────────────────────────────────────

def test_create_returns_new_id(qtbot, tmp_profiles_dir):
    pm = _make_manager(tmp_profiles_dir)
    new_id = pm.create("My Profile", channel_count=5)
    assert new_id == "profile-1"
    assert (tmp_profiles_dir / "profile-1.json").exists()


def test_create_increments_id(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1"))
    pm = _make_manager(tmp_profiles_dir)
    new_id = pm.create("Second", channel_count=5)
    assert new_id == "profile-2"


def test_create_profile_has_correct_fields(qtbot, tmp_profiles_dir):
    pm = _make_manager(tmp_profiles_dir)
    new_id = pm.create("Test", channel_count=3)
    p = pm.load(new_id)
    assert p["name"] == "Test"
    assert p["channel_count"] == 3
    assert p["restore_fader_positions"] is False
    assert p["midi_switch_cc"] is None
    assert len(p["channels"]) == 3


# ── rename ────────────────────────────────────────────────────────────────────

def test_rename_updates_name(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", "Old"))
    pm = _make_manager(tmp_profiles_dir)
    pm.rename("profile-1", "New Name")
    assert pm.load("profile-1")["name"] == "New Name"


# ── delete ────────────────────────────────────────────────────────────────────

def test_delete_removes_file(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1"))
    write_profile(tmp_profiles_dir, make_profile("profile-2", "B"))
    pm = _make_manager(tmp_profiles_dir)
    pm.delete("profile-1")
    assert not (tmp_profiles_dir / "profile-1.json").exists()


def test_delete_last_profile_raises(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1"))
    pm = _make_manager(tmp_profiles_dir)
    with pytest.raises(ValueError, match="last profile"):
        pm.delete("profile-1")


# ── save_current ──────────────────────────────────────────────────────────────

def test_save_current_updates_channels(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", channel_count=2))
    pm = _make_manager(tmp_profiles_dir)
    pm._active_profile_id = "profile-1"
    updated = make_profile("profile-1", channel_count=2)
    updated["channels"][0]["app_names"] = ["spotify"]
    pm.save_current(updated["channels"])
    reloaded = pm.load("profile-1")
    assert reloaded["channels"][0]["app_names"] == ["spotify"]


def test_save_current_resize_ignores_polluted_runtime_tail(tmp_profiles_dir):
    """Intentional resize must not persist stale channels beyond the stored template."""
    profile = make_profile("profile-2", channel_count=13)
    profile["channels"][5]["label"] = "keep-midi-1"
    profile["channels"][5]["is_midi"] = True
    profile["channels"][5]["midi_cc"] = 21
    profile["channels"][12]["label"] = "keep-midi-8"
    profile["channels"][12]["is_midi"] = True
    write_profile(tmp_profiles_dir, profile)

    polluted = make_profile("profile-1", channel_count=31)["channels"]
    polluted[:13] = json.loads(json.dumps(profile["channels"]))
    polluted[13]["label"] = "stale-midi-9"
    polluted[13]["is_midi"] = True
    polluted[13]["app_names"] = ["Stale App"]
    polluted[13]["midi_cc"] = 99

    pm = _make_manager(tmp_profiles_dir)
    pm._active_profile_id = "profile-2"
    pm.save_current(polluted, allow_resize=True, target_channel_count=14)

    reloaded = pm.load("profile-2")
    assert reloaded["channel_count"] == 14
    assert len(reloaded["channels"]) == 14
    assert reloaded["channels"][5]["label"] == "keep-midi-1"
    assert reloaded["channels"][5]["midi_cc"] == 21
    assert reloaded["channels"][12]["label"] == "keep-midi-8"
    assert reloaded["channels"][13]["index"] == 13
    assert reloaded["channels"][13]["label"] is None
    assert reloaded["channels"][13]["app_names"] == []
    assert reloaded["channels"][13]["midi_cc"] is None


# ── switch ────────────────────────────────────────────────────────────────────

def test_switch_sets_active_id(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1"))
    pm = _make_manager(tmp_profiles_dir)
    pm.switch("profile-1")
    assert pm.active_profile_id == "profile-1"


def test_switch_next_wraps(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", "A"))
    write_profile(tmp_profiles_dir, make_profile("profile-2", "B"))
    pm = _make_manager(tmp_profiles_dir)
    pm._active_profile_id = "profile-2"
    pm.switch_next()
    assert pm.active_profile_id == "profile-1"


def test_switch_prev_wraps(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", "A"))
    write_profile(tmp_profiles_dir, make_profile("profile-2", "B"))
    pm = _make_manager(tmp_profiles_dir)
    pm._active_profile_id = "profile-1"
    pm.switch_prev()
    assert pm.active_profile_id == "profile-2"


# ── ensure_profile_for_hw ─────────────────────────────────────────────────────

def test_ensure_hw_no_new_profile_when_hw_bigger(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", channel_count=5))
    pm = _make_manager(tmp_profiles_dir)
    pm._active_profile_id = "profile-1"
    pm.ensure_profile_for_hw(7)  # hw has MORE channels → no new profile
    assert pm.active_profile_id == "profile-1"
    assert len(pm.list_profiles()) == 1


def test_ensure_hw_creates_profile_when_hw_smaller(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", channel_count=7))
    pm = _make_manager(tmp_profiles_dir)
    pm._active_profile_id = "profile-1"
    pm.ensure_profile_for_hw(5)  # hw has FEWER channels → new profile
    assert pm.active_profile_id == "profile-2"
    assert len(pm.list_profiles()) == 2
    new_p = pm.load("profile-2")
    assert new_p["channel_count"] == 5


# ── Signal emissions ──────────────────────────────────────────────────────────

def test_switch_emits_profile_changed(pm, qtbot):
    pm.create("Second", 7)
    ids = [p["id"] for p in pm.list_profiles()]
    other = next(i for i in ids if i != pm.active_profile_id)
    with qtbot.waitSignal(pm.profile_changed, timeout=1000) as blocker:
        pm.switch(other)
    assert blocker.args == [other]


def test_create_emits_profile_list_changed(pm, qtbot):
    with qtbot.waitSignal(pm.profile_list_changed, timeout=1000):
        pm.create("New Profile", 7)


def test_rename_emits_profile_list_changed(pm, qtbot):
    profile_id = pm.active_profile_id
    with qtbot.waitSignal(pm.profile_list_changed, timeout=1000):
        pm.rename(profile_id, "Renamed")


def test_delete_emits_profile_list_changed(pm, qtbot):
    pm.create("Second", 7)
    ids = [p["id"] for p in pm.list_profiles()]
    other = next(i for i in ids if i != pm.active_profile_id)
    with qtbot.waitSignal(pm.profile_list_changed, timeout=1000):
        pm.delete(other)


def test_ensure_hw_emits_profile_changed(pm, qtbot):
    with qtbot.waitSignal(pm.profile_changed, timeout=1000):
        pm.ensure_profile_for_hw(3)  # active profile has 5 channels, hw has only 3 → new profile
    assert pm.active_profile_id != "profile-1"
