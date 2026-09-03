from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot

logger = logging.getLogger(__name__)


class VolumeIntentSource(str, Enum):
    GUI = "gui"
    LOCAL_MIDI = "local_midi"
    REMOTE_MIDI = "remote_midi"


class VolumeMutePolicy(str, Enum):
    ALWAYS = "always"
    MOVEMENT_THRESHOLD = "movement_threshold"


@dataclass(frozen=True)
class VolumeIntent:
    source: VolumeIntentSource
    sequence: int
    generation: int
    key: tuple[int, ...]
    channels: tuple[int, ...]
    channel: int
    volume: float
    origin: object | None
    mute_policy: VolumeMutePolicy

    @property
    def effective_key(self) -> tuple[int, ...]:
        return self.key

    @property
    def siblings(self) -> tuple[int, ...]:
        return tuple(channel for channel in self.channels if channel != self.channel)

    @property
    def requested_value(self) -> float:
        return self.volume

    @property
    def provenance(self) -> object | None:
        return self.origin


@dataclass(frozen=True)
class VolumeWrite:
    generation: int
    channel: int
    volume: float
    key: tuple[int, ...]
    context: object | None = None
    payload: object | None = None


class _VolumeWriteWorker(QObject):
    completed = pyqtSignal(object, float, object)

    def __init__(self, backend: Any) -> None:
        super().__init__()
        scheduled_apply = getattr(backend, "execute_midi_volume_write", None)
        self._apply = scheduled_apply if callable(scheduled_apply) else backend.apply_midi_volumes
        self._uses_prepared_write = callable(scheduled_apply)

    @pyqtSlot(object)
    def execute(self, write: VolumeWrite) -> None:
        started = time.monotonic()
        error: Exception | None = None
        comtypes = None
        try:
            if sys.platform == "win32" and not self._uses_prepared_write:
                import comtypes as comtypes_module

                comtypes = comtypes_module
                comtypes.CoInitialize()
            if self._uses_prepared_write:
                self._apply(write.payload)
            else:
                self._apply([(write.channel, write.volume)])
        except Exception as exc:  # surfaced on the owning Qt thread
            error = exc
        finally:
            if comtypes is not None:
                comtypes.CoUninitialize()
        self.completed.emit(write, time.monotonic() - started, error)


