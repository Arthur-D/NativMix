from __future__ import annotations

from unittest.mock import patch

import mido
import pytest

from nativmix.audio.base import StreamInfo
from nativmix.audio.manager import PipeWireManager
from nativmix.hardware.midi import MidiThread
from nativmix.utils.config_manager import ConfigManager
from nativmix.utils.midi_values import midi_cc_to_volume, volume_to_midi_cc


class _Output:
    def __init__(self) -> None:
        self.messages: list[mido.Message] = []

    def send(self, message: mido.Message) -> None:
        self.messages.append(message)


def _manager(tmp_path, *, remote_role: str = "off") -> tuple[PipeWireManager, ConfigManager]:
    config = ConfigManager(
        config_path=tmp_path / "config.json",
        profiles_dir=tmp_path / "profiles",
    )
    config.num_channels = 16
    config.remote_midi_role = remote_role
    config.set_app_names(5, ["Spotify"])
    config.set_app_names(11, ["Spotify"])
    config.set_midi_cc(5, 6)
    config.set_midi_cc(11, 12)
    config.midi_fader_feedback = remote_role == "off"
    config.set_channel_volume(5, 0.6201)
    config.set_channel_volume(11, 0.627)
    manager = PipeWireManager(config=config)
    manager.pw_only_mode = True
    manager.effective_routing_owner = "none"
    manager._active_streams[42] = StreamInfo(42, "Spotify", volume=0.624)
    manager._poti_volumes.update({5: 0.6201, 11: 0.627})
    return manager, config


def test_duplicate_app_bank_remap_uses_one_canonical_cc_and_suppresses_motor_move(tmp_path) -> None:
    manager, config = _manager(tmp_path)
    targets = manager.get_canonical_midi_feedback_targets([5, 11])

    assert int(0.6201 * 100) == int(0.627 * 100) == 62
    assert targets == [
        (5, pytest.approx(0.6201), "app:spotify"),
        (11, pytest.approx(0.6201), "app:spotify"),
    ]
    assert config.get_channel_volume(5) == config.get_channel_volume(11)
    assert volume_to_midi_cc(targets[0][1]) == volume_to_midi_cc(targets[1][1]) == 79

    midi = MidiThread(input_mode="midi_only")
    midi.set_fader_feedback_enabled(True)
    output = _Output()
    midi.update_mappings({(0, 6): 5})
    midi.request_fader_sync([targets[0]], reason="bank_refresh")
    midi._process_pending_sync(output)
    assert [message.value for message in output.messages] == [79]

    midi.update_mappings({(0, 6): 11})
    midi.request_fader_sync([targets[1]], reason="bank_refresh")
    midi._process_pending_sync(output)
    assert [message.value for message in output.messages] == [79]

    with patch.object(manager, "_apply_volume_by_name_pw_only"):
        manager.set_channel_volume(11, midi_cc_to_volume(80))
    crossed = manager.get_canonical_midi_feedback_targets([11])
    midi.request_fader_sync(crossed, reason="backend_confirmation")
    midi._process_pending_sync(output)
    assert [message.value for message in output.messages] == [79, 80]


def test_unresolved_duplicate_targets_keep_independent_saved_feedback(tmp_path) -> None:
    manager, config = _manager(tmp_path)
    manager._unresolved_targets.add("Spotify")

    targets = manager.get_canonical_midi_feedback_targets([5, 11])

    assert targets == [
        (5, pytest.approx(0.6201), "channel:5"),
        (11, pytest.approx(0.627), "channel:11"),
    ]
    assert config.get_channel_volume(5) == pytest.approx(0.6201)
    assert config.get_channel_volume(11) == pytest.approx(0.627)
    assert volume_to_midi_cc(targets[0][1]) == 79
    assert volume_to_midi_cc(targets[1][1]) == 80


def test_offline_duplicate_targets_fail_closed_before_unresolved_audit(tmp_path) -> None:
    manager, config = _manager(tmp_path)
    manager._active_streams.clear()

    targets = manager.get_canonical_midi_feedback_targets([5, 11])

    assert targets == [
        (5, pytest.approx(0.6201), "channel:5"),
        (11, pytest.approx(0.627), "channel:11"),
    ]
    assert config.get_channel_volume(5) != config.get_channel_volume(11)


