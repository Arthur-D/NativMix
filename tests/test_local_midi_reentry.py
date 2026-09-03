from __future__ import annotations

import json
import threading
from pathlib import Path

import mido
import pytest
from PyQt6.QtCore import QObject, QSettings, Qt, pyqtSignal, pyqtSlot

from nativmix.audio.base import AudioBackendBase
from nativmix.audio.volume_scheduler import LatestVolumeScheduler
from nativmix.gui import main_window, settings_panel
from nativmix.gui.main_window import MainWindow
from nativmix.hardware.midi import LocalControllerOrigin, MidiThread
from nativmix.remote_sync.authority import ReceiverMixerAuthority
from nativmix.utils.config_manager import ConfigManager
from nativmix.utils.profile_manager import ProfileManager


class _Output:
    def __init__(self) -> None:
        self.messages: list[mido.Message] = []

    def send(self, message: mido.Message) -> None:
        self.messages.append(message)


class _BlockedBackend(AudioBackendBase):
    channel_volume_changed = pyqtSignal(int, float)
    mute_state_changed = pyqtSignal(int, bool)
    other_apps_changed = pyqtSignal(list)
    unresolved_targets_changed = pyqtSignal(set)
    status_changed = pyqtSignal(str, str)
    capability_changed = pyqtSignal(str, bool)

    gain_control_supported = True
    v_sink_supported = True
    v_sink_capability_reason = ""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.midi_writes: list[tuple[int, float]] = []
        self.gui_writes: list[tuple[int, float]] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def get_real_sinks(self) -> list:
        return []

    def get_real_sources(self) -> list:
        return []

    def get_active_streams(self) -> list:
        return []

    def get_unresolved_targets(self) -> set:
        return set()

    def get_default_sink_name(self) -> None:
        return None

    def get_effective_shared_target_channels(self, channel_index: int) -> list[int]:
        return [channel_index]

    def apply_midi_volumes(self, mappings: list[tuple[int, float]]) -> None:
        self.midi_writes.extend(mappings)
        if len(self.midi_writes) == 1:
            self.entered.set()
            assert self.release.wait(5)
        for channel, volume in mappings:
            self.channel_volume_changed.emit(channel, volume)

    def set_channel_volume(self, channel_index: int, volume: float) -> None:
        self.gui_writes.append((channel_index, volume))
        self.channel_volume_changed.emit(channel_index, volume)

    def is_channel_muted(self, channel_index: int) -> bool:
        return False

    def toggle_mute(self, channel_index: int) -> None:
        self.mute_state_changed.emit(channel_index, True)


class _LocalMidiWiring(QObject):
    def __init__(
        self,
        midi: MidiThread,
        scheduler: LatestVolumeScheduler,
        window: MainWindow,
        authority: ReceiverMixerAuthority,
    ) -> None:
        super().__init__()
        self._midi = midi
        self._scheduler = scheduler
        self._window = window
        self._authority = authority
        self.feedback_events: list[tuple[int, float]] = []

    @pyqtSlot(object)
    def submit_local_volume(self, request: tuple[int, float, object]) -> None:
        channel, volume, origin = request
        self._scheduler.submit_with_context([(channel, volume, origin)])

    @pyqtSlot(list)
    def display_midi_volumes(self, mappings: list[tuple[int, float]]) -> None:
        for channel, volume in mappings:
            self._window.on_channel_volume_changed(channel, volume)

    @pyqtSlot(int, float, object)
    def note_write_started(self, channel: int, volume: float, origin: object | None) -> None:
        if origin is not None and (
            not isinstance(origin, LocalControllerOrigin) or self._midi.is_current_local_origin(origin)
        ):
            self._authority.note_remote_controller_origin(origin)
        self._window.on_channel_volume_changed(channel, volume)

    @pyqtSlot(int, float)
    def send_canonical_feedback(self, channel: int, volume: float) -> None:
        suppressed, preloads = self._authority.controller_feedback_directive(channel, volume)
        self.feedback_events.append((channel, volume))
        self._midi.request_fader_sync(
            [(channel, volume)],
            suppressed_bindings=suppressed,
            preload_values=preloads,
            reason="controller_origin" if suppressed else "canonical",
        )


def _config(
    tmp_path: Path,
    *,
    app_names: list[str],
) -> tuple[ConfigManager, ProfileManager]:
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    profile = {
        "id": "profile-1",
        "name": "Profile 1",
        "channel_count": 1,
        "restore_fader_positions": False,
        "midi_switch_cc": None,
        "channels": [
            {
                "index": 0,
                "label": None,
                "is_midi": True,
                "midi_cc": 7,
                "midi_mute_cc": None,
                "midi_channel": 0,
                "midi_mute_channel": 0,
                "inverted": False,
                "v_sink": False,
                "mode": "app",
                "app_names": app_names,
                "hardware_id": None,
                "volume": 0.0,
            }
        ],
    }
    (profiles_dir / "profile-1.json").write_text(json.dumps(profile))
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 7,
                "active_profile": profile["id"],
                "hardware": {
                    "num_channels": 0,
                    "input_mode": "midi_only",
                    "midi_channel_count": 1,
                },
                "settings": {
                    "compact_mode": False,
                    "midi_fader_feedback": True,
                    "show_invert_option": False,
                    "stay_open": True,
                    "transparency": False,
                },
            }
        )
    )
    config = ConfigManager(config_path=config_path, profiles_dir=profiles_dir)
    profiles = ProfileManager(profiles_dir=profiles_dir)
    profiles.set_active_silently(profile["id"])
    config.apply_profile(profile)
    return config, profiles