class LatestVolumeScheduler(QObject):
    """Serialize slow backend writes while retaining only each control's newest value."""

    _execute = pyqtSignal(object)
    write_started = pyqtSignal(int, float, object)
    write_completed = pyqtSignal(int, float)
    write_finished = pyqtSignal(object, object)

    def __init__(
        self,
        backend: Any,
        *,
        key_provider: Callable[[int], Iterable[int]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        self._key_provider = key_provider
        self._clock = clock
        self._generation = 0
        self._inflight: VolumeWrite | None = None
        self._pending: dict[tuple[int, ...], VolumeWrite] = {}
        self._order: list[tuple[int, ...]] = []
        self._coalesced = 0
        self._last_diagnostic_at = clock()
        self._thread = QThread(self)
        self._worker = _VolumeWriteWorker(backend)
        self._worker.moveToThread(self._thread)
        self._execute.connect(  # type: ignore[call-arg]
            self._worker.execute,
            Qt.ConnectionType.QueuedConnection,
        )
        self._worker.completed.connect(  # type: ignore[call-arg]
            self._on_completed,
            Qt.ConnectionType.QueuedConnection,
        )
        self._thread.start()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def inflight_count(self) -> int:
        return int(self._inflight is not None)

    @property
    def coalesced_count(self) -> int:
        return self._coalesced

    @pyqtSlot(list)
    def submit(self, mappings: list[tuple[int, float]]) -> None:
        self.submit_with_context(
            (channel, volume, None)
            for channel, volume in mappings
        )

    def submit_with_context(
        self,
        mappings: Iterable[tuple[int, float, object | None]],
    ) -> None:
        for channel, volume, context in mappings:
            key = self._effective_key(int(channel))
            write = VolumeWrite(self._generation, int(channel), float(volume), key, context)
            if self._inflight is not None and self._inflight.key == key:
                if key in self._pending:
                    self._coalesced += 1
                else:
                    self._order.append(key)
                self._pending[key] = write
                continue
            if key in self._pending:
                self._coalesced += 1
                self._pending[key] = write
                continue
            self._pending[key] = write
            self._order.append(key)
        self._dispatch_next()
        self._log_diagnostics()

    @pyqtSlot()
    def reset(self) -> None:
        self._generation += 1
        self._pending.clear()
        self._order.clear()

    def discard_key(self, key: tuple[int, ...]) -> None:
        self._pending.pop(key, None)
        self._order = [pending_key for pending_key in self._order if pending_key != key]

    def stop(self, timeout_ms: int = 14000) -> bool:
        self.reset()
        self._execute.disconnect(self._worker.execute)
        self._thread.quit()
        stopped = self._thread.wait(timeout_ms)
        if not stopped:
            logger.warning("Volume write worker did not stop within %d ms", timeout_ms)
        return stopped

    def _effective_key(self, channel: int) -> tuple[int, ...]:
        if self._key_provider is None:
            return (channel,)
        channels = tuple(sorted({int(item) for item in self._key_provider(channel)}))
        return channels or (channel,)

    def _dispatch_next(self) -> None:
        if self._inflight is not None:
            return
        while self._order:
            key = self._order.pop(0)
            write = self._pending.pop(key, None)
            if write is None or write.generation != self._generation:
                continue
            prepare = getattr(self._backend, "prepare_midi_volume_write", None)
            if callable(prepare):
                mute_policy = getattr(write.context, "mute_policy", VolumeMutePolicy.MOVEMENT_THRESHOLD)
                write = VolumeWrite(
                    write.generation,
                    write.channel,
                    write.volume,
                    write.key,
                    write.context,
                    prepare(write.channel, write.volume, mute_policy),
                )
            self._inflight = write
            # Queue worker execution before notifying semantic observers. Arbitrary
            # main-thread slots therefore cannot delay dispatch of blocking I/O.
            self._execute.emit(write)
            self.write_started.emit(write.channel, write.volume, write.context)
            return

    @pyqtSlot(object, float, object)
    def _on_completed(
        self,
        write: VolumeWrite,
        duration: float,
        error: Exception | None,
    ) -> None:
        if self._inflight == write:
            self._inflight = None
        complete = getattr(self._backend, "complete_midi_volume_write", None)
        if callable(complete) and write.generation == self._generation:
            complete(write.payload, error)
        if error is not None:
            logger.error(
                "Scheduled volume write failed for channels %s: %s",
                write.key,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
        elif write.generation == self._generation:
            self.write_completed.emit(write.channel, write.volume)
        self.write_finished.emit(write.context, error)
        if duration >= 0.1:
            logger.debug(
                "Slow scheduled volume write: channels=%s duration=%.3fs pending=%d coalesced=%d",
                write.key,
                duration,
                len(self._pending),
                self._coalesced,
            )
        self._dispatch_next()

    def _log_diagnostics(self) -> None:
        now = self._clock()
        if self._coalesced and now - self._last_diagnostic_at >= 5.0:
            logger.debug(
                "Volume scheduler coalesced=%d pending=%d inflight=%d",
                self._coalesced,
                len(self._pending),
                int(self._inflight is not None),
            )
            self._last_diagnostic_at = now


class VolumeIntentCoordinator(QObject):
    """Own latest requested volume state and publish only canonical completions."""

    display_requested = pyqtSignal(int, float)
    committed = pyqtSignal(object)
    corrected = pyqtSignal(object)
    external_committed = pyqtSignal(int, float)
    write_started = pyqtSignal(object)

    def __init__(
        self,
        backend: Any,
        config: Any,
        *,
        key_provider: Callable[[int], Iterable[int]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._key_provider = key_provider
        self._clock = clock
        self._generation = 0
        self._sequence = 0
        self._latest: dict[tuple[int, ...], VolumeIntent] = {}
        self._requested: dict[int, float] = {}
        self._confirmed: dict[int, float] = {}
        self._coalesced = 0
        self._last_diagnostic_at = clock()
        self.scheduler = LatestVolumeScheduler(
            backend,
            key_provider=key_provider,
            clock=clock,
            parent=self,
        )
        self.scheduler.write_started.connect(self._on_write_started)
        self.scheduler.write_finished.connect(self._on_write_finished)

    @property
    def pending_count(self) -> int:
        return self.scheduler.pending_count

    @property
    def inflight_count(self) -> int:
        return self.scheduler.inflight_count

    @property
    def coalesced_count(self) -> int:
        return self.scheduler.coalesced_count

    def requested_volume(self, channel: int) -> float:
        return self._requested.get(channel, float(self._config.get_channel_volume(channel)))

    def submit_gui(self, channel: int, volume: float) -> None:
        self.submit(VolumeIntentSource.GUI, channel, volume)

    def submit_local_midi(self, channel: int, volume: float, origin: object | None) -> None:
        self.submit(VolumeIntentSource.LOCAL_MIDI, channel, volume, origin)

    def submit_remote_midi(self, channel: int, volume: float, origin: object | None) -> None:
        self.submit(VolumeIntentSource.REMOTE_MIDI, channel, volume, origin)

    def submit(
        self,
        source: VolumeIntentSource,
        channel: int,
        volume: float,
        origin: object | None = None,
    ) -> None:
        channel = int(channel)
        volume = float(volume)
        if channel < 0 or channel >= int(self._config.num_channels) or not 0.0 <= volume <= 1.0:
            logger.warning(
                "Ignoring invalid volume intent: source=%s channel=%d volume=%r",
                source.value,
                channel,
                volume,
            )
            return
        channels = self._effective_key(channel)
        for sibling in channels:
            self._confirmed.setdefault(sibling, float(self._config.get_channel_volume(sibling)))
        self._sequence += 1
        if channels in self._latest:
            self._coalesced += 1
        intent = VolumeIntent(
            source,
            self._sequence,
            self._generation,
            channels,
            channels,
            channel,
            volume,
            origin,
            VolumeMutePolicy.ALWAYS if source is VolumeIntentSource.GUI else VolumeMutePolicy.MOVEMENT_THRESHOLD,
        )
        self._latest[channels] = intent
        for sibling in channels:
            self._requested[sibling] = volume
            if source is VolumeIntentSource.GUI and sibling == channel:
                continue
            self.display_requested.emit(sibling, volume)
        self.scheduler.submit_with_context([(channel, volume, intent)])
        self._log_diagnostics(intent)

    @pyqtSlot(int, float)
    def observe_external(self, channel: int, volume: float) -> None:
        """Treat genuine backend observations as the newest event without write echo."""
        channel = int(channel)
        volume = float(volume)
        if channel < 0 or channel >= int(self._config.num_channels):
            return
        key = self._effective_key(channel)
        superseded = self._latest.pop(key, None)
        if superseded is not None:
            self.scheduler.discard_key(key)
            logger.debug(
                "External volume superseded local intent: source=%s key=%s sequence=%d",
                superseded.source.value,
                key,
                superseded.sequence,
            )
        for sibling in key:
            self._confirmed[sibling] = volume
            self._requested[sibling] = volume
            self._config.set_channel_volume(sibling, volume)
            self.display_requested.emit(sibling, volume)
        self.external_committed.emit(channel, volume)

    @pyqtSlot()
    def reset(self) -> None:
        requested_channels = tuple(self._requested)
        for channel in requested_channels:
            self.display_requested.emit(channel, float(self._config.get_channel_volume(channel)))
        self._generation += 1
        self._latest.clear()
        self._requested.clear()
        self._confirmed.clear()
        self.scheduler.reset()

    def stop(self, timeout_ms: int = 14000) -> bool:
        return self.scheduler.stop(timeout_ms)

    def _effective_key(self, channel: int) -> tuple[int, ...]:
        if self._key_provider is None:
            return (channel,)
        channels = tuple(sorted({int(item) for item in self._key_provider(channel)}))
        return channels or (channel,)

    @pyqtSlot(int, float, object)
    def _on_write_started(self, _channel: int, _volume: float, context: object | None) -> None:
        if isinstance(context, VolumeIntent):
            self.write_started.emit(context)

    @pyqtSlot(object, object)
    def _on_write_finished(self, context: object | None, error: Exception | None) -> None:
        if not isinstance(context, VolumeIntent) or context.generation != self._generation:
            return
        latest = self._latest.get(context.key)
        if latest is None or latest.sequence != context.sequence:
            logger.debug(
                "Suppressing stale volume completion: source=%s key=%s sequence=%d latest=%s error=%s",
                context.source.value,
                context.key,
                context.sequence,
                latest.sequence if latest is not None else None,
                error is not None,
            )
            return
        self._latest.pop(context.key, None)
        if error is not None:
            for channel in context.channels:
                confirmed = self._confirmed[channel]
                self._requested[channel] = confirmed
                self._config.set_channel_volume(channel, confirmed)
                self.display_requested.emit(channel, confirmed)
            logger.warning(
                "Correcting failed volume intent: source=%s key=%s sequence=%d",
                context.source.value,
                context.key,
                context.sequence,
            )
            self.corrected.emit(context)
            return
        for channel in context.channels:
            self._confirmed[channel] = context.volume
            self._requested[channel] = context.volume
            self._config.set_channel_volume(channel, context.volume)
            self.display_requested.emit(channel, context.volume)
        self.committed.emit(context)

    def _log_diagnostics(self, intent: VolumeIntent) -> None:
        now = self._clock()
        if self._coalesced and now - self._last_diagnostic_at >= 5.0:
            logger.debug(
                "Volume intents source=%s key=%s sequence=%d coalesced=%d pending=%d",
                intent.source.value,
                intent.key,
                intent.sequence,
                self._coalesced,
                self.scheduler.pending_count,
            )
            self._last_diagnostic_at = now
