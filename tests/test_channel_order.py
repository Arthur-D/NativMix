"""Per-profile visual channel ordering."""

from __future__ import annotations

from nativmix.utils.channel_order import normalize_channel_order, order_after_remove
from nativmix.utils.profile_manager import ProfileManager
from tests.conftest import make_profile, write_profile


def test_normalize_channel_order_repairs_malformed_values_deterministically():
    assert normalize_channel_order([2, "2", None, 9, "1", {}, 0], [0, 1, 2, 3]) == [2, 1, 0, 3]
    assert normalize_channel_order(None, [0, 1, 2]) == [0, 1, 2]


def test_order_after_remove_preserves_visual_intent():
    assert order_after_remove([3, 0, 2, 1], 2) == [2, 0, 1]


def test_profile_order_persists_and_repairs_channel_count(tmp_profiles_dir):
    profile = make_profile(channel_count=3)
    profile["channel_order"] = [2, 0, 1]
    write_profile(tmp_profiles_dir, profile)
    manager = ProfileManager(profiles_dir=tmp_profiles_dir)
    manager.set_active_silently(profile["id"])

    assert manager.get_channel_order() == [2, 0, 1]
    manager.save_current(profile["channels"], allow_resize=True, target_channel_count=4)

    loaded = manager.load(profile["id"])
    assert loaded["channel_order"] == [2, 0, 1, 3]
    assert [channel["index"] for channel in loaded["channels"]] == [0, 1, 2, 3]


def test_profile_switch_keeps_independent_orders(tmp_profiles_dir):
    first = make_profile("profile-1", channel_count=3)
    first["channel_order"] = [2, 0, 1]
    second = make_profile("profile-2", channel_count=3)
    second["channel_order"] = [1, 2, 0]
    write_profile(tmp_profiles_dir, first)
    write_profile(tmp_profiles_dir, second)
    manager = ProfileManager(profiles_dir=tmp_profiles_dir)

    manager.set_active_silently("profile-1")
    assert manager.get_channel_order() == [2, 0, 1]
    manager.switch("profile-2")
    assert manager.get_channel_order() == [1, 2, 0]


def test_reordering_never_mutates_channel_identity_or_midi_bindings(tmp_profiles_dir):
    profile = make_profile(channel_count=3)
    profile["channels"][0].update({"midi_cc": 12, "midi_mute_cc": 13})
    profile["channels"][2].update({"hardware_id": "sink:USB DAC"})
    write_profile(tmp_profiles_dir, profile)
    manager = ProfileManager(profiles_dir=tmp_profiles_dir)
    manager.set_active_silently(profile["id"])

    manager.set_channel_order([2, 0, 1])
    loaded = manager.load(profile["id"])

    assert loaded["channel_order"] == [2, 0, 1]
    assert [channel["index"] for channel in loaded["channels"]] == [0, 1, 2]
    assert loaded["channels"][0]["midi_cc"] == 12
    assert loaded["channels"][0]["midi_mute_cc"] == 13
    assert loaded["channels"][2]["hardware_id"] == "sink:USB DAC"


def test_none_order_is_natural():
    assert normalize_channel_order(None, [0, 1, 2]) == [0, 1, 2]


def test_keeps_permutation():
    assert normalize_channel_order([2, 0, 1], [0, 1, 2]) == [2, 0, 1]


def test_drops_unknown_and_appends_missing():
    assert normalize_channel_order([9, 1, 0], [0, 1, 2]) == [1, 0, 2]


def test_empty_order_is_natural():
    assert normalize_channel_order([], [0, 1]) == [0, 1]


def test_order_after_remove_middle():
    assert order_after_remove([0, 2, 1, 3], 1) == [0, 1, 2]


def test_save_current_preserves_channel_order(tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", channel_count=3))
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)
    pm.set_active_silently("profile-1")
    pm.set_channel_order([2, 0, 1])
    channels = make_profile(channel_count=3)["channels"]
    pm.save_current(channels)
    loaded = pm.load("profile-1")
    assert loaded["channel_order"] == [2, 0, 1]


def test_local_mixer_facade_uses_profile_order(tmp_path):
    from nativmix.gui.mixer_facade import LocalMixerFacade
    from nativmix.utils.config_manager import ConfigManager

    cfg_path = tmp_path / "config.json"
    profiles_dir = tmp_path / "profiles"
    config = ConfigManager(config_path=cfg_path, profiles_dir=profiles_dir)
    profile = make_profile(channel_count=3, channel_order=[2, 0, 1])
    write_profile(profiles_dir, profile)
    manager = ProfileManager(profiles_dir=profiles_dir)
    manager.set_active_silently(profile["id"])
    mixer = LocalMixerFacade(config, manager, backend=None)

    assert mixer.get_channel_order() == [2, 0, 1]
    mixer.set_channel_order([1, 2, 0])
    assert mixer.get_channel_order() == [1, 2, 0]