def test_reconnect_feedback_uses_restored_shared_runtime_state_over_profile_caches(tmp_path) -> None:
    manager, config = _manager(tmp_path)
    confirmed = midi_cc_to_volume(79)
    manager._poti_volumes.update({5: confirmed, 11: confirmed})
    config.set_channel_volume(5, 0.6201)
    config.set_channel_volume(11, 0.627)

    targets = manager.get_canonical_midi_feedback_targets([5, 11])

    assert targets == [
        (5, pytest.approx(confirmed), "app:spotify"),
        (11, pytest.approx(confirmed), "app:spotify"),
    ]
    assert config.get_channel_volume(5) == config.get_channel_volume(11) == confirmed


def test_profile_change_discards_previous_channel_index_volume_cache(tmp_path) -> None:
    manager, config = _manager(tmp_path)
    manager._poti_volumes[5] = 0.9
    manager._last_applied_volumes[("app", "spotify")] = 0.9

    manager.reset_profile_volume_state()
    config.set_channel_volume(5, 0.3)
    targets = manager.get_canonical_midi_feedback_targets([5])

    assert targets == [(5, pytest.approx(0.3), "app:spotify")]
    assert manager._last_applied_volumes == {}


def test_signal_driven_feedback_observes_new_canonical_value_before_backend_write(tmp_path) -> None:
    manager, _config = _manager(tmp_path)
    manager._last_applied_volumes[("app", "spotify")] = 0.3
    observed: list[tuple[int, float, str]] = []
    manager.channel_volume_changed.connect(
        lambda channel, _volume: observed.extend(manager.get_canonical_midi_feedback_targets([channel]))
    )

    with patch.object(manager, "_apply_volume_by_name_pw_only"):
        manager.set_channel_volume(5, 0.9)

    assert observed == [
        (11, pytest.approx(0.9), "app:spotify"),
        (5, pytest.approx(0.9), "app:spotify"),
    ]


def test_live_output_aliases_share_feedback_and_backend_write_identity(tmp_path) -> None:
    manager, config = _manager(tmp_path)
    config.set_app_names(11, ["spotify-bin"])
    manager._active_streams[42] = StreamInfo(
        42,
        "Spotify",
        volume=0.624,
        props={"application.process.binary": "spotify-bin"},
    )

    targets = manager.get_canonical_midi_feedback_targets([5, 11])
    with patch.object(manager, "_apply_volume_by_name_pw_only") as apply_volume:
        manager.set_channel_volume(11, 0.5)

    assert targets == [
        (5, pytest.approx(0.6201), "app:spotify"),
        (11, pytest.approx(0.6201), "app:spotify"),
    ]
    apply_volume.assert_called_once_with("spotify-bin", 0.5)


def test_receiver_mode_fans_out_exact_shared_target_volume_with_local_feedback_disabled(tmp_path) -> None:
    manager, config = _manager(tmp_path, remote_role="receive")
    updates: list[tuple[int, float]] = []
    manager.channel_volume_changed.connect(lambda channel, volume: updates.append((channel, volume)))

    with patch.object(manager, "_apply_volume_by_name_pw_only") as apply_volume:
        manager.set_channel_volume(11, midi_cc_to_volume(80))

    assert config.get_channel_volume(5) == config.get_channel_volume(11) == midi_cc_to_volume(80)
    assert updates == [(5, pytest.approx(midi_cc_to_volume(80))), (11, pytest.approx(midi_cc_to_volume(80)))]
    apply_volume.assert_called_once_with("Spotify", midi_cc_to_volume(80))


def test_output_alias_feedback_uses_one_canonical_volume(tmp_path) -> None:
    config = ConfigManager(
        config_path=tmp_path / "config.json",
        profiles_dir=tmp_path / "profiles",
    )
    config.set_channel_mode(0, "hardware")
    config.set_hardware_id(0, "sink:alsa_output.current")
    config.set_app_names(1, ["System Master"])
    config.set_midi_cc(0, 6)
    config.set_midi_cc(1, 12)
    config.set_channel_volume(0, 0.6201)
    config.set_channel_volume(1, 0.627)
    manager = PipeWireManager(config=config)
    manager._live_physical_output_sinks = frozenset({"alsa_output.current"})
    manager._effective_default_output_sink = "alsa_output.current"
    manager._poti_volumes.update({0: 0.6201, 1: 0.627})

    targets = manager.get_canonical_midi_feedback_targets([0, 1])

    assert targets == [
        (0, pytest.approx(0.6201), "output_sink:alsa_output.current"),
        (1, pytest.approx(0.6201), "output_sink:alsa_output.current"),
    ]
