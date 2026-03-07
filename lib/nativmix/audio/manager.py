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

---------------------------------------------------------------------------
Setup & Installation Info (CachyOS / Arch Linux)
---------------------------------------------------------------------------
To test this module or NativMix locally, ensure you have the required 
packages installed. `pulsectl` is strictly required for PipeWire routing.

Using python-venv:
  python -m venv .venv
  source .venv/bin/activate
  pip install pulsectl PyQt6 pyserial setproctitle

Using Native Arch Linux packages (system-wide testing):
  sudo pacman -S python-pulsectl python-pyqt6 python-pyserial python-setproctitle

Note: NativMix installs flawlessly on CachyOS using the provided
`./install.sh` script, which automatically relies on PEP-517 and `requirements.txt`.
---------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
import subprocess
from typing import Any

import pulsectl
from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot

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
    master_sink_changed = pyqtSignal(float, bool)

    def __init__(self, config: ConfigManager, parent: Any = None) -> None:
        super().__init__(parent)
        self._config = config
        self._running = False
        # Thread-safe dictionary to detach from GUI / Config updates
        # Format: { channel_index: {'vol': float, 'v_sink': bool, 'apps': list[str]} }
        self.channel_states: dict[int, dict] = {}
        # Track streams muted by the reflex stage
        self._reflex_muted: set[int] = set()
        # Track streams we have already emitted `stream_added` for
        self._known_streams: set[int] = set()

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
                pulse.event_mask_set("sink_input", "sink")
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
        Runs inside QThread.
        """
        try:
            if event.facility == pulsectl.PulseEventFacilityEnum.sink_input:
                if event.t == pulsectl.PulseEventTypeEnum.new:
                    # Stage 1: Reflex - Mute immediately before resolving (Rule 11)
                    with pulsectl.Pulse("nativmix-reflex") as pulse_reflex:
                        try:
                            stream = pulse_reflex.sink_input_info(event.index)
                            app_name = stream.proplist.get("application.name", "") or stream.proplist.get("media.name", "")
                            if "Loopback" in app_name or "dummy" in app_name.lower() or "speech-dispatcher" in app_name.lower():
                                pass
                            else:
                                pulse_reflex.sink_input_mute(event.index, mute=True)
                                self._reflex_muted.add(event.index)
                        except pulsectl.PulseError:
                            pass
                            
                elif event.t == pulsectl.PulseEventTypeEnum.change:
                    # Stage 2: Resolution - Wait for metadata to resolve
                    with pulsectl.Pulse("nativmix-resolver-change") as pulse_listen:
                        try:
                            stream = pulse_listen.sink_input_info(event.index)
                        except pulsectl.PulseIndexError:
                            return # Stream already gone
                            
                        props = dict(stream.proplist)
                        
                        pid_str = props.get("application.process.id", "0")
                        try:
                            pid = int(pid_str)
                        except ValueError:
                            pid = 0

                        pa_fallback = (
                            props.get("application.name")
                            or props.get("application.process.binary")
                            or props.get("media.name")
                            or "Unknown"
                        )

                        app_name = resolve_app_name(pid, fallback=pa_fallback)
                        
                        # 1. Check: Is app_name part of a NativMix channel?
                        target_ch = None
                        target_vol = 0.5
                        v_sink_active = False
                        
                        for ch_idx, state in self.channel_states.items():
                            # If a channel is in hardware mode, we do NOT touch sink_inputs.
                            if state.get("mode", "app") == "hardware":
                                continue

                            apps = state.get("apps", [])
                            if app_name.lower() in [a.lower() for a in apps]:
                                target_ch = ch_idx
                                target_vol = state.get("vol", 0.5)
                                v_sink_active = state.get("v_sink", False)
                                break
                                
                        # 2. If yes:
                        if target_ch is not None:
                            current_vol = sum(stream.volume.values) / len(stream.volume.values) if stream.volume.values else 0.0
                            
                            if v_sink_active:
                                # IF channel has V-Sink ON → move stream to V-Sink.
                                # Move BEFORE unmute so the reflex-unmute below
                                # clears the cork on the correct (new) sink.
                                try:
                                    v_sink = pulse_listen.get_sink_by_name(f"NativMix_CH_{target_ch}")
                                    if stream.sink != v_sink.index:
                                        subprocess.run(
                                            ["pactl", "move-sink-input", str(stream.index), str(v_sink.index)],
                                            capture_output=True,
                                        )
                                        logger.debug("Routed '%s' to V-Sink CH_%d", app_name, target_ch)
                                        # Re-fetch the stream after the move so volume is set on new sink
                                        try:
                                            stream = pulse_listen.sink_input_info(stream.index)
                                        except pulsectl.PulseError:
                                            pass
                                    # Always enforce unity gain inside V-Sink
                                    if abs(sum(stream.volume.values) / max(len(stream.volume.values), 1) - 1.0) > 0.02:
                                        pulse_listen.volume_set_all_chans(stream, 1.0)
                                except pulsectl.PulseError:
                                    pass
                            else:
                                # ELSE -> pulse_listen.volume_set_all_chans(stream, gespeicherte_volume)
                                if abs(current_vol - target_vol) >= 0.02:
                                    pulse_listen.volume_set_all_chans(stream, target_vol)
                        
                        # Reflex Unmute
                        if event.index in self._reflex_muted:
                            try:
                                pulse_listen.sink_input_mute(event.index, mute=False)
                            except pulsectl.PulseError:
                                pass
                            self._reflex_muted.remove(event.index)
                            
                        # Also notify the GUI about the visual update
                        info = self._build_stream_info(stream)
                        self.stream_changed.emit(info)
                        
                        if event.index not in self._known_streams:
                            self._known_streams.add(event.index)
                            self.stream_added.emit(info)

                elif event.t == pulsectl.PulseEventTypeEnum.remove:
                    if event.index in self._known_streams:
                        self._known_streams.remove(event.index)
                    if event.index in self._reflex_muted:
                        self._reflex_muted.remove(event.index)
                    self._handle_remove(event.index)
                
        except Exception as e:
            print(f"[Listener Error] {e}")

    def _handle_remove(self, index: int) -> None:
        """Notify the GUI that a stream has been removed."""
        self.stream_removed.emit(index)

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

    mute_state_changed = pyqtSignal(int, bool)
    channel_volume_changed = pyqtSignal(int, float)

    def __init__(self, config: ConfigManager | None = None, parent=None) -> None:
        super().__init__(parent)
        self._config: ConfigManager = config if config is not None else ConfigManager()
        self._thread: _AudioListenerThread | None = None
        # Latest poti volumes received from ArduinoThread: channel → volume
        self._poti_volumes: dict[int, float] = {}
        # Currently active audio streams, keyed by sink_input index
        self._active_streams: dict[int, StreamInfo] = {}
        # Stores the explicit mute state per channel (from IPC hotkeys)
        self._channel_muted: dict[int, bool] = {}
        # Stores the physical slider volume at the moment the channel was muted
        self._muted_at_volume: dict[int, float] = {}
        # Previous app name lists per channel, used to diff add/remove events
        self._prev_app_names: dict[int, list[str]] = {}

    # ------------------------------------------------------------------
    # Public stream access (for GUI)
    # ------------------------------------------------------------------

    def get_active_streams(self) -> list[StreamInfo]:
        """
        Return a snapshot of all currently active audio streams.

        Performs a live query against PulseAudio to guarantee we don't miss
        apps that started before NativMix. V-Sink loopbacks are strictly filtered out.
        """
        result: list[StreamInfo] = []
        try:
            with pulsectl.Pulse("nativmix-lister") as pulse:
                loopback_module_ids = {m.index for m in pulse.module_list() if m.name == "module-loopback"}

                for si in pulse.sink_input_list():
                    # Strict filtering: filter out NativMix V-Sink loopbacks
                    if si.owner_module in loopback_module_ids:
                        continue
                    
                    app_name = si.proplist.get("application.name", "") or si.proplist.get("media.name", "")
                    if "Loopback" in app_name or "speech-dispatcher" in app_name.lower() or "dummy" in app_name.lower():
                        continue

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
    # ------------------------------------------------------------------        self._update_thread_states()

    def _update_thread_states(self) -> None:
        """Pushes current config/volumes to the Listener thread safely."""
        if not self._thread:
            return
            
        states = {}
        for ch in range(self._config.num_channels):
            states[ch] = {
                'vol': self._poti_volumes.get(ch, 0.5),
                'v_sink': self._config.is_v_sink_enabled(ch),
                'apps': self._config.get_app_names(ch),
                'mode': self._config.get_channel_mode(ch)
            }
        self._thread.channel_states = states

    def start(self) -> None:
        """Start the background audio event listener thread."""
        if self._thread is not None and self._thread.isRunning():
            logger.warning("PipeWireManager.start() called but thread is already running")
            return

        # Pre-populate _prev_app_names so the first mapping change doesn't
        # incorrectly treat all configured apps as "newly added".
        for ch in range(self._config.num_channels):
            self._prev_app_names[ch] = list(self._config.get_app_names(ch))

        self._thread = _AudioListenerThread(config=self._config)
        self._thread.stream_added.connect(self._on_stream_added)
        self._thread.stream_removed.connect(self._on_stream_removed)
        self._thread.stream_changed.connect(self._on_stream_changed)
        self._thread.master_sink_changed.connect(self._on_master_sink_changed)
        self._thread.start()
        self._update_thread_states() # Initial push of states

        # Audit and fix loopbacks / apps routing (replaces _adopt_existing_v_sinks)
        self.perform_initial_audio_audit()
        logger.info("PipeWireManager started")

    def stop(self) -> None:
        """Stop the listener thread gracefully."""
        if self._thread is not None:
            self._thread.stop()
            self._thread = None
        logger.info("PipeWireManager stopped")

    def perform_initial_audio_audit(self) -> None:
        """
        1. Auto-Correction on Startup: Check all running apps and route them.
        2. Sink-to-Device Verification: Ensure all V-Sinks have valid loopbacks.
        """
        logger.info("Performing initial audio audit...")
        try:
            with pulsectl.Pulse("nativmix-audit") as pulse:
                # Verification of V-Sinks and Loopbacks
                loopbacks = [m for m in pulse.module_list() if m.name == "module-loopback"]
                for ch in range(self._config.num_channels):
                    if self._config.is_v_sink_enabled(ch):
                        sink_name = f"NativMix_CH_{ch}"
                        try:
                            pulse.get_sink_by_name(sink_name)
                        except pulsectl.PulseError:
                            self.enable_v_sink(ch)
                            continue
                        
                        has_loopback = False
                        for m in loopbacks:
                            if m.argument and f"source={sink_name}.monitor" in m.argument:
                                has_loopback = True
                                break
                                
                        if not has_loopback:
                            logger.info("Missing loopback for %s, re-establishing...", sink_name)
                            try:
                                subprocess.run(
                                    ["pactl", "load-module", "module-loopback", f"source={sink_name}.monitor"],
                                    check=True,
                                    capture_output=True
                                )
                            except subprocess.CalledProcessError as e:
                                logger.warning("Re-establishing loopback failed: %s", e.stderr)
                                
        except pulsectl.PulseError as exc:
            logger.error("Initial audio audit failed (V-Sink verification): %s", exc)
            
        # Run standard sync to correct any misrouted applications
        self._sync_v_sink_routing()

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
        logger.debug("Stream added: [%d] %s (pid=%d, vol=%.2f)", info.index, info.app_name, info.pid, info.volume)

    def _on_stream_removed(self, index: int) -> None:
        """Slot: remove stream and clear cache."""
        self._active_streams.pop(index, None)
        invalidate_cache()
        logger.debug("Stream removed: [%d]", index)

    def _on_stream_changed(self, info: StreamInfo) -> None:
        """Slot: update cached stream info on change."""
        self._active_streams[info.index] = info
        logger.debug("Stream changed: [%d] %s vol=%.2f muted=%s", info.index, info.app_name, info.volume, info.muted)

    def _on_mapping_changed(self, channel_index: int, app_names: list[str]) -> None:
        """
        Slot: called when the GUI updates a channel mapping via ConfigManager.

        Invalidates the PID cache so the new mapping takes effect immediately
        for already running streams without delay.
        """
        old_names: set[str] = {n.lower() for n in self._prev_app_names.get(channel_index, [])}
        new_names: set[str] = {n.lower() for n in app_names}
        
        removed = old_names - new_names
        added = new_names - old_names
        
        # Store current state for next diff
        self._prev_app_names[channel_index] = list(app_names)
        
        # Clear cache so we rescan and pick up the new app assignment instantly
        invalidate_cache()

        current_volume = self._poti_volumes.get(channel_index, 0.5)
        logger.debug(
            "Mapping changed: channel %d → %s (applying vol=%.2f)",
            channel_index, app_names, current_volume,
        )
        
        # ── CRITICAL: update thread state FIRST ──────────────────────────────
        # The listener thread reacts to PipeWire change events.
        # If we move a stream first and update thread state later, the listener
        # sees the old mapping (app still mapped to V-Sink channel) on the
        # change event triggered by our own move, and immediately moves the
        # stream BACK into the V-Sink.  Pushing the new state before the move
        # prevents this race condition.
        self._update_thread_states()

        v_sink_enabled = self._config.is_v_sink_enabled(channel_index)
        sink_name = f"NativMix_CH_{channel_index}"
        # Handle explicitly removed apps: evacuate from any NativMix sink
        if removed:
            try:
                with pulsectl.Pulse("nativmix-evac-removed") as pulse:
                    default_sink_name = pulse.server_info().default_sink_name
                    try:
                        default_sink = pulse.get_sink_by_name(default_sink_name)
                    except pulsectl.PulseError:
                        default_sink = None

                    # Safety: never evacuate back into another NativMix V-Sink
                    if default_sink and default_sink.name.startswith("NativMix_"):
                        for s in pulse.sink_list():
                            if not s.name.startswith("NativMix_") and "dummy" not in s.name.lower():
                                default_sink = s
                                break

                    if default_sink:
                        # Scan ALL sinks (not just the current V-Sink) – the
                        # app may still be in *any* NativMix virtual sink.
                        nativmix_sink_indices = {
                            s.index for s in pulse.sink_list()
                            if s.name.startswith("NativMix_")
                        }
                        for si in pulse.sink_input_list():
                            if si.sink not in nativmix_sink_indices:
                                continue  # Not in any V-Sink, nothing to do

                            props = dict(si.proplist)
                            pid_str = props.get("application.process.id", "0")
                            try:
                                pid = int(pid_str)
                            except ValueError:
                                pid = 0
                            pa_fallback = props.get("application.name") or props.get("application.process.binary") or "Unknown"
                            resolved = resolve_app_name(pid, fallback=pa_fallback)
                            if resolved.lower() not in removed:
                                continue

                            logger.info(
                                "App '%s' removed from CH%d – evacuating to '%s'",
                                resolved, channel_index, default_sink.name
                            )

                            # _seamless_move: volume on old sink → pactl move → unmute
                            try:
                                pulse.volume_set_all_chans(si, current_volume)
                            except pulsectl.PulseError:
                                pass
                            self._seamless_move(pulse, si.index, default_sink.index, volume=None)
                            logger.info(
                                "Stream %d moved back to Main Sink (vol=%.2f).",
                                si.index, current_volume
                            )
            except pulsectl.PulseError as exc:
                logger.error("Failed to evacuate removed apps from V-Sink %s: %s", sink_name, exc)



                
        # Handle explicitly added apps: if V-Sink is on, route them into it
        if v_sink_enabled and added:
            try:
                with pulsectl.Pulse("nativmix-route-added") as pulse:
                    try:
                        target_sink = pulse.get_sink_by_name(sink_name)
                    except pulsectl.PulseError:
                        target_sink = None
                        
                    if target_sink:
                        for si in pulse.sink_input_list():
                            if si.sink == target_sink.index:
                                continue  # Already in the V-Sink
                            props = dict(si.proplist)
                            pid_str = props.get("application.process.id", "0")
                            try:
                                pid = int(pid_str)
                            except ValueError:
                                pid = 0
                            pa_fallback = props.get("application.name") or props.get("application.process.binary") or "Unknown"
                            resolved = resolve_app_name(pid, fallback=pa_fallback)
                            if resolved.lower() not in added:
                                continue

                            logger.info(
                                "App '%s' added to CH%d – routing into V-Sink '%s'",
                                resolved, channel_index, sink_name
                            )
                            # Move via pactl first, then set Unity Gain.
                            # Setting volume before the move would affect the old sink.
                            result = subprocess.run(
                                ["pactl", "move-sink-input", str(si.index), str(target_sink.index)],
                                capture_output=True
                            )
                            if result.returncode != 0:
                                logger.warning(
                                    "pactl move-sink-input to V-Sink failed (rc=%d): %s",
                                    result.returncode, result.stderr.decode(errors="replace").strip()
                                )

                            # Apply unity gain AFTER the stream is on the V-Sink
                            try:
                                si_fresh = next(
                                    (s for s in pulse.sink_input_list() if s.index == si.index),
                                    None
                                )
                                if si_fresh is not None:
                                    pulse.volume_set_all_chans(si_fresh, 1.0)
                            except pulsectl.PulseError:
                                pass
            except pulsectl.PulseError as exc:
                logger.error("Failed to route added apps into V-Sink %s: %s", sink_name, exc)


        # Apply volumes for still-mapped apps (apps that were neither added nor removed)
        for name in app_names:
            self._apply_volume_by_name(name, current_volume)

        # Do NOT call _sync_v_sink_routing() here: it would re-process every
        # stream and may double-move or un-cork streams that are mid-transition.
        self._update_thread_states()

    def _sync_v_sink_routing(self) -> None:
        """
        Scan all active sink_inputs.
        If an app is in an active V-Sink channel, ensure it is routed there.
        If an app is in a V-Sink but no longer mapped to a V-Sink channel, evacuate it to default.
        """
        try:
            with pulsectl.Pulse("nativmix-vsink-sync") as pulse:
                default_sink_name = pulse.server_info().default_sink_name
                try:
                    default_sink = pulse.get_sink_by_name(default_sink_name)
                except pulsectl.PulseError:
                    return

                # Map of configured V-Sink channels to their sink index (if they exist)
                v_sinks: dict[int, int] = {} 
                for ch in range(self._config.num_channels):
                    if self._config.is_v_sink_enabled(ch):
                        try:
                            s = pulse.get_sink_by_name(f"NativMix_CH_{ch}")
                            v_sinks[ch] = s.index
                        except pulsectl.PulseError:
                            pass
                
                # Active V-Sink indices
                active_v_sink_indices = set(v_sinks.values())
                
                for si in pulse.sink_input_list():
                    props = dict(si.proplist)
                    pid_str = props.get("application.process.id", "0")
                    try:
                        pid = int(pid_str)
                    except ValueError:
                        pid = 0
                    pa_fallback = props.get("application.name") or props.get("application.process.binary") or "Unknown"
                    resolved = resolve_app_name(pid, fallback=pa_fallback)
                    
                    target_ch = self._config.find_channel_for_app(resolved)
                    # Ignore channels in hardware mode
                    if target_ch is not None and self._config.get_channel_mode(target_ch) == "hardware":
                        target_ch = None
                        
                    # Case A: App mapped to a V-Sink channel
                    if target_ch is not None and target_ch in v_sinks:
                        target_sink_index = v_sinks[target_ch]
                        if si.sink != target_sink_index:
                            logger.debug("Routing %s (idx: %d) into V-Sink CH_%d", resolved, si.index, target_ch)
                            # Move then unmute (PipeWire may cork during move)
                            self._seamless_move(pulse, si.index, target_sink_index, volume=1.0)

                    # Case B: App is in a V-Sink but shouldn't be
                    elif si.sink in active_v_sink_indices:
                        # Unmapped or mapped to a non-vsink channel → evacuate to default sink
                        logger.debug("Evacuating %s (idx: %d) out of V-Sink to Default", resolved, si.index)
                        vol = self._poti_volumes.get(target_ch, 0.5) if target_ch is not None else 0.5
                        self._seamless_move(pulse, si.index, default_sink.index, volume=vol)
                            
        except pulsectl.PulseError as exc:
            logger.error("V-Sink Routing Sync failed: %s", exc)

    @pyqtSlot(list)
    def apply_poti_volumes(self, volumes: list[float]) -> None:
        """
        Called when the Arduino pushed new raw hardware sliding values.
        Opens a single PulseAudio connection shared across all channels for the tick.
        """
        try:
            with pulsectl.Pulse("nativmix-poti-tick") as shared_pulse:
                for channel, volume in enumerate(volumes):
                    # Auto-unmute if the hardware slider moves significantly (>5% since muted)
                    if self._channel_muted.get(channel, False):
                        muted_vol = self._muted_at_volume.get(channel, volume)
                        if abs(volume - muted_vol) > 0.05:
                            self.toggle_mute(channel)
                            # Update reference temporarily so we don't spam toggle_mute
                            self._muted_at_volume[channel] = volume

                    self._poti_volumes[channel] = volume

                    mode = self._config.get_channel_mode(channel)
                    if mode == "hardware":
                        hw_id = self._config.get_hardware_id(channel)
                        if hw_id:
                            self._apply_hardware_volume(hw_id, volume)
                    else:
                        if self._config.is_v_sink_enabled(channel):
                            self._set_v_sink_volume(channel, volume, pulse=shared_pulse)
                        else:
                            app_names = self._config.get_app_names(channel)
                            for name in app_names:
                                self._apply_volume_by_name(name, volume, pulse=shared_pulse)
        except pulsectl.PulseError as exc:
            logger.error("apply_poti_volumes: PulseAudio connection lost: %s", exc)
                    
        self._update_thread_states()

    def set_channel_volume(self, channel_index: int, volume: float) -> None:
        """Called directly by the GUI slider to override volume."""
        if channel_index < 0 or channel_index >= self._config.num_channels:
            return
            
        self._poti_volumes[channel_index] = volume
            
        # GUI slide -> auto unmute
        if self._channel_muted.get(channel_index, False):
            self.toggle_mute(channel_index)

        mode = self._config.get_channel_mode(channel_index)
        
        if mode == "hardware":
            hw_id = self._config.get_hardware_id(channel_index)
            if hw_id:
                self._apply_hardware_volume(hw_id, volume)
        else:
            if self._config.is_v_sink_enabled(channel_index):
                self._set_v_sink_volume(channel_index, volume) # Slider can open its own connection
            else:
                app_names = self._config.get_app_names(channel_index)
                for name in app_names:
                    self._apply_volume_by_name(name, volume)
                
        self._update_thread_states()

    def _set_v_sink_volume(self, channel_index: int, volume: float, pulse: pulsectl.Pulse | None = None) -> None:
        """
        Set the hardware volume for a virtual sink directly.
        This ensures apps stay at 100% (Unity Gain) relative to the sink.
        """
        sink_name = f"NativMix_CH_{channel_index}"
        
        def _do_apply(p: pulsectl.Pulse) -> None:
            try:
                sink = p.get_sink_by_name(sink_name)
                p.volume_set_all_chans(sink, volume)
            except pulsectl.PulseError:
                return # V-sink might not exist yet

        try:
            if pulse is not None:
                _do_apply(pulse)
            else:
                with pulsectl.Pulse("nativmix-vsink-vol") as p:
                    _do_apply(p)
        except pulsectl.PulseError as exc:
            logger.error("Failed to apply V-Sink volume for CH %d: %s", channel_index, exc)

    def _seamless_move(
        self,
        pulse: pulsectl.Pulse,
        stream_index: int,
        target_sink_index: int,
        volume: float | None = None,
    ) -> None:
        """
        Move a sink-input to a new sink without stopping playback.

        Sequence:
          1. pactl move-sink-input  (most reliable PipeWire PA-layer move)
          2. sink_input_mute(False) (clear any cork PipeWire set during the move)
          3. optional: re-fetch stream and set volume on the new sink
        """
        result = subprocess.run(
            ["pactl", "move-sink-input", str(stream_index), str(target_sink_index)],
            capture_output=True,
        )
        if result.returncode != 0:
            logger.warning(
                "_seamless_move: pactl failed (rc=%d): %s",
                result.returncode,
                result.stderr.decode(errors="replace").strip(),
            )

        # PipeWire may cork the stream during the sink switch → explicitly unmute.
        try:
            pulse.sink_input_mute(stream_index, False)
        except pulsectl.PulseError:
            pass

        if volume is not None:
            try:
                si_fresh = next(
                    (s for s in pulse.sink_input_list() if s.index == stream_index),
                    None,
                )
                if si_fresh is not None:
                    pulse.volume_set_all_chans(si_fresh, volume)
            except pulsectl.PulseError:
                pass

    def _apply_volume_by_name(self, app_name: str, volume: float, pulse: pulsectl.Pulse | None = None) -> None:
        """
        Set the volume of all active streams matching app_name.
        Only called when V-Sink is INACTIVE for this channel.
        Accepts an optional shared Pulse connection to avoid repeated reconnects.
        """
        def _do_apply(p: pulsectl.Pulse) -> None:
            if app_name.lower() == "system master":
                default_sink_name = p.server_info().default_sink_name
                sink = p.get_sink_by_name(default_sink_name)
                p.volume_set_all_chans(sink, volume)
                return

            for si in p.sink_input_list():
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
                    p.volume_set_all_chans(si, volume)

        try:
            if pulse is not None:
                _do_apply(pulse)
            else:
                with pulsectl.Pulse("nativmix-poti-apply") as p:
                    _do_apply(p)
        except pulsectl.PulseError as exc:
            logger.error("apply_volume_by_name('%s', %.2f) failed: %s", app_name, volume, exc)

    def _apply_hardware_volume(self, hw_id: str, volume: float) -> None:
        """Apply hardware volume directly to a specific sink or source."""
        try:
            parts = hw_id.split(':', 1)
            if len(parts) != 2:
                return
            kind, name = parts
            
            with pulsectl.Pulse("nativmix-hw-vol") as pulse:
                if kind == "sink":
                    dev = pulse.get_sink_by_name(name)
                    pulse.volume_set_all_chans(dev, volume)
                elif kind == "source":
                    dev = pulse.get_source_by_name(name)
                    pulse.volume_set_all_chans(dev, volume)
        except pulsectl.PulseError as exc:
            logger.error("Failed to apply hardware volume to %s: %s", hw_id, exc)

    def toggle_mute(self, channel_index: int) -> None:
        """
        Toggle the mute state of an entire channel (all apps assigned to it).
        Called by the CLI IPC server.
        """
        if channel_index < 0 or channel_index >= self._config.num_channels:
            logger.warning("toggle_mute requested for invalid channel %d", channel_index)
            return

        is_currently_muted = self._channel_muted.get(channel_index, False)
        new_mute_state = not is_currently_muted
        self._channel_muted[channel_index] = new_mute_state
        if new_mute_state:
            self._muted_at_volume[channel_index] = self._poti_volumes.get(channel_index, 0.0)
            
        logger.info("IPC: Toggling mute for channel %d -> %s", channel_index, new_mute_state)
        
        self.mute_state_changed.emit(channel_index, new_mute_state)

        mode = self._config.get_channel_mode(channel_index)
        if mode == "hardware":
            hw_id = self._config.get_hardware_id(channel_index)
            if hw_id:
                try:
                    parts = hw_id.split(':', 1)
                    if len(parts) == 2:
                        kind, name = parts
                        with pulsectl.Pulse("nativmix-ipc-mute") as pulse:
                            if kind == "sink":
                                dev = pulse.get_sink_by_name(name)
                                pulse.mute(dev, new_mute_state)
                            elif kind == "source":
                                dev = pulse.get_source_by_name(name)
                                pulse.mute(dev, new_mute_state)
                except pulsectl.PulseError as exc:
                    logger.error("toggle_mute for HW %s failed: %s", hw_id, exc)
            return

        app_names = [n.lower() for n in self._config.get_app_names(channel_index)]
        if not app_names:
            return

        # Find all currently active streams that map to those apps and mute them
        try:
            with pulsectl.Pulse("nativmix-ipc-mute") as pulse:
                if "system master" in app_names:
                    default_sink_name = pulse.server_info().default_sink_name
                    sink = pulse.get_sink_by_name(default_sink_name)
                    pulse.mute(sink, new_mute_state)

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
                    if resolved.lower() in app_names:
                        pulse.sink_input_mute(si.index, mute=new_mute_state)
        except pulsectl.PulseError as exc:
            logger.error("toggle_mute for channel %d failed: %s", channel_index, exc)

    def _on_stream_changed(self, info: StreamInfo) -> None:
        """
        Slot: called when a known stream's properties (volume/mute) changed.

        TODO (Phase 2): Update the GUI and re-sync hardware poti position.
        """
        pass

    def _on_master_sink_changed(self, volume: float, muted: bool) -> None:
        """
        Slot: called when the default sink (System Master) changes externally.
        Updates GUI and hardware states for all channels assigned to System Master.
        """
        for ch in range(self._config.num_channels):
            if "system master" in [n.lower() for n in self._config.get_app_names(ch)]:
                self._poti_volumes[ch] = volume
                self._channel_muted[ch] = muted
                self.channel_volume_changed.emit(ch, volume)
                self.mute_state_changed.emit(ch, muted)

    # ------------------------------------------------------------------
    # Virtual Sinks (Pro-Routing)
    # ------------------------------------------------------------------

    def _adopt_existing_v_sinks(self) -> None:
        """Called on start to hook into V-Sinks that survived a restart/crash."""
        for ch in range(self._config.num_channels):
            if self._config.is_v_sink_enabled(ch):
                self._move_apps_to_sink(ch, f"NativMix_CH_{ch}")

    def enable_v_sink(self, channel_index: int) -> None:
        """Create a Virtual Sink and move mapped streams to it."""
        sink_name = f"NativMix_CH_{channel_index}"
        logger.info("Enabling V-Sink for channel %d: %s", channel_index, sink_name)

        # 1. Create Sink
        try:
            subprocess.run(
                ["pactl", "load-module", "module-null-sink", f"sink_name={sink_name}", f"sink_properties=device.description=NativMix_Channel_{channel_index}"],
                check=True,
                capture_output=True
            )
        except subprocess.CalledProcessError as e:
            # Usually means it already exists (e.g., from a crash)
            logger.warning("pactl load-module returned error or existed: %s", e.stderr)

        # 1b. Create Loopback to Hardware
        try:
            subprocess.run(
                ["pactl", "load-module", "module-loopback", f"source={sink_name}.monitor"],
                check=True,
                capture_output=True
            )
        except subprocess.CalledProcessError as e:
            logger.warning("pactl load-module module-loopback returned error: %s", e.stderr)

        # 2. Wait for PipeWire to actually register the Sink
        current_volume = self._poti_volumes.get(channel_index, 0.5)
        import time
        verified = False
        for _ in range(10):
            time.sleep(0.05)
            try:
                with pulsectl.Pulse("nativmix-vsink-poll") as p_poll:
                    if sink_name in [s.name for s in p_poll.sink_list()]:
                        verified = True
                        break
            except pulsectl.PulseError:
                pass
                
        if not verified:
            logger.warning("V-Sink %s did not appear in Pulse list after 500ms.", sink_name)
            return

        # 3. Throttle the V-Sink (MANDATORY before moving apps)
        self.set_channel_volume(channel_index, current_volume)
        logger.info("V-Sink %s throttled to %.2f BEFORE app injection", sink_name, current_volume)

        # 4. Move Apps and set Unity Gain (1.0 inside V-Sink)
        self._move_apps_to_sink(channel_index, sink_name, target_volume=1.0)

        self._update_thread_states()

    def disable_v_sink(self, channel_index: int) -> None:
        """
        Destroy the Virtual Sink and evacuate streams to the default real sink.

        Sequence (no-gap design):
          1. Move every stream from the V-Sink to the hardware sink.
          2. Immediately unmute / un-cork each moved stream.
          3. Apply the correct fader volume.
          4. Wait 150 ms so PipeWire can stabilise the stream on the new device.
          5. Unload the null-sink and loopback modules.
        """
        sink_name = f"NativMix_CH_{channel_index}"
        logger.info("Disabling V-Sink for channel %d: %s", channel_index, sink_name)

        current_volume = self._poti_volumes.get(channel_index, 0.5)

        # ── CRITICAL: push new state to listener BEFORE any moves ────────────
        # Same race condition as _on_mapping_changed: if the listener thread
        # still has v_sink=True when it processes the change event triggered
        # by our pactl move, it immediately routes the stream back into the
        # V-Sink.  Pushing the updated state first (V-Sink is now disabled)
        # prevents this.  ConfigManager.set_v_sink_enabled(False) was already
        # called by the GUI toggle before disable_v_sink() is invoked.
        self._update_thread_states()

        # ── Step 1-3: Evacuate streams BEFORE destroying the device ──────────
        try:

            with pulsectl.Pulse("nativmix-vsink-evac") as pulse:
                # Resolve the real hardware target sink
                default_sink_name = pulse.server_info().default_sink_name
                try:
                    target_sink = pulse.get_sink_by_name(default_sink_name)
                except pulsectl.PulseError:
                    sinks = pulse.sink_list()
                    target_sink = sinks[0] if sinks else None

                if not target_sink:
                    logger.error("No target sink found for V-Sink evacuation!")
                    return

                # Safety: never route into another NativMix virtual sink
                if target_sink.name.startswith("NativMix_"):
                    for s in pulse.sink_list():
                        if not s.name.startswith("NativMix_") and "dummy" not in s.name.lower():
                            target_sink = s
                            break

                # Locate the virtual sink by name
                try:
                    v_sink = pulse.get_sink_by_name(sink_name)
                    v_sink_index = v_sink.index
                except pulsectl.PulseError:
                    v_sink_index = None

                if v_sink_index is not None:

                    for si in pulse.sink_input_list():
                        if si.sink != v_sink_index:
                            continue
                        logger.info(
                            "Stream %d evacuating from V-Sink '%s' → Main Sink '%s'",
                            si.index, sink_name, target_sink.name
                        )
                        # _seamless_move: pactl move → unmute → set fader volume
                        self._seamless_move(pulse, si.index, target_sink.index, volume=current_volume)
                        logger.info(
                            "Stream %d moved back to Main Sink and forced to resume (vol=%.2f).",
                            si.index, current_volume
                        )

        except pulsectl.PulseError as e:
            logger.error("Failed to evacuate V-Sink %s apps: %s", sink_name, e)

        # ── Step 4: Safety delay ─────────────────────────────────────────────
        # Give PipeWire ~150 ms to stabilise streams on the new sink before
        # the V-Sink device disappears.  Browsers (Chromium) are especially
        # sensitive to the device vanishing too early.
        import time
        time.sleep(0.15)

        # ── Step 5: Destroy null-sink + loopback modules ─────────────────────
        try:
            pid_out = subprocess.run(
                ["pactl", "list", "short", "modules"],
                capture_output=True, text=True, check=True
            )
            for line in pid_out.stdout.splitlines():
                if f"sink_name={sink_name}" in line or f"source={sink_name}.monitor" in line:
                    mod_id = line.split()[0]
                    subprocess.run(["pactl", "unload-module", mod_id], check=True)
                    logger.info("Unloaded module ID %s for %s", mod_id, sink_name)
        except (subprocess.CalledProcessError, IndexError) as e:
            logger.error("pactl unload-module failed: %s", e)

        # Fallback: re-apply fader volumes directly in case streams were missed above
        app_names = self._config.get_app_names(channel_index)
        for name in app_names:
            self._apply_volume_by_name(name, current_volume)

        self._update_thread_states()

    def _move_apps_to_sink(self, channel_index: int, target_sink_name: str, target_volume: float | None = None) -> None:
        """
        Move all apps belonging to a given channel to `target_sink_name`.
        If target_volume is provided, set it on all moved streams.
        """
        app_names = [n.lower() for n in self._config.get_app_names(channel_index)]
        if not app_names:
            return
            
        try:
            with pulsectl.Pulse("nativmix-vsink-router") as pulse:
                try:
                    target_sink = pulse.get_sink_by_name(target_sink_name)
                except pulsectl.PulseError:
                    logger.warning("Sink %s not found, cannot route yet.", target_sink_name)
                    return
                
                for si in pulse.sink_input_list():
                    props = dict(si.proplist)
                    pid_str = props.get("application.process.id", "0")
                    try:
                        pid = int(pid_str)
                    except ValueError:
                        pid = 0
                    pa_fallback = props.get("application.name") or props.get("application.process.binary") or "Unknown"
                    resolved = resolve_app_name(pid, fallback=pa_fallback)
                    
                    if resolved.lower() not in app_names:
                        continue

                    if si.sink != target_sink.index:
                        self._seamless_move(pulse, si.index, target_sink.index, volume=target_volume)
                    elif target_volume is not None:
                        # Already on correct sink, just update volume
                        try:
                            pulse.volume_set_all_chans(si, target_volume)
                        except pulsectl.PulseError:
                            pass
        except pulsectl.PulseError as exc:
            logger.error("Routing apps to %s failed: %s", target_sink_name, exc)


    # ------------------------------------------------------------------
    # Debug / Status Helpers
    # ------------------------------------------------------------------



    def get_real_sinks(self) -> list[tuple[str, str]]:
        """Return a list of (description, name) of all real hardware sinks, excluding V-Sinks and monitors."""
        sinks: list[tuple[str, str]] = []
        try:
            with pulsectl.Pulse("nativmix-getsinks") as pulse:
                for s in pulse.sink_list():
                    # Skip NativMix virtual sinks and PipeWire dummy sinks
                    if s.name.startswith("NativMix_"):
                        continue
                    if "dummy" in s.name.lower():
                        continue
                    desc = s.description or s.name
                    sinks.append((desc, s.name))
        except pulsectl.PulseError as exc:
            logger.error("Failed to get real sinks: %s", exc)
        return sinks

    def get_real_sources(self) -> list[tuple[str, str]]:
        """Return a list of (description, name) of all real hardware sources (Inputs)."""
        sources: list[tuple[str, str]] = []
        try:
            with pulsectl.Pulse("nativmix-getsources") as pulse:
                for s in pulse.source_list():
                    # PipeWire monitors are often Sources for Outputs. Filter them optionally if needed,
                    # but usually, we want physical inputs. We keep all for flexibility, except internal Monitors if needed.
                    if "monitor" in s.name.lower():
                        continue
                    desc = s.description or s.name
                    sources.append((desc, s.name))
        except pulsectl.PulseError as exc:
            logger.error("Failed to get real sources: %s", exc)
        return sources

    def get_default_sink_name(self) -> str:
        """Return the current system default sink name."""
        try:
            with pulsectl.Pulse("nativmix-getdef") as pulse:
                return pulse.server_info().default_sink_name
        except pulsectl.PulseError:
            return ""

    def set_default_sink_and_move_loopbacks(self, sink_name: str) -> None:
        """Change the default system sink and move all loopback modules to it."""
        try:
            with pulsectl.Pulse("nativmix-setdef") as pulse:
                # 1. Set the new default sink
                target_sink = pulse.get_sink_by_name(sink_name)
                pulse.default_set(target_sink)
                logger.info("Set default system sink to %s", sink_name)

                # 2. Find and move loopback modules
                loopback_module_ids = {m.index for m in pulse.module_list() if m.name == "module-loopback"}
                
                for si in pulse.sink_input_list():
                    # Check if the sink input belongs to a module-loopback
                    is_loopback = si.owner_module in loopback_module_ids
                    if is_loopback:
                        if si.sink != target_sink.index:
                            logger.info("Moving loopback stream [%d] to new Master Output", si.index)
                            pulse.sink_input_move(si.index, target_sink.index)
                            
        except pulsectl.PulseError as exc:
            logger.error("Failed to set default sink and route loopbacks: %s", exc)

    def panic_reset(self) -> None:
        """
        Emergency reset: Evacuate all streams to default sink and destroy all modules.
        This must be called synchronously from the main thread GUI trigger.
        """
        logger.warning("Panic Reset triggered!")
        
        # 1. Evacuate all apps
        try:
            with pulsectl.Pulse("nativmix-panic-evac") as pulse:
                default_sink_name = pulse.server_info().default_sink_name
                default_sink = pulse.get_sink_by_name(default_sink_name)
                
                for si in pulse.sink_input_list():
                    try:
                        pulse.sink_input_move(si.index, default_sink.index)
                    except pulsectl.PulseError as e:
                        logger.warning("Could not evacuate %d: %s", si.index, e)
        except pulsectl.PulseError as e:
            logger.error("Panic Evac failed: %s", e)
            
        # 2. Destroy all NativMix Sinks and their Loopbacks
        try:
            pid_out = subprocess.run(
                ["pactl", "list", "short", "modules"], 
                capture_output=True, text=True, check=True
            )
            for line in pid_out.stdout.splitlines():
                if "sink_name=NativMix_CH_" in line or "source=NativMix_CH_" in line:
                    mod_id = line.split()[0]
                    subprocess.run(["pactl", "unload-module", mod_id], check=True)
                    logger.info("Panic unloaded module ID %s", mod_id)
        except subprocess.CalledProcessError as e:
            logger.error("pactl unload-module failed during Panic: %s", e.stderr)
