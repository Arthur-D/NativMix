from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtDBus import QDBusPendingCall, QDBusVariant
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QLabel

from nativmix.gui.media_binding import MediaBindingButton
from nativmix.hardware.midi import MidiThread
from nativmix.utils import media_control
from nativmix.utils.config_manager import ConfigManager
from nativmix.utils.media_control import PREFIX, MediaController, MediaControlRouter, Player, select_player


class FakeBus:
    """Real Qt completed-call/watchers, without connecting to any bus or players."""

    def __init__(self, players: list[Player]) -> None:
        self.players = {player.service: player for player in players}
        self.messages = []
        self.fail_method = ""

    def asyncCall(self, message, timeout):
        assert timeout == 1000
        assert not message.autoStartService()
        self.messages.append(message)
        method = message.member()
        if method == self.fail_method:
            reply = message.createErrorReply("org.freedesktop.DBus.Error.ServiceUnknown", "Player unavailable")
        elif method == "ListNames":
            reply = message.createReply([list(self.players) + ["org.unrelated.Service"]])
        elif method == "Get":
            player = self.players[message.service()]
            prop = message.arguments()[1]
            value = {"Identity": player.identity, "DesktopEntry": player.desktop_entry,
                     "PlaybackStatus": player.status}[prop]
            reply = message.createReply([QDBusVariant(value)])
        else:
            assert method == "PlayPause"
            reply = message.createReply([])
        return QDBusPendingCall.fromCompletedCall(reply)

    @property
    def toggles(self):
        return [message.service() for message in self.messages if message.member() == "PlayPause"]


@pytest.fixture
def players():
    return [
        Player(PREFIX + "spotify", "Spotify", "com.spotify.Client", "Paused"),
        Player(PREFIX + "firefox.instance1", "Firefox", "org.mozilla.firefox", "Playing"),
    ]


@pytest.fixture
def bus(monkeypatch, players):
    fake = FakeBus(players)
    monkeypatch.setattr(media_control, "QDBusConnection", SimpleNamespace(sessionBus=lambda: fake))
    return fake


def test_select_only_mapped_app_even_if_paused(players):
    assert select_player(players, ["Spotify"], None) == players[0]
    assert select_player(players, ["Not running"], players[1].service) is None
    assert select_player(players, [], None) is None
    assert select_player(players, ["System Master"], None) is None
    assert select_player(players, ["Spot"], None) is None


def test_active_and_multiple_apps_prefer_playing_then_remembered(players):
    assert select_player(players, None, players[0].service) == players[1]
    assert select_player(players, ["Spotify", "Firefox"], None) == players[1]
    paused = [Player(p.service, p.identity, p.desktop_entry, "Paused") for p in players]
    assert select_player(paused, None, players[1].service) == paused[1]
    assert select_player(paused, ["Spotify"], players[1].service) == paused[0]


def test_desktop_alias_and_instance_suffix():
    player = Player(PREFIX + "vlc.instance123", "VLC media player", "vlc", "Paused")
    assert player.matches(["VLC"])
    player = Player(PREFIX + "custom.instance123", "Media player", "com.spotify.Client", "Paused")
    assert player.matches(["Spotify"])


def test_actual_async_qt_calls_control_paused_mapped_player(qtbot, bus):
    controller = MediaController()
    with qtbot.waitSignal(controller.status_changed):
        controller.toggle(0, ["Spotify"])
    assert bus.toggles == [PREFIX + "spotify"]
    assert not controller._watchers
    assert not controller._busy


@pytest.mark.parametrize("apps", [["Missing App"], ["Spot"], []])
def test_unavailable_mapping_never_falls_back_or_launches(qtbot, bus, apps):
    controller = MediaController()
    with qtbot.waitSignal(controller.status_changed):
        controller.toggle(0, apps)
    assert bus.toggles == []


def test_closed_app_is_rediscovered_when_it_returns(qtbot, bus, players):
    controller = MediaController()
    bus.players.pop(PREFIX + "spotify")
    with qtbot.waitSignal(controller.status_changed):
        controller.toggle(0, ["Spotify"])
    assert bus.toggles == []
    bus.players[players[0].service] = players[0]
    with qtbot.waitSignal(controller.status_changed):
        controller.toggle(0, ["Spotify"])
    assert bus.toggles == [players[0].service]


@pytest.mark.parametrize("failed_method", ["ListNames", "Get", "PlayPause"])
def test_disappearing_players_and_bus_errors_do_not_retry_or_fallback(qtbot, bus, failed_method):
    bus.fail_method = failed_method
    controller = MediaController()
    with qtbot.waitSignal(controller.status_changed):
        controller.toggle(0, ["Spotify"])
    assert len(bus.toggles) == (1 if failed_method == "PlayPause" else 0)
    assert PREFIX + "firefox.instance1" not in bus.toggles
    assert not controller._busy


def test_cancel_before_discovery_finishes_prevents_playback(qtbot, bus):
    controller = MediaController()
    controller.toggle(0, ["Spotify"])
    controller.cancel()
    qtbot.wait(10)
    assert bus.toggles == []
    assert not controller._watchers


@pytest.mark.parametrize(("mode", "values", "expected"), [
    ("momentary", [0, 127, 127, 100, 0, 127, 0], 2),
    ("toggle", [127, 127, 0, 0, 127], 3),
    ("toggle", [0, 0, 127], 2),
])
def test_button_edges_and_protocol_channel(qtbot, mode, values, expected):
    midi = MidiThread(input_mode="midi_only")
    midi.update_media_mappings({(7, 20): (0, mode)})
    spy = QSignalSpy(midi.media_toggle_requested)
    midi._handle_cc(0, 20, 127)
    for value in values:
        midi._handle_cc(7, 20, value)
    assert len(spy) == expected
    assert all(row[1] == 0 for row in spy)


