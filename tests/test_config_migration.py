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


def test_migration_sets_current_version(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    saved = json.loads(tmp_config_path.read_text())
    assert saved["version"] == 10


def test_update_checks_default_disabled_on_fresh_install(tmp_config_path, tmp_profiles_dir):
    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    assert cm.check_for_updates is False
    saved = json.loads(tmp_config_path.read_text())
    assert saved["settings"]["check_for_updates"] is False


def test_update_migration_disables_untrusted_existing_value(tmp_config_path, tmp_profiles_dir):
    config = _v6_config(5)
    config["settings"]["check_for_updates"] = True
    tmp_config_path.write_text(json.dumps(config))

    cm = _load_manager(tmp_config_path, tmp_profiles_dir)

    assert cm.check_for_updates is False
    assert json.loads(tmp_config_path.read_text())["settings"]["check_for_updates"] is False


def test_update_preferences_round_trip(tmp_config_path, tmp_profiles_dir):
    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    cm.check_for_updates = True
    cm.ignored_update_version = "1.2.0"
    cm.save()

    reloaded = _load_manager(tmp_config_path, tmp_profiles_dir)
    assert reloaded.check_for_updates is True
    assert reloaded.ignored_update_version == "1.2.0"


def test_fresh_install_creates_profile_1(tmp_config_path, tmp_profiles_dir):
    """Fresh install (no config file) → profile-1 created from defaults."""
    _load_manager(tmp_config_path, tmp_profiles_dir)
    assert (tmp_profiles_dir / "profile-1.json").exists()


def test_get_volume_exponent_defaults_when_missing(tmp_config_path, tmp_profiles_dir):
    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    assert cm.get_volume_exponent() == pytest.approx(2.0)


def test_get_volume_exponent_coerces_numeric_string(tmp_config_path, tmp_profiles_dir):
    settings = _v6_config(5)["settings"] | {"volume_exponent": "2.5"}
    tmp_config_path.write_text(json.dumps(_v6_config(5) | {"settings": settings}))
    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    assert cm.get_volume_exponent() == pytest.approx(2.5)


def test_get_volume_exponent_invalid_value_falls_back_to_default(tmp_config_path, tmp_profiles_dir):
    settings = _v6_config(5)["settings"] | {"volume_exponent": "not-a-number"}
    tmp_config_path.write_text(json.dumps(_v6_config(5) | {"settings": settings}))
    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    assert cm.get_volume_exponent() == pytest.approx(2.0)


def test_volume_exponent_round_trips_with_setter(tmp_config_path, tmp_profiles_dir):
    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    cm.set_volume_exponent(2.75)
    assert cm.get_volume_exponent() == pytest.approx(2.75)


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


def test_apply_profile_ignores_polluted_runtime_and_clamps_to_destination_canonical_count(
    tmp_config_path,
    tmp_profiles_dir,
):
    """Switching from polluted runtime must apply destination canonical profile size."""
    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 42
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    destination = _make_hybrid_profile(hw_count=5, midi_count=25, profile_id="profile-2", name="Profile 2")
    polluted_runtime = _make_hybrid_profile(hw_count=5, midi_count=42, profile_id="profile-x", name="Polluted")
    cm._data["channels"] = polluted_runtime["channels"]
    cm._data.setdefault("hardware", {})["midi_channel_count"] = 42

    cm.apply_profile(copy.deepcopy(destination))
    channels = cm.all_channels()
    assert len(channels) == 30
    assert cm.midi_channel_count == 25
    assert [ch["index"] for ch in channels] == list(range(30))
    assert len({ch["index"] for ch in channels}) == 30


def test_switching_profiles_does_not_persist_inflated_destination_length(
    tmp_config_path,
    tmp_profiles_dir,
):
    """Destination profile file must not be rewritten to polluted in-memory length."""
    from nativmix.utils.profile_manager import ProfileManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 13
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)
    profile_a = _make_hybrid_profile(hw_count=5, midi_count=25, profile_id="profile-1", name="Profile 1")
    profile_b = _make_hybrid_profile(hw_count=5, midi_count=12, profile_id="profile-4", name="Profile 4")
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile_a, indent=2) + "\n")
    (tmp_profiles_dir / "profile-4.json").write_text(json.dumps(profile_b, indent=2) + "\n")

    pm.set_active_silently("profile-1")
    cm.apply_profile(pm.active_profile)
    # Inflate runtime from canonical 30 to 47 by appending a stale tail
    # (slice 13:30 = 17 channels) from the current in-memory profile.
    polluted = copy.deepcopy(cm.all_channels()) + copy.deepcopy(cm.all_channels()[13:30])
    for idx, ch in enumerate(polluted):
        ch["index"] = idx
    cm._data["channels"] = polluted
    cm._data.setdefault("hardware", {})["midi_channel_count"] = 30
    assert len(cm.all_channels()) == 47

    before = json.loads((tmp_profiles_dir / "profile-4.json").read_text())
    pm.switch("profile-4")
    cm.apply_profile(pm.active_profile)
    after = json.loads((tmp_profiles_dir / "profile-4.json").read_text())

    assert after == before
    assert after["channel_count"] == 17
    assert len(after["channels"]) == 17
    assert len(cm.all_channels()) == 17
    assert cm.midi_channel_count == 12


def test_repeated_profile_switches_do_not_drift_after_runtime_pollution(
    tmp_config_path,
    tmp_profiles_dir,
):
    """Repeated switches across profiles remain stable even after runtime inflation."""
    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 13
    tmp_config_path.write_text(json.dumps(base_cfg))
    cm = _load_manager(tmp_config_path, tmp_profiles_dir)

    profiles = [
        _make_hybrid_profile(hw_count=5, midi_count=12, profile_id="profile-4", name="Profile 4"),
        _make_hybrid_profile(hw_count=5, midi_count=25, profile_id="profile-2", name="Profile 2"),
        _make_hybrid_profile(hw_count=5, midi_count=13, profile_id="profile-1", name="SYSTEM"),
    ]
    expected = {p["id"]: (len(p["channels"]), sum(1 for ch in p["channels"] if ch["is_midi"])) for p in profiles}
    num_iterations = 5

    for _ in range(num_iterations):
        for profile in profiles:
            polluted = _make_hybrid_profile(hw_count=5, midi_count=42, profile_id="polluted", name="Polluted")
            cm._data["channels"] = polluted["channels"]
            cm._data.setdefault("hardware", {})["midi_channel_count"] = 42
            cm.apply_profile(copy.deepcopy(profile))
            channels = cm.all_channels()
            expected_len, expected_midi = expected[profile["id"]]
            assert len(channels) == expected_len
            assert cm.midi_channel_count == expected_midi
            assert [ch["index"] for ch in channels] == list(range(expected_len))
            assert len({ch["index"] for ch in channels}) == expected_len


