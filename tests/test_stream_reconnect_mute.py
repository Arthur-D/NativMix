"""Unit tests for channel mute restore on stream appearance and metadata changes."""

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


def test_desired_channel_mute_unmapped_is_false(tmp_path) -> None:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    thread = _AudioListenerThread(config=cfg)
    assert thread._desired_channel_mute("UnmappedApp") is False


def test_desired_channel_mute_reads_channel_state(tmp_path) -> None:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    cfg.set_app_names(0, ["Minecraft"])
    thread = _AudioListenerThread(config=cfg)
    with thread._states_lock:
        thread.channel_states = {0: {"vol": 0.4, "muted": True}}
    assert thread._desired_channel_mute("Minecraft") is True
    with thread._states_lock:
        thread.channel_states[0]["muted"] = False
    assert thread._desired_channel_mute("Minecraft") is False


def test_dedupe_state_includes_app_name() -> None:
    """Late identity resolve must change the dedupe key (volume/mute alone is not enough)."""
    a = (0.5, False, "Unknown")
    b = (0.5, False, "Minecraft")
    assert a != b


def test_apply_post_reflex_mute_honors_channel(tmp_path) -> None:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    cfg.set_app_names(0, ["Spotify"])
    thread = _AudioListenerThread(config=cfg)
    with thread._states_lock:
        thread.channel_states = {0: {"muted": True, "apps": ["Spotify"]}}

    pulse = MagicMock()
    info = MagicMock()
    info.index = 42
    info.app_name = "Spotify"

    thread._apply_post_reflex_mute(pulse, info)
    pulse.sink_input_mute.assert_called_once_with(42, mute=True)


def test_other_apps_catch_all_resolves_unassigned(tmp_path) -> None:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    cfg.set_app_names(0, ["Spotify"])
    cfg.set_app_names(1, ["Other Apps"])
    thread = _AudioListenerThread(config=cfg)
    with thread._states_lock:
        thread.channel_states = {
            0: {"vol": 0.8, "muted": False, "apps": ["Spotify"]},
            1: {"vol": 0.3, "muted": True, "apps": ["Other Apps"]},
        }

    assert thread._resolve_target_channel("Spotify") == 0
    assert thread._resolve_target_channel("Minecraft") == 1
    assert thread._desired_channel_mute("Minecraft") is True
    assert thread._resolve_target_channel("System Master") is None


def test_explicit_mapping_beats_other_apps(tmp_path) -> None:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    cfg.set_app_names(0, ["Minecraft"])
    cfg.set_app_names(1, ["Other Apps"])
    thread = _AudioListenerThread(config=cfg)
    with thread._states_lock:
        thread.channel_states = {
            0: {"apps": ["Minecraft"], "muted": False},
            1: {"apps": ["Other Apps"], "muted": True},
        }
    assert thread._resolve_target_channel("Minecraft") == 0
    assert thread._desired_channel_mute("Minecraft") is False
