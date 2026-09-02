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


def test_special_mappings_allow_cross_channel_duplicates_but_remain_isolated(tmp_path):
    config = _make_config(tmp_path)
    config.update_mapping("System Master", 0)
    config.update_mapping("System Master", 1)

    with pytest.raises(ValueError, match="Not allowed"):
        config.update_mapping("Spotify", 0)

    config.update_mapping("Other Apps", 2)
    config.update_mapping("Other Apps", 3)
    with pytest.raises(ValueError, match="Not allowed"):
        config.update_mapping("System Master", 2)

    assert config.get_app_names(0) == ["System Master"]
    assert config.get_app_names(1) == ["System Master"]
    assert config.get_app_names(2) == ["Other Apps"]
    assert config.get_app_names(3) == ["Other Apps"]
    assert config.find_channels_for_app("system master") == [0, 1]
    assert config.find_channels_for_app("other apps") == [2, 3]


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


def test_shared_target_channels_match_exact_hardware_identity(tmp_path):
    config = _make_config(tmp_path)
    for channel in range(4):
        config.set_channel_mode(channel, "hardware")
    config.set_hardware_id(0, "sink:device-1")
    config.set_hardware_id(1, "sink:device-1")
    config.set_hardware_id(2, "source:device-1")
    config.set_hardware_id(3, "sink:Device-1")

    assert config.find_channels_for_hardware("sink:device-1") == [0, 1]
    assert config.find_channel_for_hardware("sink:device-1") == 0
    assert config.get_shared_target_channels(0) == [0, 1]
    assert config.get_shared_target_channels(2) == [2]
    assert config.get_shared_target_channels(3) == [3]


def test_duplicate_hardware_target_persists_and_syncs_with_single_write(tmp_path):
    from nativmix.audio.manager import PipeWireManager
    from nativmix.utils.profile_manager import ProfileManager

    config = _make_config(tmp_path)
    for channel in (0, 1):
        config.set_channel_mode(channel, "hardware")
        config.set_hardware_id(channel, "sink:alsa_output.shared")
    config.midi_fader_feedback = True
    manager = PipeWireManager(config=config)
    manager.pw_only_mode = True
    sibling_updates: list[tuple[int, float]] = []
    manager.channel_volume_changed.connect(
        lambda channel, volume: sibling_updates.append((channel, volume))
    )

    with patch.object(manager, "_apply_hardware_volume") as apply_hardware:
        manager.set_channel_volume(0, 0.47)

    assert config.get_channel_volume(0) == pytest.approx(0.47)
    assert config.get_channel_volume(1) == pytest.approx(0.47)
    assert sibling_updates == [(1, pytest.approx(0.47)), (0, pytest.approx(0.47))]
    apply_hardware.assert_called_once_with("sink:alsa_output.shared", 0.47, pulse=None)

    config._persist_active_profile_channels()
    profile = ProfileManager(profiles_dir=tmp_path / "profiles").load(config.active_profile_id)
    assert profile["channels"][0]["hardware_id"] == "sink:alsa_output.shared"
    assert profile["channels"][1]["hardware_id"] == "sink:alsa_output.shared"


def test_duplicate_hardware_feedback_off_uses_last_moved_control(tmp_path):
    from nativmix.audio.manager import PipeWireManager

    config = _make_config(tmp_path)
    for channel in (0, 1):
        config.set_channel_mode(channel, "hardware")
        config.set_hardware_id(channel, "source:shared-input")
    config.midi_fader_feedback = False
    manager = PipeWireManager(config=config)
    manager.pw_only_mode = True

    with patch.object(manager, "_apply_hardware_volume") as apply_hardware:
        manager.apply_poti_volumes([0.4, 0.7])
        apply_hardware.reset_mock()
        manager.apply_poti_volumes([0.2, 0.7])

    apply_hardware.assert_called_once_with("source:shared-input", 0.2, pulse=None)
    assert config.get_channel_volume(0) == pytest.approx(0.2)
    assert config.get_channel_volume(1) == pytest.approx(0.7)