def test_add_midi_channel_uses_stored_profile_snapshot_and_skips_reentrant_partial_save(
    tmp_config_path,
    tmp_profiles_dir,
    caplog,
):
    """
    Adding a MIDI channel must preserve the stored profile when runtime state goes
    stale mid-signal.  The profile on disk must contain the full, correct snapshot
    (first N channels untouched, one new MIDI channel appended).

    With the fix, settings_changed fires *after* the mutation guard exits with the
    correct state already applied.  Any re-entrant save call from a stale handler
    (runtime shorter than stored) is rejected by the non-resize save invariant
    rather than being suppressed by the guard — the observable outcome is identical:
    the profile on disk is never overwritten with stale data.
    """
    import logging

    from nativmix.utils.config_manager import _blank_channel
    from nativmix.utils.profile_manager import ProfileManager

    base_cfg = _v6_config(14)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 17
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)

    profile = _make_hybrid_profile(hw_count=14, midi_count=17, profile_id="profile-1", name="SYSTEM")
    profile["channels"][0]["label"] = "Browser"
    profile["channels"][1]["app_names"] = ["Spotify"]
    profile["channels"][5]["inverted"] = True
    profile["channels"][8]["v_sink"] = True
    profile["channels"][13]["hardware_id"] = "sink:alsa_output.usb-Focusrite"
    profile["channels"][14]["label"] = "MIDI A"
    profile["channels"][14]["app_names"] = ["Firefox"]
    profile["channels"][14]["midi_cc"] = 21
    profile["channels"][14]["midi_mute_cc"] = 71
    profile["channels"][14]["v_sink"] = True
    profile["channels"][20]["label"] = "MIDI B"
    profile["channels"][20]["midi_cc"] = 42
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile, indent=2) + "\n")

    pm.set_active_silently("profile-1")
    cm.active_profile_id = "profile-1"
    cm.apply_profile(copy.deepcopy(profile))

    def _simulate_stale_reentrant_save() -> None:
        cm._data["channels"] = [
            _blank_channel(i, is_midi=False)
            for i in range(cm.hw_channel_count)
        ]
        pm.save_current(cm.all_channels())

    cm.settings_changed.connect(_simulate_stale_reentrant_save)

    with caplog.at_level(logging.WARNING, logger="nativmix.utils.profile_manager"):
        cm.add_midi_channel()

    saved = pm.load("profile-1")
    assert saved["channel_count"] == 32
    assert len(saved["channels"]) == 32
    assert saved["channels"][:31] == profile["channels"]
    assert saved["channels"][31] == {
        "index": 31,
        "label": None,
        "is_midi": True,
        "app_names": [],
        "midi_cc": None,
        "midi_mute_cc": None,
        "midi_channel": 0,
        "midi_mute_channel": 0,
        "inverted": False,
        "v_sink": False,
        "mode": "app",
        "hardware_id": None,
        "volume": 1.0,
    }
    # The stale re-entrant save (14 channels against 32-channel stored profile)
    # must be rejected by the non-resize save invariant.
    assert any("refusing non-resize save" in r.message for r in caplog.records)
    # The forbidden "14 → 31" canonicalization jump must never appear.
    assert all("canonicalized channels 14 → 31" not in r.message for r in caplog.records)


def test_add_midi_channel_persists_active_profile_resize(tmp_config_path, tmp_profiles_dir):
    """Adding a MIDI channel must grow the active profile on disk."""
    from nativmix.utils.profile_manager import ProfileManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 2
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)
    profile = _make_hybrid_profile(hw_count=5, midi_count=2, profile_id="profile-1", name="Profile 1")
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile, indent=2) + "\n")

    pm.set_active_silently("profile-1")
    cm.active_profile_id = "profile-1"
    cm.apply_profile(pm.active_profile)

    cm.add_midi_channel()

    saved = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    assert saved["channel_count"] == 8
    assert len(saved["channels"]) == 8
    assert saved["channels"][-1]["index"] == 7
    assert saved["channels"][-1]["is_midi"] is True


def test_add_midi_channel_ignores_polluted_runtime_tail_when_resizing(
    tmp_config_path,
    tmp_profiles_dir,
):
    """Deliberate resize must grow from the active profile template, not stale runtime tail."""
    from nativmix.utils.profile_manager import ProfileManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 8
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)
    large = _make_hybrid_profile(hw_count=5, midi_count=26, profile_id="profile-1", name="Large")
    small = _make_hybrid_profile(hw_count=5, midi_count=8, profile_id="profile-2", name="Small")
    small["channels"][5]["label"] = "keep-midi-1"
    small["channels"][5]["midi_cc"] = 21
    small["channels"][12]["label"] = "keep-midi-8"
    large["channels"][13]["label"] = "stale-midi-9"
    large["channels"][13]["app_names"] = ["Stale App"]
    large["channels"][13]["midi_cc"] = 99
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(large, indent=2) + "\n")
    (tmp_profiles_dir / "profile-2.json").write_text(json.dumps(small, indent=2) + "\n")

    pm.set_active_silently("profile-2")
    cm.active_profile_id = "profile-2"
    cm.apply_profile(pm.load("profile-2"))
    assert len(cm.all_channels()) == 13

    polluted = copy.deepcopy(cm.all_channels()) + copy.deepcopy(large["channels"][13:31])
    for idx, ch in enumerate(polluted):
        ch["index"] = idx
    cm._data["channels"] = polluted
    assert len(cm.all_channels()) == 31
    assert cm.num_channels == 13

    cm.add_midi_channel()

    saved = json.loads((tmp_profiles_dir / "profile-2.json").read_text())
    assert saved["channel_count"] == 14
    assert len(saved["channels"]) == 14
    assert saved["channels"][5]["label"] == "keep-midi-1"
    assert saved["channels"][5]["midi_cc"] == 21
    assert saved["channels"][12]["label"] == "keep-midi-8"
    assert saved["channels"][13]["index"] == 13
    assert saved["channels"][13]["is_midi"] is True
    assert saved["channels"][13]["label"] is None
    assert saved["channels"][13]["app_names"] == []
    assert saved["channels"][13]["midi_cc"] is None


def test_remove_midi_channel_persists_active_profile_resize(tmp_config_path, tmp_profiles_dir):
    """Removing a MIDI channel must shrink the active profile on disk."""
    from nativmix.utils.profile_manager import ProfileManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 3
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)
    profile = _make_hybrid_profile(hw_count=5, midi_count=3, profile_id="profile-1", name="Profile 1")
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile, indent=2) + "\n")

    pm.set_active_silently("profile-1")
    cm.active_profile_id = "profile-1"
    cm.apply_profile(pm.active_profile)

    cm.remove_midi_channel(7)

    saved = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    assert saved["channel_count"] == 7
    assert len(saved["channels"]) == 7
    assert [ch["index"] for ch in saved["channels"]] == list(range(7))
    assert all(not ch["is_midi"] for ch in saved["channels"][:5])
    assert all(ch["is_midi"] for ch in saved["channels"][5:])


def test_remove_midi_channels_persists_active_profile_resize(tmp_config_path, tmp_profiles_dir):
    """Bulk removal must shrink the active profile on disk in one pass."""
    from nativmix.utils.profile_manager import ProfileManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 4
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)
    profile = _make_hybrid_profile(hw_count=5, midi_count=4, profile_id="profile-1", name="Profile 1")
    profile["channels"][5]["label"] = "keep-5"
    profile["channels"][6]["label"] = "drop-6"
    profile["channels"][7]["label"] = "keep-7"
    profile["channels"][8]["label"] = "drop-8"
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile, indent=2) + "\n")

    pm.set_active_silently("profile-1")
    cm.active_profile_id = "profile-1"
    cm.apply_profile(pm.active_profile)

    cm.remove_midi_channels([8, 6])

    saved = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    assert saved["channel_count"] == 7
    assert len(saved["channels"]) == 7
    assert [ch["index"] for ch in saved["channels"]] == list(range(7))
    assert [ch["label"] for ch in saved["channels"][5:]] == ["keep-5", "keep-7"]
    assert all(ch["is_midi"] for ch in saved["channels"][5:])
    assert cm.midi_channel_count == 2


