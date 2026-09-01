"""Easy Effects hold and automatic-route decisions."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from nativmix.audio.base import StreamInfo
from nativmix.audio.easyeffects_hold import (
    is_easyeffects_sink,
    resolve_auto_route_target,
    volume_apply_mode,
)
from nativmix.audio.manager import _AudioListenerThread


def test_only_playback_easyeffects_endpoints_hold_routing():
    assert is_easyeffects_sink("easyeffects_sink")
    assert is_easyeffects_sink("EasyEffects_Sink.2")
    assert not is_easyeffects_sink("easyeffects_source")
    assert not is_easyeffects_sink("NativMix_CH_0")


def test_held_or_manually_paused_stream_is_not_moved():
    common = {
        "vsink_enabled": True,
        "vsink_name": "NativMix_CH_0",
        "default_sink": "alsa_output.default",
    }
    assert resolve_auto_route_target(current_sink="easyeffects_sink", **common) is None
    assert resolve_auto_route_target(current_sink="alsa_output.default", routing_paused=True, **common) is None


def test_unheld_stream_routes_to_owned_sink_or_safe_default():
    assert resolve_auto_route_target(
        current_sink="alsa_output.default",
        vsink_enabled=True,
        vsink_name="NativMix_CH_0",
        default_sink="alsa_output.default",
    ) == "NativMix_CH_0"
    assert resolve_auto_route_target(
        current_sink="NativMix_CH_0",
        vsink_enabled=False,
        vsink_name="NativMix_CH_0",
        default_sink="alsa_output.default",
    ) == "alsa_output.default"


def test_volume_mode_uses_owned_sink_only_after_confirmed_route():
    assert volume_apply_mode(
        current_sink="NativMix_CH_0",
        vsink_enabled=True,
        vsink_name="NativMix_CH_0",
    ) == "vsink"
    assert volume_apply_mode(
        current_sink="easyeffects_sink",
        vsink_enabled=True,
        vsink_name="NativMix_CH_0",
    ) == "stream"


def test_listener_resolves_sink_index_and_leaves_easyeffects_stream_held():
    config = MagicMock()
    config.find_channel_for_app.return_value = 0
    config.get_channel_volume.return_value = 0.4
    config.is_v_sink_enabled.return_value = True
    listener = _AudioListenerThread(config)
    listener.channel_states = {
        0: {
            "vol": 0.4,
            "v_sink": True,
            "routing_paused_apps": [],
        }
    }
    stream = MagicMock(index=7, sink=40)
    pulse = MagicMock()
    pulse.sink_list.return_value = [SimpleNamespace(index=40, name="easyeffects_sink")]
    pulse.sink_input_info.return_value = stream
    info = StreamInfo(
        index=7,
        app_name="Firefox",
        pid=1,
        volume=1.0,
        muted=False,
        props={"sink_index": "40"},
    )

    with patch("nativmix.audio.manager.move_stream_to_vsink") as move:
        listener._apply_auto_reconnect(pulse, info)

    move.assert_not_called()
    pulse.volume_set_all_chans.assert_called_once_with(stream, 0.4)


def test_listener_normalizes_stream_already_on_owned_vsink_to_unity():
    config = MagicMock()
    config.find_channel_for_app.return_value = 0
    config.get_channel_volume.return_value = 0.4
    config.is_v_sink_enabled.return_value = True
    listener = _AudioListenerThread(config)
    listener.channel_states = {0: {"vol": 0.4, "v_sink": True, "routing_paused_apps": []}}
    stream = MagicMock(index=7, sink=50)
    sink = SimpleNamespace(index=50, name="NativMix_CH_0")
    pulse = MagicMock()
    pulse.sink_list.return_value = [sink]
    pulse.get_sink_by_name.return_value = sink
    pulse.sink_input_info.return_value = stream
    info = StreamInfo(index=7, app_name="Firefox", props={"sink_index": "50"})

    listener._apply_auto_reconnect(pulse, info)

    assert pulse.volume_set_all_chans.call_args_list == [
        ((stream, 1.0),),
        ((sink, 0.4),),
    ]
