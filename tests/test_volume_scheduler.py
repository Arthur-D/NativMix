from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from nativmix.audio.manager import PipeWireManager
from nativmix.audio.volume_scheduler import (
    LatestVolumeScheduler,
    VolumeIntentCoordinator,
    VolumeIntentSource,
    VolumeMutePolicy,
)
from nativmix.gui.main_window import ChannelWidget
from nativmix.gui.mixer_facade import LocalMixerFacade
from nativmix.hardware.midi import MidiThread
from nativmix.main import dispatch_remote_volume_batch
from nativmix.utils.config_manager import ConfigManager


class _BlockedBackend(QObject):
    channel_volume_changed = pyqtSignal(int, float)

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.writes: list[tuple[int, float]] = []
        self.threads: list[QThread] = []

    def apply_midi_volumes(self, mappings: list[tuple[int, float]]) -> None:
        self.threads.append(QThread.currentThread())
        self.writes.extend(mappings)
        self.entered.set()
        assert self.release.wait(5)
        for channel, volume in mappings:
            self.channel_volume_changed.emit(channel, volume)


def test_remote_display_updates_after_scheduler_submission() -> None:
    events: list[tuple[str, object]] = []
    batch = [(2, 0.4, object())]

    class _Midi:
        def take_remote_volume_batch(self) -> list[tuple[int, float, object]]:
            return batch

    class _Coordinator:
        def submit_remote_midi(self, channel: int, volume: float, origin: object) -> None:
            events.append(("submit", (channel, volume, origin)))

    dispatch_remote_volume_batch(_Midi(), _Coordinator())

    assert events == [
        ("submit", batch[0]),
    ]


def test_blocked_backend_keeps_gui_responsive_and_queue_bounded(qtbot, qapp) -> None:
    backend = _BlockedBackend()
    scheduler = LatestVolumeScheduler(backend)
    ticks: list[None] = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(None))
    timer.start(0)
    try:
        scheduler.submit([(0, 0.0)])
        assert backend.entered.wait(1)

        for value in range(1, 100):
            scheduler.submit([(0, value / 100), (1, (100 - value) / 100)])

        qtbot.waitUntil(lambda: bool(ticks))
        assert scheduler.inflight_count == 1
        assert scheduler.pending_count == 2
        assert scheduler.coalesced_count >= 190
        assert backend.writes == [(0, 0.0)]
        assert backend.threads[0] is not qapp.thread()

        backend.release.set()
        qtbot.waitUntil(lambda: len(backend.writes) == 3)
        assert backend.writes == [
            (0, 0.0),
            (0, pytest.approx(0.99)),
            (1, pytest.approx(0.01)),
        ]
    finally:
        backend.release.set()
        scheduler.stop()


def test_alias_group_has_one_inflight_and_one_latest_pending(qtbot) -> None:
    backend = _BlockedBackend()
    scheduler = LatestVolumeScheduler(backend, key_provider=lambda _channel: [0, 1])
    try:
        scheduler.submit([(0, 0.1)])
        assert backend.entered.wait(1)
        scheduler.submit([(1, 0.4), (0, 0.8), (1, 0.6)])

        assert scheduler.pending_count == 1
        assert scheduler.inflight_count == 1

        backend.release.set()
        qtbot.waitUntil(lambda: len(backend.writes) == 2)
        assert backend.writes == [(0, 0.1), (1, pytest.approx(0.6))]
    finally:
        backend.release.set()
        scheduler.stop()


def test_reset_drops_pending_generation_without_replaying_it(qtbot) -> None:
    backend = _BlockedBackend()
    scheduler = LatestVolumeScheduler(backend)
    try:
        scheduler.submit([(0, 0.1)])
        assert backend.entered.wait(1)
        scheduler.submit([(0, 0.9), (1, 0.7)])
        scheduler.reset()

        assert scheduler.pending_count == 0
        backend.release.set()
        qtbot.waitUntil(lambda: scheduler.inflight_count == 0)
        assert backend.writes == [(0, 0.1)]
    finally:
        backend.release.set()
        scheduler.stop()