@pytest.mark.parametrize("app_names", [[], ["Spotify"]], ids=["midi-only", "single-app"])
def test_local_midi_burst_has_no_generic_reentry_or_motor_replay(
    app_names: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot,
) -> None:
    config, profiles = _config(tmp_path, app_names=app_names)
    backend = _BlockedBackend()
    midi = MidiThread(device_name="Controller", input_mode="midi_only")
    midi._active_generation = 1
    midi.update_mappings({(0, 7): 0})
    midi.set_fader_feedback_enabled(True)
    scheduler = LatestVolumeScheduler(backend)
    authority = ReceiverMixerAuthority(config, profiles, backend, inventory_provider=[])
    monkeypatch.setattr(settings_panel, "update_checks_supported", lambda: True)
    monkeypatch.setattr(settings_panel, "_real_ports", lambda: [])
    monkeypatch.setattr(
        main_window,
        "QSettings",
        lambda *_args: QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat),
    )
    window = MainWindow(
        config=config,
        backend=backend,
        midi_thread=midi,
        profile_manager=profiles,
    )
    qtbot.addWidget(window)
    window.show()
    wiring = _LocalMidiWiring(midi, scheduler, window, authority)
    midi.local_volume_requested.connect(wiring.submit_local_volume)
    midi.midi_volumes_changed.connect(wiring.display_midi_volumes)
    midi.midi_cc_received.connect(window.on_midi_cc_received)
    scheduler.write_started.connect(wiring.note_write_started)
    backend.channel_volume_changed.connect(window.on_channel_volume_changed)
    backend.channel_volume_changed.connect(wiring.send_canonical_feedback)
    output = _Output()
    raw_values = list(range(32, 97))

    def emit_learn_burst() -> None:
        for value in raw_values:
            midi.midi_cc_received.emit(0, 7, value)

    learn_producer = threading.Thread(target=emit_learn_burst)
    learn_producer.start()
    learn_producer.join(1)
    assert not learn_producer.is_alive()
    assert backend.midi_writes == []
    assert backend.gui_writes == []
    qtbot.wait(0)
    assert backend.midi_writes == []
    assert backend.gui_writes == []

    def emit_origin_aware_burst() -> None:
        for value in raw_values:
            midi._handle_cc(0, 7, value, throttle_volume=False, emit_learn=False)

    volume_producer = threading.Thread(target=emit_origin_aware_burst)
    volume_producer.start()
    volume_producer.join(1)
    assert not volume_producer.is_alive()
    assert backend.midi_writes == []

    try:
        qtbot.waitUntil(backend.entered.is_set)
        assert scheduler.inflight_count == 1
        assert scheduler.pending_count == 1
        assert scheduler.coalesced_count == len(raw_values) - 2
        assert backend.midi_writes == [(0, pytest.approx(raw_values[0] / 127))]

        backend.release.set()
        qtbot.waitUntil(lambda: len(backend.midi_writes) == 2)
        qtbot.waitUntil(lambda: len(wiring.feedback_events) == 2)
        assert backend.midi_writes == [
            (0, pytest.approx(raw_values[0] / 127)),
            (0, pytest.approx(raw_values[-1] / 127)),
        ]
        assert window._channels[0]._slider.value() == int(raw_values[-1] / 127 * 100)

        midi._process_pending_sync(output)
        midi._process_pending_sync(output)
        assert output.messages == []

        feedback_before_slider = len(wiring.feedback_events)
        window._channels[0]._slider.setFocus()
        qtbot.keyClick(window._channels[0]._slider, Qt.Key.Key_Up)
        qtbot.waitUntil(lambda: len(wiring.feedback_events) == feedback_before_slider + 1)
        midi._process_pending_sync(output)
        assert len(backend.gui_writes) == 1
        assert len(output.messages) == 1

        feedback_before_external = len(wiring.feedback_events)
        backend.channel_volume_changed.emit(0, 0.4)
        qtbot.waitUntil(lambda: len(wiring.feedback_events) == feedback_before_external + 1)
        midi._process_pending_sync(output)
        assert len(output.messages) == 2
        assert output.messages[-1].value == 51

        window._channels[0].set_edit_mode(True)
        window._channels[0].start_volume_learn()
        midi.midi_cc_received.emit(3, 12, 100)
        qtbot.waitUntil(lambda: config.get_midi_cc(0) == 12)
        assert config.get_midi_channel(0) == 3
    finally:
        backend.release.set()
        scheduler.stop()