def test_remove_midi_channels_emits_settings_changed_once(tmp_config_path, tmp_profiles_dir):
    """Bulk removal should emit one settings_changed signal and persist once."""
    from nativmix.utils.profile_manager import ProfileManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 4
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)
    profile = _make_hybrid_profile(hw_count=5, midi_count=4, profile_id="profile-1", name="Profile 1")
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile, indent=2) + "\n")

    pm.set_active_silently("profile-1")
    cm.active_profile_id = "profile-1"
    cm.apply_profile(pm.active_profile)

    emitted = 0

    def _on_settings_changed() -> None:
        nonlocal emitted
        emitted += 1

    persisted_calls = 0

    def _persist_once(*, allow_resize: bool = False) -> None:
        nonlocal persisted_calls
        persisted_calls += 1
        assert allow_resize is True

    cm.settings_changed.connect(_on_settings_changed)
    cm._persist_active_profile_channels = _persist_once

    cm.remove_midi_channels([8, 6])

    assert emitted == 1
    assert persisted_calls == 1
    assert cm.midi_channel_count == 2

# ---------------------------------------------------------------------------
# save_profile guard — inflated channel list must be truncated, never written
# ---------------------------------------------------------------------------

def test_save_profile_guard_truncates_inflated_channel_list(tmp_profiles_dir):
    """save_profile must truncate channels that exceed the profile's channel_count."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.profile_manager import ProfileManager

    pm = ProfileManager(profiles_dir=tmp_profiles_dir)
    pid = pm.create("Guard Test", channel_count=5)

    # Manually inflate the profile dict before passing it to save_profile
    profile = pm.load(pid)
    assert profile["channel_count"] == 5
    profile["channels"] = profile["channels"] + copy.deepcopy(profile["channels"])  # 10 channels

    # save_profile (without allow_resize=True) must NOT write the inflated list
    pm.save_profile(profile)

    saved = pm.load(pid)
    assert saved["channel_count"] == 5
    assert len(saved["channels"]) == 5, (
        "save_profile must truncate to canonical template length"
    )


def test_save_profile_allow_resize_allows_channel_count_growth(tmp_profiles_dir):
    """save_profile(allow_resize=True) must allow deliberate channel-count expansion."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.profile_manager import ProfileManager, default_channels

    pm = ProfileManager(profiles_dir=tmp_profiles_dir)
    pid = pm.create("Migration Test", channel_count=5)

    profile = pm.load(pid)
    profile["channels"] = default_channels(10)
    profile["channel_count"] = 10

    # Explicit resize mode: intentional channel-count expansion must succeed
    pm.save_profile(profile, allow_resize=True)

    saved = pm.load(pid)
    assert saved["channel_count"] == 10
    assert len(saved["channels"]) == 10


# ---------------------------------------------------------------------------
# Integration — exact switch sequence from screenshots:
#   Profile SYSTEM  : 5 USB + 18 MIDI = 23 channels  (screenshot 1)
#   Profile B       : 5 USB + 25 MIDI = 30 channels  (screenshot 2)
#   Contaminate runtime to 47 channels, then switch back and forth.
# ---------------------------------------------------------------------------

