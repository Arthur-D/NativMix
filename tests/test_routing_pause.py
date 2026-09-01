"""Per-app routing-only pause persistence and shared mapping semantics."""

from __future__ import annotations

import pytest

from nativmix.utils.config_manager import ConfigManager
from nativmix.utils.profile_manager import ProfileManager


def test_pause_is_case_insensitive_shared_and_profile_persistent(tmp_path):
    profiles = tmp_path / "profiles"
    manager = ProfileManager(profiles_dir=profiles)
    profile_id = manager.create("Default", channel_count=2)
    manager.set_active_silently(profile_id)
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=profiles)
    config.apply_profile(manager.load(profile_id))
    config.add_app_name(0, "Firefox")
    config.add_app_name(1, "Firefox")

    config.set_app_routing_paused(1, "Firefox", True)
    assert config.is_app_routing_paused(0, "firefox")
    assert config.get_routing_paused_apps(0) == ["Firefox"]
    assert config.get_routing_paused_apps(1) == ["Firefox"]

    manager.save_current(config.all_channels())
    restarted = manager.load(profile_id)
    assert restarted["channels"][0]["routing_paused_apps"] == ["Firefox"]
    assert restarted["channels"][1]["routing_paused_apps"] == ["Firefox"]


def test_pause_affects_routing_only_and_mapping_remains(tmp_path):
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    config.add_app_name(0, "Firefox")
    config.set_app_routing_paused(0, "Firefox", True)

    assert config.get_app_names(0) == ["Firefox"]
    config.set_channel_volume(0, 0.35)
    assert config.get_channel_volume(0) == 0.35


def test_new_shared_mapping_inherits_existing_pause(tmp_path):
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    config.add_app_name(0, "Firefox")
    config.set_app_routing_paused(0, "Firefox", True)

    config.add_app_name(1, "Firefox")
    config.remove_app_name(0, "Firefox")

    assert config.is_app_routing_paused(1, "Firefox")
    assert config.get_routing_paused_apps(1) == ["Firefox"]


@pytest.mark.parametrize("special", ["System Master", "Other Apps"])
def test_special_targets_do_not_expose_routing_pause(tmp_path, special):
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    config.add_app_name(0, special)
    with pytest.raises(ValueError, match="mapped regular apps"):
        config.set_app_routing_paused(0, special, True)