def test_sender_never_controls_local_playback_and_reconnect_resets_edges(qtbot):
    midi = MidiThread(input_mode="midi_only", remote_role="send")
    midi.update_media_mappings({(0, 20): (-1, "momentary")})
    spy = QSignalSpy(midi.media_toggle_requested)
    midi._handle_cc(0, 20, 127)
    assert len(spy) == 0
    midi._remote_role = "receive"
    midi._prepare_feedback_connection()
    midi._handle_cc(0, 20, 127, emit_volume=False, emit_learn=False)
    assert len(spy) == 1


def test_binding_persistence_clear_and_malformed_values(tmp_config_path, tmp_profiles_dir):
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    config.set_media_binding(-1, 21, 7, "toggle")
    config.set_media_binding(0, 22, 8, "momentary")
    channel = config.all_channels()[0]
    assert channel["media_binding"]["cc"] == 22
    reloaded = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    assert reloaded.get_media_binding(-1) == {"cc": 21, "midi_channel": 7, "mode": "toggle"}
    config.apply_profile({"channels": [channel]})
    assert config.get_media_binding(0)["cc"] == 22
    config.set_media_binding(0, None, 8, "momentary")
    assert config.get_media_binding(0)["cc"] is None
    config.set_media_binding(-1, 999, 99, "unknown")
    assert config.get_media_binding(-1) == {"cc": None, "midi_channel": 15, "mode": "momentary"}


def test_learn_button_ignores_release_and_captures_channel(qtbot, tmp_config_path, tmp_profiles_dir):
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    button = MediaBindingButton(config)
    qtbot.addWidget(button)
    button.click()
    assert not button.learn(7, 20, 0)
    assert button.learn(7, 20, 127)
    assert config.get_media_binding(-1) == {"cc": 20, "midi_channel": 7, "mode": "momentary"}
    assert not button.isChecked()
    button._change(mode="toggle")
    button.click()
    assert button.learn(2, 30, 0)
    button._change(cc=None)
    assert config.get_media_binding(-1)["cc"] is None


def test_router_authority_stale_events_and_learning(qtbot, bus, tmp_config_path, tmp_profiles_dir):
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    config.input_mode = "midi_only"
    config.set_app_names(0, ["Spotify"])
    config.set_media_binding(0, 20, 0, "momentary")
    midi = MidiThread(input_mode="midi_only")
    button = MediaBindingButton(config)
    status = QLabel()
    qtbot.addWidget(button)
    qtbot.addWidget(status)
    window = SimpleNamespace(
        settings_panel=SimpleNamespace(active_media_button=button, media_status=status), _channels=[]
    )
    router = MediaControlRouter(config, midi, window)
    midi.media_toggle_requested.emit(-1, 0)
    assert bus.messages == []
    button.setChecked(True)
    midi._handle_cc(0, 20, 127)
    assert bus.messages == []
    button.setChecked(False)
    midi._handle_cc(0, 20, 0)
    with qtbot.waitSignal(router.controller.status_changed):
        midi._handle_cc(0, 20, 127)
    assert bus.toggles == [PREFIX + "spotify"]
    config.remote_midi_role = "send"
    midi.media_toggle_requested.emit(midi._current_midi_cc_generation(), 0)
    assert len(bus.toggles) == 1
    router.close()


def test_held_button_does_not_retrigger_after_settings_change(qtbot):
    midi = MidiThread(input_mode="midi_only")
    mappings = {(0, 20): (0, "momentary")}
    midi.update_media_mappings(mappings)
    spy = QSignalSpy(midi.media_toggle_requested)
    midi._handle_cc(0, 20, 127)
    midi.clear_midi_cc_batch()
    midi.update_media_mappings(mappings)
    midi._handle_cc(0, 20, 127)
    assert len(spy) == 1
    midi._handle_cc(0, 20, 0)
    midi._handle_cc(0, 20, 127)
    assert len(spy) == 2


def test_learned_held_button_and_feedback_echo_do_not_toggle(qtbot):
    import time

    midi = MidiThread(input_mode="midi_only")
    midi._handle_cc(0, 20, 127)
    midi.update_media_mappings({(0, 20): (0, "momentary")})
    spy = QSignalSpy(midi.media_toggle_requested)
    midi._handle_cc(0, 20, 127)
    assert len(spy) == 0
    midi._handle_cc(0, 20, 0)
    midi._mute_outbound_suppress_until[(0, 20)] = time.monotonic() + 10
    midi._handle_cc(0, 20, 127)
    assert len(spy) == 0


def test_no_players_and_busy_requests_are_bounded(qtbot, bus):
    controller = MediaController()
    bus.players.clear()
    controller.toggle(-1, None)
    controller.toggle(-1, None)
    assert len(bus.messages) == 1
    qtbot.waitUntil(lambda: not controller._busy)
    assert bus.toggles == []


def test_profile_repair_preserves_and_normalizes_media_bindings():
    from nativmix.utils.profile_manager import normalize_profile_channels

    channels, repaired = normalize_profile_channels([
        {"index": 0, "app_names": ["Spotify"]},
        {"index": 0, "media_binding": {"cc": "20", "midi_channel": 99, "mode": "toggle"}},
    ])
    assert repaired
    assert len(channels) == 1
    assert channels[0]["app_names"] == ["Spotify"]
    assert channels[0]["media_binding"] == {"cc": 20, "midi_channel": 15, "mode": "toggle"}