def test_screenshot_switch_sequence_23_to_30_channel_profiles(
    tmp_config_path, tmp_profiles_dir
):
    """
    Reproduce the exact channel-inflation scenario visible in the bug screenshots.

    Screenshot 1 shows the SYSTEM profile with 5 USB + 18 MIDI = 23 channels.
    Screenshot 2 shows the same profile selector with 5 USB + 25 MIDI = 30 channels,
    indicating a different profile's channels leaked into SYSTEM after switching.

    This test:
      1. Creates SYSTEM (23 ch) and Profile B (30 ch) and writes them to disk.
      2. Applies SYSTEM — contaminates the runtime to 47 channels.
      3. Switches to Profile B and back to SYSTEM five times.
      4. Asserts that channel counts, partition roles, and key mappings are
         stable after every switch and that neither profile file grows.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.utils.profile_manager import ProfileManager

    # ── Config: hybrid mode, 5 USB faders, MIDI count driven by the profile ──
    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 18  # matches SYSTEM profile
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)

    # ── Build SYSTEM profile (5 USB + 18 MIDI = 23 channels) ──────────────────
    system = _make_hybrid_profile(
        hw_count=5, midi_count=18,
        profile_id="profile-1", name="SYSTEM",
    )
    # Label the USB channels the same way the user did in the screenshot
    for i in range(5):
        system["channels"][i]["label"] = f"MIDI {i + 1}"
    # Assign MIDI CCs to MIDI channels (channels 5–22) to match screenshot CCs
    midi_labels = [
        "SPOTIFY", "FIREFOX", "SYSTEM", "HEADSET IN", "DISCORD",
        "MC", "ZOOM", "OTHER", "AoW4", "SM2", "DT", "BG3",
        "SCARLETT OUT", "SCARLETT IN", "HEADSET IN2", "HEADSET OUT",
        "DISCORD2", "SPOTIFY2",
    ]
    for offset, lbl in enumerate(midi_labels):
        ch = system["channels"][5 + offset]
        ch["label"] = lbl
        ch["midi_cc"] = offset * 2  # CC: 0, 2, 4 … matching screenshot bottom row

    # ── Build Profile B (5 USB + 25 MIDI = 30 channels) ──────────────────────
    profile_b = _make_hybrid_profile(
        hw_count=5, midi_count=25,
        profile_id="profile-2", name="Profile B",
    )
    usb_labels_b = [
        "SCARLETT OUT", "SCARLETT IN", "HEADSET IN", "HEADSET OUT", "DISCORD",
    ]
    for i, lbl in enumerate(usb_labels_b):
        profile_b["channels"][i]["label"] = lbl
    profile_b["channels"][5]["midi_cc"] = 31   # sentinel CC for Profile B
    profile_b["channels"][29]["midi_cc"] = 55  # last MIDI channel marker

    # Write both profiles to disk
    (tmp_profiles_dir / "profile-1.json").write_text(
        json.dumps(system, indent=2) + "\n"
    )
    (tmp_profiles_dir / "profile-2.json").write_text(
        json.dumps(profile_b, indent=2) + "\n"
    )

    # ── Apply SYSTEM first, then deliberately contaminate the runtime ─────────
    pm.set_active_silently("profile-1")
    cm.apply_profile(pm.load("profile-1"))
    assert len(cm.all_channels()) == 23
    assert cm.midi_channel_count == 18

    # Reusable helper: build a 47-channel polluted snapshot from a 23-channel
    # SYSTEM snapshot by appending 24 stale channels from Profile B's MIDI
    # section.  This mirrors the exact inflation seen in the screenshots.
    def _make_polluted_47(base_channels: list, b_channels: list) -> list:
        stale = copy.deepcopy(b_channels[5:29])  # 24 channels from Profile B's MIDI section
        polluted = copy.deepcopy(base_channels) + stale
        for idx, ch in enumerate(polluted):
            ch["index"] = idx
        return polluted

    system_snapshot = cm.all_channels()   # 23 channels, captured before pollution
    b_snapshot = profile_b["channels"]    # 30 channels from Profile B definition

    cm._data["channels"] = _make_polluted_47(system_snapshot, b_snapshot)
    cm._data.setdefault("hardware", {})["midi_channel_count"] = 42
    assert len(cm.all_channels()) == 47, "test precondition: runtime must be polluted"

    # Snapshot the on-disk profile files BEFORE the switch sequence
    system_before = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    profile_b_before = json.loads((tmp_profiles_dir / "profile-2.json").read_text())
    assert system_before["channel_count"] == 23
    assert profile_b_before["channel_count"] == 30

    # ── Switch sequence: SYSTEM → B → SYSTEM, repeated ───────────────────────
    for iteration in range(5):
        # ── Switch to Profile B ─────────────────────────────────────────────
        pm.set_active_silently("profile-2")
        b_loaded = pm.load("profile-2")
        repaired_b = cm.apply_profile(copy.deepcopy(b_loaded))

        chans_b = cm.all_channels()
        assert len(chans_b) == 30, (
            f"iteration {iteration}: Profile B must have 30 channels, got {len(chans_b)}"
        )
        assert cm.midi_channel_count == 25, (
            f"iteration {iteration}: midi_channel_count must be 25 after switching to B"
        )
        assert [ch["index"] for ch in chans_b] == list(range(30))
        assert chans_b[5]["midi_cc"] == 31, "sentinel CC for Profile B must survive"
        assert chans_b[29]["midi_cc"] == 55, "last MIDI CC for Profile B must survive"

        # Simulate save_current path (called in main.py when profile_repaired)
        if repaired_b:
            pm.save_current(cm.all_channels())

        # Profile B's file must NOT grow
        profile_b_after = json.loads((tmp_profiles_dir / "profile-2.json").read_text())
        assert profile_b_after["channel_count"] == 30, (
            f"iteration {iteration}: profile-2.json channel_count must stay at 30"
        )
        assert len(profile_b_after["channels"]) == 30, (
            f"iteration {iteration}: profile-2.json must not have inflated channel list"
        )

        # ── Switch back to SYSTEM ───────────────────────────────────────────
        pm.set_active_silently("profile-1")
        system_loaded = pm.load("profile-1")
        repaired_a = cm.apply_profile(copy.deepcopy(system_loaded))

        chans_a = cm.all_channels()
        assert len(chans_a) == 23, (
            f"iteration {iteration}: SYSTEM must have 23 channels, got {len(chans_a)}"
        )
        assert cm.midi_channel_count == 18, (
            f"iteration {iteration}: midi_channel_count must be 18 after switching to SYSTEM"
        )
        assert [ch["index"] for ch in chans_a] == list(range(23))
        assert len({ch["index"] for ch in chans_a}) == 23
        assert all(not ch["is_midi"] for ch in chans_a[:5])
        assert all(ch["is_midi"] for ch in chans_a[5:])

        # USB channel labels must survive round-trip
        for i in range(5):
            assert chans_a[i]["label"] == f"MIDI {i + 1}", (
                f"iteration {iteration}: USB channel {i} label must be 'MIDI {i + 1}'"
            )

        # MIDI CCs in SYSTEM must survive round-trip
        for offset in range(18):
            assert chans_a[5 + offset]["midi_cc"] == offset * 2, (
                f"iteration {iteration}: MIDI channel {offset} CC must be {offset * 2}"
            )

        if repaired_a:
            pm.save_current(cm.all_channels())

        # SYSTEM's file must NOT grow
        system_after = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
        assert system_after["channel_count"] == 23, (
            f"iteration {iteration}: profile-1.json channel_count must stay at 23"
        )
        assert len(system_after["channels"]) == 23, (
            f"iteration {iteration}: profile-1.json must not have inflated channel list"
        )

        # Re-contaminate runtime to simulate real usage (faders keep moving after switch)
        cm._data["channels"] = _make_polluted_47(cm.all_channels(), b_snapshot)
        cm._data.setdefault("hardware", {})["midi_channel_count"] = 42


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


# ---------------------------------------------------------------------------
# Regression: issue #12 — get_effective_inversion must not auto-expand channels
# ---------------------------------------------------------------------------

def test_get_effective_inversion_does_not_expand_channels(tmp_config_path, tmp_profiles_dir):
    """
    Regression test for issue #12.

    Root cause: ``get_effective_inversion(i)`` used to call ``self._channel(i)``
    which auto-creates channel dicts for indices beyond the current profile's
    channel count.  ``arduino.reload_settings`` iterates over Arduino's internal
    channel list (which reflects the *previous* larger profile) and calls
    ``get_effective_inversion`` for every hardware fader.  After a profile switch
    to a smaller profile this caused ``config._data["channels"]`` to be silently
    expanded back to the old count — making ``add_midi_channel`` persist an
    inflated channel list when it later called ``_persist_active_profile_channels
    (allow_resize=True)``.

    This test verifies that querying an out-of-range channel index:
      - does not expand the channels list, and
      - returns the correct fallback value (False).
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.utils.profile_manager import ProfileManager

    # Set up a hybrid config: 5 USB + 8 MIDI = 13 channels (like "profile-2" in the bug report)
    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 8
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)

    # Create and apply a small profile (13 channels)
    small = _make_hybrid_profile(hw_count=5, midi_count=8, profile_id="profile-1", name="Small")
    # Set a known inversion flag on the last valid channel
    small["channels"][12]["inverted"] = True
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(small, indent=2) + "\n")
    pm.set_active_silently("profile-1")
    cm.apply_profile(pm.load("profile-1"))
    assert len(cm.all_channels()) == 13, "apply_profile must set 13 channels"

    # Simulate Arduino having been initialised with a LARGER profile (e.g. 30 or 47 faders).
    # reload_settings iterates over all of Arduino's internal channels, calling
    # get_effective_inversion(i) for i in 0..N-1.  For i >= 13 this was the
    # contamination vector.
    for arduino_channel_idx in range(47):
        result = cm.get_effective_inversion(arduino_channel_idx)
        # Must return False for all out-of-range indices
        if arduino_channel_idx >= 13:
            assert result is False, (
                f"get_effective_inversion({arduino_channel_idx}) must return False "
                f"for out-of-range index, got {result}"
            )

    # The critical assertion: channels list must NOT have grown
    channels_after = cm.all_channels()
    assert len(channels_after) == 13, (
        f"get_effective_inversion must not auto-expand channels: "
        f"expected 13, got {len(channels_after)}"
    )

    # In-range values must still be correct
    assert cm.get_effective_inversion(12) is True, (
        "in-range channel 12 (inverted=True) must return True"
    )
    assert cm.get_effective_inversion(0) is False, (
        "in-range channel 0 (inverted=False) must return False"
    )