def test_midi_worker_burst_has_one_qt_notification_and_latest_trailing_write(qtbot) -> None:
    backend = _BlockedBackend()
    scheduler = LatestVolumeScheduler(backend)
    midi = MidiThread(input_mode="midi_only", remote_role="receive")
    continue_burst = threading.Event()
    notifications = 0

    def drain() -> None:
        nonlocal notifications
        notifications += 1
        scheduler.submit_with_context(midi.take_remote_volume_batch())

    midi.remote_volume_batch_ready.connect(drain)

    def produce() -> None:
        midi._queue_remote_volume(0, 0.1, None)
        assert continue_burst.wait(5)
        for value in range(2, 100):
            midi._queue_remote_volume(0, value / 100, None)

    producer = threading.Thread(target=produce)
    producer.start()
    try:
        qtbot.waitUntil(backend.entered.is_set)
        assert backend.writes == [(0, 0.1)]
        continue_burst.set()
        producer.join(1)
        assert not producer.is_alive()
        qtbot.waitUntil(lambda: scheduler.pending_count == 1)

        assert notifications <= 2
        assert len(midi._remote_pending_volumes) <= 1
        assert scheduler.inflight_count == 1
        backend.release.set()
        qtbot.waitUntil(lambda: len(backend.writes) == 2)
        assert backend.writes[-1] == (0, pytest.approx(0.99))
    finally:
        continue_burst.set()
        backend.release.set()
        producer.join(1)
        scheduler.stop()


def test_pipewire_prepares_config_on_main_and_runs_only_io_on_worker(qtbot, qapp, tmp_path) -> None:
    config = ConfigManager(
        config_path=tmp_path / "config.json",
        profiles_dir=tmp_path / "profiles",
    )
    manager = PipeWireManager(config=config)
    manager.pw_only_mode = True
    config.set_app_names(0, ["Spotify"])
    entered = threading.Event()
    release = threading.Event()
    io_threads: list[QThread] = []
    writes: list[tuple[int, tuple[int, ...], float]] = []
    confirmations: list[tuple[int, float]] = []
    manager.channel_volume_changed.connect(lambda channel, volume: confirmations.append((channel, volume)))

    def blocked_write(app_name: str, volume: float, pulse=None) -> None:
        assert app_name == "Spotify"
        assert pulse is None
        io_threads.append(QThread.currentThread())
        writes.append((0, (), volume))
        entered.set()
        assert release.wait(5)

    with patch.object(manager, "_apply_volume_by_name", side_effect=blocked_write):
        scheduler = LatestVolumeScheduler(
            manager,
            key_provider=manager.get_effective_shared_target_channels,
        )
        try:
            scheduler.submit([(0, 0.1)])
            assert entered.wait(1)
            assert config.get_channel_volume(0) == pytest.approx(1.0)
            assert confirmations == []
            assert len(io_threads) == 1
            assert io_threads[0] is not qapp.thread()

            scheduler.submit([(0, 0.9)])
            assert config.get_channel_volume(0) == pytest.approx(1.0)
            release.set()
            qtbot.waitUntil(lambda: len(writes) == 2)
            assert confirmations == []
            assert config.get_channel_volume(0) == pytest.approx(1.0)
        finally:
            release.set()
            scheduler.stop()


class _CoordinatorBackend(QObject):
    channel_volume_changed = pyqtSignal(int, float)

    def __init__(self, aliases: tuple[int, ...] = (0,)) -> None:
        super().__init__()
        self.aliases = aliases
        self.first_entered = threading.Event()
        self.latest_entered = threading.Event()
        self.release_first = threading.Event()
        self.release_latest = threading.Event()
        self.writes: list[tuple[int, float, VolumeMutePolicy]] = []
        self.io_threads: list[QThread] = []
        self.fail_values: set[float] = set()

    def get_effective_shared_target_channels(self, channel: int) -> tuple[int, ...]:
        return self.aliases if channel in self.aliases else (channel,)

    def prepare_midi_volume_write(
        self,
        channel: int,
        volume: float,
        mute_policy: VolumeMutePolicy,
    ) -> tuple[int, float, VolumeMutePolicy]:
        return channel, volume, mute_policy

    def execute_midi_volume_write(
        self,
        payload: tuple[int, float, VolumeMutePolicy],
    ) -> None:
        self.io_threads.append(QThread.currentThread())
        self.writes.append(payload)
        if len(self.writes) == 1:
            self.first_entered.set()
            assert self.release_first.wait(5)
        else:
            self.latest_entered.set()
            assert self.release_latest.wait(5)
        if payload[1] in self.fail_values:
            raise RuntimeError("planned volume failure")

    def complete_midi_volume_write(
        self,
        _payload: tuple[int, float, VolumeMutePolicy],
        _error: Exception | None,
    ) -> None:
        return

    def is_channel_muted(self, _channel: int) -> bool:
        return False

    def toggle_mute(self, _channel: int) -> None:
        return