@pytest.mark.parametrize("target", ["System Master", "Other Apps"])
def test_duplicate_pseudo_target_syncs_with_single_backend_write(tmp_path, target):
    from nativmix.audio.manager import PipeWireManager

    config = _make_config(tmp_path)
    config.update_mapping(target, 0)
    config.update_mapping(target, 1)
    config.midi_fader_feedback = True
    manager = PipeWireManager(config=config)
    manager.pw_only_mode = True
    manager.effective_routing_owner = "none"
    sibling_updates: list[tuple[int, float]] = []
    manager.channel_volume_changed.connect(
        lambda channel, volume: sibling_updates.append((channel, volume))
    )

    with patch.object(manager, "_apply_volume_by_name_pw_only") as apply_volume:
        manager.set_channel_volume(0, 0.36)

    assert config.get_channel_volume(0) == pytest.approx(0.36)
    assert config.get_channel_volume(1) == pytest.approx(0.36)
    assert sibling_updates == [(1, pytest.approx(0.36)), (0, pytest.approx(0.36))]
    apply_volume.assert_called_once_with(target, 0.36)


def test_pw_only_other_apps_applies_dynamic_complement_once(tmp_path):
    from nativmix.audio.manager import PipeWireManager
    from nativmix.audio.pipewire_native import PipeWireNode

    config = _make_config(tmp_path)
    config.update_mapping("Spotify", 0)
    config.update_mapping("Other Apps", 1)
    manager = PipeWireManager(config=config)
    manager.pw_only_mode = True
    manager.effective_routing_owner = "none"
    manager._pw_nodes = {
        1: PipeWireNode(
            node_id=1,
            client_id=0,
            app_name="Spotify",
            process_binary="spotify",
            media_name="",
            media_class="Stream/Output/Audio",
            app_id="",
            node_name="spotify",
            props={},
        ),
        2: PipeWireNode(
            node_id=2,
            client_id=0,
            app_name="Firefox",
            process_binary="firefox",
            media_name="",
            media_class="Stream/Output/Audio",
            app_id="",
            node_name="firefox",
            props={},
        ),
    }

    with patch(
        "nativmix.audio.manager._wpctl_set_volume_traced",
        return_value=(True, ["wpctl"], 0, "", ""),
    ) as set_volume:
        manager.set_channel_volume(1, 0.29)

    set_volume.assert_called_once_with(2, 0.29)


def test_hardware_write_dedup_preserves_case_sensitive_identity(tmp_path):
    from nativmix.audio.manager import PipeWireManager

    manager = PipeWireManager(config=_make_config(tmp_path))

    assert manager._should_apply_volume("hardware", "sink:device", 0.5)
    assert manager._should_apply_volume("hardware", "sink:Device", 0.5)


def test_named_hardware_uses_exact_pipewire_node_not_default_alias(tmp_path):
    from nativmix.audio.manager import PipeWireManager
    from nativmix.audio.pipewire_native import PipeWireNode

    manager = PipeWireManager(config=_make_config(tmp_path))
    manager.can_set_volume_pw = True
    manager.pw_only_mode = True
    manager._pw_nodes = {
        12: PipeWireNode(
            node_id=12,
            client_id=0,
            app_name="",
            process_binary="",
            media_name="",
            media_class="Audio/Sink",
            app_id="",
            node_name="alsa_output.Exact",
            props={},
        )
    }

    with (
        patch("nativmix.audio.manager._wpctl_set_volume_exact", return_value=True) as exact,
        patch("nativmix.audio.manager._wpctl_set_volume_default_sink") as default,
    ):
        manager._apply_hardware_volume("sink:alsa_output.Exact", 0.44)

    exact.assert_called_once_with("12", 0.44)
    default.assert_not_called()


def test_wasapi_pseudo_mute_and_new_other_app_inherit_state(tmp_path):
    from nativmix.audio.base import StreamInfo
    from nativmix.audio.wasapi_manager import WasapiManager

    config = _make_config(tmp_path)
    config.update_mapping("Other Apps", 0)
    config.update_mapping("Other Apps", 1)
    manager = WasapiManager(config=config)
    manager._poti_volumes[0] = 0.23
    manager._channel_muted[0] = True
    session = MagicMock()

    with (
        patch("nativmix.audio.wasapi_manager._get_sessions", return_value=[session]),
        patch("nativmix.audio.wasapi_manager._session_name", return_value="Firefox"),
        patch("nativmix.audio.wasapi_manager._set_session_mute") as set_mute,
        patch.object(manager, "_apply_volume_by_name") as apply_volume,
    ):
        manager._apply_mute_by_name("Other Apps", True)
        manager._on_stream_added(StreamInfo(index=7, app_name="Firefox", pid=7))

    assert set_mute.call_count >= 1
    apply_volume.assert_called_once_with("Firefox", 0.23)


