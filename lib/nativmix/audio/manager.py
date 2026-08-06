"""
Linux audio backend: PipeWireManager

Manages per-stream volume control for PipeWire audio sessions.  Uses the
PipeWire-native path (``pw-cli set-param``) as the primary write backend for
volume and mute operations, falling back to the PulseAudio compatibility layer
(pulsectl / pactl) when ``pw-cli`` is unavailable or a write fails.  Stream
enumeration and event subscription still use pulsectl / PipeWire's PA shim
because the PA event model covers both native PW clients and legacy PA clients.

Architecture
------------
PipeWire-native clients expose richer node metadata directly.  PulseAudio
clients that run through the ``pipewire-pulse`` compatibility shim expose
sink-input semantics but may have different node IDs and property sets.

The backend therefore uses a two-layer approach:

1. **PipeWire-native write path** (``pw-cli``) — primary volume/mute write
   target; talks directly to the PW object graph, avoids libpulse overhead,
   and works in restricted Flatpak sessions that block PA compat writes.
2. **PulseAudio-compat path** (pulsectl) — fallback write path and the sole
   path for event subscription, stream listing, and V-Sink management.

Capability probe (Phase 1)
--------------------------
On startup, ``_probe_capabilities()`` runs ``pw-cli info 0`` (PW-native) and
attempts a harmless pulsectl write to verify which paths are actually usable.
``can_set_volume_pw`` gates the PW-native write path; ``can_set_volume`` gates
the pulsectl fallback.  If both are unavailable, a single notice is emitted
via ``status_changed`` and all write operations are silently skipped.

Matching priority (Phase 3)
---------------------------
``_matches_node()`` uses a deterministic priority order:

1. Exact cached stable IDs (``node.id`` / ``client.id`` from PW inventory)
2. Exact ``application.process.binary`` match (case-insensitive)
3. Exact ``application.name`` match (case-insensitive)
4. Exact ``media.name`` match (case-insensitive)
5. Case-insensitive *contains* fallback (last resort)

Two-Stage Mute-Catch (Rule 11)
-------------------------------
Stage 1 – Reflex (on 'new' event):
    When a new sink_input appears, immediately mute it BEFORE trying to
    identify the application. At this point no metadata is available yet.

Stage 2 – Resolution (on 'change' event):
    When the 'change' event fires for the same index, read
    application.process.id and resolve the real application name. Then
    apply the correct volume from the hardware mapping and unmute.

This prevents the "audio blast" caused by new streams starting at 100 %
volume before they can be identified and volume-controlled.

---------------------------------------------------------------------------
Setup & Installation Info (CachyOS / Arch Linux)
---------------------------------------------------------------------------
To test this module or NativMix locally, ensure you have the required
packages installed. ``pulsectl`` is strictly required for PipeWire routing.

For local development / testing without AUR:
  sudo pacman -S python-pulsectl python-pyqt6 python-pyserial python-setproctitle

Or using a venv:
  python -m venv .venv && source .venv/bin/activate
  pip install pulsectl PyQt6 pyserial setproctitle
---------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

import pulsectl
from PyQt6.QtCore import QThread, QTimer, pyqtSignal, pyqtSlot

from nativmix.audio.base import AudioBackendBase, StreamInfo

# PipeWire-native helpers live in a separate module with no libpulse dependency.
from nativmix.audio.pipewire_native import (
    NATIVMIX_FORCE_PW_ONLY,
    PipeWireNode,
    _detect_pulse_available,
    _matches_node,
    _normalize_name,
    _probe_capabilities,
    _pw_dump_nodes,
    _pw_set_mute,
    _pw_set_volume,
    _pw_set_volume_traced,
    _ThrottledWarner,
    _wpctl_set_mute,
    _wpctl_set_volume,
    _wpctl_set_volume_default_sink,
    _wpctl_set_volume_traced,
    _wpctl_set_volume_default_source,
    _wpctl_set_volume_exact,
    detect_easyeffects,
)
from nativmix.utils import routing
from nativmix.utils.config_manager import ConfigManager
from nativmix.utils.proc_resolver import (
    GENERIC_PA_NAMES,
    IS_FLATPAK,
    invalidate_cache,
    resolve_app_name,
    resolve_binary_name,
)

logger = logging.getLogger(__name__)

# Volume applied immediately on 'new' event as a safety floor
_SAFETY_VOLUME: float = 0.0  # fully muted until we know who this stream belongs to

# Shared timeout (seconds) for all pactl/subprocess calls in this module.
_SUBPROCESS_TIMEOUT: int = 5


@dataclass
class _PendingStream:
    """Tracks a stream that has been muted but not yet identified."""
    index: int


@dataclass
class PwIdentityTuple:
    """
    Persistent binding identity for a PW stream target.

    Stored per app_name in ``PipeWireManager._pw_identity`` and updated every
    time a node is successfully matched.  The tuple is richer than the legacy
    ``_stable_ids`` cache (node_id / client_id sets only) — it also records the
    canonical ``node_name`` and ``process_binary`` so future pw-dump scans can
    re-anchor on field matches even when the numeric IDs change (e.g. after an
    app restart).

    Fields
    ------
    app_label : str
        Original user-visible display label (as stored in the config), preserved
        for display purposes.
    node_name : str
        ``node.name`` from the last successfully matched PipeWire node, or ``""``
        if never resolved.
    process_binary : str
        ``application.process.binary`` of the last matched node, or ``""``.
    last_node_id : int
        ``node.id`` of the last successfully matched node.  0 if unknown.
    """
    app_label: str
    node_name: str
    process_binary: str
    last_node_id: int


@dataclass
class PwOwnedGainPath:
    """Owned writable PW-only gain path for a bound app target."""

    app_name: str
    node_id: int
    node_name: str
    writable: bool
    available: bool
    degraded_reason: str = ""


@dataclass
class PwOwnedRoutePath:
    """PW-only owned route graph for a bound app target."""

    app_name: str
    input_node_id: int = 0
    input_node_name: str = ""
    gain_node_id: int = 0
    gain_node_name: str = ""
    output_node_id: int = 0
    output_node_name: str = ""
    gain_control_writable: bool = False
    writable: bool = False
    active: bool = False
    degraded_reason: str = ""


def _is_internal_stream(proplist: dict[str, str]) -> bool:
    """
    Unified filter for internal, system, or NativMix-managed streams.
    Returns True if the stream should be HIDDEN from both the main list
    and the "Other Apps" tooltip.
    """
    app_name = proplist.get("application.name", "") or proplist.get("media.name", "")
    media_class = proplist.get("media.class", "").lower()

    # Filter by common system/monitor keywords
    keywords = ["loopback", "monitor", "peak detect", "dummy", "speech-dispatcher", "nativmix"]
    name_lower = app_name.lower()

    if any(kd in name_lower for kd in keywords):
        return True

    if "monitor" in media_class or "loopback" in media_class:
        return True

    return False


def _matches_app_name(props: dict[str, str], resolved: str, target: str) -> bool:
    """
    Return True if a sink-input matches *target* using a deterministic
    priority order that is robust under PipeWire/Pulse in Flatpak sessions:

    1. Exact ``application.name`` match (case-insensitive).
    2. Exact ``media.name`` match (case-insensitive).
    3. Resolved process name from proc_resolver (covers Electron/Chromium apps
       that report generic Pulse names but are identified via /proc or Flatpak
       sandbox metadata).
    """
    target_lc = target.lower()
    if not target_lc:
        return False
    app_name = props.get("application.name", "")
    if app_name and app_name.lower() == target_lc:
        return True
    media_name = props.get("media.name", "")
    if media_name and media_name.lower() == target_lc:
        return True
    return resolved.lower() == target_lc


_throttled_warner = _ThrottledWarner(interval=30.0)


def move_stream_to_vsink(
    stream_index: int,
    vsink_name: str,
    pulse: "pulsectl.Pulse",
) -> bool:
    """
    Move a PulseAudio sink-input to a virtual sink by name.

    This is the **single authoritative call-site** for ``pactl move-sink-input``
    in host mode.  All other routing code must call this function instead of
    invoking ``pactl`` directly.

    Behaviour
    ----------
    * **Flatpak mode** (``IS_FLATPAK`` is ``True``): the ``pactl`` invocation is
      skipped entirely.  PipeWire's graph/link routing handles placement without
      requiring a privileged host tool.  Returns ``False`` (not moved via pactl).
    * **Host mode**: runs ``pactl move-sink-input <stream_index> <vsink_name>``
      with a subprocess timeout.  Returns ``True`` on success, ``False`` on
      failure or timeout.

    Parameters
    ----------
    stream_index:
        PulseAudio sink-input index to move.
    vsink_name:
        Target virtual sink name (or index as a string).
    pulse:
        Active ``pulsectl.Pulse`` connection — currently unused for the move
        itself but kept for API consistency and future PW-native fallback.

    Returns
    -------
    bool
        ``True`` if the stream was successfully moved via ``pactl``; ``False``
        if the move was skipped (Flatpak guard) or failed.
    """
    if IS_FLATPAK:
        logger.info(
            "move_stream_to_vsink: Flatpak hard guard active — skipping "
            "pactl move-sink-input for stream %d -> %s",
            stream_index, vsink_name,
        )
        return False

    try:
        result = subprocess.run(
            ["pactl", "move-sink-input", str(stream_index), vsink_name],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        if result.returncode == 0:
            return True
        logger.warning(
            "move_stream_to_vsink: pactl move-sink-input failed (rc=%d): %s",
            result.returncode,
            result.stderr.decode(errors="replace").strip(),
        )
        return False
    except subprocess.TimeoutExpired:
        logger.warning(
            "move_stream_to_vsink: pactl move-sink-input timed out after %ds "
            "(stream %d -> %s)",
            _SUBPROCESS_TIMEOUT, stream_index, vsink_name,
        )
        return False



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
    status_changed = pyqtSignal(str, str)           # (status_type, message)

    def __init__(self, config: ConfigManager, parent: Any = None) -> None:
        super().__init__(parent)
        self._config = config
        self._running = False
        # Thread-safe dictionary to detach from GUI / Config updates
        # Format: { channel_index: {'vol': float, 'v_sink': bool, 'apps': list[str]} }
        self.channel_states: dict[int, dict] = {}
        self._states_lock = threading.Lock()
        # Track streams muted by the reflex stage
        self._reflex_muted: set[int] = set()
        # Track streams we have already emitted `stream_added` for
        self._known_streams: set[int] = set()
        self._pulse: pulsectl.Pulse | None = None
        self._resolver: pulsectl.Pulse | None = None  # Second connection for lookups/actions
        # Cooldown: tracks last routing timestamp per sink_input index to
        # suppress duplicate log lines from audit + stream_changed race.
        self._recently_routed: dict[int, float] = {}
        # Deduplication: last known (volume, muted) per stream index.
        # PipeWire emits one change event per property update (vol, mute,
        # proplist, routing …), so a single stream start can fire 20+ events
        # with identical vol/mute.  We skip the full IPC+routing path when
        # nothing audio-relevant has actually changed.
        self._stream_last_state: dict[int, tuple[float, bool]] = {}
        # Routing owner policy forwarded from PipeWireManager after start().
        # "nativmix" | "easyeffects" | "none"
        self.routing_owner: str = "nativmix"

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main loop running inside the worker thread."""
        self._running = True
        logger.info("AudioListenerThread started")
        self.status_changed.emit("connecting", "Connecting to PipeWire...")

        try:
            # We use TWO connections:
            # 1. 'pulse' for the blocking event_listen loop
            # 2. 'resolver' for all information requests (info) and actions (volume/mute)
            # This prevents SIGSEGV crashes caused by re-entrant calls in the callback thread.
            with pulsectl.Pulse("nativmix-listener") as pulse, \
                 pulsectl.Pulse("nativmix-resolver") as resolver:

                self._pulse = pulse
                self._resolver = resolver

                # 1. Fetch currently existing streams
                try:
                    for si in resolver.sink_input_list():
                        if _is_internal_stream(dict(si.proplist)):
                            continue

                        # ANTI-BLAST for existing streams (Rule 11/18):
                        # Mute immediately to prevent start-up spikes before mapping is applied.
                        try:
                            # Use pactl for non-blocking execution
                            subprocess.run(
                                ["pactl", "set-sink-input-mute", str(si.index), "1"],
                                capture_output=True, timeout=_SUBPROCESS_TIMEOUT,
                            )
                            self._reflex_muted.add(si.index)
                        except Exception:
                            pass

                        info = self._build_stream_info(si)
                        if info:
                            # Auto-sync on startup
                            self._apply_auto_reconnect(resolver, info)
                            self.stream_added.emit(info)

                            # Reflex: apply correct mute state for startup streams.
                            # Respect channel mute state (muted channels keep streams muted).
                            if si.index in self._reflex_muted:
                                startup_muted = self._get_channel_mute_state(info.app_name)
                                logger.debug(
                                    "Startup stream %d (%s): reflex → mute=%s",
                                    si.index, info.app_name, startup_muted,
                                )
                                try:
                                    resolver.sink_input_mute(si.index, mute=startup_muted)
                                    self._reflex_muted.remove(si.index)
                                except pulsectl.PulseError:
                                    pass
                except pulsectl.PulseError as exc:
                    logger.warning("Could not list initial streams: %s", exc)

                # 2. Subscribe to future events
                pulse.event_mask_set("sink_input", "sink")
                pulse.event_callback_set(self._on_event)

                self.status_changed.emit("stable", "PipeWire connected")
                logger.info("Listening for PulseAudio sink_input events …")
                # Block and process events in 100ms chunks
                while self._running:
                    try:
                        pulse.event_listen(timeout=0.1)
                    except pulsectl.PulseLoopStop:
                        break
                    except Exception as e:
                        logger.error("Error in pulse event_listen: %s", e)
                        time.sleep(0.1)

            logger.info("AudioListenerThread finished cleanly")

        except pulsectl.PulseError as exc:
            logger.error("PulseAudio connection error: %s", exc)
            self.status_changed.emit("error_temporary", str(exc))
        except Exception as exc:
            logger.exception("Unhandled crash in AudioListenerThread")
            self.status_changed.emit("error_critical", str(exc))

    def stop(self) -> None:
        """Signal the thread to stop and wait for it to finish (with timeout)."""
        self._running = False
        if not self.wait(2000):
            logger.warning("AudioListenerThread did not stop in time, terminating...")
            self.terminate()
            # Strategy B: bounded wait after terminate() so libpulse blocked on
            # a dead PipeWire socket during system shutdown cannot hang forever.
            if not self.wait(1000):
                logger.error("AudioListenerThread still alive after terminate — abandoning")

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def _on_event(self, event: pulsectl.PulseEventInfo) -> None:
        """Called by pulsectl for every subscribed event."""
        if not self._pulse:
            return

        try:
            if event.facility == pulsectl.PulseEventFacilityEnum.sink_input:
                if event.t == pulsectl.PulseEventTypeEnum.new:
                    # Stage 1: Reflex - Mute immediately (Rule 11)
                    # We use 'pactl' because it is external and 100% safe from Pulse thread locks.
                    try:
                        subprocess.run(
                            ["pactl", "set-sink-input-mute", str(event.index), "1"],
                            capture_output=True, timeout=_SUBPROCESS_TIMEOUT,
                        )
                        self._reflex_muted.add(event.index)
                    except Exception as e:
                        logger.debug("Reflex mute failed for index %d: %s", event.index, e)

                elif event.t == pulsectl.PulseEventTypeEnum.change:
                    # Stage 2: Resolution - Resolve and Reconnect
                    # We MUST use the separate 'resolver' connection here.
                    if not self._resolver:
                        return
                    try:
                        si = self._resolver.sink_input_info(event.index)
                        if si and not isinstance(si, int):
                            props = getattr(si, "proplist", {})
                            if not hasattr(props, "get") and not isinstance(props, dict):
                                props = {}
                            if _is_internal_stream(dict(props)):
                                # Ensure we clean up any reflex mute even for internal streams
                                if event.index in self._reflex_muted:
                                    try:
                                        self._resolver.sink_input_mute(event.index, mute=False)
                                        self._reflex_muted.remove(event.index)
                                    except pulsectl.PulseError:
                                        pass
                                return
                        else:
                            # Cleanup reflex mute even if we can't get info (e.g. vanished)
                            if event.index in self._reflex_muted:
                                self._reflex_muted.remove(event.index)
                            return
                    except (pulsectl.PulseIndexError, pulsectl.PulseError):
                        if event.index in self._reflex_muted:
                            self._reflex_muted.remove(event.index)
                        return

                    info = self._build_stream_info(si)
                    if info:
                        # Deduplication: skip full IPC+routing when vol/mute
                        # haven't changed and this is an already-known stream
                        # that is not pending a reflex unmute.  PipeWire fires
                        # one change event per property (proplist, routing,
                        # format …), so a stream start can produce 20+ events
                        # with identical audio state.
                        current_state = (info.volume, info.muted)
                        last_state = self._stream_last_state.get(event.index)
                        self._stream_last_state[event.index] = current_state
                        if (last_state == current_state
                                and event.index in self._known_streams
                                and event.index not in self._reflex_muted):
                            return  # metadata-only update — no audio action needed

                        # PERSISTENCE / AUTO-RECONNECT (using resolver)
                        self._apply_auto_reconnect(self._resolver, info)

                        # Reflex: resolve to the correct mute state.
                        # If the channel this stream belongs to is currently
                        # muted, keep the stream muted instead of blindly
                        # unmuting after the reflex period.
                        if event.index in self._reflex_muted:
                            channel_muted = self._get_channel_mute_state(info.app_name)
                            logger.debug(
                                "Stream %d (%s): reflex → mute=%s",
                                event.index, info.app_name, channel_muted,
                            )
                            try:
                                self._resolver.sink_input_mute(event.index, mute=channel_muted)
                            except pulsectl.PulseError:
                                pass
                            self._reflex_muted.remove(event.index)

                        self.stream_changed.emit(info)
                        if event.index not in self._known_streams:
                            self._known_streams.add(event.index)
                            self.stream_added.emit(info)

                elif event.t == pulsectl.PulseEventTypeEnum.remove:
                    if event.index in self._known_streams:
                        self._known_streams.remove(event.index)
                    if event.index in self._reflex_muted:
                        self._reflex_muted.remove(event.index)
                    self._stream_last_state.pop(event.index, None)
                    self.stream_removed.emit(event.index)

            elif event.facility == pulsectl.PulseEventFacilityEnum.sink:
                pass  # Sink volume/hotplug is polled by SinkPollThread — no IPC here

        except Exception as e:
            logger.error("Listener Error: %s", e)

    def _apply_auto_reconnect(self, pulse: pulsectl.Pulse, info: StreamInfo) -> None:
        """Apply volume and V-Sink routing based on persistence config."""
        # 1. Check if app is assigned to any channel
        target_ch = self._config.find_channel_for_app(info.app_name)
        logger.debug(
            "Auto-reconnect: stream %d app=%r -> ch=%s",
            info.index, info.app_name, target_ch,
        )
        if target_ch is None:
            return

        # 2. Get state (from cache or config)
        with self._states_lock:
            state = dict(self.channel_states.get(target_ch, {}))
        vol = state.get("vol", self._config.get_channel_volume(target_ch))
        vsink_enabled = state.get("v_sink", self._config.is_v_sink_enabled(target_ch))

        try:
            if vsink_enabled:
                # Routing owner guard: only nativmix may auto-route streams.
                if self.routing_owner != "nativmix":
                    logger.info(
                        "_apply_auto_reconnect: app=%r ch=%d V-Sink routing blocked "
                        "(routing_owner=%r — NativMix must not reroute streams in this mode)",
                        info.app_name, target_ch, self.routing_owner,
                    )
                    return
                v_sink_name = f"NativMix_CH_{target_ch}"
                try:
                    v_sink = pulse.get_sink_by_name(v_sink_name)
                except pulsectl.PulseError:
                    v_sink = None

                if not v_sink:
                    if state.get("v_sink_busy", False):
                        logger.debug("V-Sink %s being (re)created — reconnect deferred", v_sink_name)
                    else:
                        logger.warning("V-Sink %s not found for reconnect", v_sink_name)
                    return

                if info.props.get("sink_name") != v_sink_name:
                    now = time.monotonic()
                    last = self._recently_routed.get(info.index)
                    if last is not None and now - last < 2.0:
                        logger.debug(
                            "Routing %s skipped – cooldown active (%.0fms ago)",
                            info.app_name, (now - last) * 1000,
                        )
                    else:
                        # Prune stale entries (> 10 s) to keep the dict small
                        self._recently_routed = {
                            k: v for k, v in self._recently_routed.items()
                            if now - v < 10.0
                        }
                        self._recently_routed[info.index] = now
                        logger.debug("Routing %s into V-Sink %s", info.app_name, v_sink_name)
                        moved = move_stream_to_vsink(info.index, v_sink_name, pulse)

                        if moved:
                            # Robust pulsectl call: fetch fresh info object and VALIDATE type
                            try:
                                si_fresh = pulse.sink_input_info(info.index)
                                if si_fresh and not isinstance(si_fresh, int):
                                    pulse.volume_set_all_chans(si_fresh, 1.0)  # Unity gain inside V-Sink
                                else:
                                    # If si_fresh is 200 (int) or None, we cannot resolve metadata right now
                                    logger.info(
                                        "Received status ID (%s) instead of metadata object for %s, skipping volume sync",
                                        si_fresh, info.app_name,
                                    )
                            except (pulsectl.PulseError, TypeError, ValueError) as e:
                                logger.debug("Minor: Could not update volume after move (stream may have closed): %s", e)
            else:
                try:
                    si_fresh = pulse.sink_input_info(info.index)
                    if si_fresh and not isinstance(si_fresh, int):
                        pulse.volume_set_all_chans(si_fresh, vol)
                    else:
                        logger.info("Received status ID (%s) instead of metadata object for %s, skipping volume sync",
                                    si_fresh, info.app_name)
                except (pulsectl.PulseError, TypeError, ValueError) as e:
                    logger.debug("Minor: Could not apply volume (stream may have closed): %s", e)
        except Exception as e:
            logger.error("Auto-reconnect process error for %s: %s", info.app_name, e)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_channel_mute_state(self, app_name: str) -> bool:
        """
        Return True if the channel mapped to *app_name* is currently muted.

        Looks up the channel via the config and reads the 'muted' flag from
        the lock-protected channel_states snapshot (updated by the main thread
        via _update_thread_states).  Returns False (unmuted) when the app is
        not mapped to any channel, or the channel has no explicit mute state
        recorded.

        Args:
            app_name: Resolved application name, e.g. "Spotify". The lookup
                is case-insensitive.
        """
        target_ch = self._config.find_channel_for_app(app_name)
        if target_ch is None:
            return False
        with self._states_lock:
            return bool(self.channel_states.get(target_ch, {}).get('muted', False))

    @staticmethod
    def _build_stream_info(sink_input: Any) -> StreamInfo | None:
        """
        Convert a pulsectl PulseSinkInputInfo into our StreamInfo dataclass.
        Returns None if sink_input is invalid (e.g. an integer status code).
        """
        if sink_input is None or isinstance(sink_input, int):
            # logger.debug("Cannot build StreamInfo: sink_input is %s", type(sink_input))
            return None

        # Robust Metadata Parsing (Fix for "Firefox-Bug")
        # Ensure proplist is actually a dictionary-like object.
        raw_props = getattr(sink_input, "proplist", {})
        if not hasattr(raw_props, "get") and not isinstance(raw_props, dict):
            logger.warning("Stream %d has invalid metadata (proplist type: %s)",
                           sink_input.index, type(raw_props))
            props = {}
        else:
            props = dict(raw_props)

        pid_str = str(props.get("application.process.id", "0"))
        try:
            pid = int(pid_str)
        except (ValueError, TypeError):
            pid = 0

        # application.process.binary is always set by the audio client library
        # and is reliable even when /proc is inaccessible (e.g. Flatpak sandbox).
        # Check it against the known-binary map first so that native (non-Flatpak)
        # apps are resolved correctly when NativMix itself runs in Flatpak.
        proc_binary = str(props.get("application.process.binary", "")).strip()
        binary_mapped = resolve_binary_name(proc_binary) if proc_binary else None

        # Determine a pa-level fallback in case /proc is unavailable (e.g. containers)
        pa_fallback = (
            binary_mapped
            or str(props.get("application.name", ""))
            or proc_binary
            or str(props.get("media.name", ""))
            or "Unknown"
        )

        # Full /proc-based resolution with Electron/Chromium hack.
        # When running in Flatpak, /proc reads of other processes may be
        # restricted; the pa_fallback above (via application.process.binary)
        # already carries the best available name in that case.
        app_name = resolve_app_name(pid, fallback=pa_fallback)

        # Warn when the name is still generic after resolution — this means
        # either pid=0 (virtual PipeWire node, unfixable) or the app is not
        # yet in the binary/Flatpak maps and needs a new entry.
        if app_name.lower() in GENERIC_PA_NAMES:
            if pid > 0:
                logger.debug(
                    "Unresolved generic stream: index=%d name=%r pid=%d — "
                    "add binary to _BINARY_MAP or _FLATPAK_APP_MAP%s",
                    sink_input.index, app_name, pid,
                    " (running in Flatpak sandbox)" if IS_FLATPAK else "",
                )
            else:
                logger.debug(
                    "Virtual/anonymous stream: index=%d name=%r (pid=0, no proc info)",
                    sink_input.index, app_name,
                )

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


