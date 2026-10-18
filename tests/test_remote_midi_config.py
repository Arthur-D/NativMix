from __future__ import annotations

import json
import uuid

import pytest

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

    assert saved["version"] == CONFIG_VERSION == 13
    assert config.remote_midi_role == "off"
    assert uuid.UUID(config.remote_midi_instance_id)
    assert config.remote_midi_name.startswith("NativMix on ")
    assert config.remote_midi_peer_id == ""
    assert config.remote_midi_peer_name == ""
    assert config.midi_device == "Controller"
    assert config.prevent_remote_sleep is True


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


def test_v9_migration_enables_machine_local_remote_sleep_prevention(
    tmp_config_path,
    tmp_profiles_dir,
) -> None:
    config_data = _v8_config()
    config_data["version"] = 9
    config_data["hardware"]["remote_midi_role"] = "receive"
    tmp_config_path.write_text(json.dumps(config_data), encoding="utf-8")

    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)

    assert config.remote_midi_role == "receive"
    assert config.prevent_remote_sleep is True
    assert json.loads(tmp_config_path.read_text(encoding="utf-8"))["settings"]["prevent_remote_sleep"] is True


def test_v10_migration_adds_disabled_remote_mixer_permission(
    tmp_config_path,
    tmp_profiles_dir,
) -> None:
    config_data = _v8_config()
    config_data["version"] = 10
    config_data["settings"]["prevent_remote_sleep"] = False
    tmp_config_path.write_text(json.dumps(config_data), encoding="utf-8")

    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    saved = json.loads(tmp_config_path.read_text(encoding="utf-8"))

    assert config.allow_remote_mixer_editing is False
    assert config.prevent_remote_sleep is False
    assert saved["settings"]["allow_remote_mixer_editing"] is False


def test_remote_mixer_permission_persists_and_emits_once(
    tmp_config_path,
    tmp_profiles_dir,
) -> None:
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    emissions: list[None] = []
    config.settings_changed.connect(lambda: emissions.append(None))

    config.allow_remote_mixer_editing = True
    config.save()

    assert emissions == [None]
    assert ConfigManager(
        config_path=tmp_config_path,
        profiles_dir=tmp_profiles_dir,
    ).allow_remote_mixer_editing is True


@pytest.mark.parametrize("role", ["off", "send", "receive"])
def test_v11_migration_preserves_remote_selection(role, tmp_config_path, tmp_profiles_dir):
    data = _v8_config()
    data["version"] = 11
    data["hardware"]["remote_midi_role"] = role
    peer_id = str(uuid.uuid4())
    data["hardware"]["remote_midi_peer_id"] = peer_id
    data["settings"]["allow_remote_mixer_editing"] = True
    tmp_config_path.write_text(json.dumps(data), encoding="utf-8")

    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    assert config.remote_midi_send_enabled is (role == "send")
    assert config.remote_midi_receive_enabled is (role == "receive")
    assert config.remote_midi_peer_id == peer_id
    assert config.allow_remote_mixer_editing
    assert "remote_midi_role" not in json.loads(tmp_config_path.read_text())["hardware"]


def test_remote_capabilities_persist_independently(tmp_config_path, tmp_profiles_dir):
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    emissions = []
    config.settings_changed.connect(lambda: emissions.append(None))
    config.remote_midi_send_enabled = True
    config.remote_midi_receive_enabled = True
    assert emissions == [None, None]
    config.save()

    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    assert config.remote_midi_role == "send_receive"
    config.remote_midi_send_enabled = False
    assert config.remote_midi_receive_enabled
    assert config.remote_midi_role == "receive"
