from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pulsectl

from nativmix.audio.manager import _AudioListenerThread
from nativmix.utils.config_manager import ConfigManager


def _config(tmp_path):
    return ConfigManager(
        config_path=tmp_path / "config.json",
        profiles_dir=tmp_path / "profiles",
    )


def test_channel_mute_restore_uses_explicit_and_other_apps(tmp_path):
    config = _config(tmp_path)
    config.set_app_names(0, ["Spotify"])
    config.set_app_names(1, ["Other Apps"])
    thread = _AudioListenerThread(config)
    with thread._states_lock:
        thread.channel_states = {
            0: {"muted": False, "apps": ["Spotify"]},
            1: {"muted": True, "apps": ["Other Apps"]},
        }

    assert thread._get_channel_mute_state("Spotify") is False
    assert thread._get_channel_mute_state("Minecraft") is True


def test_late_identity_resolution_is_not_deduplicated(tmp_path):
    config = _config(tmp_path)
    config.set_app_names(0, ["Spotify"])
    thread = _AudioListenerThread(config)
    thread._pulse = MagicMock()
    thread._resolver = MagicMock()
    thread._resolver.sink_input_info.return_value = SimpleNamespace(proplist={})
    thread._known_streams.add(42)
    thread._stream_last_state[42] = (0.5, False, "Unknown")
    with thread._states_lock:
        thread.channel_states = {0: {"muted": True, "apps": ["Spotify"]}}
    info = SimpleNamespace(index=42, volume=0.5, muted=False, app_name="Spotify")
    event = SimpleNamespace(
        facility=pulsectl.PulseEventFacilityEnum.sink_input,
        t=pulsectl.PulseEventTypeEnum.change,
        index=42,
    )

    with (
        patch.object(thread, "_build_stream_info", return_value=info),
        patch.object(thread, "_apply_auto_reconnect") as reconnect,
    ):
        thread._on_event(event)

    reconnect.assert_called_once_with(thread._resolver, info)
    thread._resolver.sink_input_mute.assert_called_once_with(42, mute=True)
    assert thread._stream_last_state[42] == (0.5, False, "Spotify")


def test_stable_identity_metadata_event_is_deduplicated(tmp_path):
    thread = _AudioListenerThread(_config(tmp_path))
    thread._pulse = MagicMock()
    thread._resolver = MagicMock()
    thread._resolver.sink_input_info.return_value = SimpleNamespace(proplist={})
    thread._known_streams.add(42)
    thread._stream_last_state[42] = (0.5, False, "Spotify")
    info = SimpleNamespace(index=42, volume=0.5, muted=False, app_name="Spotify")
    event = SimpleNamespace(
        facility=pulsectl.PulseEventFacilityEnum.sink_input,
        t=pulsectl.PulseEventTypeEnum.change,
        index=42,
    )

    with (
        patch.object(thread, "_build_stream_info", return_value=info),
        patch.object(thread, "_apply_auto_reconnect") as reconnect,
    ):
        thread._on_event(event)

    reconnect.assert_not_called()