def _coordinator(
    tmp_path,
    backend: _CoordinatorBackend,
) -> tuple[ConfigManager, VolumeIntentCoordinator]:
    config = ConfigManager(
        config_path=tmp_path / "config.json",
        profiles_dir=tmp_path / "profiles",
    )
    config.set_channel_volume(0, 0.3)
    coordinator = VolumeIntentCoordinator(
        backend,
        config,
        key_provider=backend.get_effective_shared_target_channels,
    )
    backend.channel_volume_changed.connect(coordinator.observe_external)
    return config, coordinator


def test_gui_drag_is_immediate_coalesced_and_never_snaps_on_stale_completion(
    qtbot,
    qapp,
    tmp_path,
) -> None:
    backend = _CoordinatorBackend()
    config, coordinator = _coordinator(tmp_path, backend)
    facade = LocalMixerFacade(config, None, backend)
    widget = ChannelWidget(0, facade, facade)
    qtbot.addWidget(widget)
    facade.volume_intent_requested.connect(coordinator.submit_gui)
    coordinator.display_requested.connect(widget.set_volume)
    commits = []
    coordinator.committed.connect(commits.append)
    timer_fired = threading.Event()
    QTimer.singleShot(0, timer_fired.set)

    try:
        widget._slider.setValue(0)
        assert backend.first_entered.wait(1)
        for value in range(1, 101):
            widget._slider.setValue(value)

        qtbot.waitUntil(timer_fired.is_set)
        assert widget._slider.value() == 100
        assert widget._level_label.text() == "100 %"
        assert coordinator.inflight_count == 1
        assert coordinator.pending_count == 1
        assert coordinator.coalesced_count == 99
        assert config.get_channel_volume(0) == pytest.approx(0.3)
        assert backend.writes == [(0, 0.0, VolumeMutePolicy.ALWAYS)]
        assert backend.io_threads[0] is not qapp.thread()

        backend.release_first.set()
        qtbot.waitUntil(backend.latest_entered.is_set)
        qtbot.waitUntil(lambda: coordinator.inflight_count == 1)
        assert widget._slider.value() == 100
        assert commits == []
        assert config.get_channel_volume(0) == pytest.approx(0.3)

        backend.release_latest.set()
        qtbot.waitUntil(lambda: len(commits) == 1)
        assert backend.writes == [
            (0, 0.0, VolumeMutePolicy.ALWAYS),
            (0, 1.0, VolumeMutePolicy.ALWAYS),
        ]
        assert config.get_channel_volume(0) == pytest.approx(1.0)
    finally:
        backend.release_first.set()
        backend.release_latest.set()
        coordinator.stop()


def test_obsolete_failure_is_suppressed_and_latest_failure_corrects_once(qtbot, tmp_path) -> None:
    backend = _CoordinatorBackend()
    backend.fail_values = {0.1, 0.9}
    config, coordinator = _coordinator(tmp_path, backend)
    displayed: list[float] = []
    corrections = []
    coordinator.display_requested.connect(lambda _channel, volume: displayed.append(volume))
    coordinator.corrected.connect(corrections.append)

    try:
        coordinator.submit_local_midi(0, 0.1, object())
        assert backend.first_entered.wait(1)
        coordinator.submit_local_midi(0, 0.9, object())
        backend.release_first.set()
        qtbot.waitUntil(backend.latest_entered.is_set)
        qtbot.waitUntil(lambda: displayed[-1] == pytest.approx(0.9))
        assert corrections == []
        assert config.get_channel_volume(0) == pytest.approx(0.3)

        backend.release_latest.set()
        qtbot.waitUntil(lambda: len(corrections) == 1)
        assert displayed[-1] == pytest.approx(0.3)
        assert config.get_channel_volume(0) == pytest.approx(0.3)
    finally:
        backend.release_first.set()
        backend.release_latest.set()
        coordinator.stop()