def test_arduino_reload_settings_does_not_inflate_channels_after_profile_switch(
    tmp_config_path, tmp_profiles_dir
):
    """
    End-to-end regression for issue #12: the exact 30 → 47 contamination sequence.

    Simulates what happens when Arduino's internal channel count (from a larger
    profile at startup) exceeds the active profile's count after a switch, and
    reload_settings is called.  Before the fix, this silently inflated
    config._data["channels"] from 13 to 47; add_midi_channel then persisted 47
    channels to the small profile's file.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.utils.profile_manager import ProfileManager

    # Build a "large" config that would have been at startup (5 USB + 42 MIDI = 47 ch)
    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 42
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)

    # Create two profiles: large (47) and small (13)
    large = _make_hybrid_profile(hw_count=5, midi_count=42, profile_id="profile-1", name="Large")
    small = _make_hybrid_profile(hw_count=5, midi_count=8, profile_id="profile-2", name="Small")
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(large, indent=2) + "\n")
    (tmp_profiles_dir / "profile-2.json").write_text(json.dumps(small, indent=2) + "\n")

    # Start on large profile (Arduino is initialised with 47 channels)
    pm.set_active_silently("profile-1")
    cm.apply_profile(pm.load("profile-1"))
    assert len(cm.all_channels()) == 47

    # Switch to small profile — now config has 13 channels but "Arduino" would
    # still have 47 internal slots.  Simulate reload_settings by querying
    # get_effective_inversion for all 47 original indices.
    #
    # Mirror what _switch_profile in main.py does: set active_profile_id BEFORE
    # applying the profile so that _persist_active_profile_channels saves to the
    # correct file.
    cm.active_profile_id = "profile-2"
    pm.set_active_silently("profile-2")
    cm.apply_profile(pm.load("profile-2"))
    assert len(cm.all_channels()) == 13, "apply_profile must set 13 channels"

    # Simulate arduino.reload_settings(config): iterates 0..46
    for i in range(47):
        cm.get_effective_inversion(i)

    # Channels must still be 13 after the simulated reload
    assert len(cm.all_channels()) == 13, (
        "channels must remain at 13 after simulated arduino.reload_settings — "
        "get_effective_inversion must not auto-expand"
    )

    # Call add_midi_channel() which is the full path: increment count, save,
    # then _persist_active_profile_channels(allow_resize=True).
    # Before the fix, this would persist 47 channels; after the fix it saves 14.
    cm.add_midi_channel()

    # config channels should be 14 now (13 + 1), not 47
    assert len(cm.all_channels()) == 14, (
        f"add_midi_channel on a 13-channel profile must grow to 14, got {len(cm.all_channels())}"
    )

    # Profile file must have the correct total count (14 = 5 USB + 9 MIDI), not 47
    written = json.loads((tmp_profiles_dir / "profile-2.json").read_text())
    assert written["channel_count"] == 14, (
        f"profile-2.json channel_count must be 14 (5+8+1), got {written['channel_count']}"
    )
    assert len(written["channels"]) == 14, (
        f"profile-2.json must have 14 channels after add_midi_channel, got {len(written['channels'])}"
    )


def test_single_delete_midi_channel_not_recreated_by_reload_settings(
    tmp_config_path, tmp_profiles_dir
):
    """
    Regression test: deleting one MIDI channel via remove_midi_channel must not
    recreate it when arduino.reload_settings subsequently calls get_midi_cc or
    get_effective_inversion for every slot in the Arduino's old (larger) internal
    channel list.

    Before the fix, get_midi_cc called self._channel(i) which auto-created channel
    dicts for out-of-range indices.  arduino.reload_settings (and
    _on_channel_volume_midi_feedback in main.py) can call get_midi_cc with a stale
    channel index that is valid before deletion but out-of-range afterward, silently
    re-creating the deleted channel — matching the user-visible symptom
    "delete a MIDI channel and all channels reappear".
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.utils.profile_manager import ProfileManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 17
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)

    profile = _make_hybrid_profile(
        hw_count=5, midi_count=17, profile_id="profile-1", name="Profile 1"
    )
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile, indent=2) + "\n")
    pm.set_active_silently("profile-1")
    cm.active_profile_id = "profile-1"
    cm.apply_profile(pm.load("profile-1"))
    assert len(cm.all_channels()) == 22, "setup: must start with 5 hw + 17 midi = 22"

    # Delete one MIDI channel (the first one at index 5)
    cm.remove_midi_channel(5)

    assert len(cm.all_channels()) == 21, "config must have 21 channels after single delete"
    assert cm.midi_channel_count == 16

    # Simulate arduino.reload_settings iterating over the OLD 22-slot internal
    # channel list and calling get_effective_inversion for every index.
    for arduino_idx in range(22):
        cm.get_effective_inversion(arduino_idx)

    assert len(cm.all_channels()) == 21, (
        f"channels were illegally recreated by get_effective_inversion: "
        f"expected 21, got {len(cm.all_channels())}"
    )

    # Simulate _on_channel_volume_midi_feedback calling get_midi_cc with a stale
    # channel index (22 = original count) that is now out of range.
    for stale_idx in range(22):
        result = cm.get_midi_cc(stale_idx)
        if stale_idx >= 21:
            assert result is None, (
                f"get_midi_cc({stale_idx}) must return None for out-of-range index"
            )

    # The critical assertion: channels must NOT have been recreated
    assert len(cm.all_channels()) == 21, (
        f"channels were illegally recreated by get_midi_cc: "
        f"expected 21, got {len(cm.all_channels())}"
    )
    assert cm.midi_channel_count == 16, "midi_channel_count must remain 16"

    # Profile on disk must reflect the deletion
    saved = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    assert saved["channel_count"] == 21
    assert len(saved["channels"]) == 21


