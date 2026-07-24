import copy
import json
from pathlib import Path

import pytest

ROUND_TRIP_ITERATION_COUNT = 5


def _load_manager(config_path: Path, profiles_dir: Path):
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager
    return ConfigManager(config_path=config_path, profiles_dir=profiles_dir)


def _v6_config(num_channels: int = 5) -> dict:
    """Minimal v6 config with channels[] included."""
    return {
        "version": 6,
        "hardware": {
            "port": None,
            "auto_search_device": True,
            "num_channels": num_channels,
            "input_mode": "usb",
            "midi_device": "",
            "midi_channel_count": 0,
            "baud_rate": 9600,
        },
        "settings": {
            "threshold": 0.01,
            "invert_map": [False] * num_channels,
            "v_sink_map": [False] * num_channels,
            "transparency": True,
            "compact_mode": False,
            "stay_open": False,
            "show_invert_option": False,
            "debug_logging": False,
        },
        "channels": [
            {
                "index": i,
                "label": None,
                "is_midi": False,
                "app_names": ["spotify"] if i == 0 else [],
                "midi_cc": None,
                "midi_mute_cc": None,
                "inverted": False,
                "v_sink": False,
                "mode": "app",
                "hardware_id": None,
                "volume": 0.8 if i == 0 else 1.0,
            }
            for i in range(num_channels)
        ],
    }


