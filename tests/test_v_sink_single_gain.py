"""V-Sink gain ownership must never square a channel fader value."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nativmix.audio.base import StreamInfo
from nativmix.audio.manager import PipeWireManager
from nativmix.utils.config_manager import ConfigManager


def _manager(tmp_path, *, owner: str = "nativmix") -> PipeWireManager:
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    config.set_app_names(0, ["Firefox"])
    config.set_v_sink_enabled(0, True)
    manager = PipeWireManager(config=config)
    manager.routing_owner = owner
    manager.effective_routing_owner = owner
    manager.pw_only_mode = False
    return manager


def test_owned_vsink_gets_gain_and_routed_streams_do_not(tmp_path):
    manager = _manager(tmp_path)
    with (
        patch.object(manager, "_should_apply_volume", return_value=True),
        patch.object(manager, "_set_v_sink_volume") as set_sink,
        patch.object(manager, "_apply_volume_to_streams_outside_sink") as set_external,
        patch.object(manager, "_apply_volume_by_name") as set_all_streams,
    ):
        manager._apply_channel_volume(0, 0.4)

    set_sink.assert_called_once_with(0, 0.4, pulse=None)
    set_external.assert_called_once_with("Firefox", 0.4, "NativMix_CH_0", pulse=None)
    set_all_streams.assert_not_called()


def test_routing_owner_none_preserves_direct_stream_gain(tmp_path):
    manager = _manager(tmp_path, owner="none")
    with (
        patch.object(manager, "_should_apply_volume", return_value=True),
        patch.object(manager, "_set_v_sink_volume") as set_sink,
        patch.object(manager, "_apply_volume_by_name") as set_stream,
    ):
        manager._apply_channel_volume(0, 0.4)

    set_sink.assert_not_called()
    set_stream.assert_called_once_with("Firefox", 0.4, pulse=None)


@pytest.mark.parametrize(("feedback_enabled", "expected_siblings"), [(False, []), (True, [1])])
def test_shared_mapping_feedback_does_not_change_gain_owner(
    tmp_path,
    feedback_enabled,
    expected_siblings,
):
    manager = _manager(tmp_path)
    manager._config.set_app_names(1, ["Firefox"])
    manager._config.midi_fader_feedback = feedback_enabled
    manager._active_streams[1] = StreamInfo(1, "Firefox")
    assert manager._sync_shared_volume(0, 0.4) == expected_siblings
    assert manager._config.find_channel_for_app("Firefox") == 0


def test_pseudo_target_never_uses_vsink_gain(tmp_path):
    manager = _manager(tmp_path)
    manager._config.set_v_sink_enabled(0, False)
    manager._config.set_app_names(0, ["System Master"])
    with (
        patch.object(manager, "_should_apply_volume", return_value=True),
        patch.object(manager, "_set_v_sink_volume") as set_sink,
        patch.object(manager, "_apply_volume_by_name") as set_stream,
    ):
        manager._apply_channel_volume(0, 0.4)
    set_sink.assert_not_called()
    set_stream.assert_called_once_with("System Master", 0.4, pulse=None)


def test_loopback_stream_is_restored_to_unity(tmp_path):
    manager = _manager(tmp_path)
    stream = MagicMock(owner_module=42, index=7)
    pulse = MagicMock()
    pulse.sink_input_list.return_value = [stream]

    manager._unmute_module_streams(42, pulse=pulse)

    pulse.sink_input_mute.assert_called_once_with(7, mute=False)
    pulse.volume_set_all_chans.assert_called_once_with(stream, 1.0)