def test_wasapi_force_clears_shared_target_write_cache(tmp_path):
    from nativmix.audio.wasapi_manager import WasapiManager

    config = _make_config(tmp_path)
    config.update_mapping("Spotify", 0)
    manager = WasapiManager(config=config)

    with patch.object(manager, "_apply_volume_by_name") as apply_volume:
        manager.apply_poti_volumes([0.5])
        manager.apply_poti_volumes([0.5], force=True)

    assert apply_volume.call_count == 2


def test_unmapping_running_app_reapplies_other_apps_volume_and_mute(tmp_path):
    from nativmix.audio.manager import PipeWireManager

    config = _make_config(tmp_path)
    config.update_mapping("Spotify", 0)
    config.update_mapping("Other Apps", 1)
    manager = PipeWireManager(config=config)
    manager.pw_only_mode = True
    manager._prev_app_names[0] = ["Spotify"]
    manager._poti_volumes[1] = 0.18
    manager._channel_muted[1] = True
    config.remove_app_name(0, "Spotify")

    with (
        patch.object(manager, "_apply_channel_volume") as apply_volume,
        patch.object(manager, "_apply_channel_mute_state") as apply_mute,
    ):
        manager.on_mapping_changed(0, [])

    apply_volume.assert_any_call(1, 0.18)
    apply_mute.assert_called_once_with(1, True, emit=False)


def test_duplicate_hardware_picker_keeps_assigned_target_enabled(tmp_path, qtbot):
    from PyQt6.QtCore import pyqtSignal
    from PyQt6.QtWidgets import QMenu

    from nativmix.audio.base import AudioBackendBase
    from nativmix.gui.main_window import ChannelWidget

    class Backend(AudioBackendBase):
        other_apps_changed = pyqtSignal(list)

        def start(self): pass
        def stop(self): pass
        def get_real_sinks(self): return [("Shared Output", "shared")]
        def get_real_sources(self): return []
        def get_active_streams(self): return []
        def get_unresolved_targets(self): return set()

    config = _make_config(tmp_path)
    for channel in (0, 1):
        config.set_channel_mode(channel, "hardware")
        config.set_hardware_id(channel, "sink:shared")
    widget = ChannelWidget(1, config, Backend())
    qtbot.addWidget(widget)
    captured_actions = []

    def capture_menu(menu, _position):
        captured_actions.extend(menu.actions())

    with patch.object(QMenu, "exec", capture_menu):
        widget._open_hw_picker()

    shared_action = next(action for action in captured_actions if action.text() == "Shared Output")
    assert shared_action.isEnabled()
    assert shared_action.isChecked()


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
    assert sibling_updates == [(1, pytest.approx(0.42)), (0, pytest.approx(0.42))]
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


def test_new_unassigned_stream_uses_duplicate_other_apps_owner_and_mute(tmp_path):
    from nativmix.audio.base import StreamInfo
    from nativmix.audio.manager import _AudioListenerThread

    config = _make_config(tmp_path)
    config.update_mapping("Other Apps", 1)
    config.update_mapping("Other Apps", 2)
    listener = _AudioListenerThread(config)
    listener.channel_states = {
        1: {"vol": 0.27, "v_sink": False, "muted": True},
        2: {"vol": 0.27, "v_sink": False, "muted": True},
    }
    pulse = MagicMock()
    stream = MagicMock()
    pulse.sink_input_info.return_value = stream
    info = StreamInfo(index=8, app_name="Firefox")

    listener._apply_auto_reconnect(pulse, info)

    pulse.volume_set_all_chans.assert_called_once_with(stream, 0.27)
    assert listener._get_channel_mute_state("Firefox") is True


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
