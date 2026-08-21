"""Focused tests for protocol MIDI channel bindings and feedback."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import make_profile, write_profile

from nativmix.hardware.midi import MidiThread
from nativmix.utils.config_manager import ConfigManager
from nativmix.utils.profile_manager import ProfileManager


def _write_config(path, profile: dict) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 7,
                "active_profile": profile["id"],
                "hardware": {
                    "num_channels": 0,
                    "input_mode": "hybrid",
                    "midi_channel_count": profile["channel_count"],
                },
                "settings": {"midi_fader_feedback": False},
            }
        )
    )


def test_same_cc_on_different_protocol_channels(tmp_config_path, tmp_profiles_dir) -> None:
    profile = make_profile(channel_count=2)
    for index, midi_channel in enumerate((0, 7)):
        profile["channels"][index].update(
            {"is_midi": True, "midi_cc": 11, "midi_channel": midi_channel}
        )
    write_profile(tmp_profiles_dir, profile)
    _write_config(tmp_config_path, profile)

    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    config.apply_profile(profile)
    assert config.get_all_midi_mappings() == {(0, 11): 0, (7, 11): 1}

    thread = MidiThread(input_mode="midi_only")
    thread.update_mappings(config.get_all_midi_mappings())
    received: list[tuple[int, float]] = []
    thread.midi_volumes_changed.connect(received.extend)
    thread._handle_cc(0, 11, 32)
    thread._handle_cc(7, 11, 96)
    assert received == [(0, pytest.approx(32 / 127)), (1, pytest.approx(96 / 127))]


def test_legacy_bindings_default_to_protocol_channel_zero(
    tmp_config_path,
    tmp_profiles_dir,
) -> None:
    profile = make_profile(channel_count=1)
    profile["channels"][0].update({"is_midi": True, "midi_cc": 9, "midi_mute_cc": 10})
    write_profile(tmp_profiles_dir, profile)
    _write_config(tmp_config_path, profile)

    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    config.apply_profile(profile)
    assert config.get_midi_channel(0) == 0
    assert config.get_midi_mute_channel(0) == 0
    assert config.get_all_midi_mappings() == {(0, 9): 0}
    assert config.get_all_midi_mute_mappings() == {(0, 10): 0}


def test_malformed_and_multi_slot_bindings_normalize_deterministically(
    tmp_config_path,
    tmp_profiles_dir,
) -> None:
    profile = make_profile(channel_count=1)
    profile["channels"][0].update(
        {
            "is_midi": True,
            "midi_cc": 99,
            "midi_channel": 4,
            "midi_bindings": [
                {"cc": "bad", "midi_channel": 99},
                {"cc": 12, "midi_channel": 3},
            ],
            "midi_mute_cc": 200,
            "midi_mute_channel": "bad",
        }
    )
    write_profile(tmp_profiles_dir, profile)
    _write_config(tmp_config_path, profile)

    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    config.apply_profile(profile)
    channel = config.all_channels()[0]
    assert channel["midi_bindings"] == [{"cc": 99, "midi_channel": 4}]
    assert channel["midi_cc"] == 99
    assert channel["midi_channel"] == 4
    assert channel["midi_mute_cc"] is None
    assert channel["midi_mute_channel"] == 0


class _OutputPort:
    def __init__(self) -> None:
        self.messages = []

    def send(self, message) -> None:
        self.messages.append(message)


def test_outbound_fader_uses_configured_protocol_channel() -> None:
    thread = MidiThread(input_mode="midi_only")
    thread.set_fader_feedback_enabled(True)
    thread.update_mappings({(6, 20): 2})
    thread._queue_fader_sync([(2, 0.5)])
    output = _OutputPort()
    thread._process_pending_sync(output)
    assert [(message.channel, message.control, message.value) for message in output.messages] == [
        (6, 20, 64)
    ]


def test_mute_feedback_queue_led_mapping_and_echo_suppression() -> None:
    thread = MidiThread(input_mode="midi_only")
    thread.set_fader_feedback_enabled(True)
    thread.update_mute_mappings({(4, 5): 1})
    thread._queue_mute_feedback([(1, True)])
    output = _OutputPort()
    toggles: list[int] = []
    thread.midi_mute_toggled.connect(toggles.append)

    thread._process_pending_mute_feedback(output)
    assert [(message.channel, message.control, message.value) for message in output.messages] == [
        (4, 5, 127),
        (4, 32, 0),
    ]
    thread._handle_cc(4, 5, 127)
    assert toggles == []
    thread._queue_mute_feedback([(1, False)])
    thread._process_pending_mute_feedback(output)
    assert [(message.control, message.value) for message in output.messages[-2:]] == [
        (5, 0),
        (32, 42),
    ]


def test_feedback_queues_coalesce_shared_channel_updates() -> None:
    thread = MidiThread(input_mode="midi_only")
    thread.set_fader_feedback_enabled(True)

    thread._queue_fader_sync([(1, 0.25)])
    thread._queue_fader_sync([(2, 0.5), (1, 0.75)])
    thread._queue_mute_feedback([(1, True)])
    thread._queue_mute_feedback([(2, False), (1, False)])

    assert dict(thread._pending_sync or []) == {1: 0.75, 2: 0.5}
    assert dict(thread._pending_mute_feedback or []) == {1: False, 2: False}


def test_failed_feedback_send_is_not_cached() -> None:
    thread = MidiThread(input_mode="midi_only")

    class _FailingPort:
        def send(self, _message) -> None:
            raise OSError("disconnected")

    with pytest.raises(OSError, match="disconnected"):
        thread._send_raw_cc(_FailingPort(), 4, 42, 127)

    assert (4, 42) not in thread._last_sent_cc_value


def test_feedback_delivery_cache_is_cleared_for_new_connection() -> None:
    thread = MidiThread(input_mode="midi_only")
    thread._last_sent_cc_value[(4, 42)] = 127
    thread._feedback_takeover[(4, 42)] = 1.0
    thread._mute_outbound_suppress_until[(4, 42)] = 10.0

    thread._prepare_feedback_connection()

    assert thread._last_sent_cc_value == {}
    assert thread._feedback_takeover == {}
    assert thread._mute_outbound_suppress_until == {}


def test_profile_round_trip_preserves_independent_protocol_channels(tmp_profiles_dir) -> None:
    manager = ProfileManager(profiles_dir=tmp_profiles_dir)
    profile_id = manager.create("MIDI", channel_count=1)
    profile = manager.load(profile_id)
    profile["channels"][0].update(
        {
            "is_midi": True,
            "midi_cc": 21,
            "midi_channel": 2,
            "midi_bindings": [{"cc": 21, "midi_channel": 2}],
            "midi_mute_cc": 22,
            "midi_mute_channel": 9,
        }
    )
    manager.save_profile(profile)

    reloaded = manager.load(profile_id)["channels"][0]
    assert reloaded["midi_cc"] == 21
    assert reloaded["midi_channel"] == 2
    assert reloaded["midi_mute_cc"] == 22
    assert reloaded["midi_mute_channel"] == 9
