from unittest.mock import MagicMock, patch

import pulsectl
import pytest

from nativmix.audio.manager import (
    PipeWireManager,
    _AudioListenerThread,
    _matches_app_name,
    _pa_name_fallback,
    _resolve_pa_app_name,
)
from nativmix.audio.pipewire_native import PipeWireNode, _matches_node, _node_identity_name
from nativmix.utils.config_manager import ConfigManager

DISCORD_PROPS = {
    "application.name": "WEBRTC VoiceEngine",
    "application.process.binary": "Discord",
    "application.process.id": "999999",
    "pipewire.access.portal.app_id": "com.discordapp.Discord",
    "media.name": "playStream",
    "node.name": "WEBRTC VoiceEngine",
}


def _config(tmp_path):
    return ConfigManager(
        config_path=tmp_path / "config.json",
        profiles_dir=tmp_path / "profiles",
    )


def _manager(tmp_path):
    manager = PipeWireManager(config=_config(tmp_path))
    manager.can_set_volume = True
    manager.can_set_volume_pw = False
    manager.pw_only_mode = False
    manager._pw_nodes = {}
    manager.unresolved_targets_changed = MagicMock()
    return manager


def _sink_input(index, props):
    sink_input = MagicMock()
    sink_input.index = index
    sink_input.proplist = props
    sink_input.volume.values = [1.0]
    sink_input.mute = False
    return sink_input


def test_discord_exact_metadata_matches_without_proc_or_pw_graph():
    with patch("nativmix.audio.manager.resolve_app_name", side_effect=lambda _pid, fallback: fallback):
        resolved = _resolve_pa_app_name(DISCORD_PROPS)

    assert resolved == "Discord"
    assert _matches_app_name(DISCORD_PROPS, resolved, "Discord")


def test_discord_portal_app_id_is_case_insensitive_and_matches_native_node():
    props = {
        **DISCORD_PROPS,
        "application.process.binary": "chromium",
        "application.id": "org.chromium.Chromium",
        "pipewire.access.portal.app_id": "COM.DISCORDAPP.DISCORD",
    }
    node = PipeWireNode(
        node_id=12,
        client_id=0,
        app_name="WEBRTC VoiceEngine",
        process_binary="chromium",
        media_name="playStream",
        media_class="Stream/Output/Audio",
        app_id="org.chromium.Chromium",
        node_name="WEBRTC VoiceEngine",
        props={
            "application.id": "org.chromium.Chromium",
            "pipewire.access.portal.app_id": "COM.DISCORDAPP.DISCORD",
        },
    )

    with patch("nativmix.audio.manager.resolve_app_name", side_effect=lambda _pid, fallback: fallback):
        resolved = _resolve_pa_app_name(props)

    assert resolved == "Discord"
    assert _matches_app_name(props, resolved, "discord")
    assert not _matches_app_name(props, resolved, "Chromium")
    assert _matches_node(node, "Discord")
    assert not _matches_node(node, "Chromium")
    assert _node_identity_name(node) == "Discord"


def test_unrelated_webrtc_stream_does_not_match_discord():
    props = {
        "application.name": "WEBRTC VoiceEngine",
        "application.process.binary": "chromium",
        "media.name": "playStream",
        "node.name": "WEBRTC VoiceEngine",
    }

    assert not _matches_app_name(props, "WEBRTC VoiceEngine", "Discord")


def test_discord_pulsectl_volume_write_and_unresolved_clear(tmp_path):
    manager = _manager(tmp_path)
    manager._unresolved_targets.add("Discord")
    discord = _sink_input(41, DISCORD_PROPS)
    pulse = MagicMock()
    pulse.sink_input_list.return_value = [discord]

    with patch("nativmix.audio.manager.resolve_app_name", side_effect=lambda _pid, fallback: fallback):
        manager._apply_volume_by_name("Discord", 0.37, pulse=pulse)

    pulse.volume_set_all_chans.assert_called_once_with(discord, 0.37)
    assert "Discord" not in manager._unresolved_targets