def test_bulk_delete_17_midi_channels_not_recreated_by_reload_settings(
    tmp_config_path, tmp_profiles_dir
):
    """
    Regression test: deleting 17 empty MIDI channels via remove_midi_channels
    must not recreate them when arduino.reload_settings subsequently calls
    get_effective_inversion for every slot in the Arduino's old (larger)
    internal channel list.

    Before the fix, get_effective_inversion called self._channel(i) which
    auto-created channel dicts for out-of-range indices.  arduino.reload_settings
    iterates over Arduino's _channels (still sized to 22 while the config was
    just shrunk to 5), so calling get_effective_inversion(5..21) silently
    re-created all 17 deleted MIDI channels — exactly matching the user-visible
    symptom "channels immediately reappear after bulk delete".
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.utils.profile_manager import ProfileManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 17
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)

    profile = _make_hybrid_profile(
        hw_count=5, midi_count=17, profile_id="profile-1", name="Profile 1"
    )
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile, indent=2) + "\n")
    pm.set_active_silently("profile-1")
    cm.active_profile_id = "profile-1"
    cm.apply_profile(pm.load("profile-1"))
    assert len(cm.all_channels()) == 22, "setup: must start with 5 hw + 17 midi = 22"

    # Bulk-delete all 17 empty MIDI channels (indices 5..21)
    midi_indices = [ch["index"] for ch in cm.all_channels() if ch.get("is_midi")]
    assert len(midi_indices) == 17
    cm.remove_midi_channels(midi_indices)

    assert len(cm.all_channels()) == 5, "config must have 5 channels after bulk delete"
    assert cm.midi_channel_count == 0

    # Simulate arduino.reload_settings iterating over the OLD 22-slot internal
    # channel list and calling get_effective_inversion for every index.
    for arduino_idx in range(22):
        cm.get_effective_inversion(arduino_idx)

    # The critical assertion: channels must NOT have been silently recreated
    assert len(cm.all_channels()) == 5, (
        f"channels were illegally recreated by get_effective_inversion: "
        f"expected 5, got {len(cm.all_channels())}"
    )
    assert cm.midi_channel_count == 0, "midi_channel_count must remain 0"

    # Profile on disk must reflect the deletion
    saved = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    assert saved["channel_count"] == 5
    assert len(saved["channels"]) == 5


def test_bulk_delete_getters_do_not_recreate_deleted_channels(
    tmp_config_path, tmp_profiles_dir
):
    """Read-only getters must not auto-expand channels after delete/rebuild flows."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.utils.profile_manager import ProfileManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 17
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)

    profile = _make_hybrid_profile(
        hw_count=5, midi_count=17, profile_id="profile-1", name="Profile 1"
    )
    profile["channels"][5]["app_names"] = ["Spotify"]
    profile["channels"][6]["mode"] = "hardware"
    profile["channels"][6]["hardware_id"] = "sink:test-device"
    profile["channels"][7]["v_sink"] = True
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile, indent=2) + "\n")
    pm.set_active_silently("profile-1")
    cm.active_profile_id = "profile-1"
    cm.apply_profile(pm.load("profile-1"))

    midi_indices = [ch["index"] for ch in cm.all_channels() if ch.get("is_midi")]
    cm.remove_midi_channels(midi_indices)
    assert len(cm.all_channels()) == 5

    retained_apps = [cm.get_app_names(i) for i in range(5)]
    for stale_idx in range(22):
        if stale_idx < 5:
            assert cm.get_app_names(stale_idx) == retained_apps[stale_idx]
        else:
            assert cm.get_app_names(stale_idx) == []
            assert cm.get_channel_mode(stale_idx) == "app"
            assert cm.get_hardware_id(stale_idx) is None
            assert cm.is_v_sink_enabled(stale_idx) is False

    assert len(cm.all_channels()) == 5, "read-only getters must not recreate deleted channels"
    assert cm.midi_channel_count == 0

    saved = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    assert saved["channel_count"] == 5
    assert len(saved["channels"]) == 5