class SinkPollThread(QThread):
    """
    Polls the PipeWire default sink every 250 ms from a dedicated background thread.

    This replaces the previous approach of doing server_info() / get_sink_by_name()
    directly inside the PipeWire event callback (_AudioListenerThread._on_event),
    which caused deadlocks and CPU spikes during event storms (e.g. external
    system volume changes).

    Signals
    -------
    master_volume_changed(float, bool)
        Emitted whenever the default sink's volume or mute state changes.
    default_sink_changed(str)
        Emitted whenever the default sink name changes (hotplug / device switch).
    """

    master_volume_changed = pyqtSignal(float, bool)
    default_sink_changed = pyqtSignal(str)

    _POLL_INTERVAL: float = 0.25  # seconds

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._running = False

    def run(self) -> None:
        self._running = True
        _last_sink_name: str | None = None
        _last_vol: float | None = None
        _last_mute: bool | None = None

        while self._running:
            try:
                with pulsectl.Pulse("nativmix-sink-poll") as pulse:
                    while self._running:
                        try:
                            info = pulse.server_info()
                            name = info.default_sink_name
                            if name != _last_sink_name:
                                _last_sink_name = name
                                self.default_sink_changed.emit(name)
                            sink = pulse.get_sink_by_name(name)
                            vol = sink.volume.value_flat
                            mute = bool(sink.mute)
                            if vol != _last_vol or mute != _last_mute:
                                _last_vol = vol
                                _last_mute = mute
                                self.master_volume_changed.emit(vol, mute)
                        except pulsectl.PulseError:
                            pass
                        # Interruptible sleep: 5 × 50 ms so stop() wakes us promptly
                        for _ in range(5):
                            if not self._running:
                                break
                            time.sleep(self._POLL_INTERVAL / 5)
            except Exception:
                # PipeWire restarted or connection failed — retry after 1 s
                for _ in range(10):
                    if not self._running:
                        break
                    time.sleep(0.1)

    def stop(self) -> None:
        self._running = False
        if not self.wait(2000):
            logger.warning("SinkPollThread did not stop in time, terminating...")
            self.terminate()
            if not self.wait(1000):
                logger.error("SinkPollThread still alive after terminate — abandoning")


class _PipeWirePollerThread(QThread):
    """
    Background thread for PW-only mode (Pulse socket absent).

    Polls ``pw-dump`` every *poll_interval* seconds to detect changes in the
    PipeWire stream node inventory and emits :attr:`streams_changed` so the
    manager can refresh its active-stream cache without a PulseAudio connection.

    Signals
    -------
    streams_changed(list[PipeWireNode])
        Emitted whenever the set of active audio output nodes changes.
    status_changed(str, str)
        Emitted with ``("pw_only", "PW-only (Flatpak)")`` on startup, and
        ``("error_temporary", …)`` if pw-dump fails repeatedly.
    """

    streams_changed = pyqtSignal(list)   # list[PipeWireNode]
    status_changed = pyqtSignal(str, str)

    _POLL_INTERVAL: float = 2.0   # seconds between pw-dump polls
    _ERROR_THRESHOLD: int = 5     # consecutive failures before emitting error

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._running = False

    def run(self) -> None:
        self._running = True
        logger.info("PipeWirePollerThread started (PW-only mode)")
        self.status_changed.emit("pw_only", "PW-only (Flatpak)")

        last_node_ids: set[int] = set()
        error_count = 0

        while self._running:
            try:
                nodes = _pw_dump_nodes()
                current_ids = {n.node_id for n in nodes}
                if current_ids != last_node_ids:
                    last_node_ids = current_ids
                    self.streams_changed.emit(nodes)
                error_count = 0
            except Exception as exc:
                error_count += 1
                if error_count >= self._ERROR_THRESHOLD:
                    logger.warning("PipeWirePollerThread: pw-dump failed %d times: %s", error_count, exc)
                    self.status_changed.emit("error_temporary", f"pw-dump unavailable: {exc}")
                    error_count = 0  # reset so we don't spam

            # Interruptible sleep: chunks of 200 ms so stop() responds promptly
            slept = 0.0
            while self._running and slept < self._POLL_INTERVAL:
                time.sleep(0.2)
                slept += 0.2

        logger.info("PipeWirePollerThread stopped")

    def stop(self) -> None:
        self._running = False
        if not self.wait(2000):
            logger.warning("PipeWirePollerThread did not stop in time, terminating...")
            self.terminate()
            if not self.wait(1000):
                logger.error("PipeWirePollerThread still alive after terminate — abandoning")


