from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot

logger = logging.getLogger(__name__)


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
                write = VolumeWrite(
                    write.generation,
                    write.channel,
                    write.volume,
                    write.key,
                    write.context,
                    prepare(write.channel, write.volume),
                )
            self._inflight = write
            self.write_started.emit(write.channel, write.volume, write.context)
            self._execute.emit(write)
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