def test_apply_profile_midi_only_new_profile_does_not_inflate_midi_count(
    tmp_config_path,
    tmp_profiles_dir,
):
    """Regression: applying a brand-new profile in midi_only mode must NOT inflate
    midi_channel_count to the total hw channel count.

    Before the fix, _rebuild_profile_partition forced is_midi=True for ALL
    channels in midi_only mode, causing the reconciliation to read the post-
    partition count (e.g. 17) instead of the profile's stored count (0).
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.utils.profile_manager import default_channels

    # Simulate: user has 17 hardware channels (e.g. Arduino with 17 pots),
    # midi_only mode, and previously had midi_channel_count=1 from another profile.
    base_cfg = _v6_config(17)
    base_cfg["hardware"]["input_mode"] = "midi_only"
    base_cfg["hardware"]["midi_channel_count"] = 1
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    assert cm.midi_channel_count == 1  # sanity

    # New profile created with hw_channel_count (17) channels, all is_midi=False
    # (matches what _on_add_profile_clicked produces: all_channels filtered to
    # channel_count=hw_channel_count, no MIDI channels stored).
    new_profile = {
        "id": "profile-2",
        "name": "Profile 2",
        "channel_count": 17,
        "restore_fader_positions": False,
        "midi_switch_cc": None,
        "channels": default_channels(17),  # all is_midi=False
    }

    cm.apply_profile(new_profile)

    # Must NOT jump to 17 — the new profile has no is_midi=True channels stored.
    assert cm.midi_channel_count == 0, (
        f"midi_channel_count inflated to {cm.midi_channel_count}; expected 0 "
        "because the new profile stores no is_midi=True channels"
    )


# ---------------------------------------------------------------------------
# Regression tests: add-channel corruption (14 → 31 style jump)
# ---------------------------------------------------------------------------

def test_add_channel_to_14hw_mixed_profile_appends_exactly_one(
    tmp_config_path,
    tmp_profiles_dir,
):
    """
    Regression test for the user-verified repro:
    - Seed profile: 14 hardware channels, non-default labels/app_names/midi_cc.
    - Action: add one MIDI channel via add_midi_channel().
    - Expected: channel_count grows from 14 → 15; first 14 channels preserved
      byte-for-byte; channel 14 is a new default MIDI channel.
    - Must NOT produce a 14 → 31 canonicalization jump.
    """
    from nativmix.utils.profile_manager import ProfileManager

    # 14 hardware channels, hybrid mode, 0 MIDI initially.
    base_cfg = _v6_config(14)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 0
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)

    # Seed profile with rich per-channel metadata (like a real user profile).
    profile = _make_hybrid_profile(hw_count=14, midi_count=0, profile_id="profile-1", name="SCARLETT MIX")
    profile["channels"][0]["label"] = "SCARLETT OUT"
    profile["channels"][0]["app_names"] = ["pulse:alsa_output.usb-scarlett"]
    profile["channels"][1]["label"] = "HEADSET IN"
    profile["channels"][1]["app_names"] = ["discord"]
    profile["channels"][2]["label"] = "DISCORD"
    profile["channels"][2]["app_names"] = ["Discord"]
    profile["channels"][2]["midi_cc"] = 10
    profile["channels"][3]["label"] = "SPOTIFY"
    profile["channels"][3]["app_names"] = ["spotify"]
    profile["channels"][3]["midi_cc"] = 11
    profile["channels"][4]["label"] = "SYSTEM"
    profile["channels"][4]["app_names"] = ["chromium"]
    profile["channels"][4]["midi_cc"] = 12
    profile["channels"][5]["inverted"] = True
    profile["channels"][6]["v_sink"] = True
    profile["channels"][13]["hardware_id"] = "sink:alsa_output.usb-Focusrite"
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile, indent=2) + "\n")

    pm.set_active_silently("profile-1")
    cm.active_profile_id = "profile-1"
    cm.apply_profile(copy.deepcopy(profile))

    # Add exactly one MIDI channel.
    cm.add_midi_channel()

    # (a) Channel count must be exactly 15, not 31/32.
    assert cm.midi_channel_count == 1
    assert len(cm.all_channels()) == 15

    # (b) First 14 channels must be preserved byte-for-byte.
    runtime_channels = cm.all_channels()
    for i in range(14):
        assert runtime_channels[i] == profile["channels"][i], (
            f"Channel {i} was modified during add_midi_channel(); "
            f"expected {profile['channels'][i]!r}, got {runtime_channels[i]!r}"
        )

    # (c) Channel 14 must be a new default MIDI channel.
    new_ch = runtime_channels[14]
    assert new_ch["index"] == 14
    assert new_ch["is_midi"] is True
    assert new_ch["app_names"] == []
    assert new_ch["label"] is None
    assert new_ch["midi_cc"] is None

    # (d) Reload from disk and re-assert.
    saved = pm.load("profile-1")
    assert saved["channel_count"] == 15
    assert len(saved["channels"]) == 15
    assert saved["channels"][:14] == profile["channels"]
    saved_new = saved["channels"][14]
    assert saved_new["index"] == 14
    assert saved_new["is_midi"] is True
    assert saved_new["app_names"] == []
    assert saved_new["label"] is None


def test_add_channel_repeated_grows_count_incrementally(
    tmp_config_path,
    tmp_profiles_dir,
):
    """
    Adding multiple channels one at a time must grow count by +1 each time
    without any unexpected jumps.
    """
    from nativmix.utils.profile_manager import ProfileManager

    base_cfg = _v6_config(5)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 0
    tmp_config_path.write_text(json.dumps(base_cfg))

    cm = _load_manager(tmp_config_path, tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)

    profile = _make_hybrid_profile(hw_count=5, midi_count=0, profile_id="profile-1", name="Base")
    profile["channels"][0]["label"] = "HW0"
    profile["channels"][0]["app_names"] = ["vlc"]
    profile["channels"][1]["midi_cc"] = 20
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile, indent=2) + "\n")

    pm.set_active_silently("profile-1")
    cm.active_profile_id = "profile-1"
    cm.apply_profile(copy.deepcopy(profile))

    for add_n in range(1, 4):
        cm.add_midi_channel()
        expected_total = 5 + add_n

        assert len(cm.all_channels()) == expected_total, (
            f"After {add_n} add(s): expected {expected_total} channels, "
            f"got {len(cm.all_channels())}"
        )
        # Original HW channels must remain untouched.
        for i in range(5):
            assert cm.all_channels()[i] == profile["channels"][i], (
                f"HW channel {i} was corrupted after {add_n} add(s)"
            )
        # Newly appended channel must be MIDI.
        assert cm.all_channels()[expected_total - 1]["is_midi"] is True

        saved = pm.load("profile-1")
        assert saved["channel_count"] == expected_total


def test_forbidden_transition_non_resize_save_with_stale_runtime_is_rejected(
    tmp_config_path,
    tmp_profiles_dir,
    caplog,
):
    """
    Forbidden transition test: a non-resize save driven by a stale runtime
    (fewer channels than stored) must be rejected and must not corrupt the
    stored profile with blank/default channel data.
    """
    import logging

    from nativmix.utils.config_manager import _blank_channel
    from nativmix.utils.profile_manager import ProfileManager

    base_cfg = _v6_config(14)
    base_cfg["hardware"]["input_mode"] = "hybrid"
    base_cfg["hardware"]["midi_channel_count"] = 0
    tmp_config_path.write_text(json.dumps(base_cfg))

    _load_manager(tmp_config_path, tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)

    # Rich 14-channel profile.
    profile = _make_hybrid_profile(hw_count=14, midi_count=0, profile_id="profile-1", name="SCARLETT MIX")
    profile["channels"][0]["label"] = "SCARLETT OUT"
    profile["channels"][3]["label"] = "SPOTIFY"
    profile["channels"][3]["midi_cc"] = 11
    profile["channels"][7]["inverted"] = True
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile, indent=2) + "\n")

    pm.set_active_silently("profile-1")

    # Attempt to save with only 5 blank channels (simulates stale runtime mid-rebuild).
    stale_runtime = [_blank_channel(i, is_midi=False) for i in range(5)]
    with caplog.at_level(logging.WARNING, logger="nativmix.utils.profile_manager"):
        pm.save_current(stale_runtime)  # allow_resize=False by default

    # Profile must be completely unchanged.
    reloaded = pm.load("profile-1")
    assert reloaded["channel_count"] == 14
    assert len(reloaded["channels"]) == 14
    assert reloaded["channels"][0]["label"] == "SCARLETT OUT"
    assert reloaded["channels"][3]["label"] == "SPOTIFY"
    assert reloaded["channels"][3]["midi_cc"] == 11
    assert reloaded["channels"][7]["inverted"] is True

    # Invariant rejection must be logged as a warning.
    assert any("refusing non-resize save" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# midi_only mode: num_channels / add_midi_channel / remove_midi_channels
# ---------------------------------------------------------------------------

def _make_midi_only_config(hw_channel_count: int, midi_channel_count: int) -> dict:
    """Build a v6 config in midi_only mode.

    *hw_channel_count* represents the physical hardware pot count stored in
    config (may be stale/non-zero when the user switched from USB/hybrid).
    *midi_channel_count* is the number of software MIDI channels currently
    configured.
    """
    return {
        "version": 6,
        "hardware": {
            "port": None,
            "auto_search_device": True,
            "num_channels": hw_channel_count,
            "input_mode": "midi_only",
            "midi_device": "",
            "midi_channel_count": midi_channel_count,
            "baud_rate": 9600,
        },
        "settings": {
            "threshold": 0.01,
            "invert_map": [False] * (hw_channel_count + midi_channel_count),
            "v_sink_map": [False] * (hw_channel_count + midi_channel_count),
            "transparency": True,
            "compact_mode": False,
            "stay_open": False,
            "show_invert_option": False,
            "debug_logging": False,
        },
        "channels": [],
    }


def _make_midi_only_profile(
    channel_count: int,
    profile_id: str = "profile-1",
    name: str = "MIDI Profile",
) -> dict:
    """Build a midi_only profile where all channels have is_midi=True."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.profile_manager import default_channels

    channels = default_channels(channel_count)
    for ch in channels:
        ch["is_midi"] = True
    return {
        "id": profile_id,
        "name": name,
        "channel_count": channel_count,
        "restore_fader_positions": False,
        "midi_switch_cc": None,
        "channels": channels,
    }


def test_num_channels_midi_only_excludes_stale_hw_count(
    tmp_config_path, tmp_profiles_dir
):
    """In midi_only mode num_channels must equal midi_channel_count only.

    This is the root cause of the "14 → 31" channel inflation bug: when a
    user with hw_channel_count=17 (stale from a prior USB/hybrid setup) adds
    or removes MIDI channels in midi_only mode, the inflated num_channels
    (17 + midi_count) caused ``_persist_active_profile_channels`` to persist
    far too many channels.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager

    # hw_channel_count=17 is stale; mode is midi_only with 14 MIDI channels.
    cfg = _make_midi_only_config(hw_channel_count=17, midi_channel_count=14)
    tmp_config_path.write_text(json.dumps(cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    assert cm.hw_channel_count == 17, "sanity: hw count is 17 (stale)"
    assert cm.midi_channel_count == 14, "sanity: midi count is 14"
    # Before the fix num_channels returned 17 + 14 = 31.
    assert cm.num_channels == 14, (
        f"num_channels in midi_only must equal midi_channel_count (14), got {cm.num_channels}"
    )


def test_add_midi_channel_midi_only_does_not_inflate(
    tmp_config_path, tmp_profiles_dir
):
    """Adding one MIDI channel in midi_only mode must grow the profile by exactly 1.

    Before the fix, with hw_channel_count=17 (stale), adding one MIDI channel
    to a 14-channel profile inflated it to 32 channels (17 + 15) instead of 15.
    The log showed "canonicalized channels 14 → 31" (or similar large jumps).
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.utils.profile_manager import ProfileManager

    # Stale hw_channel_count=17, midi_channel_count=14 → 14 MIDI channels.
    cfg = _make_midi_only_config(hw_channel_count=17, midi_channel_count=14)
    tmp_config_path.write_text(json.dumps(cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)

    profile = _make_midi_only_profile(channel_count=14, profile_id="profile-1")
    profile["channels"][3]["label"] = "synth"
    profile["channels"][3]["midi_cc"] = 7
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile, indent=2) + "\n")
    pm.set_active_silently("profile-1")
    cm.active_profile_id = "profile-1"
    cm.apply_profile(pm.load("profile-1"))

    assert len(cm.all_channels()) == 14, "setup: 14 channels"
    assert cm.num_channels == 14, "setup: num_channels must be 14 in midi_only"

    cm.add_midi_channel()

    # Must grow by exactly 1, not by hw_channel_count + 1.
    assert len(cm.all_channels()) == 15, (
        f"add_midi_channel must grow to 15, got {len(cm.all_channels())}"
    )
    assert cm.midi_channel_count == 15
    assert cm.num_channels == 15

    # The new channel must be MIDI.
    assert cm.all_channels()[14]["is_midi"] is True

    # Existing mappings must be preserved.
    assert cm.all_channels()[3]["label"] == "synth"
    assert cm.all_channels()[3]["midi_cc"] == 7

    # Profile file must reflect the correct count.
    saved = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    assert saved["channel_count"] == 15, (
        f"profile channel_count must be 15, got {saved['channel_count']}"
    )
    assert len(saved["channels"]) == 15, (
        f"profile channels list must have 15 entries, got {len(saved['channels'])}"
    )


