from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_config(tmp_path):
    from nativmix.utils.config_manager import ConfigManager

    return ConfigManager(
        config_path=tmp_path / "config.json",
        profiles_dir=tmp_path / "profiles",
    )


def test_regular_app_mapping_is_preserved_on_multiple_channels(tmp_path):
    config = _make_config(tmp_path)

    config.update_mapping("Spotify", 0)
    config.update_mapping("Spotify", 2)

    assert config.get_app_names(0) == ["Spotify"]
    assert config.get_app_names(2) == ["Spotify"]
    assert config.find_channels_for_app("spotify") == [0, 2]
    assert config.find_channel_for_app("Spotify") == 0
    assert config.get_all_assigned_apps_by_name()["spotify"] == 0


def test_duplicate_regular_mapping_persists_in_active_profile(tmp_path):
    from nativmix.utils.profile_manager import ProfileManager

    config = _make_config(tmp_path)
    config.update_mapping("Spotify", 0)
    config.update_mapping("Spotify", 1)

    config._persist_active_profile_channels()

    profile = ProfileManager(profiles_dir=tmp_path / "profiles").load(config.active_profile_id)
    assert profile["channels"][0]["app_names"] == ["Spotify"]
    assert profile["channels"][1]["app_names"] == ["Spotify"]


def test_special_mappings_remain_globally_exclusive_and_isolated(tmp_path):
    config = _make_config(tmp_path)
    config.update_mapping("System Master", 0)

    with pytest.raises(ValueError, match="Not allowed"):
        config.update_mapping("System Master", 1)
    with pytest.raises(ValueError, match="Not allowed"):
        config.update_mapping("Spotify", 0)

    config.update_mapping("Spotify", 1)
    with pytest.raises(ValueError, match="Not allowed"):
        config.update_mapping("Other Apps", 1)

    assert config.get_app_names(0) == ["System Master"]
    assert config.get_app_names(1) == ["Spotify"]


def test_other_apps_assigned_set_keeps_duplicate_regular_app_excluded(tmp_path):
    config = _make_config(tmp_path)
    config.update_mapping("Spotify", 0)
    config.update_mapping("Spotify", 2)
    config.update_mapping("Other Apps", 1)

    assigned = config.get_all_assigned_apps_by_name()

    assert set(assigned) == {"spotify", "other apps"}
    assert config.get_all_app_channels_by_name()["spotify"] == [0, 2]


def test_shared_regular_channels_include_transitive_mapping_component(tmp_path):
    config = _make_config(tmp_path)
    config.set_app_names(0, ["App A"])
    config.set_app_names(1, ["App A", "App B"])
    config.set_app_names(2, ["App B"])

    assert config.get_shared_regular_channels(0) == [0, 1, 2]


def test_feedback_off_keeps_last_moved_channel_position_independent(tmp_path):
    config = _make_config(tmp_path)
    config.update_mapping("Spotify", 0)
    config.update_mapping("Spotify", 1)
    config.set_channel_volume(1, 0.8)
    config.midi_fader_feedback = False

    siblings = config.update_shared_channel_volumes(0, 0.25)

    assert siblings == []
    assert config.get_channel_volume(0) == pytest.approx(0.25)
    assert config.get_channel_volume(1) == pytest.approx(0.8)


def test_feedback_off_backend_applies_only_the_control_that_moved_last(tmp_path):
    from nativmix.audio.manager import PipeWireManager

    config = _make_config(tmp_path)
    config.update_mapping("Spotify", 0)
    config.update_mapping("Spotify", 1)
    config.midi_fader_feedback = False
    manager = PipeWireManager(config=config)
    manager.pw_only_mode = True
    manager.effective_routing_owner = "none"

    with patch.object(manager, "_apply_volume_by_name_pw_only") as apply_volume:
        manager.apply_poti_volumes([0.5, 0.8])
        apply_volume.reset_mock()
        manager.apply_poti_volumes([0.2, 0.8])

    apply_volume.assert_called_once_with("Spotify", 0.2)


def test_forced_hardware_sync_reapplies_unchanged_positions_after_profile_switch(tmp_path):
    from nativmix.audio.manager import PipeWireManager

    config = _make_config(tmp_path)
    config.update_mapping("Spotify", 0)
    manager = PipeWireManager(config=config)
    manager.pw_only_mode = True
    manager.effective_routing_owner = "none"

    with patch.object(manager, "_apply_volume_by_name_pw_only") as apply_volume:
        manager.apply_poti_volumes([0.5])
        apply_volume.reset_mock()
        manager.apply_poti_volumes([0.5], force=True)

    apply_volume.assert_called_once_with("Spotify", 0.5)