def test_alias_intents_share_one_write_key_and_commit_one_fanout(qtbot, tmp_path) -> None:
    backend = _CoordinatorBackend((0, 1))
    config, coordinator = _coordinator(tmp_path, backend)
    config.set_channel_volume(1, 0.4)
    displayed: list[tuple[int, float]] = []
    commits = []
    coordinator.display_requested.connect(lambda channel, volume: displayed.append((channel, volume)))
    coordinator.committed.connect(commits.append)

    try:
        coordinator.submit_gui(0, 0.2)
        assert backend.first_entered.wait(1)
        coordinator.submit_gui(1, 0.8)
        assert coordinator.pending_count == 1
        assert displayed[-1] == (0, pytest.approx(0.8))

        backend.release_first.set()
        qtbot.waitUntil(backend.latest_entered.is_set)
        assert commits == []
        backend.release_latest.set()
        qtbot.waitUntil(lambda: len(commits) == 1)
        assert [(channel, volume) for channel, volume, _policy in backend.writes] == [
            (0, pytest.approx(0.2)),
            (1, pytest.approx(0.8)),
        ]
        assert config.get_channel_volume(0) == pytest.approx(0.8)
        assert config.get_channel_volume(1) == pytest.approx(0.8)
    finally:
        backend.release_first.set()
        backend.release_latest.set()
        coordinator.stop()


def test_external_volume_wins_pending_race_without_echo_write(qtbot, tmp_path) -> None:
    backend = _CoordinatorBackend()
    config, coordinator = _coordinator(tmp_path, backend)
    external: list[tuple[int, float]] = []
    commits = []
    coordinator.external_committed.connect(lambda channel, volume: external.append((channel, volume)))
    coordinator.committed.connect(commits.append)

    try:
        coordinator.submit(VolumeIntentSource.REMOTE_MIDI, 0, 0.2, object())
        assert backend.first_entered.wait(1)
        coordinator.submit_remote_midi(0, 0.9, object())
        backend.channel_volume_changed.emit(0, 0.55)
        assert config.get_channel_volume(0) == pytest.approx(0.55)
        assert external == [(0, pytest.approx(0.55))]

        backend.release_first.set()
        qtbot.waitUntil(lambda: coordinator.inflight_count == 0)
        assert len(backend.writes) == 1
        assert commits == []
        assert config.get_channel_volume(0) == pytest.approx(0.55)
    finally:
        backend.release_first.set()
        backend.release_latest.set()
        coordinator.stop()


def test_midi_sources_keep_threshold_mute_policy(qtbot, tmp_path) -> None:
    backend = _CoordinatorBackend()
    _config, coordinator = _coordinator(tmp_path, backend)
    try:
        coordinator.submit_local_midi(0, 0.4, object())
        assert backend.first_entered.wait(1)
        backend.release_first.set()
        qtbot.waitUntil(lambda: coordinator.inflight_count == 0)
        assert backend.writes == [(0, 0.4, VolumeMutePolicy.MOVEMENT_THRESHOLD)]
    finally:
        backend.release_first.set()
        backend.release_latest.set()
        coordinator.stop()


def test_reset_restores_optimistic_display_to_confirmed_state(tmp_path) -> None:
    backend = _CoordinatorBackend()
    config, coordinator = _coordinator(tmp_path, backend)
    displayed: list[float] = []
    coordinator.display_requested.connect(lambda _channel, volume: displayed.append(volume))
    try:
        coordinator.submit_local_midi(0, 0.8, object())
        assert backend.first_entered.wait(1)
        assert coordinator.requested_volume(0) == pytest.approx(0.8)

        coordinator.reset()

        assert displayed[-1] == pytest.approx(0.3)
        assert coordinator.requested_volume(0) == pytest.approx(0.3)
        assert config.get_channel_volume(0) == pytest.approx(0.3)
    finally:
        backend.release_first.set()
        backend.release_latest.set()
        coordinator.stop()