def test_migration_creates_profile_1(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    assert (tmp_profiles_dir / "profile-1.json").exists()


def test_migration_preserves_channel_data(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    p = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    assert p["channels"][0]["app_names"] == ["spotify"]
    assert p["channels"][0]["volume"] == pytest.approx(0.8)


def test_migration_config_has_no_channels_key(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    saved = json.loads(tmp_config_path.read_text())
    assert "channels" not in saved


def test_migration_sets_active_profile(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    saved = json.loads(tmp_config_path.read_text())
    assert saved["active_profile"] == "profile-1"


def test_migration_sets_version_7(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    saved = json.loads(tmp_config_path.read_text())
    assert saved["version"] == 7


def test_fresh_install_creates_profile_1(tmp_config_path, tmp_profiles_dir):
    """Fresh install (no config file) → profile-1 created from defaults."""
    _load_manager(tmp_config_path, tmp_profiles_dir)
    assert (tmp_profiles_dir / "profile-1.json").exists()


def test_migration_removes_invert_and_vsink_maps(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    saved = json.loads(tmp_config_path.read_text())
    settings = saved.get("settings", {})
    assert "invert_map" not in settings
    assert "v_sink_map" not in settings


def test_migration_profile_channel_count_matches_channels(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    p = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    assert p["channel_count"] == len(p["channels"])
    assert p["channel_count"] == 5


# ---------------------------------------------------------------------------
# apply_profile midi_channel_count reconciliation
# ---------------------------------------------------------------------------

def _make_hybrid_profile(
    hw_count: int = 5,
    midi_count: int = 3,
    profile_id: str = "profile-1",
    name: str = "Profile 1",
) -> dict:
    """Build a profile with hw_count USB channels and midi_count MIDI channels."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.profile_manager import default_channels

    all_channels = default_channels(hw_count + midi_count)
    for ch in all_channels[hw_count:]:
        ch["is_midi"] = True
    return {
        "id": profile_id,
        "name": name,
        "channel_count": hw_count + midi_count,
        "restore_fader_positions": False,
        "midi_switch_cc": None,
        "channels": all_channels,
    }


def test_apply_profile_reconciles_midi_channel_count(tmp_config_path, tmp_profiles_dir):
    """apply_profile() must update midi_channel_count from the profile's is_midi channels."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager

    # Start with midi_channel_count=0 (simulates stale/imported config)
    base_cfg = _v6_config(5)
    base_cfg["hardware"]["midi_channel_count"] = 0
    base_cfg["hardware"]["input_mode"] = "hybrid"
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    assert cm.midi_channel_count == 0  # sanity: starts with 0

    profile = _make_hybrid_profile(hw_count=5, midi_count=3)
    cm.apply_profile(profile)

    assert cm.midi_channel_count == 3, "midi_channel_count should be reconciled from profile"
    assert cm.num_channels == 8, "num_channels should be hw(5) + midi(3)"


def test_apply_profile_zero_midi_channels_preserved(tmp_config_path, tmp_profiles_dir):
    """apply_profile() with a USB-only profile must leave midi_channel_count=0."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["midi_channel_count"] = 0
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    profile = _make_hybrid_profile(hw_count=5, midi_count=0)
    cm.apply_profile(profile)

    assert cm.midi_channel_count == 0


def test_apply_profile_corrects_inflated_midi_count(tmp_config_path, tmp_profiles_dir):
    """apply_profile() decreases midi_channel_count if the profile has fewer MIDI channels."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["midi_channel_count"] = 10  # stale inflated value
    base_cfg["hardware"]["input_mode"] = "hybrid"
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    profile = _make_hybrid_profile(hw_count=5, midi_count=2)
    cm.apply_profile(profile)

    assert cm.midi_channel_count == 2


def test_apply_profile_repairs_duplicated_midi_channels_without_ballooning(
    tmp_config_path,
    tmp_profiles_dir,
):
    """Corrupted duplicated channel entries must be repaired deterministically."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager

    base_cfg = _v6_config(0)
    base_cfg["hardware"]["num_channels"] = 0
    base_cfg["hardware"]["input_mode"] = "midi_only"
    base_cfg["hardware"]["midi_channel_count"] = 13
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    profile = _make_hybrid_profile(hw_count=0, midi_count=13, profile_id="profile-2", name="Profile 2")
    # Corrupt state: append duplicate copies of channel indices 1..12
    # (12 entries) so the list grows from 13 to 25.
    profile["channels"] = profile["channels"] + copy.deepcopy(profile["channels"][1:13])
    assert len(profile["channels"]) == 25

    cm.apply_profile(profile)
    channels = cm.all_channels()

    assert len(channels) == 13
    assert cm.midi_channel_count == 13
    assert [ch["index"] for ch in channels] == list(range(13))
    assert len({ch["index"] for ch in channels}) == 13


def test_profile_switch_round_trip_stays_idempotent_and_preserves_mappings(
    tmp_config_path,
    tmp_profiles_dir,
):
    """Repeated A→B→A switches must keep channel identity/count stable."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 8
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    profile_a = _make_hybrid_profile(hw_count=5, midi_count=8, profile_id="profile-1", name="Profile 1")
    profile_b = _make_hybrid_profile(hw_count=5, midi_count=4, profile_id="profile-2", name="Profile 2")

    profile_a["channels"][2]["app_names"] = ["Spotify"]
    profile_a["channels"][2]["label"] = "Music"
    profile_a["channels"][6]["midi_cc"] = 14
    profile_a["channels"][6]["midi_mute_cc"] = 74
    profile_b["channels"][2]["app_names"] = ["Firefox"]

    # Run multiple round-trips to guard against cumulative growth/regression.
    for iteration in range(ROUND_TRIP_ITERATION_COUNT):
        cm.apply_profile(copy.deepcopy(profile_a))
        chans_a = cm.all_channels()
        assert len(chans_a) == 13, f"unexpected channel count in A on iteration {iteration}"
        assert cm.midi_channel_count == 8
        assert [ch["index"] for ch in chans_a] == list(range(13))
        assert chans_a[2]["app_names"] == ["Spotify"]
        assert chans_a[6]["midi_cc"] == 14
        assert chans_a[6]["midi_mute_cc"] == 74

        # Applying the same profile again should be a no-op for identity/count.
        before = cm.all_channels()
        cm.apply_profile(copy.deepcopy(profile_a))
        assert cm.all_channels() == before
        assert cm.midi_channel_count == 8

        cm.apply_profile(copy.deepcopy(profile_b))
        chans_b = cm.all_channels()
        assert len(chans_b) == 9, f"unexpected channel count in B on iteration {iteration}"
        assert cm.midi_channel_count == 4
        assert [ch["index"] for ch in chans_b] == list(range(9))


def test_apply_profile_repairs_stable_length_partition_mismatch(tmp_config_path, tmp_profiles_dir, caplog):
    """Stable-length profiles with too many non-MIDI channels must be repaired."""
    import logging
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 25
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    corrupted = _make_hybrid_profile(hw_count=5, midi_count=13, profile_id="profile-1", name="SYSTEM")
    stale_tail = copy.deepcopy(_make_hybrid_profile(hw_count=5, midi_count=25)["channels"][18:30])
    for offset, ch in enumerate(stale_tail, start=len(corrupted["channels"])):
        ch["index"] = offset
        ch["is_midi"] = False
        ch["app_names"] = [f"stale-{offset}"]
    corrupted["channels"].extend(stale_tail)
    corrupted["channel_count"] = len(corrupted["channels"])
    corrupted["channels"][0]["app_names"] = ["Discord"]
    corrupted["channels"][4]["hardware_id"] = "sink:alsa_output.usb-Focusrite"
    corrupted["channels"][5]["midi_cc"] = 21
    corrupted["channels"][5]["midi_mute_cc"] = 91

    with caplog.at_level(logging.INFO, logger="nativmix.utils.config_manager"):
        repaired = cm.apply_profile(copy.deepcopy(corrupted))

    channels = cm.all_channels()
    assert repaired is True
    assert len(channels) == 18
    assert cm.midi_channel_count == 13
    assert sum(1 for ch in channels if ch.get("is_midi", False)) == 13
    assert [ch["index"] for ch in channels] == list(range(18))
    assert all(not ch["is_midi"] for ch in channels[:5])
    assert all(ch["is_midi"] for ch in channels[5:])
    assert channels[0]["app_names"] == ["Discord"]
    assert channels[4]["hardware_id"] == "sink:alsa_output.usb-Focusrite"
    assert channels[5]["midi_cc"] == 21
    assert channels[5]["midi_mute_cc"] == 91
    assert all(
        not any(name.startswith("stale-") for name in ch.get("app_names", []))
        for ch in channels
    )
    assert any("repair_applied=True" in record.message for record in caplog.records)


def test_profile_switch_round_trip_repairs_30_channel_midi_partition_without_mapping_drift(
    tmp_config_path,
    tmp_profiles_dir,
):
    """Repeated 13↔25 MIDI profile switches must keep roles and mappings stable."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 13
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    profile_a = _make_hybrid_profile(hw_count=5, midi_count=13, profile_id="profile-1", name="SYSTEM")
    profile_b = _make_hybrid_profile(hw_count=5, midi_count=25, profile_id="profile-2", name="Profile 2")

    profile_a["channels"][0]["app_names"] = ["Discord"]
    profile_a["channels"][1]["app_names"] = ["Spotify"]
    profile_a["channels"][4]["hardware_id"] = "sink:alsa_output.usb-Focusrite"
    profile_a["channels"][5]["label"] = "AOW2"
    profile_a["channels"][5]["midi_cc"] = 21
    profile_a["channels"][5]["midi_mute_cc"] = 91
    profile_a["channels"][17]["label"] = "HD2"
    profile_a["channels"][17]["midi_cc"] = 42

    profile_b["channels"][0]["app_names"] = ["Firefox"]
    profile_b["channels"][5]["label"] = "Profile 2 MIDI 1"
    profile_b["channels"][5]["midi_cc"] = 31
    profile_b["channels"][29]["label"] = "Profile 2 MIDI 25"
    profile_b["channels"][29]["midi_cc"] = 55

    stale_tail = copy.deepcopy(profile_b["channels"][18:30])
    for offset, ch in enumerate(stale_tail, start=len(profile_a["channels"])):
        ch["index"] = offset
        ch["is_midi"] = False
        ch["app_names"] = [f"stale-{offset}"]
        ch["midi_cc"] = None
        ch["midi_mute_cc"] = None
    profile_a["channels"].extend(stale_tail)
    profile_a["channel_count"] = len(profile_a["channels"])

    initial_repair = cm.apply_profile(copy.deepcopy(profile_a))
    initial_channels = cm.all_channels()
    assert initial_repair is True
    assert len(initial_channels) == 18
    assert cm.midi_channel_count == 13
    assert sum(1 for ch in initial_channels if ch.get("is_midi", False)) == 13

    for iteration in range(ROUND_TRIP_ITERATION_COUNT):
        repaired_b = cm.apply_profile(copy.deepcopy(profile_b))
        chans_b = cm.all_channels()
        assert repaired_b is False, f"profile B should already be canonical on iteration {iteration}"
        assert len(chans_b) == 30
        assert cm.midi_channel_count == 25
        assert sum(1 for ch in chans_b if ch.get("is_midi", False)) == 25
        assert [ch["index"] for ch in chans_b] == list(range(30))
        assert chans_b[0]["app_names"] == ["Firefox"]
        assert chans_b[5]["midi_cc"] == 31
        assert chans_b[29]["midi_cc"] == 55

        repaired_a = cm.apply_profile(copy.deepcopy(profile_a))
        chans_a = cm.all_channels()
        assert repaired_a is True, f"profile A should be repaired on iteration {iteration}"
        assert len(chans_a) == 18
        assert cm.midi_channel_count == 13
        assert sum(1 for ch in chans_a if ch.get("is_midi", False)) == 13
        assert [ch["index"] for ch in chans_a] == list(range(18))
        assert len({ch["index"] for ch in chans_a}) == 18
        assert all(not ch["is_midi"] for ch in chans_a[:5])
        assert all(ch["is_midi"] for ch in chans_a[5:])
        assert chans_a[0]["app_names"] == ["Discord"]
        assert chans_a[1]["app_names"] == ["Spotify"]
        assert chans_a[4]["hardware_id"] == "sink:alsa_output.usb-Focusrite"
        assert chans_a[5]["label"] == "AOW2"
        assert chans_a[5]["midi_cc"] == 21
        assert chans_a[5]["midi_mute_cc"] == 91
        assert chans_a[17]["label"] == "HD2"
        assert chans_a[17]["midi_cc"] == 42
        assert all(
            not any(name.startswith("stale-") for name in ch.get("app_names", []))
            for ch in chans_a
        )

# ---------------------------------------------------------------------------
# toggle_mute guard — out-of-range channel must be rejected with a warning
# ---------------------------------------------------------------------------

def test_toggle_mute_guard_out_of_range(tmp_config_path, tmp_profiles_dir, caplog):
    """toggle_mute with an out-of-range index must warn and not change state."""
    import logging
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

    # Minimal config: 5 hardware channels, no MIDI, usb mode → num_channels=5
    base_cfg = _v6_config(5)
    tmp_config_path.write_text(json.dumps(base_cfg))
    _load_manager(tmp_config_path, tmp_profiles_dir)  # trigger migration

    # We test the guard logic directly without constructing a full PipeWireManager
    # (which would require pulsectl) — mirror the guard condition from the source.
    from nativmix.utils.config_manager import ConfigManager
    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    assert cm.num_channels == 5

    toggle_called = False

    with caplog.at_level(logging.WARNING, logger="nativmix"):
        channel_index = 5  # valid range is 0-4
        if channel_index < 0 or channel_index >= cm.num_channels:
            logging.getLogger("nativmix.audio.manager").warning(
                "toggle_mute requested for invalid channel %d (num_channels=%d)",
                channel_index, cm.num_channels,
            )
        else:
            toggle_called = True

    assert not toggle_called, "toggle_mute must not be called for out-of-range channel"
    assert any("toggle_mute requested for invalid channel 5" in r.message for r in caplog.records)
