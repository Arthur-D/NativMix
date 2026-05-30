"""
MIDI hardware backend for NativMix.

Handles MIDI input devices (via mido/rtmidi) and maps Control Change (CC)
messages to volume levels. Supports a "Learn" mode for interactive setup.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

import mido
from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot

logger = logging.getLogger(__name__)

# Ignore inbound mapped fader CC while within this band of the last outbound sync.
_FADER_FEEDBACK_TOLERANCE = 0.05


def _inbound_fader_suppressed(takeover_volume: float | None, cc_value: int) -> bool:
    """Return True when an inbound CC likely echoes our own outbound fader sync."""
    if takeover_volume is None:
        return False
    return abs(cc_value / 127.0 - takeover_volume) <= _FADER_FEEDBACK_TOLERANCE


def _match_midi_port(names: list[str], device_key: str) -> str | None:
    """Find the first port name containing *device_key*."""
    for name in names:
        if device_key in name:
            return name
    return None


def ensure_midi_backend() -> str | None:
    """Probe and set the best available mido backend.

    On Windows: uses rtmidi/WinMM directly — no portmidi library search.
    On Linux: tries rtmidi first; on Fedora/Nobara portmidi is preferred.
    Returns the backend name ('rtmidi' or 'portmidi') or None if none is available.
    Idempotent — safe to call multiple times.
    """
    if sys.platform == "win32":
        try:
            import rtmidi  # noqa: F401
            mido.set_backend('mido.backends.rtmidi')
            return 'rtmidi'
        except ImportError:
            return None

    from nativmix.utils.distro import is_fedora
    backends_to_try = ['portmidi', 'rtmidi'] if is_fedora() else ['rtmidi', 'portmidi']

    for b_name in backends_to_try:
        try:
            if b_name == 'rtmidi':
                import rtmidi  # noqa: F401
                mido.set_backend('mido.backends.rtmidi')
                return 'rtmidi'
            else:
                import ctypes
                import ctypes.util
                _lib = ctypes.util.find_library('portmidi')
                if not _lib:
                    raise ImportError("libportmidi.so not found")
                ctypes.CDLL(_lib)
                mido.set_backend('mido.backends.portmidi')
                return 'portmidi'
        except (ImportError, OSError):
            continue
    return None


class MidiThread(QThread):
    """
    Background thread that listens for MIDI CC messages from a specific device.

    Signals
    -------
    midi_volumes_changed(list[tuple[int, float]])
        Emitted when mapped MIDI CC values change.
        List of (channel_index, volume_0_to_1).
    midi_cc_received(int, int)
        Emitted for the "Learn" handshake: (control_number, value).
    connection_changed(bool)
        Emitted when the device is opened (True) or closed/missing (False).
    """

    midi_volumes_changed = pyqtSignal(list)  # list[tuple[int, float]]
    midi_cc_received = pyqtSignal(int, int)
    midi_mute_toggled = pyqtSignal(int)  # channel_index
    connection_changed = pyqtSignal(bool)
    # Status signal: (status_type, display_message)
    # Types: "connecting", "stable", "error_temporary", "error_critical"
    status_changed = pyqtSignal(str, str)
    profile_switch_requested = pyqtSignal(str)  # "next", "prev", or profile_id
    fader_sync_requested = pyqtSignal(list)  # list[tuple[int, float]] (channel, volume)

    def __init__(self, device_name: str = "", input_mode: str = "hybrid", parent=None) -> None:
        super().__init__(parent)
        self._device_name: str = device_name
        self._input_mode: str = input_mode  # "usb", "hybrid", "midi_only"
        self._running: bool = False
        self._panic_flag: bool = False
        self._critical_error: bool = False
        self._error_count: int = 0
        self._cc_map: dict[int, int] = {}       # cc_number -> channel_index (volume)
        self._mute_cc_map: dict[int, int] = {}  # cc_number -> channel_index (mute toggle)
        self._last_values: dict[int, int] = {}  # cc_number -> last_seen_value (0-127)
        self._last_vol_emit: dict[int, float] = {}  # cc_number -> monotonic time of last emit
        # Persistent virtual port – kept alive across USB ↔ hybrid mode
        # switches so ALSA clients see one stable "NativMix:Input" port.
        self._virtual_client = None
        self._profile_next_cc: int | None = None
        self._profile_prev_cc: int | None = None
        self._profile_direct_map: dict[int, str] = {}  # cc -> profile_id
        self._fader_feedback_enabled: bool = False
        self._feedback_lock = threading.Lock()
        self._feedback_takeover: dict[int, float] = {}  # channel_index -> last sent volume
        self._last_sent_cc_value: dict[int, int] = {}  # cc -> 0-127
        self._pending_sync: list[tuple[int, float]] | None = None
        self.fader_sync_requested.connect(self._queue_fader_sync)

    def set_fader_feedback_enabled(self, enabled: bool) -> None:
        """Enable or disable outbound MIDI CC fader position sync."""
        if self._fader_feedback_enabled != enabled:
            logger.debug("MIDI fader feedback %s", "enabled" if enabled else "disabled")
        self._fader_feedback_enabled = enabled
        if not enabled:
            with self._feedback_lock:
                self._feedback_takeover.clear()
                self._last_sent_cc_value.clear()
                self._pending_sync = None

    @pyqtSlot(list)
    def _queue_fader_sync(self, mappings: list[tuple[int, float]]) -> None:
        """Queue outbound fader positions (thread-safe via queued signal)."""
        if not self._fader_feedback_enabled or not mappings:
            return
        with self._feedback_lock:
            self._pending_sync = list(mappings)

    def request_fader_sync(self, mappings: list[tuple[int, float]]) -> None:
        """Request outbound CC sync; safe to call from the GUI/main thread."""
        self.fader_sync_requested.emit(mappings)

    def set_device(self, name: str) -> None:
        """Update the target MIDI device. Reconnects on the next loop cycle."""
        if self._device_name != name:
            logger.info("MIDI Port change requested: %s", name)
            self._device_name = name
            self._panic_flag = True

    def set_mode(self, mode: str) -> None:
        """Update the input mode (to know if MIDI is allowed)."""
        if self._input_mode != mode:
            logger.debug("MIDI Mode changed: %s -> %s", self._input_mode, mode)
            self._input_mode = mode
            self._panic_flag = True

    def update_mappings(self, mappings: dict[int, int]) -> None:
        """
        Update the CC -> Channel mappings.
        Args:
            mappings: dict where key is CC number, value is channel index.
        """
        self._cc_map = mappings
        logger.debug("MIDI CC mappings updated: %s", self._cc_map)

    def update_mute_mappings(self, mappings: dict[int, int]) -> None:
        """
        Update the mute-CC -> Channel mappings.
        Args:
            mappings: dict where key is CC number, value is channel index.
        """
        self._mute_cc_map = mappings
        logger.debug("MIDI Mute CC mappings updated: %s", self._mute_cc_map)

    def set_profile_ccs(
        self,
        next_cc: int | None,
        prev_cc: int | None,
        direct_map: dict[int, str],
    ) -> None:
        """
        Configure MIDI CCs for profile switching.

        next_cc:    CC number that triggers switch_next (fires on value 127).
        prev_cc:    CC number that triggers switch_prev (fires on value 127).
        direct_map: {cc_number: profile_id} for direct profile jumps.
        """
        self._profile_next_cc = next_cc
        self._profile_prev_cc = prev_cc
        self._profile_direct_map = dict(direct_map)
        logger.debug(
            "Profile CCs updated: next=%s prev=%s direct=%s",
            next_cc, prev_cc, direct_map,
        )

    def get_mapped_volumes(self) -> list[tuple[int, float]]:
        """Return a list of (channel_index, volume) for all current mappings."""
        results = []
        for cc, ch_idx in self._cc_map.items():
            if cc in self._last_values:
                val = self._last_values[cc]
                results.append((ch_idx, val / 127.0))
        return results

    def refresh_ports(self) -> None:
        """Trigger a re-scan of MIDI ports (Hot-Plug support)."""
        logger.info("MIDI Refresh requested (Hot-Plug).")
        self._panic_flag = True

    def stop(self) -> None:
        """Gracefully stop the thread loop."""
        self._running = False
        # Give the loop one more slice to check _running
        # Only terminate if it's really stuck (finally blocks might not run!)
        if not self.wait(2000):
            logger.warning("MidiThread: Force-terminating (graceful stop took too long)")
            self.terminate()
            # Strategy B: bounded wait after terminate() so rtmidi/ALSA calls
            # blocked during system audio teardown cannot hang indefinitely.
            if not self.wait(1000):
                logger.error("MidiThread still alive after terminate — abandoning")
        # Close the persistent virtual port (if still open) now that the
        # thread has stopped.  This releases the ALSA sequencer client.
        if self._virtual_client is not None:
            try:
                self._virtual_client.close_port()
            except Exception:
                pass
            self._virtual_client = None

    def restart_midi(self) -> None:
        """Manual reset to clear critical errors and restart the backend."""
        logger.info("MIDI Restart requested by user/system.")
        self._critical_error = False
        self._error_count = 0
        self._panic_flag = True
        self.status_changed.emit("connecting", "Restarting MIDI...")

    def run(self) -> None:
        """Main loop with Circuit Breaker protection."""
        self._running = True
        self._panic_flag = False
        self._critical_error = False
        self._error_count = 0

        logger.info("MidiThread started. (Mode: %s, Device: %s)", self._input_mode, self._device_name)

        while self._running:
            try:
                self._run_safe()
                # _run_safe() exited cleanly (e.g. stop() called) — reset circuit breaker
                # so a subsequent restart() begins from a clean state.
                self._critical_error = False
                self._error_count = 0
            except Exception as exc:
                self._error_count += 1
                logger.exception("CRITICAL MidiThread crash (Circuit Breaker triggered)")

                if self._error_count >= 3:
                    self._critical_error = True
                    self.status_changed.emit("error_critical", f"MIDI Error: {str(exc)}")
                    logger.error(
                        "MIDI Circuit Breaker: Backend disabled after %d consecutive failures.",
                        self._error_count,
                    )
                else:
                    self.status_changed.emit("error_temporary", "MIDI Backend crashed - Recovering...")

                # Cooldown before retry or while disabled
                self._sleep_checked(5.0)

    def _run_safe(self) -> None:
        """Inner loop for MIDI processing logic."""
        backend_found = ensure_midi_backend()

        if backend_found == 'rtmidi':
            logger.info("MIDI Backend loaded: rtmidi (supports virtual ports)")
        elif backend_found == 'portmidi':
            logger.info("MIDI Backend loaded: portmidi via ctypes")

        if not backend_found:
            logger.error("CRITICAL: No MIDI backend (rtmidi or portmidi) found! MIDI will not work.")
            self.connection_changed.emit(False)
            self.status_changed.emit("error_critical", "No MIDI backend found.")
            # Stay in loop but idle
            while self._running and not self._panic_flag:
                self._sleep_checked(1.0)
            return

        self._error_count = 0 # Reset on successful backend load
        self.status_changed.emit("stable", "MIDI Ready")

        _vport_warning_logged = False
        while self._running:
            if self._panic_flag:
                self._panic_flag = False
                logger.debug("MidiThread: Internally restarting due to flag.")

            # Is MIDI even enabled?
            if self._input_mode == "usb":
                # USB-only: idle without closing the virtual port so ALSA
                # clients see one stable "NativMix:Input" across mode switches.
                if self._virtual_client is None:
                    self.connection_changed.emit(False)
                # Wait for setting changes
                while self._running and not self._panic_flag and self._input_mode == "usb":
                    time.sleep(0.5)
                continue

            try:
                if self._critical_error:
                    self._sleep_checked(2.0)
                    continue

                target_device = self._device_name if self._device_name else "VIRTUAL_PORT"

                if target_device == "VIRTUAL_PORT":
                    if sys.platform == "win32":
                        # WinMM does not support virtual MIDI ports.
                        if not _vport_warning_logged:
                            logger.warning("MidiThread: Virtual Port is not supported on Windows (WinMM).")
                            _vport_warning_logged = True
                        self.connection_changed.emit(False)
                        self.status_changed.emit("disabled", "Virtual Port: not supported on Windows")
                        self._sleep_checked(5.0)
                        continue

                    if backend_found != "rtmidi":
                        if not _vport_warning_logged:
                            logger.info(
                                "MidiThread: Virtual Port requires rtmidi, but %s is loaded"
                                " — expected on Fedora/Nobara. Skipping.",
                                backend_found,
                            )
                            _vport_warning_logged = True
                        self.connection_changed.emit(False)
                        self.status_changed.emit("disabled", "Virtual Port needs rtmidi")
                        self._sleep_checked(5.0)
                        continue

                    # Reuse the existing virtual port if already open so ALSA
                    # clients see one stable port across USB ↔ hybrid switches.
                    if self._virtual_client is None:
                        logger.debug("MidiThread: Opening Virtual Port 'NativMix:Input'...")
                        self.status_changed.emit("connecting", "Opening Virtual Port...")
                        _client = None
                        try:
                            import rtmidi  # Local import for safety
                            _client = rtmidi.MidiIn(rtmidi.API_LINUX_ALSA, name="NativMix")
                            _client.open_virtual_port("Input")
                            self._virtual_client = _client
                        except Exception as e:
                            logger.warning("MidiThread: Could not open virtual port: %s", e)
                            if _client is not None:
                                try:
                                    _client.close_port()
                                except Exception as exc:
                                    logger.debug("MidiThread: close_port cleanup failed: %s", exc)
                            self._virtual_client = None
                            self.connection_changed.emit(False)
                            self.status_changed.emit("error_temporary", "Virtual Port failed - retrying...")
                            self._sleep_checked(5.0)
                            continue
                    else:
                        logger.debug("MidiThread: Reusing existing Virtual Port 'NativMix:Input'.")

                    self.connection_changed.emit(True)
                    self.status_changed.emit("stable", "Virtual MIDI Online")

                    while self._running and not self._panic_flag:
                        # Only exit if switching to a physical device; a mode
                        # change to USB keeps the port alive (handled above).
                        if self._device_name not in ("", "VIRTUAL_PORT"):
                            self._virtual_client.close_port()
                            self._virtual_client = None
                            logger.debug("MidiThread: Virtual Port closed (device change).")
                            break

                        # In USB mode just idle – don't process MIDI events.
                        if self._input_mode == "usb":
                            time.sleep(0.01)
                            continue

                        self._process_pending_sync(None)

                        msg_data = self._virtual_client.get_message()
                        if msg_data:
                            msg, _ = msg_data
                            if len(msg) >= 3 and (msg[0] & 0xF0) == 0xB0:
                                self._handle_cc(msg[1], msg[2])

                        time.sleep(0.01)

                else:
                    # Physical Device Mode
                    logger.info("MidiThread: Connecting to physical device: %s", target_device)
                    names = mido.get_input_names()
                    logger.info("MidiThread: Available MIDI ports: %s", names)
                    target_name = None
                    for name in names:
                        if target_device in name:
                            target_name = name
                            break

                    if not target_name:
                        logger.warning(
                            "MidiThread: Device '%s' not found. Available: %s",
                            target_device, names
                        )
                        self.connection_changed.emit(False)
                        self.status_changed.emit("error_temporary", f"Device '{target_device}' not found")
                        self._sleep_checked(5.0)
                        continue

                    out_name = None
                    if self._fader_feedback_enabled:
                        try:
                            out_name = _match_midi_port(mido.get_output_names(), target_device)
                        except Exception as exc:
                            logger.debug("MidiThread: could not list MIDI outputs: %s", exc)
                        if out_name is None:
                            logger.warning(
                                "MIDI fader feedback enabled but no output port matched '%s'",
                                target_device,
                            )

                    if out_name:
                        with mido.open_input(target_name) as inport, mido.open_output(out_name) as outport:
                            logger.info(
                                "MidiThread: Connected to %s (out: %s)", target_name, out_name
                            )
                            self.status_changed.emit("stable", f"Connected: {target_device}")
                            self.connection_changed.emit(True)
                            self._device_loop(inport, outport, target_device)
                    else:
                        with mido.open_input(target_name) as inport:
                            logger.info("MidiThread: Connected to %s", target_name)
                            self.status_changed.emit("stable", f"Connected: {target_device}")
                            self.connection_changed.emit(True)
                            self._device_loop(inport, None, target_device)

            except (OSError, EOFError, RuntimeError, TypeError) as exc:
                logger.warning("MIDI Recoverable Error: %s", exc)
                self.connection_changed.emit(False)
                self.status_changed.emit("error_temporary", "MIDI Disconnected - Retrying...")
                self._sleep_checked(5.0)

        logger.debug("MidiThread stopped")

    def _device_loop(self, inport, outport, target_device: str) -> None:
        """Poll a physical MIDI input (and optional output) until reconnect is needed."""
        while self._running and not self._panic_flag:
            if self._input_mode == "usb" or self._device_name != target_device:
                break
            self._process_pending_sync(outport)
            msg = inport.receive(block=False)
            if msg is None:
                time.sleep(0.05)
                continue
            if msg.type == "control_change":
                self._handle_cc(msg.control, msg.value)

    def _process_pending_sync(self, outport) -> None:
        """Send queued outbound fader CC values when feedback is enabled."""
        if not self._fader_feedback_enabled:
            return
        with self._feedback_lock:
            pending = self._pending_sync
            self._pending_sync = None
        if not pending:
            return
        if outport is None:
            return

        ch_to_cc = {ch_idx: cc for cc, ch_idx in self._cc_map.items()}
        for ch_idx, volume in pending:
            cc = ch_to_cc.get(ch_idx)
            if cc is None:
                continue
            self._send_fader_cc(outport, cc, ch_idx, volume)

    def _send_fader_cc(self, outport, cc: int, ch_idx: int, volume: float) -> None:
        """Send one outbound volume CC and arm takeover suppression for that channel."""
        cc_value = max(0, min(127, int(round(max(0.0, min(1.0, volume)) * 127))))
        with self._feedback_lock:
            if self._last_sent_cc_value.get(cc) == cc_value:
                return
            self._last_sent_cc_value[cc] = cc_value
            self._last_values[cc] = cc_value
            self._feedback_takeover[ch_idx] = cc_value / 127.0
        try:
            outport.send(mido.Message("control_change", channel=0, control=cc, value=cc_value))
            logger.debug("MIDI fader feedback: ch=%d cc=%d value=%d", ch_idx, cc, cc_value)
        except (OSError, RuntimeError) as exc:
            logger.warning("MIDI fader feedback send failed (cc=%d): %s", cc, exc)

    def _handle_cc(self, cc: int, val: int) -> None:
        """Process a single MIDI Control Change message."""
        self._last_values[cc] = val

        # 1. Always emit for Learn handshake
        self.midi_cc_received.emit(cc, val)

        # 2. Check if mapped to a fader — throttled to 50 Hz per CC (20 ms)
        # to prevent Qt signal queue flooding from misbehaving MIDI controllers.
        if cc in self._cc_map:
            ch_idx = self._cc_map[cc]
            with self._feedback_lock:
                takeover_vol = self._feedback_takeover.get(ch_idx)
            if _inbound_fader_suppressed(takeover_vol, val):
                return
            if takeover_vol is not None:
                with self._feedback_lock:
                    self._feedback_takeover.pop(ch_idx, None)
            now = time.monotonic()
            if now - self._last_vol_emit.get(cc, 0.0) >= 0.02:
                self._last_vol_emit[cc] = now
                vol = val / 127.0
                self.midi_volumes_changed.emit([(ch_idx, vol)])

        # 3. Check if mapped to a mute toggle.
        # Only react to val == 127 (standard button-on) so faders/potis cannot
        # cause rapid toggle-flicker when sweeping through intermediate values.
        if cc in self._mute_cc_map and val == 127:
            self.midi_mute_toggled.emit(self._mute_cc_map[cc])

        # 4. Profile switching (only on button press, value == 127)
        if val == 127:
            if cc == self._profile_next_cc:
                self.profile_switch_requested.emit("next")
            elif cc == self._profile_prev_cc:
                self.profile_switch_requested.emit("prev")
            elif cc in self._profile_direct_map:
                self.profile_switch_requested.emit(self._profile_direct_map[cc])

    def _sleep_checked(self, seconds: float) -> None:
        """Sleep while checking for thread stop request."""
        end_time = time.time() + seconds
        while self._running and time.time() < end_time:
            time.sleep(0.1)
