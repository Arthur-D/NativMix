from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from nativmix.audio.manager import PipeWireManager
from nativmix.audio.volume_scheduler import LatestVolumeScheduler
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

    class _Scheduler:
        def submit_with_context(self, mappings: list[tuple[int, float, object]]) -> None:
            events.append(("submit", mappings))

    class _Window:
        def on_channel_volume_changed(self, channel: int, volume: float) -> None:
            events.append(("display", (channel, volume)))

    dispatch_remote_volume_batch(_Midi(), _Scheduler(), _Window())

    assert events == [
        ("submit", batch),
        ("display", (2, 0.4)),
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
            assert config.get_channel_volume(0) == pytest.approx(0.1)
            assert confirmations == []
            assert len(io_threads) == 1
            assert io_threads[0] is not qapp.thread()

            scheduler.submit([(0, 0.9)])
            assert config.get_channel_volume(0) == pytest.approx(0.1)
            release.set()
            qtbot.waitUntil(lambda: len(writes) == 2)
            qtbot.waitUntil(lambda: confirmations[-1:] == [(0, pytest.approx(0.9))])
            assert config.get_channel_volume(0) == pytest.approx(0.9)
        finally:
            release.set()
            scheduler.stop()