def test_discord_pulse_connection_failure_stays_unresolved(tmp_path):
    manager = _manager(tmp_path)
    pulse = MagicMock()
    pulse.sink_input_list.side_effect = pulsectl.PulseError("sink-input-list", 5)

    manager._apply_volume_by_name("Discord", 0.37, pulse=pulse)

    assert "Discord" in manager._unresolved_targets


def test_discord_mute_uses_same_identity_for_shared_target(tmp_path):
    manager = _manager(tmp_path)
    manager.can_set_volume_pw = True
    manager._config.set_app_names(0, ["Discord"])
    manager._config.set_app_names(2, ["Discord"])
    discord = _sink_input(42, DISCORD_PROPS)
    pulse = MagicMock()
    pulse.sink_input_list.return_value = [discord]
    pulse_context = MagicMock()
    pulse_context.__enter__.return_value = pulse

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse_context),
        patch("nativmix.audio.manager.resolve_app_name", side_effect=lambda _pid, fallback: fallback),
    ):
        manager._apply_channel_mute_state(0, True)

    pulse.sink_input_mute.assert_called_once_with(42, mute=True)
    assert manager._channel_muted[0] is True
    assert manager._channel_muted[2] is True


def test_late_stream_restore_resolves_discord_before_channel_lookup(tmp_path):
    config = _config(tmp_path)
    config.set_app_names(0, ["Discord"])
    thread = _AudioListenerThread(config)
    with thread._states_lock:
        thread.channel_states = {0: {"muted": True, "apps": ["Discord"]}}
    discord = _sink_input(43, DISCORD_PROPS)

    with patch("nativmix.audio.manager.resolve_app_name", side_effect=lambda _pid, fallback: fallback):
        info = thread._build_stream_info(discord)

    assert info is not None
    assert info.app_name == "Discord"
    assert thread._find_effective_channel_for_app(info.app_name) == 0
    assert thread._get_channel_mute_state(info.app_name) is True


def test_explicit_discord_is_excluded_from_other_apps(tmp_path):
    manager = _manager(tmp_path)
    manager._config.set_app_names(0, ["Discord"])
    manager._config.set_app_names(1, ["Other Apps"])
    discord = _sink_input(44, DISCORD_PROPS)
    meet = _sink_input(45, {
        "application.name": "WEBRTC VoiceEngine",
        "application.process.binary": "chromium",
        "application.process.id": "0",
        "media.name": "playStream",
    })
    pulse = MagicMock()
    pulse.sink_input_list.return_value = [discord, meet]

    with patch("nativmix.audio.manager.resolve_app_name", side_effect=lambda _pid, fallback: fallback):
        manager._apply_volume_by_name("Other Apps", 0.24, pulse=pulse)

    pulse.volume_set_all_chans.assert_called_once_with(meet, 0.24)


@pytest.mark.parametrize(
    ("props", "expected"),
    [
        ({"application.name": "Chromium", "application.process.binary": "spotify"}, "Spotify"),
        ({
            "application.name": "Chromium",
            "pipewire.access.portal.app_id": "com.spotify.Client",
        }, "Spotify"),
    ],
)
def test_existing_spotify_electron_identity_stays_correct(props, expected):
    with patch("nativmix.audio.manager.resolve_app_name", side_effect=lambda _pid, fallback: fallback):
        assert _resolve_pa_app_name(props) == expected


def test_display_fallback_order_is_unchanged():
    assert _pa_name_fallback(DISCORD_PROPS) == "WEBRTC VoiceEngine"
    assert _pa_name_fallback({
        "application.process.binary": "Discord",
        "media.name": "playStream",
        "node.name": "node",
    }) == "Discord"
    assert _pa_name_fallback({"media.name": "playStream", "node.name": "node"}) == "playStream"
    assert _pa_name_fallback({}) == "Unknown"
