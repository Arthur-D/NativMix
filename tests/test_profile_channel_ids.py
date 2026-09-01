from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

from nativmix.utils.profile_manager import ProfileManager, default_channels
from tests.conftest import make_profile, write_profile


def _manager(path: Path) -> ProfileManager:
    return ProfileManager(profiles_dir=path)


def test_legacy_channel_ids_are_deterministic_per_profile(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    legacy = make_profile("profile-7", channel_count=4)
    write_profile(first_dir, legacy)
    write_profile(second_dir, legacy)

    first = _manager(first_dir).load("profile-7")
    second = _manager(second_dir).load("profile-7")
    first_ids = [channel["channel_id"] for channel in first["channels"]]

    assert first_ids == [channel["channel_id"] for channel in second["channels"]]
    assert len(set(first_ids)) == 4
    assert all(uuid.UUID(channel_id).version == 5 for channel_id in first_ids)
    assert first["profile_schema_version"] == 1


def test_channel_id_repair_is_deterministic_and_preserves_channel_data(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    duplicate_id = str(uuid.uuid4())
    profile = make_profile("profile-1", channel_count=3)
    profile["channels"][0]["channel_id"] = duplicate_id
    profile["channels"][1].update({"channel_id": duplicate_id, "label": "Music", "midi_cc": 17})
    profile["channels"][2].update({"channel_id": "malformed", "hardware_id": "sink:usb"})
    write_profile(profiles_dir, profile)

    manager = _manager(profiles_dir)
    first = manager.load("profile-1")
    second = manager.load("profile-1")
    ids = [channel["channel_id"] for channel in first["channels"]]

    assert ids == [channel["channel_id"] for channel in second["channels"]]
    assert ids[0] == duplicate_id
    assert len(set(ids)) == 3
    assert first["channels"][1]["label"] == "Music"
    assert first["channels"][1]["midi_cc"] == 17
    assert first["channels"][2]["hardware_id"] == "sink:usb"


def test_duplicate_index_uses_valid_identity_and_merges_bindings(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    retained_id = str(uuid.uuid4())
    profile = make_profile("profile-1", channel_count=2)
    profile["channels"][1].update({"channel_id": "bad", "app_names": ["Spotify"]})
    duplicate = copy.deepcopy(profile["channels"][1])
    duplicate.update({"channel_id": retained_id, "app_names": ["Firefox"], "midi_mute_cc": 44})
    profile["channels"].append(duplicate)
    write_profile(profiles_dir, profile)

    channel = _manager(profiles_dir).load("profile-1")["channels"][1]

    assert channel["channel_id"] == retained_id
    assert channel["app_names"] == ["Spotify", "Firefox"]
    assert channel["midi_mute_cc"] == 44


def test_new_and_cloned_profiles_get_fresh_channel_ids(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    manager = _manager(profiles_dir)
    source_id = manager.create("Source", channel_count=3)
    source = manager.load(source_id)

    clone_id = manager.create("Clone", channel_count=3, channels=source["channels"])
    clone = manager.load(clone_id)

    source_ids = {channel["channel_id"] for channel in source["channels"]}
    clone_ids = {channel["channel_id"] for channel in clone["channels"]}
    assert source_ids.isdisjoint(clone_ids)
    assert all(uuid.UUID(channel_id).version == 4 for channel_id in source_ids | clone_ids)

    partial_id = manager.create("Partial", channel_count=4, channels=source["channels"][:1])
    partial_ids = {channel["channel_id"] for channel in manager.load(partial_id)["channels"]}
    assert len(partial_ids) == 4
    assert all(uuid.UUID(channel_id).version == 4 for channel_id in partial_ids)


def test_resize_delete_and_reorder_keep_ids_attached(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    manager = _manager(profiles_dir)
    profile_id = manager.create("Mutable", channel_count=3)
    manager.set_active_silently(profile_id)
    original = manager.load(profile_id)
    original_ids = [channel["channel_id"] for channel in original["channels"]]

    grown = copy.deepcopy(original["channels"])
    new_channel = default_channels(1)[0]
    new_channel.update({"index": 3, "is_midi": True, "label": "MIDI"})
    grown.append(new_channel)
    manager.save_current(grown, allow_resize=True, target_channel_count=4)
    after_add = manager.load(profile_id)
    assert [channel["channel_id"] for channel in after_add["channels"][:3]] == original_ids
    assert after_add["channels"][3]["channel_id"] == new_channel["channel_id"]
    assert after_add["channels"][3]["label"] == "MIDI"
    assert after_add["channels"][3]["is_midi"] is True

    reduced = copy.deepcopy(after_add["channels"])
    removed_id = reduced.pop(1)["channel_id"]
    for index, channel in enumerate(reduced):
        channel["index"] = index
    manager.save_current(reduced, allow_resize=True, target_channel_count=3)
    manager.set_channel_order([2, 0, 1])
    final = manager.load(profile_id)

    assert [channel["channel_id"] for channel in final["channels"]] == [
        original_ids[0],
        original_ids[2],
        new_channel["channel_id"],
    ]
    assert removed_id not in {channel["channel_id"] for channel in final["channels"]}
    assert manager.get_channel_order_ids() == [
        new_channel["channel_id"],
        original_ids[0],
        original_ids[2],
    ]
    assert manager.get_channel_index(new_channel["channel_id"]) == 2
    assert manager.get_channel_id(0) == original_ids[0]


def test_profile_switch_keeps_independent_id_namespaces(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    first = make_profile("profile-1", channel_count=2)
    second = make_profile("profile-2", channel_count=2)
    write_profile(profiles_dir, first)
    write_profile(profiles_dir, second)
    manager = _manager(profiles_dir)

    manager.switch("profile-1")
    first_ids = set(manager.get_channel_order_ids())
    manager.switch("profile-2")
    second_ids = set(manager.get_channel_order_ids())

    assert first_ids.isdisjoint(second_ids)
    assert json.loads((profiles_dir / "profile-1.json").read_text())["id"] == "profile-1"
    assert json.loads((profiles_dir / "profile-2.json").read_text())["id"] == "profile-2"


def test_fresh_install_default_channels_use_unique_uuid4_ids() -> None:
    from nativmix.utils.config_manager import _default_config

    first = _default_config(3)["channels"]
    second = _default_config(3)["channels"]
    first_ids = {channel["channel_id"] for channel in first}
    second_ids = {channel["channel_id"] for channel in second}

    assert first_ids.isdisjoint(second_ids)
    assert all(uuid.UUID(channel_id).version == 4 for channel_id in first_ids | second_ids)
