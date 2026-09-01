from __future__ import annotations

import json
import uuid

from nativmix.utils.config_manager import CONFIG_VERSION, ConfigManager


def _v8_config() -> dict[str, object]:
    return {
        "version": 8,
        "hardware": {
            "port": None,
            "auto_search_device": True,
            "num_channels": 2,
            "input_mode": "midi_only",
            "midi_device": "Controller",
            "midi_channel_count": 2,
            "baud_rate": 9600,
        },
        "settings": {
            "threshold": 0.01,
            "transparency": True,
            "compact_mode": False,
            "stay_open": False,
            "show_invert_option": False,
            "debug_logging": False,
            "midi_fader_feedback": False,
        },
        "active_profile": "profile-1",
    }


def test_v8_migration_adds_disabled_remote_controller_identity(
    tmp_config_path,
    tmp_profiles_dir,
) -> None:
    tmp_config_path.write_text(json.dumps(_v8_config()), encoding="utf-8")

    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    saved = json.loads(tmp_config_path.read_text(encoding="utf-8"))

    assert saved["version"] == CONFIG_VERSION == 9
    assert config.remote_midi_role == "off"
    assert uuid.UUID(config.remote_midi_instance_id)
    assert config.remote_midi_name.startswith("NativMix on ")
    assert config.remote_midi_peer_id == ""
    assert config.remote_midi_peer_name == ""
    assert config.midi_device == "Controller"


def test_remote_identity_is_stable_across_reloads(tmp_config_path, tmp_profiles_dir) -> None:
    first = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    instance_id = first.remote_midi_instance_id

    second = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)

    assert second.remote_midi_instance_id == instance_id


def test_remote_settings_normalize_values_and_persist(tmp_config_path, tmp_profiles_dir) -> None:
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    peer_id = str(uuid.uuid4())

    config.remote_midi_role = "invalid"
    config.remote_midi_name = "  Laptop\x00 Mixer  "
    config.remote_midi_peer_id = peer_id
    config.remote_midi_peer_name = "  Living Room Desktop  "
    config.save()

    reloaded = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    assert reloaded.remote_midi_role == "off"
    assert reloaded.remote_midi_name == "Laptop Mixer"
    assert reloaded.remote_midi_peer_id == peer_id
    assert reloaded.remote_midi_peer_name == "Living Room Desktop"