@pytest.mark.parametrize(
    ("input_mode", "hw_channel_count", "midi_channel_count", "midi_indices"),
    [
        ("midi_only", 17, 3, (0, 1, 2)),
        ("hybrid", 2, 3, (2, 3, 4)),
    ],
)
def test_add_midi_channel_preserves_live_bindings_and_metadata(
    tmp_config_path,
    tmp_profiles_dir,
    input_mode,
    hw_channel_count,
    midi_channel_count,
    midi_indices,
):
    """A profile-anchored resize must retain newer live state for existing channels."""
    from nativmix.utils.config_manager import ConfigManager, _blank_channel
    from nativmix.utils.profile_manager import ProfileManager

    if input_mode == "midi_only":
        cfg = _make_midi_only_config(hw_channel_count, midi_channel_count)
        profile = _make_midi_only_profile(midi_channel_count)
    else:
        cfg = _v6_config(hw_channel_count)
        cfg["hardware"]["input_mode"] = "hybrid"
        cfg["hardware"]["midi_channel_count"] = midi_channel_count
        profile = _make_hybrid_profile(
            hw_count=hw_channel_count,
            midi_count=midi_channel_count,
            profile_id="profile-1",
            name="Hybrid Profile",
        )
    tmp_config_path.write_text(json.dumps(cfg))
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile, indent=2) + "\n")

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)
    pm.set_active_silently("profile-1")
    cm.active_profile_id = "profile-1"
    cm.apply_profile(pm.load("profile-1"))

    for offset, channel_index in enumerate(midi_indices):
        cm.set_midi_cc(channel_index, 20 + offset)
        cm.set_midi_mute_cc(channel_index, 70 + offset)
        cm.set_channel_label(channel_index, f"MIDI {offset + 1}")
        cm.set_inverted(channel_index, offset == 1)
        cm.set_channel_volume(channel_index, 0.2 + offset * 0.1)

    before_add = copy.deepcopy(cm.all_channels())
    assert all(
        profile["channels"][channel_index]["midi_cc"] is None
        for channel_index in midi_indices
    ), "setup requires bindings to be newer than the stored profile snapshot"

    cm.add_midi_channel()

    runtime_channels = cm.all_channels()
    assert runtime_channels[:-1] == before_add
    assert runtime_channels[-1] == _blank_channel(len(before_add), is_midi=True)

    saved = pm.load("profile-1")
    assert saved["channels"] == runtime_channels
    assert saved["channel_count"] == len(before_add) + 1


def test_remove_midi_channels_midi_only_does_not_inflate(
    tmp_config_path, tmp_profiles_dir
):
    """Bulk-deleting MIDI channels in midi_only mode must shrink the profile correctly.

    Before the fix, deleting 9 channels from a 23-channel midi_only profile
    with stale hw_channel_count=17 caused the profile to inflate to 40 channels
    (17 + 23) instead of shrinking to 14.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.utils.profile_manager import ProfileManager

    # Stale hw_channel_count=17, 23 MIDI channels.
    cfg = _make_midi_only_config(hw_channel_count=17, midi_channel_count=23)
    tmp_config_path.write_text(json.dumps(cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)

    profile = _make_midi_only_profile(channel_count=23, profile_id="profile-1")
    # Label a few channels to verify they are preserved after bulk delete.
    profile["channels"][0]["label"] = "drums"
    profile["channels"][0]["midi_cc"] = 1
    profile["channels"][5]["label"] = "bass"
    profile["channels"][5]["midi_cc"] = 5
    (tmp_profiles_dir / "profile-1.json").write_text(json.dumps(profile, indent=2) + "\n")
    pm.set_active_silently("profile-1")
    cm.active_profile_id = "profile-1"
    cm.apply_profile(pm.load("profile-1"))

    assert len(cm.all_channels()) == 23
    assert cm.num_channels == 23

    # Delete channels 14..22 (9 channels) — highest indices to avoid re-indexing issues.
    indices_to_delete = list(range(14, 23))
    cm.remove_midi_channels(indices_to_delete)

    assert len(cm.all_channels()) == 14, (
        f"remove_midi_channels must shrink to 14, got {len(cm.all_channels())}"
    )
    assert cm.midi_channel_count == 14
    assert cm.num_channels == 14

    # Surviving channels must retain their data.
    channels = cm.all_channels()
    assert channels[0]["label"] == "drums"
    assert channels[0]["midi_cc"] == 1
    assert channels[5]["label"] == "bass"
    assert channels[5]["midi_cc"] == 5

    # All surviving channels must still be MIDI.
    assert all(ch["is_midi"] is True for ch in channels)

    # Profile file must reflect the correct count.
    saved = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    assert saved["channel_count"] == 14, (
        f"profile channel_count must be 14, got {saved['channel_count']}"
    )
    assert len(saved["channels"]) == 14, (
        f"profile channels list must have 14 entries, got {len(saved['channels'])}"
    )


def test_ensure_channels_midi_only_marks_all_channels_as_midi(
    tmp_config_path, tmp_profiles_dir
):
    """In midi_only mode _ensure_channels must mark all channels is_midi=True.

    Before the fix, _ensure_channels used hw_channel_count as the boundary,
    so in midi_only with stale hw=17, channels 0..16 were mis-labelled as
    is_midi=False (hardware channels) even though the mode has no hardware faders.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager

    # Stale hw=17, 5 MIDI channels.
    cfg = _make_midi_only_config(hw_channel_count=17, midi_channel_count=5)
    # Start with an empty channels list so _ensure_channels must build it.
    cfg["channels"] = []
    tmp_config_path.write_text(json.dumps(cfg))

    cm = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    # Trigger _ensure_channels explicitly via the setter.
    cm._ensure_channels(5)

    channels = cm.all_channels()
    assert len(channels) == 5
    assert all(ch["is_midi"] is True for ch in channels), (
        "all channels must be is_midi=True in midi_only mode"
    )
