"""
Linux audio backend: PipeWireManager

Listens to PipeWire/PulseAudio sink-input events via pulsectl and manages
per-stream volume control. Runs inside a QThread to keep the GUI responsive.

Key design: Two-Stage Mute-Catch (Rule 11)
-------------------------------------------
Stage 1 – Reflex (on 'new' event):
    When a new sink_input appears, immediately mute it BEFORE trying to
    identify the application. At this point no metadata is available yet.

Stage 2 – Resolution (on 'change' event):
    When the 'change' event fires for the same index, read application.process.id
    and resolve the real application name. Then apply the correct volume from the
    hardware mapping and unmute the stream.

This prevents the "audio blast" caused by new streams starting at 100% volume
before they can be identified and volume-controlled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pulsectl
from PyQt6.QtCore import QThread, pyqtSignal

from nativmix.audio.base import AudioBackendBase, StreamInfo
from nativmix.utils.proc_resolver import resolve_app_name, invalidate_cache
from nativmix.utils.config_manager import ConfigManager

logger = logging.getLogger(__name__)

# Volume applied immediately on 'new' event as a safety floor
_SAFETY_VOLUME: float = 0.0  # fully muted until we know who this stream belongs to


@dataclass
class _PendingStream:
    """Tracks a stream that has been muted but not yet identified."""
    index: int


class _AudioListenerThread(QThread):
    """
    Background thread that subscribes to PulseAudio/PipeWire events.

    Signals
    -------
    stream_added(StreamInfo)
        Emitted when a new stream is fully identified and ready to be mapped.
    stream_removed(int)
        Emitted when a stream has been removed (index is passed).
    stream_changed(StreamInfo)
        Emitted when the volume or mute state of a known stream changes.
    """

    stream_added = pyqtSignal(object)    # StreamInfo
    stream_removed = pyqtSignal(int)     # sink_input index
    stream_changed = pyqtSignal(object)  # StreamInfo
    stream_list_changed = pyqtSignal()   # emitted after add or remove (for GUI refresh)

    def __init__(self, config: ConfigManager, parent: Any = None) -> None:
        super().__init__(parent)
        self._config = config
        self._running = False
        # Streams that have been muted in Stage 1 and await Stage 2 resolution
        self._pending: dict[int, _PendingStream] = {}
        # Last known poti volumes: channel_index → volume float
        self._poti_volumes: dict[int, float] = {}

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main loop running inside the worker thread."""
        self._running = True
        logger.info("AudioListenerThread started")

        try:
            with pulsectl.Pulse("nativmix-listener") as pulse:
                # 1. Fetch currently existing streams to catch anything playing before we started
                try:
                    for si in pulse.sink_input_list():
                        info = self._build_stream_info(si)
                        self.stream_added.emit(info)
                except pulsectl.PulseError as exc:
                    logger.warning("Could not list initial streams: %s", exc)

                # 2. Subscribe to future events
                pulse.event_mask_set("sink_input")
                pulse.event_callback_set(self._on_event)

                logger.info("Listening for PulseAudio sink_input events …")
                # Block and process events in 100ms chunks to allow clean exit mechanism
                while self._running:
                    try:
                        pulse.event_listen(timeout=0.1)
                    except pulsectl.PulseLoopStop:
                        break

            logger.info("AudioListenerThread finished cleanly")

        except pulsectl.PulseError as exc:
            logger.error("PulseAudio connection error: %s", exc)

    def stop(self) -> None:
        """Signal the thread to stop and wait for it to finish."""
        self._running = False
        self.wait()

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def _on_event(self, event: pulsectl.PulseEventInfo) -> None:
        """
        Called by pulsectl for every subscribed event.

        This runs inside the QThread, NOT on the main thread. We interact with
        PipeWire/PulseAudio here and send data to the GUI via pyqtSignal only.
        """
        if event.facility != pulsectl.PulseEventFacilityEnum.sink_input:
            return

        t = event.t  # event type: 'new', 'change', or 'remove'

        logger.debug("Event: %s index=%d", t, event.index)

        if t == pulsectl.PulseEventTypeEnum.new:
            self._handle_new(event.index)
        elif t == pulsectl.PulseEventTypeEnum.change:
            self._handle_change(event.index)
        elif t == pulsectl.PulseEventTypeEnum.remove:
            self._handle_remove(event.index)

    def _handle_new(self, index: int) -> None:
        """
        Stage 1 – Reflex: mute the stream immediately, add to pending queue.

        We do NOT try to read metadata here because PipeWire/PulseAudio may
        not have populated it yet.
        """
        # Re-open a separate, short-lived Pulse connection for the mute operation.
        # The event listener uses the main connection; using it here would
        # cause re-entrant calls on the same PA mainloop context.
        try:
            with pulsectl.Pulse("nativmix-muter") as pulse:
                pulse.sink_input_mute(index, mute=True)
                logger.info("Stage 1: muted new stream index=%d", index)
        except pulsectl.PulseIndexError:
            # Stream already gone before we could mute – very rare race condition
            logger.debug("Stage 1: stream %d already gone on mute attempt", index)
            return
        except pulsectl.PulseError as exc:
            logger.warning("Stage 1: could not mute stream %d: %s", index, exc)
            return

        self._pending[index] = _PendingStream(index=index)

    def _handle_change(self, index: int) -> None:
        """
        Stage 2 – Resolution: identify the stream and apply the correct volume.

        Only processes streams that were previously seen in Stage 1.
        """
        if index not in self._pending:
            # This is a volume/mute change on an already-known stream
            self._emit_stream_update(index)
            return

        # Remove from pending to avoid processing it twice
        del self._pending[index]

        try:
            with pulsectl.Pulse("nativmix-resolver") as pulse:
                # Retrieve the up-to-date sink_input info
                inputs = pulse.sink_input_list()
                target = next((si for si in inputs if si.index == index), None)

                if target is None:
                    logger.debug("Stage 2: stream %d already removed, skipping", index)
                    return

                info = self._build_stream_info(target)
                logger.info(
                    "Stage 2: resolved stream index=%d → app='%s' pid=%d",
                    index,
                    info.app_name,
                    info.pid,
                )

                # Look up the configured poti channel for this app via ConfigManager.
                # If no mapping exists yet, default to 0.5 until the user configures one.
                channel = self._config.find_channel_for_app(info.app_name)
                if channel is not None:
                    target_volume = self._poti_volumes.get(channel, 0.5)
                    logger.debug(
                        "Stage 2: app '%s' → channel %d → volume %.2f",
                        info.app_name, channel, target_volume,
                    )
                else:
                    target_volume = 0.5
                    logger.debug(
                        "Stage 2: no channel mapping for '%s', using default 0.5",
                        info.app_name,
                    )

                pulse.volume_set_all_chans(target, target_volume)
                pulse.sink_input_mute(index, mute=False)

                info.volume = target_volume
                info.muted = False

        except pulsectl.PulseIndexError:
            logger.debug("Stage 2: stream %d vanished before resolution", index)
            return
        except pulsectl.PulseError as exc:
            logger.warning("Stage 2: error resolving stream %d: %s", index, exc)
            return

        self.stream_added.emit(info)

    def _handle_remove(self, index: int) -> None:
        """Notify the GUI that a stream has been removed."""
        self._pending.pop(index, None)  # clean up if still pending
        logger.info("Stream removed index=%d", index)
        self.stream_removed.emit(index)

    def _emit_stream_update(self, index: int) -> None:
        """Emit a stream_changed signal for an already-resolved stream."""
        try:
            with pulsectl.Pulse("nativmix-updater") as pulse:
                inputs = pulse.sink_input_list()
                target = next((si for si in inputs if si.index == index), None)
                if target is None:
                    return
                info = self._build_stream_info(target)
        except pulsectl.PulseError as exc:
            logger.warning("Could not update stream %d: %s", index, exc)
            return

        self.stream_changed.emit(info)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_stream_info(sink_input: pulsectl.PulseSinkInputInfo) -> StreamInfo:
        """
        Convert a pulsectl PulseSinkInputInfo into our StreamInfo dataclass.

        App name resolution order (via proc_resolver):
          1. /proc/<PID>/cmdline → binary name map (fast path for native apps)
          2. --user-data-dir flag → Electron/Chromium profile dir map
          3. --app-id flag → Electron app ID map
          4. Parent-PID traversal (repeat 1–3 for each ancestor process)
          5. application.name / application.process.binary / media.name fallbacks
          6. "Unknown"
        """
        props: dict[str, str] = dict(sink_input.proplist)

        pid_str = props.get("application.process.id", "0")
        try:
            pid = int(pid_str)
        except ValueError:
            pid = 0

        # Determine a pa-level fallback in case /proc is unavailable (e.g. containers)
        pa_fallback = (
            props.get("application.name")
            or props.get("application.process.binary")
            or props.get("media.name")
            or "Unknown"
        )

        # Full /proc-based resolution with Electron/Chromium hack
        app_name = resolve_app_name(pid, fallback=pa_fallback)

        # Retrieve volume: take the average across all channels
        volume_values = sink_input.volume.values
        avg_volume = sum(volume_values) / len(volume_values) if volume_values else 0.0

        return StreamInfo(
            index=sink_input.index,
            app_name=app_name,
            pid=pid,
            volume=avg_volume,
            muted=bool(sink_input.mute),
            props=props,
        )


