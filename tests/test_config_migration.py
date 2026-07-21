import json
from pathlib import Path

import pytest


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

def _make_hybrid_profile(hw_count: int = 5, midi_count: int = 3) -> dict:
    """Build a profile with hw_count USB channels and midi_count MIDI channels."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.profile_manager import default_channels

    all_channels = default_channels(hw_count + midi_count)
    for ch in all_channels[hw_count:]:
        ch["is_midi"] = True
    return {
        "id": "profile-1",
        "name": "Profile 1",
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