def test_feedback_on_syncs_sibling_config_gui_and_midi_signal_without_duplicate_write(
    tmp_path,
    qtbot,
):
    from nativmix.audio.manager import PipeWireManager

    config = _make_config(tmp_path)
    config.update_mapping("Spotify", 0)
    config.update_mapping("Spotify", 1)
    config.set_midi_cc(1, 7)
    config.midi_fader_feedback = True
    manager = PipeWireManager(config=config)
    manager.pw_only_mode = True
    manager.effective_routing_owner = "none"
    sibling_updates: list[tuple[int, float]] = []
    manager.channel_volume_changed.connect(
        lambda channel, volume: sibling_updates.append((channel, volume))
    )

    with patch.object(manager, "_apply_volume_by_name_pw_only") as apply_volume:
        manager.set_channel_volume(0, 0.42)

    assert config.get_channel_volume(0) == pytest.approx(0.42)
    assert config.get_channel_volume(1) == pytest.approx(0.42)
    assert manager._poti_volumes[1] == pytest.approx(0.42)
    assert sibling_updates == [(1, pytest.approx(0.42))]
    assert config.get_midi_fader_feedback_targets() == [(1, pytest.approx(0.42))]
    apply_volume.assert_called_once_with("Spotify", 0.42)


def test_feedback_on_applies_sibling_only_apps_without_rewriting_shared_app(
    tmp_path,
):
    from nativmix.audio.manager import PipeWireManager

    config = _make_config(tmp_path)
    config.set_app_names(0, ["Spotify"])
    config.set_app_names(1, ["Spotify", "Firefox"])
    config.midi_fader_feedback = True
    manager = PipeWireManager(config=config)
    manager.pw_only_mode = True
    manager.effective_routing_owner = "none"

    with patch.object(manager, "_apply_volume_by_name_pw_only") as apply_volume:
        manager.set_channel_volume(0, 0.31)

    assert apply_volume.call_count == 2
    apply_volume.assert_any_call("Spotify", 0.31)
    apply_volume.assert_any_call("Firefox", 0.31)


def test_auto_reconnect_routes_duplicate_app_to_deterministic_owner(tmp_path):
    from nativmix.audio.base import StreamInfo
    from nativmix.audio.manager import _AudioListenerThread

    config = _make_config(tmp_path)
    config.update_mapping("Spotify", 0)
    config.update_mapping("Spotify", 1)
    config.set_v_sink_enabled(0, True)
    config.set_v_sink_enabled(1, True)
    listener = _AudioListenerThread(config)
    listener.routing_owner = "nativmix"
    listener.channel_states = {
        0: {"vol": 0.2, "v_sink": True},
        1: {"vol": 0.8, "v_sink": True},
    }
    pulse = MagicMock()
    pulse.get_sink_by_name.return_value = MagicMock()
    info = StreamInfo(index=9, app_name="Spotify", props={"sink_name": "default"})

    with patch("nativmix.audio.manager.move_stream_to_vsink", return_value=False) as move:
        listener._apply_auto_reconnect(pulse, info)

    move.assert_called_once_with(9, "NativMix_CH_0", pulse)


def test_removing_routing_owner_transfers_stream_to_next_duplicate(tmp_path):
    from nativmix.audio.manager import PipeWireManager

    config = _make_config(tmp_path)
    config.update_mapping("Spotify", 0)
    config.update_mapping("Spotify", 1)
    config.set_v_sink_enabled(1, True)
    manager = PipeWireManager(config=config)
    manager.pw_only_mode = False
    manager.effective_routing_owner = "nativmix"
    manager._prev_app_names[0] = ["Spotify"]
    config.remove_app_name(0, "Spotify")

    pulse = MagicMock()
    pulse.server_info.return_value.default_sink_name = "default"
    pulse.get_sink_by_name.return_value = MagicMock(index=100, name="default")
    pulse.sink_list.return_value = [
        MagicMock(index=10, name="NativMix_CH_0"),
        MagicMock(index=11, name="NativMix_CH_1"),
        MagicMock(index=100, name="default"),
    ]
    stream = MagicMock(index=9, sink=100)
    stream.proplist = {
        "application.name": "Spotify",
        "application.process.id": "0",
    }
    pulse.sink_input_list.return_value = [stream]
    pulse_context = MagicMock()
    pulse_context.__enter__.return_value = pulse

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse_context),
        patch("nativmix.audio.manager.move_stream_to_vsink", return_value=False) as move,
    ):
        manager.on_mapping_changed(0, [])

    move.assert_called_once_with(9, "NativMix_CH_1", pulse)


def test_mute_on_duplicate_regular_app_synchronizes_sibling_state(tmp_path):
    from nativmix.audio.manager import PipeWireManager

    config = _make_config(tmp_path)
    config.update_mapping("Spotify", 0)
    config.update_mapping("Spotify", 1)
    manager = PipeWireManager(config=config)
    manager.pw_only_mode = True
    manager.can_set_volume_pw = True
    mute_updates: list[tuple[int, bool]] = []
    manager.mute_state_changed.connect(
        lambda channel, muted: mute_updates.append((channel, muted))
    )

    manager.toggle_mute(1)

    assert manager._channel_muted[0] is True
    assert manager._channel_muted[1] is True
    assert mute_updates == [(0, True), (1, True)]