class PipeWireManager(AudioBackendBase):
    """
    Linux audio backend for PipeWire audio sessions.

    Uses the PipeWire-native path (``pw-cli set-param``) as the primary write
    backend for volume and mute operations.  Falls back to the PulseAudio
    compatibility layer (pulsectl) when ``pw-cli`` is unavailable or a write
    fails.  Supplements both paths with a PipeWire-native node inventory built
    from ``pw-dump`` for richer metadata and more reliable stream matching.

    Capability flags (set by :func:`_probe_capabilities` on :meth:`start`):
        - ``can_set_volume_pw`` — pw-cli volume writes are permitted (primary path).
        - ``can_set_volume`` — pulsectl writes are permitted (fallback path).
        - ``can_move_stream`` — pactl move-sink-input is available.
        - ``pw_dump_available`` — ``pw-dump`` is present.
        - ``pw_cli_available`` — ``pw-cli`` is present.

    If both ``can_set_volume_pw`` and ``can_set_volume`` are False a single UI
    notice is emitted via ``status_changed`` and all volume-write paths are
    silently skipped.
    """

    mute_state_changed = pyqtSignal(int, bool)
    channel_volume_changed = pyqtSignal(int, float)
    other_apps_changed = pyqtSignal(list)
    audit_finished = pyqtSignal()
    status_changed = pyqtSignal(str, str)  # (status_type, message) — forwarded from _AudioListenerThread
    unresolved_targets_changed = pyqtSignal(set)  # emitted when the set of unresolvable app targets changes
    capability_changed = pyqtSignal(str, bool)   # (capability_name, supported) — emitted when probe results arrive

    _BACKOFF_BASE: float = 2.0
    _BACKOFF_MAX: float = 60.0

    def __init__(self, config: ConfigManager | None = None, parent=None) -> None:
        super().__init__(parent)
        self._config: ConfigManager = config if config is not None else ConfigManager()
        self._thread: _AudioListenerThread | None = None
        self._running: bool = False
        self._restart_count: int = 0
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
        # Track hardware sink for hotplug to avoid redundant re-links
        self._last_hardware_sink: str | None = None
        # Guard: hotplug re-links are only allowed after the initial audit has
        # settled.  Set to True 2 s after audit_finished to absorb any sink
        # events that PipeWire emits as a side-effect of the audit itself.
        self._initial_audit_complete: bool = False
        # Reentrant lock protecting shared mutable dicts (_poti_volumes,
        # _channel_muted, _active_streams).  RLock is required because
        # apply_poti_volumes / apply_midi_volumes call toggle_mute, which
        # also acquires the lock on the same thread.
        self._state_lock = threading.RLock()
        # Channels whose V-Sink is currently being created. Slider volume
        # updates are suppressed for these channels until creation completes
        # to prevent stray writes hitting the system sink instead of the V-Sink.
        self._vsink_creating: set[int] = set()
        self._last_other_apps: list[str] = []
        # Sink poller: polls default sink volume/name from a dedicated thread
        # instead of doing blocking IPC inside the PipeWire event callback.
        self._sink_poll_thread = SinkPollThread()
        # Persistent pulsectl connection reused across all poti/MIDI volume
        # ticks.  Opening a new Pulse() on every tick (~10–60×/s) causes
        # libpulse C-heap churn that Python's GC does not promptly release,
        # leading to gradual RSS growth.  Lazily initialised on first use;
        # reconnected transparently on PulseError.
        self._vol_pulse: pulsectl.Pulse | None = None
        # Last volume sent to each Pulse target. Arduino emits a full channel
        # snapshot when any fader changes, so without this guard one noisy or
        # active fader re-applies every other channel on each tick. On GNOME,
        # repeated master/hardware sink writes can put pressure on gnome-shell
        # via volume-change handling even when the effective volume is unchanged.
        self._last_applied_volumes: dict[tuple[str, str], float] = {}
        # Debounce rapid stream add/remove events: coalesces multiple events
        # within 50 ms into a single get_active_streams() call.
        self._stream_refresh_timer = QTimer(self)
        self._stream_refresh_timer.setSingleShot(True)
        self._stream_refresh_timer.setInterval(50)
        self._stream_refresh_timer.timeout.connect(self.get_active_streams)

        # ------------------------------------------------------------------
        # Capability flags (populated in start() via _probe_capabilities())
        # ------------------------------------------------------------------
        self.can_set_volume_pw: bool = False
        """True when pw-cli volume writes are permitted (primary path, probe result)."""
        self.can_set_volume: bool = True
        """True when pulsectl volume writes are permitted (fallback path, probe result)."""
        self.can_move_stream: bool = True
        """True when pactl move-sink-input is available (probe result)."""
        self.pw_dump_available: bool = False
        """True when the ``pw-dump`` binary is present on PATH."""
        self.pw_cli_available: bool = False
        """True when the ``pw-cli`` binary is present on PATH."""
        self.wpctl_available: bool = False
        """True when ``wpctl`` is present and can reach the PipeWire session (Flatpak-safe PW write path)."""

        # PW-only mode: True when the PulseAudio socket is unavailable/blocked
        # (e.g. ``--nosocket=pulseaudio`` in Flatpak) but PipeWire tools are
        # reachable.  All PA-dependent codepaths are skipped in this mode.
        self.pw_only_mode: bool = False
        """True when operating in PW-only mode (no PulseAudio socket)."""

        self.owned_gain_supported: bool = True
        """True when the PipeWire owned gain node probe succeeded (set by _probe_owned_gain())."""
        self.loopback_backend_supported: bool = False
        """True when the pw-loopback virtual node probe succeeded (set by _probe_loopback_backend())."""

        # Routing owner: resolved effective owner after startup detection.
        # Matches ConfigManager.routing_owner values ("nativmix"|"easyeffects"|"none").
        # "auto" is only stored in config; this attribute always holds the resolved value.
        self.routing_owner: str = "nativmix"
        """Resolved routing owner policy: ``"nativmix"`` | ``"easyeffects"`` | ``"none"``."""

        # PW-only poller thread (used instead of _AudioListenerThread in PW-only mode)
        self._pw_poller_thread: _PipeWirePollerThread | None = None

        # PipeWire-native node inventory (Phase 2).
        # Refreshed by _refresh_pw_nodes() after the audit settles.
        # Maps node_id → PipeWireNode for quick lookup.
        self._pw_nodes: dict[int, PipeWireNode] = {}
        self._pw_nodes_lock = threading.Lock()

        # Stable ID cache: app_name_lc → (set[node_id], set[client_id])
        # Populated as streams are successfully matched and controlled.
        # Used to speed up future lookups (Phase 3).
        self._stable_ids: dict[str, tuple[set[int], set[int]]] = {}

        # PW identity binding: app_name_lc → PwIdentityTuple
        # Richer than _stable_ids: also stores node_name and process_binary
        # so future pw-dump scans can re-anchor on field matches even when
        # numeric IDs change (e.g. after an app restart).  Updated every time
        # a node is successfully matched.
        self._pw_identity: dict[str, PwIdentityTuple] = {}
        self._owned_gain_paths: dict[str, PwOwnedGainPath] = {}
        self._owned_route_paths: dict[str, PwOwnedRoutePath] = {}
        self._pw_owned_path_status: str = "inactive"
        self._pw_owned_path_reason: str = ""

        # Set of app target names (original case) that could not be resolved in
        # the most recent volume-apply cycle.  Used to drive the UI "unresolved"
        # indicator.  Bindings are never cleared when a target is unresolved.
        self._unresolved_targets: set[str] = set()
        self._unresolved_lock = threading.Lock()

        # Flatpak hard guard: when True, PA sink-input fallback writes are
        # suppressed for any target that cannot be resolved via the PW-native
        # path.  Initialised from IS_FLATPAK so the policy is driven by a
        # single instance flag rather than scattered bare IS_FLATPAK checks.
        self._flatpak_hard_guard: bool = IS_FLATPAK

    # ------------------------------------------------------------------
    # Public stream access (for GUI)
    # ------------------------------------------------------------------

    def is_valid_app_stream(self, stream_info: StreamInfo | None) -> bool:
        """
        Standardized filter for manageable apps.
        Returns True if the app is valid/controllable.
        Returns False if it's an internal loopback, monitor, or system stream.
        """
        if stream_info is None:
            return False

        # 1. Base filter via proplist
        if _is_internal_stream(stream_info.props):
            return False

        # 2. Add-on check: filter by resolved app_name for cases where the props
        # dict does not carry application.name (e.g. PW-only nodes resolved from
        # node.name / media.name).
        _internal_keywords = [
            "loopback", "monitor", "peak detect", "dummy",
            "speech-dispatcher", "nativmix",
        ]
        name_lower = stream_info.app_name.lower()
        if any(kw in name_lower for kw in _internal_keywords):
            return False

        return True

    def get_unresolved_targets(self) -> set[str]:
        """Return a snapshot of app target names that could not be resolved in the last volume cycle."""
        with self._unresolved_lock:
            return set(self._unresolved_targets)

    def _on_pw_nodes_changed(self, nodes: list) -> None:
        """
        Slot: called by :class:`_PipeWirePollerThread` when the PW node inventory
        changes (PW-only mode).

        Updates the internal node map and refreshes the active-stream cache so
        the GUI channel strips reflect the current playback applications.
        """
        with self._pw_nodes_lock:
            self._pw_nodes = {n.node_id: n for n in nodes}
        self._refresh_owned_gain_paths()
        # Rebuild the active-stream cache from the new nodes
        self.get_active_streams()

    def _get_pw_owned_node_candidates(self) -> list[PipeWireNode]:
        """Return writable NativMix-owned PW nodes suitable as gain targets."""
        with self._pw_nodes_lock:
            nodes_snapshot = list(self._pw_nodes.values())
        return [
            node
            for node in nodes_snapshot
            if "w" in node.permissions
            and (
                "nativmix" in (node.app_name or "").lower()
                or "nativmix" in (node.node_name or "").lower()
                or "nativmix" in (node.media_name or "").lower()
            )
        ]

    def _iter_pw_owned_route_candidates(self, app_name: str) -> list[PipeWireNode]:
        """Return NativMix-owned route nodes associated with *app_name*."""
        target_norm = _normalize_name(app_name)
        candidates: list[PipeWireNode] = []
        for node in self._get_pw_owned_node_candidates():
            route_target = _normalize_name(
                node.props.get("target.object", "")
                or node.props.get("node.target", "")
                or node.props.get("application.name.target", "")
            )
            if route_target and route_target != target_norm:
                continue
            candidates.append(node)
        return candidates

    def _select_owned_gain_node_for_app(self, app_name: str) -> PipeWireNode | None:
        """Pick the owned writable node to control for *app_name* in PW-only mode."""
        candidates = self._iter_pw_owned_route_candidates(app_name)
        target_norm = _normalize_name(app_name)
        for node in candidates:
            role = _normalize_name(
                node.props.get("nativmix.role", "")
                or node.props.get("media.name", "")
                or node.node_name
            )
            if "gain" in role:
                return node
        for node in candidates:
            route_target = _normalize_name(
                node.props.get("target.object", "")
                or node.props.get("node.target", "")
                or node.props.get("application.name.target", "")
            )
            if route_target and route_target == target_norm:
                return node
        return candidates[0] if candidates else None

    def _build_owned_route_path(self, app_name: str) -> PwOwnedRoutePath:
        """Build owned route-path metadata for *app_name* from the current PW graph."""
        route = PwOwnedRoutePath(app_name=app_name)
        route_candidates = self._iter_pw_owned_route_candidates(app_name)
        target_norm = _normalize_name(app_name)
        for node in route_candidates:
            role = _normalize_name(
                node.props.get("nativmix.role", "")
                or node.props.get("media.name", "")
                or node.node_name
            )
            route_target = _normalize_name(
                node.props.get("target.object", "")
                or node.props.get("node.target", "")
                or node.props.get("application.name.target", "")
            )
            if route_target and route_target != target_norm:
                continue
            is_writable = "w" in node.permissions
            if "input" in role and route.input_node_id == 0:
                route.input_node_id = node.node_id
                route.input_node_name = node.node_name
            elif "output" in role and route.output_node_id == 0:
                route.output_node_id = node.node_id
                route.output_node_name = node.node_name
            elif route.gain_node_id == 0:
                route.gain_node_id = node.node_id
                route.gain_node_name = node.node_name
                route.gain_control_writable = is_writable

        route.writable = route.gain_node_id > 0 and route.gain_control_writable
        route.active = route.writable
        if not route.active:
            missing: list[str] = []
            if not route.input_node_id:
                missing.append("input node")
            if not route.gain_node_id:
                missing.append("gain node")
            if not route.output_node_id:
                missing.append("output node")
            if route.gain_node_id and not route.gain_control_writable:
                missing.append("w permission")
            route.degraded_reason = "missing " + ", ".join(missing) if missing else "missing writable owned path"
        return route

    def _ensure_pw_owned_gain_path(self, app_name: str) -> PwOwnedRoutePath:
        """Ensure the owned PW-only graph exists for *app_name* and refresh path state."""
        if not self.pw_only_mode or self.routing_owner != "nativmix":
            return PwOwnedRoutePath(app_name=app_name, degraded_reason="inactive")
        if not self.owned_gain_supported:
            return PwOwnedRoutePath(
                app_name=app_name,
                degraded_reason="PW owned gain unsupported in this runtime",
            )
        route = self._create_pw_owned_route(app_name)
        self._refresh_pw_nodes()
        refreshed = self._build_owned_route_path(app_name)
        if route.degraded_reason and not refreshed.degraded_reason:
            refreshed.degraded_reason = route.degraded_reason
        self._owned_route_paths[app_name.lower()] = refreshed
        return refreshed

    def _run_pw_command(self, cmd: list[str]) -> tuple[bool, str, str]:
        """Run a PipeWire-related subprocess command and return success/stdout/stderr."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return False, "", f"timed out after {_SUBPROCESS_TIMEOUT}s"
        except OSError as exc:
            return False, "", str(exc)
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()

    def _get_default_sink_node_name(self) -> str | None:
        """Return the current default hardware sink name, excluding NativMix-owned nodes."""
        if not shutil.which("wpctl"):
            return None
        ok, stdout, stderr = self._run_pw_command(["wpctl", "status", "--name"])
        if not ok:
            logger.debug("_get_default_sink_node_name: wpctl status failed: %s", stderr)
            return None
        in_sinks = False
        default_name: str | None = None
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("Sinks:"):
                in_sinks = True
                continue
            if in_sinks and not raw_line.startswith((" ", "	")):
                break
            if not in_sinks or "." not in line or "[vol:" not in line:
                continue
            entry = line.lstrip("* ").strip()
            parts = entry.split(".", 1)
            if len(parts) != 2:
                continue
            name = parts[1].split("[vol:", 1)[0].strip()
            if name.startswith("NativMix_"):
                continue
            if raw_line.lstrip().startswith("*"):
                return name
            if default_name is None:
                default_name = name
        return default_name

    def _stable_owned_gain_node_name(self, app_name: str) -> str:
        """Return a stable unique node.name for an owned gain node."""
        app_token = re.sub(r"[^a-z0-9]+", "-", _normalize_name(app_name)).strip("-") or "app"
        return f"nativmix-owned-gain-{app_token}"

    def _pw_dump_nodes_with_raw(self) -> tuple[list[PipeWireNode], list[dict[str, object]]]:
        """Return parsed pw-dump nodes plus raw objects for diagnostics."""
        if not shutil.which("pw-dump"):
            return [], []
        ok, stdout, stderr = self._run_pw_command(["pw-dump"])
        if not ok:
            logger.debug("_pw_dump_nodes_with_raw: pw-dump failed: %s", stderr)
            return [], []
        try:
            raw = json.loads(stdout)
        except Exception as exc:
            logger.debug("_pw_dump_nodes_with_raw: invalid JSON: %s", exc)
            return [], []
        parsed: list[PipeWireNode] = []
        for obj in raw:
            if not isinstance(obj, dict):
                continue
            obj_type = str(obj.get("type", ""))
            if "Node" not in obj_type:
                continue
            info = obj.get("info", {})
            if not isinstance(info, dict):
                continue
            props_raw = info.get("props", {})
            if not isinstance(props_raw, dict):
                continue
            props = {str(k): str(v) for k, v in props_raw.items()}
            try:
                node_id = int(obj.get("id", 0))
            except (TypeError, ValueError):
                node_id = 0
            try:
                client_id = int(props.get("client.id", "0"))
            except (TypeError, ValueError):
                client_id = 0
            permissions_raw = obj.get("permissions", [])
            permissions = [str(p) for p in permissions_raw] if isinstance(permissions_raw, list) else []
            parsed.append(PipeWireNode(
                node_id=node_id,
                client_id=client_id,
                app_name=props.get("application.name", ""),
                process_binary=props.get("application.process.binary", ""),
                media_name=props.get("media.name", ""),
                media_class=props.get("media.class", ""),
                app_id=props.get("application.id", "") or props.get("pipewire.access.portal.app_id", ""),
                node_name=props.get("node.name", ""),
                props=props,
                permissions=permissions,
            ))
        return parsed, raw

    def _resolve_created_owned_route(self, app_name: str, node_name: str) -> PwOwnedRoutePath:
        """Poll pw-dump briefly to resolve a newly created owned route node by exact node.name."""
        deadline = time.monotonic() + 2.0
        candidates: list[str] = []
        while time.monotonic() < deadline:
            nodes, raw = self._pw_dump_nodes_with_raw()
            if nodes:
                with self._pw_nodes_lock:
                    self._pw_nodes = {n.node_id: n for n in nodes}
                route = self._build_owned_route_path(app_name)
                if route.gain_node_name == node_name:
                    return route
                if not candidates:
                    matches: list[str] = []
                    for obj in raw:
                        if not isinstance(obj, dict):
                            continue
                        info = obj.get("info", {})
                        if not isinstance(info, dict):
                            continue
                        props_raw = info.get("props", {})
                        if not isinstance(props_raw, dict):
                            continue
                        cand_name = str(props_raw.get("node.name", ""))
                        if cand_name.startswith(node_name) or node_name.startswith(cand_name):
                            matches.append(cand_name)
                    candidates = matches[:3]
            time.sleep(0.1)
        logger.warning(
            "Owned gain node unresolved for %r after create: node.name=%s candidates=%s",
            app_name,
            node_name,
            candidates or [],
        )
        return self._build_owned_route_path(app_name)

    def _probe_owned_gain(self) -> None:
        """
        Run a disposable create+resolve probe for an owned gain node.

        Creates a temporary filter-chain gain node with a known probe name,
        polls pw-dump for up to 2 s to check if it resolves, then destroys it.
        Sets ``owned_gain_supported`` accordingly and logs once on failure.
        Emits ``capability_changed("owned_gain_supported", ...)``.

        Only meaningful in PW-only mode (where owned gain paths are used).
        """
        if not self.pw_cli_available:
            logger.info(
                "_probe_owned_gain: pw-cli unavailable — skipping probe, "
                "marking owned_gain_supported=False",
            )
            self.owned_gain_supported = False
            self.capability_changed.emit("owned_gain_supported", False)
            return

        probe_name = "nativmix-probe-owned-gain"
        filter_graph = (
            '{ nodes = [ { type = builtin name = gain plugin = volume label = volume '
            'control = { Volume = 1.0 } } ] inputs = [ "in_L" "in_R" ] '
            'outputs = [ "out_L" "out_R" ] }'
        )
        props = [
            "factory.name=filter-chain",
            f"node.name={probe_name}",
            "node.description=NativMix Probe",
            "media.class=Audio/Duplex",
            f"filter.graph={filter_graph}",
        ]
        cmd = ["pw-cli", "create-node", "adapter"] + props
        ok, _stdout, stderr = self._run_pw_command(cmd)
        if not ok:
            logger.warning(
                "_probe_owned_gain: create-node failed (stderr=%r) — "
                "marking owned_gain_supported=False",
                stderr,
            )
            self.owned_gain_supported = False
            self.capability_changed.emit("owned_gain_supported", False)
            return

        # Poll pw-dump for up to 2 s to see if the probe node appears.
        probe_id: int = 0
        deadline = time.monotonic() + 2.0
        resolved = False
        while time.monotonic() < deadline:
            nodes, _ = self._pw_dump_nodes_with_raw()
            for node in nodes:
                if node.node_name == probe_name:
                    probe_id = node.node_id
                    resolved = True
                    break
            if resolved:
                break
            time.sleep(0.1)

        # Destroy the probe node.
        if probe_id:
            self._run_pw_command(["pw-cli", "destroy", str(probe_id)])
        else:
            self._run_pw_command(["pw-cli", "destroy", probe_name])

        if not resolved:
            logger.warning(
                "PW owned gain unsupported in this runtime "
                "(probe node %r not resolved within 2 s)",
                probe_name,
            )
            self.owned_gain_supported = False
        else:
            logger.info(
                "_probe_owned_gain: probe node %r resolved (node_id=%d) — "
                "owned gain is supported",
                probe_name, probe_id,
            )
            self.owned_gain_supported = True
        self.capability_changed.emit("owned_gain_supported", self.owned_gain_supported)

    def _probe_loopback_backend(self) -> None:
        """
        Probe whether a pw-loopback / WirePlumber-managed virtual node is
        available as an alternate backend for per-app volume control.

        Uses ``pw-cli create-node loopback`` to attempt virtual node creation
        and polls pw-dump for resolution.  Sets ``loopback_backend_supported``
        and logs the outcome once.  Emits
        ``capability_changed("loopback_backend_supported", ...)``.

        Only called when ``owned_gain_supported`` is False.
        """
        if not self.pw_cli_available:
            logger.info(
                "_probe_loopback_backend: pw-cli unavailable — skipping probe",
            )
            self.loopback_backend_supported = False
            self.capability_changed.emit("loopback_backend_supported", False)
            return

        probe_name = "nativmix-probe-loopback"
        ok, _stdout, stderr = self._run_pw_command([
            "pw-cli", "create-node", "loopback",
            f"node.name={probe_name}",
            "media.class=Audio/Duplex",
            "object.linger=false",
        ])
        if not ok:
            logger.info(
                "_probe_loopback_backend: pw-loopback probe failed (stderr=%r) — "
                "alternate backend unavailable",
                stderr,
            )
            self.loopback_backend_supported = False
            self.capability_changed.emit("loopback_backend_supported", False)
            return

        # Poll pw-dump for up to 2 s.
        probe_id: int = 0
        deadline = time.monotonic() + 2.0
        resolved = False
        while time.monotonic() < deadline:
            nodes, _ = self._pw_dump_nodes_with_raw()
            for node in nodes:
                if node.node_name == probe_name:
                    probe_id = node.node_id
                    resolved = True
                    break
            if resolved:
                break
            time.sleep(0.1)

        if probe_id:
            self._run_pw_command(["pw-cli", "destroy", str(probe_id)])

        if resolved:
            logger.info(
                "_probe_loopback_backend: pw-loopback probe resolved (node_id=%d) — "
                "alternate loopback backend available",
                probe_id,
            )
            self.loopback_backend_supported = True
        else:
            logger.info(
                "_probe_loopback_backend: pw-loopback probe not resolved within 2 s — "
                "alternate backend unavailable",
            )
            self.loopback_backend_supported = False
        self.capability_changed.emit("loopback_backend_supported", self.loopback_backend_supported)

    def _create_pw_filter_chain_node(self, app_name: str, role: str) -> bool:
        """Create a named NativMix-owned filter-chain node for *app_name* and *role*."""
        if not shutil.which("pw-cli"):
            return False
        if role != "gain":
            logger.debug("_create_pw_filter_chain_node(%r, %s): skipped non-gain role", app_name, role)
            return False
        node_name = self._stable_owned_gain_node_name(app_name)
        filter_graph = (
            '{ nodes = [ { type = builtin name = gain plugin = volume label = volume '
            'control = { Volume = 1.0 } } ] inputs = [ "in_L" "in_R" ] '
            'outputs = [ "out_L" "out_R" ] }'
        )
        props = [
            "factory.name=filter-chain",
            f"node.name={node_name}",
            "node.description=NativMix Owned Gain",
            "media.name=NativMix Owned Gain",
            "media.class=Audio/Duplex",
            f"nativmix.role={role}",
            f"target.object={app_name}",
            f"capture.props={{ node.name={node_name}.capture media.class=Audio/Source object.linger=true target.object={app_name} nativmix.role=input }}",
            f"playback.props={{ node.name={node_name}.playback media.class=Audio/Sink object.linger=true target.object={app_name} nativmix.role=output }}",
            f"filter.graph={filter_graph}",
        ]
        cmd = ["pw-cli", "create-node", "adapter"] + props
        ok, stdout, stderr = self._run_pw_command(cmd)
        logger.info(
            "_create_pw_filter_chain_node(%r, %s): cmd=%s ok=%s stdout=%r stderr=%r",
            app_name, role, cmd, ok, stdout, stderr,
        )
        return ok

    def _create_pw_owned_links(self, app_name: str, route: PwOwnedRoutePath) -> bool:
        """Create loopback links for an owned route using pw-link."""
        if not shutil.which("pw-link"):
            return False
        if not route.input_node_name or not route.output_node_name:
            return False
        target_sink = self._get_default_sink_node_name()
        if not target_sink:
            logger.debug("_create_pw_owned_links(%r): no default sink available", app_name)
            return False
        app_pattern = re.escape(app_name)
        ok_in = routing.smart_link(
            source_pattern=app_pattern,
            target_pattern=re.escape(route.input_node_name),
            source_dir="output",
            target_dir="input",
            source_port_pattern="output_|playback_|monitor_",
            target_port_pattern="input_",
        )
        ok_out = routing.smart_link(
            source_pattern=re.escape(route.output_node_name),
            target_pattern=re.escape(target_sink),
            source_dir="output",
            target_dir="input",
            source_port_pattern="output_",
            target_port_pattern="input_|playback_",
        )
        return ok_in and ok_out

    def _create_pw_owned_route(self, app_name: str) -> PwOwnedRoutePath:
        """Create the PW-only owned filter-chain path and loopback links for *app_name*."""
        route = self._build_owned_route_path(app_name)
        if route.active and route.writable:
            return route
        if not self.pw_cli_available:
            route.degraded_reason = "pw-cli unavailable"
            return route

        created = False
        if not route.gain_node_id:
            if self._create_pw_filter_chain_node(app_name, "gain"):
                created = True
        if created:
            route = self._resolve_created_owned_route(app_name, self._stable_owned_gain_node_name(app_name))

        if route.gain_node_id and route.writable:
            if self._create_pw_owned_links(app_name, route):
                route.active = True
                route.degraded_reason = ""
            elif not route.degraded_reason:
                route.degraded_reason = "failed to link owned route"
        elif not route.degraded_reason:
            route.degraded_reason = "missing writable owned path"
        return route

    def _refresh_owned_gain_paths(self) -> None:
        """Refresh PW-only owned writable gain-path state and emit concise status."""
        if not self.pw_only_mode or self.routing_owner != "nativmix":
            self._owned_gain_paths = {}
            self._owned_route_paths = {}
            self._pw_owned_path_status = "inactive"
            self._pw_owned_path_reason = ""
            return

        apps: list[str] = []
        for ch in range(self._config.num_channels):
            if self._config.get_channel_mode(ch) != "hardware":
                apps.extend(self._config.get_app_names(ch))

        new_paths: dict[str, PwOwnedGainPath] = {}
        new_routes: dict[str, PwOwnedRoutePath] = {}
        for app_name in apps:
            if not app_name or app_name.lower() in ("system master", "other apps"):
                continue
            key = app_name.lower()
            route = self._build_owned_route_path(app_name)
            new_routes[key] = route
            if route.gain_node_id:
                new_paths[key] = PwOwnedGainPath(
                    app_name=app_name,
                    node_id=route.gain_node_id,
                    node_name=route.gain_node_name,
                    writable=route.writable,
                    available=route.active,
                    degraded_reason=route.degraded_reason,
                )
            else:
                new_paths[key] = PwOwnedGainPath(
                    app_name=app_name,
                    node_id=0,
                    node_name="",
                    writable=False,
                    available=False,
                    degraded_reason=route.degraded_reason or "missing writable owned path",
                )

        self._owned_gain_paths = new_paths
        self._owned_route_paths = new_routes
        if any(path.available and path.writable for path in new_paths.values()):
            available = next(path for path in new_paths.values() if path.available and path.writable)
            status = (
                f"PW-only owned gain path ready: {available.node_name or available.node_id} "
                f"(writable=True)"
            )
            if (self._pw_owned_path_status, self._pw_owned_path_reason) != ("ready", status):
                self._pw_owned_path_status = "ready"
                self._pw_owned_path_reason = status
                logger.info(status)
                self.status_changed.emit("pw_only", status)
        elif new_paths:
            reason = "PW-only degraded: missing writable NativMix-owned gain path"
            if (self._pw_owned_path_status, self._pw_owned_path_reason) != ("degraded", reason):
                self._pw_owned_path_status = "degraded"
                self._pw_owned_path_reason = reason
                logger.warning(reason)
                self.status_changed.emit("degraded", reason)
        else:
            self._pw_owned_path_status = "inactive"
            self._pw_owned_path_reason = ""

    def get_active_streams(self) -> list[StreamInfo]:
        """
        Return a snapshot of all currently active audio streams.

        When PipeWire tools are available (``can_set_volume_pw=True``) the list
        is derived from the PipeWire-native node inventory built by ``pw-dump``
        so that stream enumeration stays consistent with the write path.  In
        PW-only mode (no PulseAudio socket) this is the sole path.  When PW
        tools are absent the list falls back to PulseAudio sink-inputs.
        """
        if not self._running:
            return []

        if self.pw_only_mode or self.can_set_volume_pw:
            return self._get_active_streams_pw_only()

        result: list[StreamInfo] = []
        try:
            with pulsectl.Pulse("nativmix-lister") as pulse:
                assigned_apps = self._config.get_all_assigned_apps_by_name()
                unmapped_found = []

                for si in pulse.sink_input_list():
                    info = _AudioListenerThread._build_stream_info(si)

                    if not self.is_valid_app_stream(info):
                        continue

                    result.append(info)
                    with self._state_lock:
                        self._active_streams[si.index] = info

                    # Check if unmapped (for Tooltip)
                    res_low = info.app_name.lower()
                    if res_low not in assigned_apps and res_low != "system master":
                        if info.app_name not in unmapped_found:
                            unmapped_found.append(info.app_name)

                # Emit unmapped apps list for the "Other Apps" tooltip
                unmapped_found.sort()
                if self._last_other_apps != unmapped_found:
                    self._last_other_apps = unmapped_found
                    self.other_apps_changed.emit(unmapped_found)

        except pulsectl.PulseError as exc:
            logger.error("Failed to list active streams: %s", exc)
            with self._state_lock:
                return list(self._active_streams.values())

        return result

    def _get_active_streams_pw_only(self) -> list[StreamInfo]:
        """
        Build the active-stream list from the PipeWire-native node inventory.

        Called by :meth:`get_active_streams` whenever PipeWire tools are
        available (``can_set_volume_pw=True``), including when PulseAudio is
        also present.  Using the PW inventory keeps stream enumeration
        consistent with the PW-native write path.

        Each audio output stream node from ``pw-dump`` is converted to a
        :class:`StreamInfo` using metadata fields in priority order:

        1. ``application.name``
        2. ``node.name``
        3. ``media.name``
        4. ``application.process.binary``

        Internal/system nodes (loopback, monitor, NativMix) are filtered out
        via :meth:`is_valid_app_stream`.

        The PW ``node.id`` is used as the ``StreamInfo.index`` (instead of a
        PA sink-input index) so volume writes can target the correct node via
        wpctl/pw-cli without a PA connection.
        """
        from nativmix.audio.base import StreamInfo as _SI

        with self._pw_nodes_lock:
            nodes_snapshot = list(self._pw_nodes.values())

        result: list[_SI] = []
        assigned_apps = self._config.get_all_assigned_apps_by_name()
        unmapped_found: list[str] = []

        # Use a fresh pw-dump snapshot if the internal cache is empty (e.g. on startup)
        if not nodes_snapshot and self.pw_dump_available:
            try:
                nodes_snapshot = _pw_dump_nodes()
                with self._pw_nodes_lock:
                    self._pw_nodes = {n.node_id: n for n in nodes_snapshot}
            except Exception as exc:
                logger.debug("_get_active_streams_pw_only: pw-dump fallback failed: %s", exc)

        seen_app_names: set[str] = set()
        for node in nodes_snapshot:
            # Determine a human-readable name from PW node properties
            app_name = (
                node.app_name
                or node.node_name
                or node.media_name
                or node.process_binary
                or "Unknown"
            )

            # Try binary map resolution for better names
            if node.process_binary:
                from nativmix.utils.proc_resolver import resolve_binary_name
                mapped = resolve_binary_name(node.process_binary)
                if mapped:
                    app_name = mapped

            # Build a minimal props dict so is_valid_app_stream filters correctly
            props: dict[str, str] = dict(node.props)

            info = StreamInfo(
                index=node.node_id,
                app_name=app_name,
                pid=0,
                volume=1.0,
                muted=False,
                props=props,
            )

            if not self.is_valid_app_stream(info):
                continue

            # De-duplicate by app name so multiple streams from the same app
            # (e.g. multiple Spotify audio threads) appear as one entry.
            if app_name in seen_app_names:
                continue
            seen_app_names.add(app_name)

            result.append(info)
            with self._state_lock:
                self._active_streams[node.node_id] = info

            res_low = app_name.lower()
            if res_low not in assigned_apps and res_low != "system master":
                if app_name not in unmapped_found:
                    unmapped_found.append(app_name)

        unmapped_found.sort()
        if self._last_other_apps != unmapped_found:
            self._last_other_apps = unmapped_found
            self.other_apps_changed.emit(unmapped_found)

        logger.debug(
            "PW active streams: %d nodes → %d unique apps",
            len(nodes_snapshot), len(result),
        )
        return result

    def get_v_sinks_debug(self) -> list[dict[str, Any]]:
        """Return detailed info about NativMix V-Sinks for CLI debugging."""
        results = []
        try:
            with pulsectl.Pulse("nativmix-debug-sinks") as pulse:
                sinks = pulse.sink_list()
                for ch in range(self._config.num_channels):
                    name = f"NativMix_CH_{ch}"
                    sink = next((s for s in sinks if s.name == name), None)
                    if sink:
                        results.append({
                            "channel": ch,
                            "name": sink.name,
                            "index": sink.index,
                            "volume": round(sum(sink.volume.values) / len(sink.volume.values), 2),
                            "muted": bool(sink.mute),
                            "description": sink.description
                        })
        except Exception as e:
            logger.error("Debug sinks failed: %s", e)
        return results

    def get_active_streams_debug(self) -> dict[str, Any]:
        """Return comprehensive info about detected apps and config for CLI debugging."""
        try:
            active = self.get_active_streams()
            assigned_apps = self._get_all_assigned_apps()

            # 1. Total active streams
            streams_list = []
            for info in active:
                streams_list.append({
                    "index": info.index,
                    "app_name": info.app_name,
                    "pid": info.pid,
                    "volume": round(info.volume, 2),
                    "muted": info.muted,
                    "binary": info.props.get("application.process.binary", "N/A"),
                    "class": info.props.get("media.class", "N/A"),
                    "is_unmapped": (
                        info.app_name.lower() not in assigned_apps
                        and info.app_name.lower() != "system master"
                    ),
                    "anonymous": (info.pid == 0 and info.app_name.lower() in GENERIC_PA_NAMES),
                })

            # 2. Configured apps vs Running status
            config_report = {}
            active_names = {s.app_name.lower() for s in active}
            for ch in range(self._config.num_channels):
                apps = self._config.get_app_names(ch)
                if not apps:
                    continue

                ch_apps = []
                for a in apps:
                    ch_apps.append({
                        "name": a,
                        "is_running": (a.lower() in active_names or a.lower() == "system master")
                    })
                config_report[f"Channel_{ch}"] = ch_apps

            # 3. PipeWire-native node inventory snapshot (Phase 2).
            with self._pw_nodes_lock:
                pw_snapshot = list(self._pw_nodes.values())
            pw_nodes_list = [
                {
                    "node_id": n.node_id,
                    "client_id": n.client_id,
                    "app_name": n.app_name,
                    "node_name": n.node_name,
                    "binary": n.process_binary,
                    "media_name": n.media_name,
                    "media_class": n.media_class,
                    "app_id": n.app_id,
                }
                for n in pw_snapshot
            ]

            return {
                "active_streams": streams_list,
                "configured_channels": config_report,
                "unmapped_summary": [s["app_name"] for s in streams_list if s["is_unmapped"]],
                "pw_nodes": pw_nodes_list,
                "capabilities": {
                    "can_set_volume_pw": self.can_set_volume_pw,
                    "can_set_volume": self.can_set_volume,
                    "can_move_stream": self.can_move_stream,
                    "pw_dump_available": self.pw_dump_available,
                    "pw_cli_available": self.pw_cli_available,
                    "wpctl_available": getattr(self, "wpctl_available", False),
                    "pw_only_mode": self.pw_only_mode,
                },
            }
        except Exception as e:
            logger.error("Debug apps failed: %s", e)
            return {"error": str(e)}


    # ------------------------------------------------------------------
    # Public API (AudioBackendBase)
    # ------------------------------------------------------------------

    def _update_thread_states(self) -> None:
        """Pushes current config/volumes to the Listener thread safely."""
        if not self._thread:
            return

        with self._state_lock:
            states = {
                ch: {
                    'vol': self._poti_volumes.get(ch, 0.5),
                    'v_sink': self._config.is_v_sink_enabled(ch),
                    'v_sink_busy': ch in self._vsink_creating,
                    'apps': self._config.get_app_names(ch),
                    'mode': self._config.get_channel_mode(ch),
                    'muted': self._channel_muted.get(ch, False),
                }
                for ch in range(self._config.num_channels)
            }
        with self._thread._states_lock:
            self._thread.channel_states = states

    def start(self) -> None:
        """Start the background audio event listener thread."""
        if self._thread is not None and self._thread.isRunning():
            logger.warning("PipeWireManager.start() called but thread is already running")
            return

        self._running = True

        # Startup self-check: log a warning if any legacy direct pactl move-sink-input
        # call-sites remain inside this class (should be zero after centralisation).
        self._startup_routing_self_check()

        # Phase 1: capability probe — verify which control paths are usable
        # before any real audio operations.
        caps = _probe_capabilities()
        self.can_set_volume_pw = caps["can_set_volume_pw"]
        self.can_set_volume = caps["can_set_volume"]
        self.can_move_stream = caps["can_move_stream"]
        self.pw_dump_available = caps["pw_dump_available"]
        self.pw_cli_available = caps["pw_cli_available"]
        self.wpctl_available = caps.get("wpctl_available", False)

        # Detect PW-only mode.
        #
        # PW-only mode is active when ANY of the following is true:
        #   1. NATIVMIX_FORCE_PW_ONLY=1 is set in the environment.
        #   2. Running inside a Flatpak sandbox (IS_FLATPAK=True) and PW tools
        #      are available — Pulse is kept only as a fallback, not the primary
        #      path, so Flatpak builds default to PW-only.
        #   3. PulseAudio socket is absent/blocked AND PW tools are available
        #      (original behaviour for non-Flatpak systems without PA).
        #
        # The Pulse path remains available as an optional fallback when
        # pulse_available is True and PW-only mode is not active.
        pulse_available = caps.get("pulse_available", False)
        force_pw_only = caps.get("force_pw_only", False)

        self.pw_only_mode = (
            force_pw_only
            or (IS_FLATPAK and self.can_set_volume_pw)
            or ((not pulse_available) and self.can_set_volume_pw)
        )

        if force_pw_only:
            logger.info(
                "PW-only mode forced via NATIVMIX_FORCE_PW_ONLY environment variable."
            )
        if self.pw_only_mode:
            reason = (
                "NATIVMIX_FORCE_PW_ONLY set" if force_pw_only
                else "Flatpak sandbox" if IS_FLATPAK
                else "PulseAudio socket unavailable"
            )
            logger.info(
                "PW-only mode activated (%s; wpctl=%s pw-dump=%s). "
                "PulseAudio path is optional fallback — skipping PA listener/audit/routing.",
                reason, self.wpctl_available, self.pw_dump_available,
            )
            self.status_changed.emit("pw_only", f"PW-only ({reason})")
        elif not self.can_set_volume_pw and not self.can_set_volume:
            logger.warning(
                "Neither PipeWire-native (pw-cli/wpctl) nor PulseAudio (pulsectl) "
                "volume control is available — volume changes will have no "
                "effect.  Ensure PipeWire is running and accessible."
            )
            self.status_changed.emit(
                "degraded",
                "Volume control unavailable: PipeWire not accessible.",
            )
        elif not self.can_set_volume_pw:
            logger.info(
                "PipeWire-native volume writes unavailable (pw-cli/wpctl) — "
                "using PulseAudio compat fallback for all write operations."
            )

        if not self.can_move_stream:
            logger.warning("pactl not found — stream routing (V-Sink move) disabled.")
        elif IS_FLATPAK:
            logger.info("Flatpak hard guard active: pactl stream moves disabled; PW-native V-Sink routing preferred.")

        # Log active write backend clearly for diagnostics.
        if self.wpctl_available:
            pw_write_backend = "wpctl (Flatpak-compatible)"
        elif self.pw_cli_available and self.can_set_volume_pw:
            pw_write_backend = "pw-cli"
        else:
            pw_write_backend = "none"
        logger.info(
            "Capability probe: PW write backend=%s can_set_volume_pw=%s "
            "can_set_volume=%s can_move_stream=%s pw_dump=%s pw_cli=%s wpctl=%s "
            "pulse_available=%s pw_only_mode=%s force_pw_only=%s",
            pw_write_backend,
            self.can_set_volume_pw, self.can_set_volume,
            self.can_move_stream, self.pw_dump_available,
            self.pw_cli_available, self.wpctl_available,
            pulse_available, self.pw_only_mode, force_pw_only,
        )

        # ------------------------------------------------------------------
        # Routing owner resolution
        # ------------------------------------------------------------------
        self.routing_owner = self._resolve_routing_owner()

        # Pre-populate _prev_app_names so the first mapping change doesn't
        # incorrectly treat all configured apps as "newly added".
        for ch in range(self._config.num_channels):
            self._prev_app_names[ch] = list(self._config.get_app_names(ch))

        # ------------------------------------------------------------------
        # Phase 1b: Owned gain node probe — only meaningful in PW-only mode,
        # where the filter-chain owned gain path is the sole per-app volume
        # control mechanism.  Run synchronously before the listener thread
        # starts so capability flags are stable before any volume tick arrives.
        # ------------------------------------------------------------------
        if self.pw_only_mode and self.routing_owner == "nativmix":
            self._probe_owned_gain()
            if not self.owned_gain_supported:
                self._probe_loopback_backend()

        # Seed _poti_volumes with last persisted channel volumes before the
        # thread starts scanning existing streams.  This ensures that MIDI
        # channels (which never get an automatic hardware tick on connect) use
        # the correct last-known position instead of the 0.5 fallback in
        # _update_thread_states / the 1.0 fallback in get_channel_volume.
        # Arduino channels are overwritten by the first hardware tick (~20 ms).
        with self._state_lock:
            for ch in range(self._config.num_channels):
                if ch not in self._poti_volumes:
                    self._poti_volumes[ch] = self._config.get_channel_volume(ch)

        if self.pw_only_mode:
            # ── PW-only path: skip PA listener, sink poller, and audit ──────
            self._pw_poller_thread = _PipeWirePollerThread()
            self._pw_poller_thread.streams_changed.connect(self._on_pw_nodes_changed)
            self._pw_poller_thread.status_changed.connect(self._on_thread_status_changed)
            self._pw_poller_thread.start()
            self._refresh_owned_gain_paths()
            # Trigger an immediate node refresh so the app list is populated
            # before the first volume tick arrives.
            QTimer.singleShot(500, self._refresh_pw_nodes)
            # Emit audit_finished so the StartupCoordinator in main.py can call
            # on_app_ready() → window.show().  perform_initial_audio_audit() skips
            # all PA steps in pw_only mode and only emits audit_finished.
            self.perform_initial_audio_audit()
        else:
            # ── Normal path: PA listener + sink poller + audit ───────────────
            self._thread = _AudioListenerThread(config=self._config)
            self._thread.routing_owner = self.routing_owner
            self._wire_thread_signals(self._thread)
            self._thread.start()
            self._update_thread_states()  # Initial push of states

            self._sink_poll_thread.master_volume_changed.connect(self._on_master_volume_changed)
            self._sink_poll_thread.default_sink_changed.connect(self._on_default_sink_changed)
            self._sink_poll_thread.start()

            # Audit and fix loopbacks / apps routing (replaces _adopt_existing_v_sinks)
            self.perform_initial_audio_audit()
        logger.info("PipeWireManager started (pw_only=%s)", self.pw_only_mode)

    def _wire_thread_signals(self, thread: _AudioListenerThread) -> None:
        """Connect all signals from a listener thread to our slots."""
        thread.stream_added.connect(self._on_stream_added)
        thread.stream_removed.connect(self._on_stream_removed)
        thread.stream_changed.connect(self._on_stream_changed)
        thread.status_changed.connect(self._on_thread_status_changed)
        thread.finished.connect(self._on_thread_finished)

    def _unwire_thread_signals(self, thread: _AudioListenerThread) -> None:
        """
        Disconnect all signals from a finished listener thread.

        Must be called before replacing self._thread to prevent duplicate
        slot invocations after a restart.  RuntimeError is silently ignored
        because pulsectl may have already cleaned up the Qt object.
        """
        try:
            thread.stream_added.disconnect(self._on_stream_added)
            thread.stream_removed.disconnect(self._on_stream_removed)
            thread.stream_changed.disconnect(self._on_stream_changed)
            thread.status_changed.disconnect(self._on_thread_status_changed)
            thread.finished.disconnect(self._on_thread_finished)
        except RuntimeError:
            pass  # Signal already disconnected — safe to ignore

    @pyqtSlot(str, str)
    def _on_thread_status_changed(self, status_type: str, message: str) -> None:
        """Forward status from the listener thread and reset restart counter on stable."""
        if status_type == "stable":
            self._restart_count = 0
        self.status_changed.emit(status_type, message)

    @pyqtSlot()
    def _on_thread_finished(self) -> None:
        """Restart the listener thread with exponential backoff if not intentionally stopped."""
        if not self._running:
            return  # intentional stop — do not restart

        wait = min(self._BACKOFF_BASE * (2 ** self._restart_count), self._BACKOFF_MAX)
        self._restart_count += 1
        logger.warning(
            "AudioListenerThread exited unexpectedly — restarting in %.0fs (attempt %d)",
            wait, self._restart_count,
        )
        self.status_changed.emit(
            "error_temporary",
            f"PipeWire lost — reconnecting in {wait:.0f}s...",
        )
        QTimer.singleShot(int(wait * 1000), self._restart_thread)

    @pyqtSlot()
    def _restart_thread(self) -> None:
        if not self._running:
            return
        logger.info("Restarting AudioListenerThread (attempt %d)", self._restart_count)

        # Disconnect the finished thread's signals BEFORE replacing the reference.
        # Without this, the old Qt object (kept alive as a child of PipeWireManager)
        # would retain live connections, causing duplicate slot invocations on the
        # next restart cycle.
        if self._thread is not None:
            self._unwire_thread_signals(self._thread)
            self._thread.deleteLater()

        self._thread = _AudioListenerThread(self._config)
        self._thread.routing_owner = self.routing_owner
        self._wire_thread_signals(self._thread)
        self._update_thread_states()
        self._thread.start()

        # Re-connect SinkPollThread signals in case they were disconnected when
        # the old listener thread was torn down (e.g. after a PipeWire crash).
        # Disconnecting first prevents duplicate connections on repeated restarts.
        try:
            self._sink_poll_thread.master_volume_changed.disconnect(self._on_master_volume_changed)
            self._sink_poll_thread.default_sink_changed.disconnect(self._on_default_sink_changed)
        except RuntimeError:
            pass  # Not connected yet — safe to ignore
        self._sink_poll_thread.master_volume_changed.connect(self._on_master_volume_changed)
        self._sink_poll_thread.default_sink_changed.connect(self._on_default_sink_changed)

        # Re-run the audio audit after PipeWire reconnects so V-Sinks are
        # recreated.  A 3 s delay lets PipeWire fully come up first.
        QTimer.singleShot(3000, self._post_reconnect_audit)

    def _post_reconnect_audit(self) -> None:
        if not self._running:
            return
        logger.info("Running post-reconnect audit (V-Sink recovery after PipeWire restart)")
        self.perform_initial_audio_audit()

    def stop(self) -> None:
        """Stop the listener thread gracefully."""
        self._running = False
        self._stream_refresh_timer.stop()
        # Flush MIDI CC volumes (set_channel_volume in-memory updates) to disk
        # so the next startup seeds _poti_volumes with the last known positions.
        try:
            self._config.save()
        except Exception as exc:
            logger.warning("Could not save config during stop: %s", exc)
        # Stop PW-only poller (if active)
        if self._pw_poller_thread is not None:
            self._pw_poller_thread.stop()
            self._pw_poller_thread.deleteLater()
            self._pw_poller_thread = None
        # Stop PA sink poller and listener (normal mode)
        if not self.pw_only_mode:
            self._sink_poll_thread.stop()
        if self._thread is not None:
            # Disconnect first so no signals fire during or after stop().
            self._unwire_thread_signals(self._thread)
            self._thread.stop()
            self._thread.deleteLater()
            self._thread = None
        if self._vol_pulse is not None:
            try:
                self._vol_pulse.disconnect()
            except Exception:
                pass
            self._vol_pulse = None
        self._last_applied_volumes.clear()
        logger.debug("PipeWireManager stopped")

    def _get_vol_pulse(self) -> pulsectl.Pulse | None:
        """Return (and lazily reconnect) the persistent volume-ops connection.

        Reusing one long-lived connection instead of opening a new Pulse()
        on every poti/MIDI tick eliminates libpulse C-heap churn that would
        otherwise cause gradual RSS growth.
        """
        try:
            if self._vol_pulse is None:
                self._vol_pulse = pulsectl.Pulse("nativmix-vol-ops")
            return self._vol_pulse
        except Exception:
            self._vol_pulse = None
            return None

    def _should_apply_volume(self, target_type: str, target_id: str, volume: float) -> bool:
        """Return True if a target volume materially changed since our last write."""
        key = (target_type, target_id.lower())
        previous = self._last_applied_volumes.get(key)
        if previous is not None and abs(previous - volume) < 0.001:
            return False
        self._last_applied_volumes[key] = volume
        return True

    def _check_tools(self) -> dict[str, bool]:
        """Check availability of required system tools (pactl, pw-link)."""
        return {tool: shutil.which(tool) is not None for tool in ("pactl", "pw-link")}

    def _startup_routing_self_check(self) -> None:
        """
        Scan the manager's own source for any direct ``pactl move-sink-input``
        or ``pactl set-sink-input-volume`` invocations that bypass the
        centralised write guards.

        This check runs once at startup and logs a warning if any such legacy
        call-site is found.  In a correct build the only ``subprocess.run`` call
        that references ``move-sink-input`` must be inside ``move_stream_to_vsink``
        itself, and ``set-sink-input-volume`` must not appear in class methods at
        all (volume writes go through ``_apply_volume_by_name``).
        """
        import inspect
        import ast

        try:
            source = inspect.getsource(type(self))
        except OSError:
            logger.debug("_startup_routing_self_check: could not retrieve source, skipping scan.")
            return

        try:
            tree = ast.parse(source)
        except SyntaxError:
            logger.debug("_startup_routing_self_check: could not parse source AST, skipping scan.")
            return

        legacy_lines: list[int] = []
        set_vol_lines: list[int] = []
        for node in ast.walk(tree):
            # Look for string literals containing "move-sink-input" that are
            # NOT inside the move_stream_to_vsink function definition.
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "move-sink-input" in node.value:
                    legacy_lines.append(getattr(node, "lineno", 0))
                # Also flag any direct "set-sink-input-volume" usage — all volume
                # writes must go through _apply_volume_by_name / the centralised
                # write guard so the Flatpak hard guard is always applied.
                if "set-sink-input-volume" in node.value:
                    set_vol_lines.append(getattr(node, "lineno", 0))

        # The only legitimate occurrence is the one inside move_stream_to_vsink
        # at module level — that function is not a method of this class, so it
        # will NOT appear in the class source returned by inspect.getsource.
        # Any hit found here is inside a class method and is therefore a legacy
        # call-site that should be migrated.
        if legacy_lines:
            logger.warning(
                "startup_routing_self_check: found %d legacy direct 'move-sink-input' "
                "string(s) inside %s methods at approximate lines %s — "
                "these should be routed through move_stream_to_vsink().",
                len(legacy_lines), type(self).__name__, legacy_lines,
            )
        else:
            logger.debug(
                "startup_routing_self_check: OK — no legacy direct 'move-sink-input' "
                "call-sites found inside %s.", type(self).__name__,
            )

        if set_vol_lines:
            logger.warning(
                "startup_routing_self_check: found %d direct 'set-sink-input-volume' "
                "string(s) inside %s methods at approximate lines %s — "
                "volume writes must go through _apply_volume_by_name() so the "
                "Flatpak hard guard is always enforced.",
                len(set_vol_lines), type(self).__name__, set_vol_lines,
            )
        else:
            logger.debug(
                "startup_routing_self_check: OK — no direct 'set-sink-input-volume' "
                "call-sites found inside %s.", type(self).__name__,
            )

    def _resolve_routing_owner(self) -> str:
        """
        Resolve the effective routing owner from config + runtime detection.

        When the persisted value is ``"auto"`` (or absent) the detection
        heuristic runs:

        * In Flatpak: default to ``"easyeffects"`` when Easy Effects is
          detected; otherwise default to ``"nativmix"``.
        * Outside Flatpak: always default to ``"nativmix"``.

        The resolved owner is persisted back to config so subsequent startups
        use the explicit value (user may override via settings).

        Logs an INFO line with the chosen owner and reason.

        Returns the resolved owner string: ``"nativmix"`` | ``"easyeffects"`` | ``"none"``.
        """
        configured = self._config.routing_owner

        if configured != "auto":
            logger.info(
                "routing_owner_selected=%s reason=persisted_config",
                configured,
            )
            return configured

        # Auto-detect
        ee_detected, ee_evidence = detect_easyeffects()
        logger.info(
            "easyeffects_detected=%s evidence=%r",
            ee_detected, ee_evidence,
        )

        if IS_FLATPAK and ee_detected:
            resolved = "easyeffects"
            reason = f"Flatpak + Easy Effects detected ({ee_evidence})"
        else:
            resolved = "nativmix"
            reason = (
                "Flatpak, no Easy Effects detected" if IS_FLATPAK
                else "non-Flatpak default"
            )

        # Persist resolved value so user can change it in settings
        self._config.routing_owner = resolved
        self._config.save()

        logger.info(
            "routing_owner_selected=%s reason=%r",
            resolved, reason,
        )
        return resolved

    def perform_initial_audio_audit(self) -> None:
        """
        1. Auto-Correction on Startup: Check all running apps and route them.
        2. Sink-to-Device Verification: Ensure all V-Sinks have valid loopbacks.
        3. Forced Re-Link: Refresh links to ensure audio is audible.

        In PW-only mode (no PulseAudio socket) all PA-dependent steps are skipped
        and only the completion signal is emitted.
        """
        if self.pw_only_mode:
            logger.info(
                "PW-only mode: skipping PA audio audit (V-Sink/routing-sync require PulseAudio)"
            )
            self.audit_finished.emit()
            QTimer.singleShot(2000, self._mark_audit_complete)
            return

        logger.debug("Performing critical audio audit & V-Sink re-validation...")
        tools = self._check_tools()
        if not tools["pactl"]:
            logger.error("CRITICAL: 'pactl' not found! Audio routing may fail.")
            # We continue anyway and try with pulsectl, but pactl is preferred for re-links

        try:
            with pulsectl.Pulse("nativmix-audit") as pulse:
                # 1. Map current modules for lookup
                modules = pulse.module_list()
                _loopbacks = [m for m in modules if m.name == "module-loopback"]
                _null_sinks = [m for m in modules if m.name == "module-null-sink"]

                # 0. Identify the real hardware sink FIRST (to restore it later).
                # Prime _last_hardware_sink so the first default_sink_changed event
                # from _restore_hardware_default_sink (end of audit) does not
                # incorrectly trigger a Hotplug re-link.
                hw_sink_node = self._get_master_hardware_sink(pulse)
                self._last_hardware_sink = hw_sink_node

                for ch in range(self._config.num_channels):
                    if self._config.is_v_sink_enabled(ch):
                        sink_name = f"NativMix_CH_{ch}"
                        logger.debug("Re-validating V-Sink: %s", sink_name)

                        # A. Ensure Sink exists & Metadata is current (Non-Destructive Reuse)
                        self.enable_v_sink(ch)
                        continue

                # D. Ensure the hardware remains the default sink (OSD BYPASS)
                self._restore_hardware_default_sink(pulse)
        except pulsectl.PulseError as exc:
            logger.error("Initial audio audit failed (V-Sink verification): %s", exc)

        # 4. Trigger "move-sink-input" for all mapped apps to force refresh
        self._sync_v_sink_routing()

        # 5. Signal completion of the audit
        self.audit_finished.emit()
        logger.debug("Audio audit completed.")
        # Allow hotplug handling only after a 2 s settling window.
        # PipeWire emits multiple sink-change events during V-Sink creation and
        # _restore_hardware_default_sink; the cooldown prevents those from
        # triggering a premature re-link.
        QTimer.singleShot(2000, self._mark_audit_complete)

    def set_volume(self, stream_index: int, volume: float) -> None:
        """
        Set the linear volume [0.0–1.0] for a specific sink input.

        Tries the PipeWire-native path (``pw-cli set-param``) first using the
        node_id resolved from the PW inventory.  Falls back to pulsectl when
        pw-cli is unavailable or the write fails.

        Returns immediately without contacting the audio server when both
        ``can_set_volume_pw`` and ``can_set_volume`` are False.
        """
        if not self.can_set_volume_pw and not self.can_set_volume:
            _throttled_warner.warn(
                "no_vol_cap_sv",
                "set_volume(%d): skipped — volume control unavailable",
                stream_index,
            )
            return
        volume = max(0.0, min(1.0, volume))

        # Attempt PipeWire-native write first.
        if self.can_set_volume_pw:
            node_id = self._resolve_node_id_for_sink_input(stream_index)
            if node_id and _pw_set_volume(node_id, volume):
                return
            # pw-cli write failed or node not found — fall through to pulsectl.

        # PulseAudio compat fallback.
        if not self.can_set_volume:
            return
        try:
            with pulsectl.Pulse("nativmix-volume-setter") as pulse:
                inputs = pulse.sink_input_list()
                target = next((si for si in inputs if si.index == stream_index), None)
                if target is not None:
                    pulse.volume_set_all_chans(target, volume)
        except pulsectl.PulseError as exc:
            _throttled_warner.warn(
                f"set_volume_{stream_index}",
                "set_volume(%d, %.2f) failed: %s",
                stream_index, volume, exc,
            )

    def set_mute(self, stream_index: int, muted: bool) -> None:
        """
        Toggle the mute state of a specific sink input.

        Tries the PipeWire-native path (``pw-cli set-param``) first using the
        node_id resolved from the PW inventory.  Falls back to pulsectl when
        pw-cli is unavailable or the write fails.

        Returns immediately without contacting the audio server when both
        ``can_set_volume_pw`` and ``can_set_volume`` are False.
        """
        if not self.can_set_volume_pw and not self.can_set_volume:
            _throttled_warner.warn(
                "no_vol_cap_sm",
                "set_mute(%d): skipped — volume control unavailable",
                stream_index,
            )
            return

        # Attempt PipeWire-native write first.
        if self.can_set_volume_pw:
            node_id = self._resolve_node_id_for_sink_input(stream_index)
            if node_id and _pw_set_mute(node_id, muted):
                return
            # pw-cli write failed or node not found — fall through to pulsectl.

        # PulseAudio compat fallback.
        if not self.can_set_volume:
            return
        try:
            with pulsectl.Pulse("nativmix-mute-setter") as pulse:
                pulse.sink_input_mute(stream_index, mute=muted)
        except pulsectl.PulseError as exc:
            _throttled_warner.warn(
                f"set_mute_{stream_index}",
                "set_mute(%d, %s) failed: %s",
                stream_index, muted, exc,
            )

    def _resolve_node_id_for_sink_input(self, sink_input_index: int) -> int:
        """
        Return the PipeWire node_id for a PA sink-input index, or 0 if unknown.

        Searches the PW-native inventory for a node whose ``object.serial``
        property matches *sink_input_index*.  This is the same correlation used
        in ``_apply_volume_by_name``.
        """
        with self._pw_nodes_lock:
            for node in self._pw_nodes.values():
                try:
                    if int(node.props.get("object.serial", "0")) == sink_input_index:
                        return node.node_id
                except (ValueError, TypeError):
                    continue
        return 0

    # ------------------------------------------------------------------
    # Signal handlers (called on the main/GUI thread by Qt's signal dispatch)
    # ------------------------------------------------------------------

    def _on_stream_added(self, info: StreamInfo) -> None:
        """Slot: track stream."""
        with self._state_lock:
            self._active_streams[info.index] = info
        logger.debug("Stream added: [%d] %s (pid=%d, vol=%.2f)", info.index, info.app_name, info.pid, info.volume)
        if not self._stream_refresh_timer.isActive():
            self._stream_refresh_timer.start()
        # Refresh PW-native inventory so the new node is available for matching.
        if self.pw_dump_available and self._initial_audit_complete:
            QTimer.singleShot(200, self._refresh_pw_nodes)

    def _on_stream_removed(self, index: int) -> None:
        """Slot: remove stream."""
        with self._state_lock:
            self._active_streams.pop(index, None)
        # Do NOT invalidate the PID cache here. PID→appname is stable for
        # the lifetime of a process; stream removal does not imply PID reuse.
        # Clearing on every removal causes cold-cache /proc walks for all
        # unrelated apps whenever any short-lived stream (e.g. Firefox media
        # elements) closes. Cache is cleared in _on_mapping_changed() when
        # the user actually changes an app assignment.
        logger.debug("Stream removed: [%d]", index)
        if not self._stream_refresh_timer.isActive():
            self._stream_refresh_timer.start()
        # Update PW-native inventory to drop the departed node.
        if self.pw_dump_available and self._initial_audit_complete:
            QTimer.singleShot(200, self._refresh_pw_nodes)

    def _on_stream_changed(self, info: StreamInfo) -> None:
        """Slot: update cached stream info on change."""
        with self._state_lock:
            self._active_streams[info.index] = info
        logger.debug("Stream changed: [%d] %s vol=%.2f muted=%s", info.index, info.app_name, info.volume, info.muted)

    def on_mapping_changed(self, channel_index: int, app_names: list[str]) -> None:
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

        # PW-only mode: there is no PA V-Sink and no PA sink-input to move, so
        # skip the legacy pactl move-sink-input machinery entirely (not just
        # a no-op inside it) and apply gain directly to matched PW nodes.
        if self.pw_only_mode:
            for name in app_names:
                self._apply_volume_by_name_pw_only(name, current_volume)
            self._update_thread_states()
            return

        # Routing owner guard: only nativmix may auto-route/move streams.
        if self.routing_owner != "nativmix":
            logger.info(
                "on_mapping_changed: channel=%d apps=%s routing blocked "
                "(routing_owner=%r — not allowed to reroute/move streams in this mode)",
                channel_index, app_names, self.routing_owner,
            )
            self._update_thread_states()
            return

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
                            pa_fallback = (
                                props.get("application.name")
                                or props.get("application.process.binary")
                                or "Unknown"
                            )
                            resolved = resolve_app_name(pid, fallback=pa_fallback)
                            if resolved.lower() not in removed:
                                continue

                            logger.debug(
                                "App '%s' removed from CH%d – evacuating to '%s'",
                                resolved, channel_index, default_sink.name
                            )

                            # _seamless_move: volume on old sink → pactl move → unmute
                            try:
                                pulse.volume_set_all_chans(si, current_volume)
                            except pulsectl.PulseError:
                                pass
                            self._seamless_move(pulse, si.index, default_sink.index, volume=None)
                            logger.debug(
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
                            pa_fallback = (
                                props.get("application.name")
                                or props.get("application.process.binary")
                                or "Unknown"
                            )
                            resolved = resolve_app_name(pid, fallback=pa_fallback)
                            if resolved.lower() not in added:
                                continue

                            logger.debug(
                                "App '%s' added to CH%d – routing into V-Sink '%s'",
                                resolved, channel_index, sink_name
                            )
                            # Route via the centralised helper (enforces Flatpak guard).
                            # Setting volume before the move would affect the old sink,
                            # so we apply unity gain only after a successful move.
                            moved = move_stream_to_vsink(si.index, sink_name, pulse)

                            # Apply unity gain AFTER the stream is on the V-Sink
                            if moved:
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
                            # Prefer PW graph/link routing under Flatpak; host builds may still move the stream.
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
        if self._config.input_mode == "midi_only":
            return

        shared_pulse = self._get_vol_pulse()
        if shared_pulse is None:
            return
        try:
            for channel, volume in enumerate(volumes):
                # Auto-unmute if the hardware slider moves significantly (>5% since muted)
                with self._state_lock:
                    is_muted = self._channel_muted.get(channel, False)
                    muted_vol = self._muted_at_volume.get(channel, volume)
                if is_muted and abs(volume - muted_vol) > 0.05:
                    self.toggle_mute(channel)
                    with self._state_lock:
                        # Update reference so we don't spam toggle_mute
                        self._muted_at_volume[channel] = volume

                with self._state_lock:
                    self._poti_volumes[channel] = volume
                    creating = channel in self._vsink_creating

                if creating:
                    continue

                mode = self._config.get_channel_mode(channel)
                if mode == "hardware":
                    hw_id = self._config.get_hardware_id(channel)
                    if hw_id and self._should_apply_volume("hardware", hw_id, volume):
                        self._apply_hardware_volume(hw_id, volume, pulse=shared_pulse)
                else:
                    if self._config.is_v_sink_enabled(channel):
                        if self._should_apply_volume("vsink", str(channel), volume):
                            self._set_v_sink_volume(channel, volume, pulse=shared_pulse)
                    else:
                        app_names = self._config.get_app_names(channel)
                        for name in app_names:
                            if self._should_apply_volume("app", name, volume):
                                self._apply_volume_by_name(name, volume, pulse=shared_pulse)
        except pulsectl.PulseError as exc:
            logger.error("apply_poti_volumes: PulseAudio connection lost: %s", exc)
            try:
                self._vol_pulse.disconnect()
            except Exception:
                pass
            self._vol_pulse = None  # force reconnect on next tick
            self._last_applied_volumes.clear()

        self._update_thread_states()

    @pyqtSlot(list)
    def apply_midi_volumes(self, mappings: list[tuple[int, float]]) -> None:
        """
        Called when the MidiThread pushes new CC values.
        Args:
            mappings: list of (channel_index, volume)
        """
        shared_pulse = self._get_vol_pulse()
        if shared_pulse is None:
            return
        try:
            for channel, volume in mappings:
                if channel < 0 or channel >= self._config.num_channels:
                    continue

                # Auto-unmute if the MIDI CC moves significantly
                with self._state_lock:
                    is_muted = self._channel_muted.get(channel, False)
                    muted_vol = self._muted_at_volume.get(channel, volume)
                if is_muted and abs(volume - muted_vol) > 0.05:
                    self.toggle_mute(channel)
                    with self._state_lock:
                        self._muted_at_volume[channel] = volume

                with self._state_lock:
                    self._poti_volumes[channel] = volume
                    creating = channel in self._vsink_creating

                # Persist in-memory so the next startup seeds _poti_volumes
                # with the last-known MIDI position (saved to disk on stop()).
                self._config.set_channel_volume(channel, volume)

                if creating:
                    continue

                mode = self._config.get_channel_mode(channel)
                if mode == "hardware":
                    hw_id = self._config.get_hardware_id(channel)
                    if hw_id and self._should_apply_volume("hardware", hw_id, volume):
                        self._apply_hardware_volume(hw_id, volume, pulse=shared_pulse)
                else:
                    if self._config.is_v_sink_enabled(channel):
                        if self._should_apply_volume("vsink", str(channel), volume):
                            self._set_v_sink_volume(channel, volume, pulse=shared_pulse)
                    else:
                        app_names = self._config.get_app_names(channel)
                        for name in app_names:
                            if self._should_apply_volume("app", name, volume):
                                self._apply_volume_by_name(name, volume, pulse=shared_pulse)
        except pulsectl.PulseError as exc:
            try:
                self._vol_pulse.disconnect()
            except Exception:
                pass
            self._vol_pulse = None  # force reconnect on next tick
            self._last_applied_volumes.clear()
            logger.error("apply_midi_volumes: PulseAudio connection lost: %s", exc)

        self._update_thread_states()

    def set_channel_volume(self, channel_index: int, volume: float) -> None:
        """Called directly by the GUI slider to override volume."""
        if channel_index < 0 or channel_index >= self._config.num_channels:
            return

        app_names_for_log = self._config.get_app_names(channel_index)
        logger.info(
            "set_channel_volume(channel=%d, app=%s, value=%.2f)",
            channel_index, app_names_for_log, volume,
        )

        with self._state_lock:
            self._poti_volumes[channel_index] = volume
            if channel_index in self._vsink_creating:
                return
            # Read muted state inside the same lock to avoid a TOCTOU race
            # with toggle_mute() called from the GUI or MIDI thread.
            is_muted = self._channel_muted.get(channel_index, False)

        # GUI slide -> auto unmute
        if is_muted:
            self.toggle_mute(channel_index)

        mode = self._config.get_channel_mode(channel_index)

        if mode == "hardware":
            hw_id = self._config.get_hardware_id(channel_index)
            if hw_id and self._should_apply_volume("hardware", hw_id, volume):
                self._apply_hardware_volume(hw_id, volume)
        else:
            if self._config.is_v_sink_enabled(channel_index):
                if self._should_apply_volume("vsink", str(channel_index), volume):
                    self._set_v_sink_volume(channel_index, volume) # Slider can open its own connection
            else:
                app_names = self._config.get_app_names(channel_index)
                for name in app_names:
                    if self._should_apply_volume("app", name, volume):
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
                sink_obj = str(getattr(sink, "index", "") or getattr(sink, "name", ""))
                if self.can_set_volume_pw and sink_obj and _wpctl_set_volume_exact(sink_obj, volume):
                    logger.debug("V-Sink volume via PW-owned node/sink ref %s (CH %d)", sink_obj, channel_index)
                    return
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

        Delegates to :func:`move_stream_to_vsink` for the actual ``pactl``
        invocation so that the Flatpak guard and all error handling live in
        exactly one place.

        Sequence:
          1. Call move_stream_to_vsink (enforces Flatpak guard centrally).
          2. sink_input_mute(False) only after a successful sink move.
          3. optional: re-fetch stream and set volume on the new sink.
        """
        moved = move_stream_to_vsink(stream_index, str(target_sink_index), pulse)

        if moved:
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

    def _get_all_assigned_apps(self) -> set[str]:
        """Return a set of all app names explicitly assigned to any fader."""
        assigned = set()
        for ch in range(self._config.num_channels):
            if self._config.get_channel_mode(ch) != "hardware":
                assigned.update(n.lower() for n in self._config.get_app_names(ch))
        assigned.discard("system master")
        assigned.discard("other apps")
        return assigned


    def _apply_volume_by_name_pw_only(self, app_name: str, volume: float) -> None:
        """
        Apply volume to all PW stream nodes matching *app_name* in PW-only mode.

        Uses the PW-native node inventory (from ``pw-dump``) directly without
        requiring a PulseAudio connection.  Matching follows the same priority
        as :func:`_matches_node`.  Volume is written via ``wpctl set-volume``
        (Flatpak-safe) or ``pw-cli set-param`` (fallback).

        On a failed match, logs one DEBUG line per candidate node so that stream
        name mismatches are immediately visible in the log:
        ``candidate: id=.. app=.. node=.. media=.. bin=..``
        """
        if app_name.lower() == "system master":
            if _wpctl_set_volume_default_sink(volume):
                logger.debug(
                    "_apply_volume_by_name_pw_only('%s', %.2f): wpctl default sink OK",
                    app_name, volume,
                )
            else:
                _throttled_warner.warn(
                    "pw_only_sys_master",
                    "_apply_volume_by_name_pw_only('%s', %.2f): wpctl default sink failed",
                    app_name, volume,
                )
            return

        with self._pw_nodes_lock:
            nodes_snapshot = list(self._pw_nodes.values())

        stable_node_ids, stable_client_ids = self._stable_ids.get(app_name.lower(), (set(), set()))
        matched_node_ids: list[int] = []
        new_node_ids: set[int] = set()
        new_client_ids: set[int] = set()
        best_matched_node: PipeWireNode | None = None
        owned_path = None
        if self.routing_owner == "nativmix":
            owned_path = self._owned_gain_paths.get(app_name.lower())
            if owned_path is None or not owned_path.available or not owned_path.writable:
                route = self._ensure_pw_owned_gain_path(app_name)
                owned_path = self._owned_gain_paths.get(app_name.lower())
                if owned_path is None:
                    owned_path = PwOwnedGainPath(
                        app_name=app_name,
                        node_id=route.gain_node_id,
                        node_name=route.gain_node_name,
                        writable=route.writable,
                        available=route.active,
                        degraded_reason=route.degraded_reason,
                    )
                    self._owned_gain_paths[app_name.lower()] = owned_path

        if self.routing_owner == "nativmix" and owned_path and owned_path.available and owned_path.writable:
            wpctl_ok, wpctl_cmd, wpctl_rc, wpctl_out, wpctl_err = _wpctl_set_volume_traced(
                owned_path.node_id, volume
            )
            if wpctl_ok:
                logger.info(
                    "_apply_volume_by_name_pw_only('%s', %.2f): owned_writable node_id=%d command=%s rc=%s stdout=%r stderr=%r",
                    app_name, volume, owned_path.node_id, wpctl_cmd, wpctl_rc, wpctl_out, wpctl_err,
                )
                matched_node_ids.append(owned_path.node_id)
                return
            pw_ok, pw_cmd, pw_rc, pw_out, pw_err = _pw_set_volume_traced(owned_path.node_id, volume)
            logger.info(
                "_apply_volume_by_name_pw_only('%s', %.2f): owned_writable node_id=%d command=%s rc=%s stdout=%r stderr=%r",
                app_name,
                volume,
                owned_path.node_id,
                pw_cmd or wpctl_cmd,
                pw_rc if pw_cmd else wpctl_rc,
                pw_out if pw_cmd else wpctl_out,
                pw_err if pw_cmd else wpctl_err,
            )
            if pw_ok:
                matched_node_ids.append(owned_path.node_id)
                return

        if (
            self.routing_owner == "nativmix"
            and self._pw_owned_path_status == "degraded"
            and (owned_path is None or not owned_path.available)
        ):
            reason = (
                owned_path.degraded_reason
                if owned_path is not None and owned_path.degraded_reason
                else "missing writable owned path"
            )
            _throttled_warner.warn(
                f"pw_owned_degraded_{app_name.lower()}",
                "_apply_volume_by_name_pw_only('%s', %.2f): degraded — %s",
                app_name, volume, reason,
            )
            with self._unresolved_lock:
                was_unresolved = app_name in self._unresolved_targets
                self._unresolved_targets.add(app_name)
            if not was_unresolved:
                self.unresolved_targets_changed.emit(set(self._unresolved_targets))
            return

        for node in nodes_snapshot:
            if not _matches_node(
                node, app_name,
                stable_node_ids=stable_node_ids,
                stable_client_ids=stable_client_ids,
            ):
                continue
            matched_node_ids.append(node.node_id)
            if node.node_id:
                new_node_ids.add(node.node_id)
            if node.client_id:
                new_client_ids.add(node.client_id)
            if best_matched_node is None:
                best_matched_node = node
            logger.info(
                "_apply_volume_by_name_pw_only: MATCHED node_id=%d app=%r "
                "(target_app=%r, value=%.2f)",
                node.node_id, node.app_name, app_name, volume,
            )
            # Permission-aware write guard: if the node has known permissions
            # and 'w' is absent, skip the direct write and log a clear reason.
            # This avoids ineffective writes on foreign stream nodes in Flatpak
            # (permissions=['r','x'] is the common case for other-app streams).
            node_perms = node.permissions
            if node_perms and "w" not in node_perms:
                logger.info(
                    "target_not_writable node_id=%d perms=%s owner_mode=%r "
                    "app=%r — skipping direct stream write (no 'w' permission); "
                    "volume control requires NativMix-owned writable node path",
                    node.node_id, node_perms, self.routing_owner, app_name,
                )
                # Count as matched but not written — do NOT attempt PA fallback.
                continue
            # Prefer wpctl (Flatpak-safe); fall back to pw-cli.
            wpctl_ok, wpctl_cmd, wpctl_rc, wpctl_out, wpctl_err = _wpctl_set_volume_traced(
                node.node_id, volume
            )
            if wpctl_ok:
                used_cmd, used_rc, used_out, used_err = wpctl_cmd, wpctl_rc, wpctl_out, wpctl_err
                pw_written = True
            else:
                pw_rc_ok, pw_cmd, pw_rc, pw_out, pw_err = _pw_set_volume_traced(
                    node.node_id, volume
                )
                pw_written = pw_rc_ok
                used_cmd = pw_cmd or wpctl_cmd
                used_rc = pw_rc if pw_cmd else wpctl_rc
                used_out = pw_out if pw_cmd else wpctl_out
                used_err = pw_err if pw_cmd else wpctl_err
            logger.info(
                "_apply_volume_by_name_pw_only('%s', %.2f): node_id=%d command=%s "
                "rc=%s stdout=%r stderr=%r",
                app_name, volume, node.node_id, used_cmd, used_rc, used_out, used_err,
            )
            if pw_written:
                logger.debug(
                    "_apply_volume_by_name_pw_only('%s', %.2f): node_id=%d OK",
                    app_name, volume, node.node_id,
                )
            else:
                _throttled_warner.warn(
                    f"pw_only_vol_fail_{node.node_id}",
                    "_apply_volume_by_name_pw_only('%s', %.2f): node_id=%d write failed",
                    app_name, volume, node.node_id,
                )

        if not matched_node_ids:
            _throttled_warner.warn(
                f"pw_only_unresolved_{app_name}",
                "_apply_volume_by_name_pw_only('%s', %.2f): no PW node matched — "
                "binding preserved, retrying on next refresh",
                app_name, volume,
            )
            # Emit an INFO-level single-line candidate summary (count + top 3
            # names) so unresolved targets are immediately visible without
            # needing DEBUG logging enabled.
            candidate_names = [
                (n.app_name or n.node_name or n.media_name or "?") for n in nodes_snapshot
            ]
            logger.info(
                "_apply_volume_by_name_pw_only('%s', %.2f): no match — %d candidate(s), top: %s",
                app_name, volume, len(candidate_names), candidate_names[:3],
            )
            # Emit one debug line per candidate so mismatches are immediately visible.
            for node in nodes_snapshot:
                logger.debug(
                    "candidate: id=%d app=%r node=%r media=%r bin=%r",
                    node.node_id,
                    node.app_name,
                    node.node_name,
                    node.media_name,
                    node.process_binary,
                )

        # Update stable ID cache
        if new_node_ids or new_client_ids:
            key = app_name.lower()
            existing_n, existing_c = self._stable_ids.get(key, (set(), set()))
            self._stable_ids[key] = (existing_n | new_node_ids, existing_c | new_client_ids)

        # Update PW identity binding with richer metadata from the best match.
        if best_matched_node is not None:
            key = app_name.lower()
            self._pw_identity[key] = PwIdentityTuple(
                app_label=app_name,
                node_name=best_matched_node.node_name,
                process_binary=best_matched_node.process_binary,
                last_node_id=best_matched_node.node_id,
            )

        # Update unresolved-targets set
        skip = app_name.lower() in ("system master", "other apps")
        if not skip:
            with self._unresolved_lock:
                was_unresolved = app_name in self._unresolved_targets
                if not matched_node_ids:
                    self._unresolved_targets.add(app_name)
                    changed = not was_unresolved
                else:
                    self._unresolved_targets.discard(app_name)
                    changed = was_unresolved
            if changed:
                with self._unresolved_lock:
                    snapshot = set(self._unresolved_targets)
                self.unresolved_targets_changed.emit(snapshot)

    def _apply_volume_by_name(self, app_name: str, volume: float, pulse: pulsectl.Pulse | None = None) -> None:
        """
        Set the volume of all active streams matching *app_name*.

        Only called when V-Sink is INACTIVE for this channel.  Accepts an
        optional shared Pulse connection to avoid repeated reconnects.

        Matching strategy (Phase 3 — deterministic priority):
        1. Cached stable IDs (node.id / client.id from PW-native inventory).
        2. ``application.process.binary`` exact match (case-insensitive).
        3. ``application.name`` exact match (case-insensitive).
        4. ``media.name`` exact match (case-insensitive).
        5. Resolved process name from proc_resolver (Electron/Chromium fallback).

        Write strategy — PipeWire-native first, PulseAudio compat fallback:
        When a matching PW node is found and ``can_set_volume_pw`` is True,
        volume is written via ``pw-cli set-param``.  If that write fails or no
        PW node is available, the call falls back to ``pulsectl``.

        Volume is applied to *all* matching sink-inputs (Phase 4 partial-success
        policy).  A failure on one candidate is logged as a warning and does not
        abort the others.  Repeated connection-level failures are collapsed into a
        throttled warning (Phase 5 compat-fallback notice).

        If both ``can_set_volume_pw`` and ``can_set_volume`` are False the
        method returns immediately without touching the audio server.
        """
        # Phase 1 feature gate: skip all writes when both write paths failed probe.
        if not self.can_set_volume_pw and not self.can_set_volume:
            _throttled_warner.warn(
                "no_vol_cap",
                "Volume control unavailable (capability probe denied writes) — skipping '%s'",
                app_name,
            )
            return

        # PW-only mode: route volume writes entirely through the PW-native path.
        # Skip the PA sink-input matching loop entirely.
        if self.pw_only_mode:
            self._apply_volume_by_name_pw_only(app_name, volume)
            return

        # Fast path: system master via PW-native (no Pulse connection needed).
        if app_name.lower() == "system master" and self.can_set_volume_pw:
            if _wpctl_set_volume_default_sink(volume):
                logger.debug(
                    "apply_volume_by_name('%s', %.2f): PW write (wpctl default sink) OK",
                    app_name, volume,
                )
                return
            # wpctl failed — fall through to PA compat below.

        # Build PW-native node index for enhanced matching (Phase 2/3).
        # Take a snapshot under the lock so the main loop is not held while
        # doing potentially slow pulsectl IPC.
        with self._pw_nodes_lock:
            pw_nodes_snapshot = dict(self._pw_nodes)

        stable_node_ids, stable_client_ids = self._stable_ids.get(app_name.lower(), (set(), set()))

        def _do_apply(p: pulsectl.Pulse) -> None:
            if app_name.lower() == "system master":
                # Reached here only when PW fast-path above failed or is unavailable.
                try:
                    default_sink_name = p.server_info().default_sink_name
                    sink = p.get_sink_by_name(default_sink_name)
                    p.volume_set_all_chans(sink, volume)
                    logger.debug(
                        "apply_volume_by_name('%s', %.2f): PA fallback write (default sink) OK",
                        app_name, volume,
                    )
                except pulsectl.PulseError as exc:
                    _throttled_warner.warn(
                        "sys_master_vol",
                        "apply_volume_by_name('%s', %.2f): failed to set system master volume: %s",
                        app_name, volume, exc,
                    )
                return

            other_apps_mode = (app_name.lower() == "other apps")
            assigned_apps = self._get_all_assigned_apps() if other_apps_mode else set()

            matched_ids: list[int] = []
            failed_ids: list[tuple[int, str]] = []
            # Track node/client IDs for newly matched streams (Phase 3 cache update).
            new_node_ids: set[int] = set()
            new_client_ids: set[int] = set()

            for si in p.sink_input_list():
                props = dict(si.proplist)
                if _is_internal_stream(props):
                    continue

                pid_str = props.get("application.process.id", "0")
                try:
                    pid = int(pid_str)
                except ValueError:
                    pid = 0
                pa_fallback = (
                    str(props.get("application.name", ""))
                    or str(props.get("application.process.binary", ""))
                    or str(props.get("media.name", ""))
                    or "Unknown"
                )
                resolved = resolve_app_name(pid, fallback=pa_fallback)

                # Phase 3: augment matching with PW-native node data when
                # a node is available for this sink-input's object serial.
                pw_node: PipeWireNode | None = None
                try:
                    obj_serial = int(props.get("object.serial", props.get("object.id", "0")))
                    pw_node = pw_nodes_snapshot.get(obj_serial)
                except (ValueError, TypeError):
                    pass

                matched = False
                if other_apps_mode:
                    matched = (
                        resolved.lower() not in assigned_apps
                        and resolved.lower() != "system master"
                    )
                else:
                    # Try PW-native matching first if we have node data.
                    if pw_node is not None:
                        matched = _matches_node(
                            pw_node, app_name,
                            stable_node_ids=stable_node_ids,
                            stable_client_ids=stable_client_ids,
                        )
                    # Fall back to the PA-compat matching path.
                    if not matched:
                        matched = _matches_app_name(props, resolved, app_name)

                if matched:
                    matched_ids.append(si.index)
                    # Phase 3: record stable IDs for future lookups.
                    if pw_node is not None:
                        if pw_node.node_id:
                            new_node_ids.add(pw_node.node_id)
                        if pw_node.client_id:
                            new_client_ids.add(pw_node.client_id)
                    # Phase 4: apply volume — PW-native first, pulsectl fallback.
                    # Prefer wpctl (Flatpak-safe) → pw-cli → pulsectl.
                    pw_written = False
                    if self.can_set_volume_pw and pw_node is not None and pw_node.node_id:
                        pw_written = (
                            _wpctl_set_volume(pw_node.node_id, volume)
                            or _pw_set_volume(pw_node.node_id, volume)
                        )
                        if pw_written:
                            logger.debug(
                                "apply_volume_by_name('%s', %.2f): PW write node_id=%d OK",
                                app_name, volume, pw_node.node_id,
                            )
                    # Flatpak hard guard: when _flatpak_hard_guard is set and the
                    # matched sink-input has no live PW node, the target is not
                    # addressable via the PW-native path.  Skip the PA sink-input
                    # fallback immediately (do not attempt pactl / pulsectl write)
                    # and emit a rate-limited warning so the log is not spammed.
                    if self._flatpak_hard_guard and pw_node is None:
                        _throttled_warner.warn(
                            f"sandbox_unresolved_{app_name}",
                            "apply_volume_by_name('%s', %.2f): unresolved in sandbox"
                            " — no live PW target; PA sink-input fallback suppressed,"
                            " binding kept",
                            app_name, volume,
                        )
                        continue
                    allow_pa_fallback = not (self._flatpak_hard_guard and app_name in self.get_unresolved_targets())
                    if not pw_written and not allow_pa_fallback:
                        logger.debug(
                            "apply_volume_by_name('%s', %.2f): skipping PA fallback"
                            " while target remains unresolved under Flatpak hard guard",
                            app_name, volume,
                        )
                    if not pw_written and allow_pa_fallback:
                        # PA compat fallback — throttle repeated failures to avoid spam.
                        try:
                            p.volume_set_all_chans(si, volume)
                        except pulsectl.PulseError as exc:
                            failed_ids.append((si.index, str(exc)))

            if matched_ids:
                logger.debug(
                    "apply_volume_by_name('%s', %.2f): matched sink-input ids=%s",
                    app_name, volume, matched_ids,
                )
            else:
                # No matching sink-input found in current snapshot.
                # This is expected in Flatpak/sandbox environments where the
                # audio graph is only partially visible.  We do NOT clear the
                # saved binding — just mark the target as unresolved for the UI.
                _throttled_warner.warn(
                    f"unresolved_{app_name}",
                    "apply_volume_by_name('%s', %.2f): target not found in current audio graph%s"
                    " — binding preserved, retrying on next refresh",
                    app_name, volume,
                    " (Flatpak hard guard: graph visibility may be limited)" if self._flatpak_hard_guard else "",
                )
            if failed_ids:
                # Phase 4 partial-success: throttle repeated per-sink-input warnings
                # to avoid log spam in Flatpak where sink-input writes consistently fail.
                for sid, reason in failed_ids:
                    _throttled_warner.warn(
                        f"si_fail_{app_name}_{sid}",
                        "apply_volume_by_name('%s', %.2f): sink-input #%d failed (PA fallback): %s",
                        app_name, volume, sid, reason,
                    )

            # Phase 3: persist newly discovered stable IDs.
            if new_node_ids or new_client_ids:
                key = app_name.lower()
                existing_n, existing_c = self._stable_ids.get(key, (set(), set()))
                self._stable_ids[key] = (existing_n | new_node_ids, existing_c | new_client_ids)

            # Update the unresolved-target set and emit a signal when it changes.
            # Special targets (System Master, Other Apps) are always treated as resolved
            # because they match by category rather than by a discovered node.
            skip_unresolved_tracking = app_name.lower() in ("system master", "other apps")
            if not skip_unresolved_tracking:
                with self._unresolved_lock:
                    was_unresolved = app_name in self._unresolved_targets
                    if not matched_ids:
                        self._unresolved_targets.add(app_name)
                        changed = not was_unresolved
                    else:
                        self._unresolved_targets.discard(app_name)
                        changed = was_unresolved
                if changed:
                    with self._unresolved_lock:
                        snapshot = set(self._unresolved_targets)
                    self.unresolved_targets_changed.emit(snapshot)

        try:
            if pulse is not None:
                _do_apply(pulse)
            else:
                with pulsectl.Pulse("nativmix-poti-apply") as p:
                    _do_apply(p)
        except pulsectl.PulseError as exc:
            # Phase 5 compat fallback: throttle repeated connection-level errors.
            _throttled_warner.warn(
                f"vol_apply_{app_name}",
                "apply_volume_by_name('%s', %.2f) failed (compat path): %s",
                app_name, volume, exc,
            )


    def _apply_hardware_volume(
        self,
        hw_id: str,
        volume: float,
        pulse: pulsectl.Pulse | None = None,
    ) -> None:
        """Apply hardware volume directly to a specific sink or source.

        Tries the PipeWire-native path (wpctl) first when available, then falls
        back to pulsectl.  This ensures correct behaviour in Flatpak where
        pulsectl sink/source writes may fail.
        """
        parts = hw_id.split(':', 1)
        if len(parts) != 2:
            return
        kind, name = parts

        # PW-native path: use wpctl default aliases for the default sink/source,
        # or fall back to PA name-based lookup for named devices.
        if self.can_set_volume_pw:
            if kind == "sink":
                # Try wpctl default alias first (works even when sink name differs
                # between host and Flatpak views), then exact-name lookup via PA.
                if _wpctl_set_volume_default_sink(volume):
                    logger.debug(
                        "_apply_hardware_volume('%s', %.2f): PW write (wpctl default sink) OK",
                        hw_id, volume,
                    )
                    return
            elif kind == "source":
                if _wpctl_set_volume_default_source(volume):
                    logger.debug(
                        "_apply_hardware_volume('%s', %.2f): PW write (wpctl default source) OK",
                        hw_id, volume,
                    )
                    return

        def _do_apply(p: pulsectl.Pulse) -> None:
            if kind == "sink":
                dev = p.get_sink_by_name(name)
                p.volume_set_all_chans(dev, volume)
            elif kind == "source":
                dev = p.get_source_by_name(name)
                p.volume_set_all_chans(dev, volume)

        try:
            if pulse is not None:
                _do_apply(pulse)
            else:
                with pulsectl.Pulse("nativmix-hw-vol") as p:
                    _do_apply(p)
        except pulsectl.PulseError as exc:
            _throttled_warner.warn(
                f"hw_vol_{hw_id}",
                "Failed to apply hardware volume to %s: %s",
                hw_id, exc,
            )

    def toggle_mute(self, channel_index: int) -> None:
        """
        Toggle the mute state of an entire channel (all apps assigned to it).
        Called by the CLI IPC server.
        """
        if channel_index < 0 or channel_index >= self._config.num_channels:
            logger.warning(
                "toggle_mute requested for invalid channel %d (num_channels=%d)",
                channel_index, self._config.num_channels,
            )
            return

        with self._state_lock:
            is_currently_muted = self._channel_muted.get(channel_index, False)
            new_mute_state = not is_currently_muted
            self._channel_muted[channel_index] = new_mute_state
            if new_mute_state:
                self._muted_at_volume[channel_index] = self._poti_volumes.get(channel_index, 0.0)

        logger.debug("IPC: Toggling mute for channel %d -> %s", channel_index, new_mute_state)

        self.mute_state_changed.emit(channel_index, new_mute_state)
        # Push updated mute state to listener thread so new streams binding to
        # this channel inherit the correct mute state immediately.
        self._update_thread_states()

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

        # PW path: mute via wpctl/pw-cli node writes.
        # Used whenever PW tools are available (pw_only_mode or can_set_volume_pw),
        # keeping mute consistent with the PW-native volume write path.
        if self.pw_only_mode or self.can_set_volume_pw:
            if "system master" in app_names:
                if self.pw_only_mode:
                    # wpctl does not expose a direct default-sink mute alias —
                    # set volume to 0.0 as a mute proxy (best effort in PW-only mode).
                    _throttled_warner.warn(
                        "pw_only_sys_mute",
                        "toggle_mute(CH%d): system master mute not available in PW-only mode",
                        channel_index,
                    )
                else:
                    # Both PA and PW available: use PA for system master mute
                    # (wpctl has no stable default-sink mute alias).
                    try:
                        with pulsectl.Pulse("nativmix-ipc-mute") as pulse:
                            default_sink_name = pulse.server_info().default_sink_name
                            sink = pulse.get_sink_by_name(default_sink_name)
                            pulse.mute(sink, new_mute_state)
                    except pulsectl.PulseError as exc:
                        logger.error("toggle_mute system master failed: %s", exc)
                # If system master is the only target there are no app nodes to mute.
                if app_names == ["system master"]:
                    return
            with self._pw_nodes_lock:
                nodes_snapshot = list(self._pw_nodes.values())
            other_apps_mode = ("other apps" in app_names)
            assigned_apps = self._get_all_assigned_apps() if other_apps_mode else set()
            for node in nodes_snapshot:
                if other_apps_mode:
                    # Resolve a display name for the node the same way _get_active_streams_pw_only does
                    node_app = (
                        node.app_name
                        or node.node_name
                        or node.media_name
                        or node.process_binary
                        or ""
                    ).lower()
                    if node_app and node_app not in assigned_apps and node_app != "system master":
                        _wpctl_set_mute(node.node_id, new_mute_state)
                else:
                    for name in app_names:
                        if name != "system master" and _matches_node(node, name):
                            _wpctl_set_mute(node.node_id, new_mute_state)
                            break
            return

        # Find all currently active streams that map to those apps and mute them
        try:
            with pulsectl.Pulse("nativmix-ipc-mute") as pulse:
                if "system master" in app_names:
                    default_sink_name = pulse.server_info().default_sink_name
                    sink = pulse.get_sink_by_name(default_sink_name)
                    pulse.mute(sink, new_mute_state)

                other_apps_mode = ("other apps" in app_names)
                assigned_apps = self._get_all_assigned_apps() if other_apps_mode else set()

                for si in pulse.sink_input_list():
                    props = dict(si.proplist)
                    if _is_internal_stream(props):
                        continue
                    pid_str = props.get("application.process.id", "0")
                    try:
                        pid = int(pid_str)
                    except ValueError:
                        pid = 0
                    pa_fallback = (
                        str(props.get("application.name", ""))
                        or str(props.get("application.process.binary", ""))
                        or str(props.get("media.name", ""))
                        or "Unknown"
                    )
                    resolved = resolve_app_name(pid, fallback=pa_fallback)

                    if other_apps_mode:
                        if resolved.lower() not in assigned_apps and resolved.lower() != "system master":
                            pulse.sink_input_mute(si.index, mute=new_mute_state)
                    elif resolved.lower() in app_names:
                        pulse.sink_input_mute(si.index, mute=new_mute_state)
        except pulsectl.PulseError as exc:
            logger.error("toggle_mute for channel %d failed: %s", channel_index, exc)

    def _on_master_volume_changed(self, volume: float, muted: bool) -> None:
        """
        Slot: Called when the System Master volume changes.
        Updates faders for any channel assigned to 'System Master'.
        """
        for ch in range(self._config.num_channels):
            if "system master" in [n.lower() for n in self._config.get_app_names(ch)]:
                with self._state_lock:
                    self._poti_volumes[ch] = volume
                    self._channel_muted[ch] = muted
                self.channel_volume_changed.emit(ch, volume)
                self.mute_state_changed.emit(ch, muted)

    @pyqtSlot()
    def _mark_audit_complete(self) -> None:
        """Called 2 s after run_audio_audit() finishes to allow hotplug handling."""
        self._initial_audit_complete = True
        logger.debug("Hotplug handling enabled (audit settled)")
        # Phase 2: refresh the PipeWire-native node inventory now that the
        # session has settled.  Schedule on main thread to avoid blocking the
        # QTimer callback with a subprocess call.
        QTimer.singleShot(0, self._refresh_pw_nodes)

    def _refresh_pw_nodes(self) -> None:
        """
        Refresh the PipeWire-native stream inventory from ``pw-dump``.

        Populates :attr:`_pw_nodes` with the current set of active audio output
        nodes.  Called once after the initial audit settles and can be called
        again at any point to update the inventory (e.g. after a stream add/remove
        event).

        This is a best-effort operation — if ``pw-dump`` is unavailable or fails
        the existing inventory is left unchanged and a debug message is logged.

        Logs a visibility summary at INFO level so that sandbox/Flatpak graph
        visibility limitations are immediately obvious in logs.
        """
        if not self.pw_dump_available:
            return
        try:
            nodes = _pw_dump_nodes()
        except Exception as exc:
            logger.debug("_refresh_pw_nodes: pw-dump failed: %s", exc)
            return
        with self._pw_nodes_lock:
            self._pw_nodes = {n.node_id: n for n in nodes}

        # Visibility summary diagnostic — log at INFO so it's easily spotted.
        sink_input_count = 0
        if not self.pw_only_mode:
            try:
                with pulsectl.Pulse("nativmix-diag-probe") as _p:
                    sink_input_count = len(_p.sink_input_list())
            except Exception:
                pass

        flatpak_hint = (
            " [PW-only mode: PA socket absent, using pw-dump for app enumeration]"
            if self.pw_only_mode else (
                " [Flatpak pulse-bridge: pw-dump graph may be partial — "
                "app streams in other sandboxes may not be visible]"
                if IS_FLATPAK else ""
            )
        )
        logger.info(
            "Audio graph visibility: pw_nodes=%d stream_candidates=%d "
            "pulse_sink_inputs=%d write_backend=%s%s",
            len(nodes),
            len(nodes),
            sink_input_count,
            "wpctl" if self.wpctl_available else ("pw-cli" if self.pw_cli_available else "pa-compat"),
            flatpak_hint,
        )

        # When PW is the primary enumeration path, trigger an immediate
        # active-stream refresh so the GUI reflects the current node inventory
        # without waiting for the next PA event or poll cycle.
        if self.pw_only_mode or self.can_set_volume_pw:
            self.get_active_streams()

    def _on_default_sink_changed(self, new_default_sink: str) -> None:
        """
        Slot: Called when the physical hardware target changes (Hotplug).
        Re-links active V-Sinks via Smart Linker using the loopback module ID
        (same lookup as enable_v_sink) so the node regex is always correct.
        Ignored for the first 2 s after startup to absorb audit-triggered events.
        """
        if not self._initial_audit_complete:
            logger.debug("Hotplug event suppressed (audit cooldown): %s", new_default_sink)
            return

        if new_default_sink.startswith("NativMix_"):
            return

        logger.debug("Hotplug detected! Default sink is now: %s. Re-linking...", new_default_sink)

        try:
            with pulsectl.Pulse("nativmix-hotplug") as pulse:
                hw_sink = self._get_master_hardware_sink(pulse)
                if hw_sink == self._last_hardware_sink:
                    return
                self._last_hardware_sink = hw_sink

                # Re-audit active V-Sinks.  Identify each loopback by its pactl
                # module ID (pulsectl module_list), then resolve the PipeWire node
                # name via pw-dump (resolve_loopback_node).  smart_link also accepts
                # the module ID directly for its own pw-dump lookup in find_ports.
                modules = pulse.module_list()
                for ch in range(self._config.num_channels):
                    if self._config.is_v_sink_enabled(ch):
                        sink_name = f"NativMix_CH_{ch}"
                        mod_id = None
                        for m in modules:
                            if m.name == "module-loopback" and m.argument:
                                if f"source={sink_name}.monitor" in m.argument:
                                    mod_id = str(m.index)
                                    break
                        if mod_id is None:
                            logger.debug("Hotplug: no loopback module found for %s, skipping",
                                         sink_name)
                            continue
                        loopback_node = routing.resolve_loopback_node(mod_id)
                        if loopback_node:
                            routing.clean_links(source_node=re.escape(loopback_node),
                                                target_node=re.escape(hw_sink))
                        routing.smart_link(
                            source_pattern=mod_id,
                            target_pattern=hw_sink,
                            source_port_pattern="output_"
                        )
        except pulsectl.PulseError as e:
            logger.error("Hotplug re-link failed: %s", e)

    # ------------------------------------------------------------------
    # Virtual Sinks (Pro-Routing)
    # ------------------------------------------------------------------

    def _get_master_hardware_sink(self, pulse: pulsectl.Pulse) -> str:
        """
        Identify the 'real' physical output device node name.
        Avoids picking NativMix Virtual Sinks or monitor sources.
        """
        try:
            default_sink_name = pulse.server_info().default_sink_name
            # If the default is a NativMix sink, we must find a real one
            if default_sink_name.startswith("NativMix_"):
                logger.debug("System default is a NativMix sink (%s). Finding hardware...", default_sink_name)
                for s in pulse.sink_list():
                    if not s.name.startswith("NativMix_") and "dummy" not in s.name.lower():
                        return s.name
            return default_sink_name
        except pulsectl.PulseError:
            # Absolute fallback: find the first non-nativmix sink
            sinks = pulse.sink_list()
            for s in sinks:
                if not s.name.startswith("NativMix_") and "dummy" not in s.name.lower():
                    return s.name
            return sinks[0].name if sinks else "auto_null"

    def _update_sink_metadata(self, sink_name: str) -> bool:
        """
        Inject/Update OSD-Bypass metadata on an existing sink without unloading it.
        Returns True if at least one property was set successfully.
        Each property is set independently so a rejected property does not abort the rest.
        """
        props = {
            "device.intended-roles": "internal",
            "device.class": "abstract",
            "node.passive": "true",
            "device.icon-name": "audio-card-virtual",
            "priority.driver": "1",
            "priority.session": "1"
        }
        any_success = False
        for key, val in props.items():
            if not val:
                continue
            try:
                subprocess.run(
                    ["pactl", "set-sink-property", sink_name, key, val],
                    check=True, capture_output=True, timeout=_SUBPROCESS_TIMEOUT,
                )
                any_success = True
            except subprocess.TimeoutExpired:
                logger.warning("pactl set-sink-property timed out after %ds for %s (%s)",
                               _SUBPROCESS_TIMEOUT, sink_name, key)
                return False  # Timeout means PipeWire may be stuck — abort
            except subprocess.CalledProcessError:
                # pactl set-sink-property is not supported for all property
                # names on every PipeWire/PulseAudio version — silently skip.
                pass
        if any_success:
            logger.debug("Updated OSD-Bypass metadata for %s", sink_name)
        return any_success

    def enable_v_sink(self, channel_index: int) -> None:
        """Create or Update a Virtual Sink and move mapped streams to it."""
        sink_name = f"NativMix_CH_{channel_index}"
        if self.pw_only_mode:
            if self.routing_owner == "nativmix":
                self._refresh_owned_gain_paths()
                logger.info(
                    "enable_v_sink(CH%d): PW-only mode — using NativMix-owned writable gain path when available",
                    channel_index,
                )
            else:
                logger.info(
                    "enable_v_sink(CH%d): PW-only mode — owned routing disabled by routing_owner=%r",
                    channel_index, self.routing_owner,
                )
            return
        # Routing owner guard: only nativmix may create V-Sinks.
        if self.routing_owner != "nativmix":
            logger.info(
                "enable_v_sink(CH%d): V-Sink creation blocked "
                "(routing_owner=%r — NativMix must not create V-Sinks in this mode)",
                channel_index, self.routing_owner,
            )
            return
        logger.debug("Enabling/Updating V-Sink for channel %d: %s", channel_index, sink_name)

        with self._state_lock:
            self._vsink_creating.add(channel_index)
        self._update_thread_states()  # propagate v_sink_busy=True to listener

        # 1. Create Sink or Update Metadata
        exists = False
        try:
            with pulsectl.Pulse("nativmix-exists-check") as pulse:
                pulse.get_sink_by_name(sink_name)
                exists = True
        except pulsectl.PulseError:
            pass

        if exists:
            logger.debug("Reusing existing V-Sink %s, updating properties...", sink_name)
            self._update_sink_metadata(sink_name)
        else:
            try:
                # Load null-sink with STRICT OSD-Bypass properties
                # - device.intended-roles=internal (Core system role)
                # - device.class=abstract (Signals non-UI component)
                # - node.passive=true (Prevents auto-management triggers)
                # - node.description (Clear identification in Helvum/pavucontrol)
                # - priority.*=1 (Stay out of default device logic)
                # sink_properties uses SPACE as separator (not comma).
                # Comma-separated values cause pactl to treat everything after
                # the first '=' as the value of device.description, resulting
                # in the full flags string appearing as the sink's display name.
                props = " ".join([
                    f"device.description={sink_name}",
                    f"node.description={sink_name}",
                    "media.class=Audio/Sink",
                    "device.intended-roles=internal",
                    "device.class=abstract",
                    "node.passive=true",
                    "device.icon-name=audio-card-virtual",
                    "priority.driver=1",
                    "priority.session=1",
                ])
                subprocess.run(
                    [
                        "pactl", "load-module", "module-null-sink",
                        f"sink_name={sink_name}",
                        f"sink_properties={props}",
                    ],
                    check=True, capture_output=True, timeout=_SUBPROCESS_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                logger.warning("pactl load-module null-sink timed out after %ds for %s",
                               _SUBPROCESS_TIMEOUT, sink_name)
                return  # Sink state unknown — abort to avoid orphaned loopback modules
            except subprocess.CalledProcessError as e:
                # Usually means it already exists (e.g., from a crash)
                logger.warning("pactl load-module returned error or existed: %s", e.stderr)

        # 1b. Create or Reuse Loopback to Hardware (Isolated & Smart Linked)
        try:
            mod_id = None
            with pulsectl.Pulse("nativmix-loopback-check") as pulse:
                # Search for existing loopback module for this sink
                for m in pulse.module_list():
                    if m.name == "module-loopback" and m.argument:
                        if f"source={sink_name}.monitor" in m.argument:
                            mod_id = str(m.index)
                            logger.debug("Reusing existing loopback module %s for %s", mod_id, sink_name)
                            break

            if mod_id is None:
                # 1. Load loopback without auto-linking (Not found, so creation is required)
                res = subprocess.run(
                    ["pactl", "load-module", "module-loopback",
                     f"source={sink_name}.monitor", "dont-link=1"],
                    check=True, capture_output=True, text=True,
                    timeout=_SUBPROCESS_TIMEOUT,
                )
                mod_id = res.stdout.strip()
                logger.debug("Created NEW loopback module %s for %s", mod_id, sink_name)

            # 2. Establish/Verify precise routing.
            # Resolve the PipeWire node name from the pactl module ID via pw-dump.
            # PipeWire node names (e.g. "output.loopback-<pid>-<pipewire-id>") do
            # NOT embed the pactl module index, so we look up the node whose
            # pulse.module.id property matches mod_id.  smart_link also accepts
            # the module ID directly (find_ports has the same pw-dump path).
            loopback_node = routing.resolve_loopback_node(mod_id)
            with pulsectl.Pulse("nativmix-vsink-setup") as pulse:
                hw_sink_node = self._get_master_hardware_sink(pulse)
                if loopback_node:
                    # Use exact node name for clean_links to avoid regex ambiguity
                    routing.clean_links(source_node=re.escape(loopback_node),
                                        target_node=re.escape(hw_sink_node))
                    routing.smart_link(
                        source_pattern=mod_id,   # find_ports resolves via pw-dump
                        target_pattern=hw_sink_node,
                        source_port_pattern="output_"
                    )
                else:
                    logger.warning("V-Sink %s: loopback node not found, routing may be incomplete",
                                   sink_name)
                # Explicitly unmute the loopback stream (safety layer)
                self._unmute_module_streams(mod_id, pulse)

            logger.debug("Smart Linker: V-Sink %s established -> %s", sink_name, hw_sink_node)
        except subprocess.TimeoutExpired:
            logger.warning("pactl load-module loopback timed out after %ds for %s",
                           _SUBPROCESS_TIMEOUT, sink_name)
        except (subprocess.CalledProcessError, pulsectl.PulseError) as e:
            logger.warning("Smart Linker failed for V-Sink %s: %s", sink_name, e)

        # 2. Wait for PipeWire to actually register the Sink
        current_volume = self._poti_volumes.get(channel_index, 0.5)
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
            with self._state_lock:
                self._vsink_creating.discard(channel_index)
            self._update_thread_states()
            return

        if exists:
            # Reuse path: V-Sink already has apps and a valid volume from the
            # previous session.  Do NOT override the volume and do NOT re-inject
            # apps — the AudioListenerThread re-routes any stream that isn't
            # already inside the sink via change events.  Touching the volume
            # here would cause a spike if _poti_volumes still holds the 1.0
            # initialisation default (first real Arduino tick is queued but not
            # yet processed).  apply_poti_volumes() will correct the level on
            # the next hardware tick (~20 ms).
            logger.debug("V-Sink %s reused – skipping volume override and re-injection", sink_name)
        else:
            # New sink: lock slider updates for this channel so no stray
            # apply_poti_volumes / set_channel_volume call can write to the
            # system sink while PipeWire is still registering the V-Sink.
            with self._state_lock:
                self._vsink_creating.add(channel_index)
            try:
                # Set sink to 0 directly before injecting apps so there is no
                # blast if _poti_volumes is still at the 1.0 default.
                # Use _set_v_sink_volume directly — set_channel_volume would
                # return early because the channel is in _vsink_creating.
                self._set_v_sink_volume(channel_index, 0.0)
                logger.debug("V-Sink %s created – zeroed before app injection (real vol=%.2f follows)",
                             sink_name, current_volume)
                # Move Apps and set Unity Gain (1.0 inside V-Sink)
                self._move_apps_to_sink(channel_index, sink_name, target_volume=1.0)
                # Give PipeWire a moment to settle the new routing before
                # applying the real fader volume.
                time.sleep(0.05)
                self._set_v_sink_volume(channel_index, current_volume)
            finally:
                with self._state_lock:
                    self._vsink_creating.discard(channel_index)

        with self._state_lock:
            self._vsink_creating.discard(channel_index)
        self._update_thread_states()  # propagate v_sink_busy=False
        self._restore_hardware_default_sink()

    def _unmute_module_streams(self, module_id: str | int, pulse: pulsectl.Pulse | None = None) -> None:
        """Explicitly unmute all sink-inputs belonging to a specific module."""
        try:
            mod_idx = int(module_id)
            if pulse:
                for si in pulse.sink_input_list():
                    if si.owner_module == mod_idx:
                        pulse.sink_input_mute(si.index, mute=False)
            else:
                with pulsectl.Pulse("nativmix-unmute-mod") as p:
                    for si in p.sink_input_list():
                        if si.owner_module == mod_idx:
                            p.sink_input_mute(si.index, mute=False)
        except (ValueError, pulsectl.PulseError):
            pass

    def _restore_hardware_default_sink(self, pulse: pulsectl.Pulse | None = None) -> None:
        """
        Force the default PulseAudio sink back to hardware.
        This prevents NativMix V-Sinks from triggering System OSDs.
        """
        def _do_restore(p: pulsectl.Pulse) -> None:
            hw_sink = self._get_master_hardware_sink(p)
            current_def = p.server_info().default_sink_name
            if current_def != hw_sink:
                logger.debug("Restoring default sink to hardware: %s (was %s)", hw_sink, current_def)
                try:
                    target = p.get_sink_by_name(hw_sink)
                    p.default_set(target)
                except pulsectl.PulseError as e:
                    logger.warning("Failed to restore default sink: %s", e)

        try:
            if pulse:
                _do_restore(pulse)
            else:
                with pulsectl.Pulse("nativmix-osd-bypass") as p:
                    _do_restore(p)
        except Exception:
            pass

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
        logger.debug("Disabling V-Sink for channel %d: %s", channel_index, sink_name)

        if self.pw_only_mode:
            self._refresh_owned_gain_paths()
            logger.info(
                "disable_v_sink(CH%d): PW-only mode — no PA V-Sink to destroy; refreshing owned gain path state",
                channel_index,
            )
            current_volume = self._poti_volumes.get(channel_index, 0.5)
            for name in self._config.get_app_names(channel_index):
                self._apply_volume_by_name_pw_only(name, current_volume)
            self._update_thread_states()
            return

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
                        logger.debug(
                            "Stream %d evacuating from V-Sink '%s' → Main Sink '%s'",
                            si.index, sink_name, target_sink.name
                        )
                        # _seamless_move: pactl move → unmute → set fader volume
                        self._seamless_move(pulse, si.index, target_sink.index, volume=current_volume)
                        logger.debug(
                            "Stream %d moved back to Main Sink and forced to resume (vol=%.2f).",
                            si.index, current_volume
                        )

        except pulsectl.PulseError as e:
            logger.error("Failed to evacuate V-Sink %s apps: %s", sink_name, e)

        # ── Step 4: Safety delay ─────────────────────────────────────────────
        # Give PipeWire ~150 ms to stabilise streams on the new sink before
        # the V-Sink device disappears.  Browsers (Chromium) are especially
        # sensitive to the device vanishing too early.
        time.sleep(0.15)

        # ── Step 5: Destroy null-sink + loopback modules ─────────────────────
        try:
            pid_out = subprocess.run(
                ["pactl", "list", "short", "modules"],
                capture_output=True, text=True, check=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
            for line in pid_out.stdout.splitlines():
                if f"sink_name={sink_name}" in line or f"source={sink_name}.monitor" in line:
                    mod_id = line.split()[0]
                    subprocess.run(["pactl", "unload-module", mod_id], check=True,
                                   timeout=_SUBPROCESS_TIMEOUT)
                    logger.info("Unloaded module ID %s for %s", mod_id, sink_name)
        except subprocess.TimeoutExpired:
            logger.warning("pactl timed out after %ds while disabling V-Sink %s",
                           _SUBPROCESS_TIMEOUT, sink_name)
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
                    # PipeWire monitors are Sources for Outputs — filter them out,
                    # keep only physical inputs for flexibility.
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
                            logger.debug("Moving loopback stream [%d] to new Master Output", si.index)
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
                capture_output=True, text=True, check=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
            for line in pid_out.stdout.splitlines():
                if "sink_name=NativMix_CH_" in line or "source=NativMix_CH_" in line:
                    mod_id = line.split()[0]
                    subprocess.run(["pactl", "unload-module", mod_id], check=True,
                                   timeout=_SUBPROCESS_TIMEOUT)
                    logger.info("Panic unloaded module ID %s", mod_id)
        except subprocess.TimeoutExpired:
            logger.warning("pactl timed out after %ds during Panic Reset",
                           _SUBPROCESS_TIMEOUT)
        except subprocess.CalledProcessError as e:
            logger.error("pactl unload-module failed during Panic: %s", e.stderr)