class PipeWireManager(AudioBackendBase):
    """
    Linux audio backend using pulsectl to control PipeWire/PulseAudio.

    Manages the listener thread and exposes volume/mute control methods
    that are safe to call from the main (GUI) thread.
    """

    def __init__(self, config: ConfigManager | None = None) -> None:
        self._config: ConfigManager = config if config is not None else ConfigManager()
        self._thread: _AudioListenerThread | None = None
        # Latest poti volumes received from ArduinoThread: channel → volume
        self._poti_volumes: dict[int, float] = {}
        # Currently active audio streams, keyed by sink_input index
        self._active_streams: dict[int, StreamInfo] = {}

    # ------------------------------------------------------------------
    # Public stream access (for GUI)
    # ------------------------------------------------------------------

    def get_active_streams(self) -> list[StreamInfo]:
        """
        Return a snapshot of all currently active audio streams.

        Performs a live query against PulseAudio to guarantee we don't miss
        apps that started before NativMix.
        """
        result: list[StreamInfo] = []
        try:
            with pulsectl.Pulse("nativmix-lister") as pulse:
                for si in pulse.sink_input_list():
                    result.append(_AudioListenerThread._build_stream_info(si))
                    # Sync cache just to be safe
                    self._active_streams[si.index] = result[-1]
        except pulsectl.PulseError as exc:
            logger.error("Failed to list active streams: %s", exc)
            # Fallback to cached streams if Pulse is temporarily unreachable
            return list(self._active_streams.values())

        return result

    # ------------------------------------------------------------------
    # Public API (AudioBackendBase)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background audio event listener thread."""
        if self._thread is not None and self._thread.isRunning():
            logger.warning("PipeWireManager.start() called but thread is already running")
            return

        self._thread = _AudioListenerThread(config=self._config)
        self._thread._poti_volumes = self._poti_volumes  # share the dict reference
        self._thread.stream_added.connect(self._on_stream_added)
        self._thread.stream_removed.connect(self._on_stream_removed)
        self._thread.stream_changed.connect(self._on_stream_changed)
        self._thread.start()

        # Live config updates: when the GUI changes a mapping, apply it immediately
        # to all currently active streams without requiring a restart.
        self._config.mapping_changed.connect(self._on_mapping_changed)
        logger.info("PipeWireManager started")

    def stop(self) -> None:
        """Stop the listener thread gracefully."""
        if self._thread is not None:
            self._thread.stop()
            self._thread = None
        logger.info("PipeWireManager stopped")

    def set_volume(self, stream_index: int, volume: float) -> None:
        """
        Set the linear volume [0.0–1.0] for a specific sink input.

        Safe to call from the main thread; opens its own short-lived
        Pulse connection so we don't share state with the listener thread.
        """
        volume = max(0.0, min(1.0, volume))
        try:
            with pulsectl.Pulse("nativmix-volume-setter") as pulse:
                inputs = pulse.sink_input_list()
                target = next((si for si in inputs if si.index == stream_index), None)
                if target is not None:
                    pulse.volume_set_all_chans(target, volume)
        except pulsectl.PulseError as exc:
            logger.error("set_volume(%d, %.2f) failed: %s", stream_index, volume, exc)

    def set_mute(self, stream_index: int, muted: bool) -> None:
        """Toggle the mute state of a specific sink input."""
        try:
            with pulsectl.Pulse("nativmix-mute-setter") as pulse:
                pulse.sink_input_mute(stream_index, mute=muted)
        except pulsectl.PulseError as exc:
            logger.error("set_mute(%d, %s) failed: %s", stream_index, muted, exc)

    # ------------------------------------------------------------------
    # Signal handlers (called on the main/GUI thread by Qt's signal dispatch)
    # ------------------------------------------------------------------

    def _on_stream_added(self, info: StreamInfo) -> None:
        """Slot: track stream."""
        self._active_streams[info.index] = info
        logger.info("Stream added: [%d] %s (pid=%d, vol=%.2f)", info.index, info.app_name, info.pid, info.volume)

    def _on_stream_removed(self, index: int) -> None:
        """Slot: remove stream and clear cache."""
        self._active_streams.pop(index, None)
        invalidate_cache()
        logger.info("Stream removed: [%d]", index)

    def _on_stream_changed(self, info: StreamInfo) -> None:
        """Slot: update cached stream info on change."""
        self._active_streams[info.index] = info
        logger.info("Stream changed: [%d] %s vol=%.2f muted=%s", info.index, info.app_name, info.volume, info.muted)

    def _on_mapping_changed(self, channel_index: int, app_names: list[str]) -> None:
        """
        Slot: called when the GUI updates a channel mapping via ConfigManager.

        Invalidates the PID cache so the new mapping takes effect immediately
        for already running streams without delay.

        Immediately applies the current poti volume to all streams that are
        now associated with *channel_index*.
        """
        # Clear cache so we rescan and pick up the new app assignment instantly
        invalidate_cache()

        current_volume = self._poti_volumes.get(channel_index, 0.5)
        logger.info(
            "Mapping changed: channel %d → %s (applying vol=%.2f)",
            channel_index, app_names, current_volume,
        )
        for name in app_names:
            self._apply_volume_by_name(name, current_volume)

    def apply_poti_volumes(self, volumes: list[float]) -> None:
        """
        Receive a new set of poti volumes from the ArduinoThread and apply
        them to all currently-active mapped streams.

        Called from the main thread (via a signal connection to ArduinoThread.
        volumes_changed).

        Args:
            volumes: One float per channel, in [0.0, 1.0].
        """
        for channel, volume in enumerate(volumes):
            self._poti_volumes[channel] = volume
            # Also propagate via the shared dict to the listener thread
            # (used in Stage 2 when a new stream is identified mid-session)
            if self._thread is not None:
                self._thread._poti_volumes[channel] = volume

            # Apply to all currently-active streams mapped to this channel
            app_names = self._config.get_app_names(channel)
            for name in app_names:
                self._apply_volume_by_name(name, volume)

    def _apply_volume_by_name(self, app_name: str, volume: float) -> None:
        """
        Set the volume of all active streams whose resolved name matches *app_name*.

        Opens a short-lived Pulse connection on the main thread.
        """
        try:
            with pulsectl.Pulse("nativmix-poti-apply") as pulse:
                for si in pulse.sink_input_list():
                    props = dict(si.proplist)
                    pid_str = props.get("application.process.id", "0")
                    try:
                        pid = int(pid_str)
                    except ValueError:
                        pid = 0
                    pa_fallback = (
                        props.get("application.name")
                        or props.get("application.process.binary")
                        or "Unknown"
                    )
                    resolved = resolve_app_name(pid, fallback=pa_fallback)
                    if resolved.lower() == app_name.lower():
                        pulse.volume_set_all_chans(si, volume)
        except pulsectl.PulseError as exc:
            logger.error("apply_volume_by_name('%s', %.2f) failed: %s", app_name, volume, exc)

    def _on_stream_changed(self, info: StreamInfo) -> None:
        """
        Slot: called when a known stream's properties (volume/mute) changed.

        TODO (Phase 2): Update the GUI and re-sync hardware poti position.
        """
        logger.info("Stream changed: [%d] %s vol=%.2f muted=%s", info.index, info.app_name, info.volume, info.muted)
