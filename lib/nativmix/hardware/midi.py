"""
MIDI hardware backend for NativMix.

Handles MIDI input devices (via mido/rtmidi) and maps Control Change (CC)
messages to volume levels. Supports a "Learn" mode for interactive setup.
"""

from __future__ import annotations

import logging
import time

import mido
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)


def ensure_midi_backend() -> str | None:
    """Probe and set the best available mido backend.

    On Windows: uses rtmidi/WinMM directly — no portmidi library search.
    On Linux: tries rtmidi first; on Fedora/Nobara portmidi is preferred.
    Returns the backend name ('rtmidi' or 'portmidi') or None if none is available.
    Idempotent — safe to call multiple times.
    """
    import sys
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
    midi_initialized = pyqtSignal()

    def __init__(self, device_name: str = "", input_mode: str = "hybrid", parent=None) -> None:
        super().__init__(parent)
        self.daemon = True
        self._device_name: str = device_name
        self._input_mode: str = input_mode  # "usb", "hybrid", "midi_only"
        self._running: bool = False
        self._panic_flag: bool = False
        self._critical_error: bool = False
        self._error_count: int = 0
        self._cc_map: dict[int, int] = {}       # cc_number -> channel_index (volume)
        self._mute_cc_map: dict[int, int] = {}  # cc_number -> channel_index (mute toggle)
        self._last_values: dict[int, int] = {}  # cc_number -> last_seen_value (0-127)
        # Persistent virtual port – kept alive across USB ↔ hybrid mode
        # switches so ALSA clients see one stable "NativMix:Input" port.
        self._virtual_client = None

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
        self.wait(2000)
        # Only terminate if it's really stuck (finally blocks might not run!)
        if self.isRunning():
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

    def trigger_panic(self) -> None:
        """Alias for restart_midi for GUI compatibility. (Task 8 Polish)"""
        self.restart_midi()

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
            except Exception as exc:
                self._error_count += 1
                logger.exception("CRITICAL MidiThread crash (Circuit Breaker triggered)")
                
                if self._error_count >= 3:
                    self._critical_error = True
                    self.status_changed.emit("error_critical", f"MIDI Error: {str(exc)}")
                    logger.error("MIDI Circuit Breaker: Backend disabled after %d consecutive failures.", self._error_count)
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

        # Signal that the initial backend probe is finished
        self.midi_initialized.emit()

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

            # We use local references for resources to ensure they are cleaned up in each cycle
            virtual_client = None

            try:
                if self._critical_error:
                    self._sleep_checked(2.0)
                    continue

                target_device = self._device_name if self._device_name else "VIRTUAL_PORT"

                if target_device == "VIRTUAL_PORT":
                    import sys as _sys
                    if _sys.platform == "win32":
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
                            logger.info("MidiThread: Virtual Port requires rtmidi, but %s is loaded — expected on Fedora/Nobara. Skipping.", backend_found)
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
                            import rtmidi # Local import for safety
                            _client = rtmidi.MidiIn(rtmidi.API_LINUX_ALSA, name="NativMix")
                            _client.open_virtual_port("Input")
                            self._virtual_client = _client
                        except Exception as e:
                            logger.warning("MidiThread: Could not open virtual port: %s", e)
                            if _client is not None:
                                try:
                                    _client.close_port()
                                except Exception:
                                    pass
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

                        msg_data = self._virtual_client.get_message()
                        if msg_data:
                            msg, _ = msg_data
                            if len(msg) >= 3 and (msg[0] & 0xF0) == 0xB0:
                                self._handle_cc(msg[1], msg[2])

                        time.sleep(0.01)
                    # virtual_client stays None here so the finally block
                    # does not close _virtual_client on a normal loop exit.

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

                    with mido.open_input(target_name) as inport:
                        logger.info("MidiThread: Connected to %s", target_name)
                        self.status_changed.emit("stable", f"Connected: {target_device}")
                        self.connection_changed.emit(True)
                        while self._running and not self._panic_flag:
                            if self._input_mode == "usb" or self._device_name != target_device:
                                break
                            msg = inport.receive(block=False)
                            if msg is None:
                                time.sleep(0.05)
                                continue
                            if msg.type == 'control_change':
                                self._handle_cc(msg.control, msg.value)

            except (IOError, EOFError, RuntimeError, TypeError) as exc:
                logger.warning("MIDI Recoverable Error: %s", exc)
                self.connection_changed.emit(False)
                self.status_changed.emit("error_temporary", "MIDI Disconnected - Retrying...")
                self._sleep_checked(5.0)
            finally:
                if virtual_client:
                    try:
                        virtual_client.close_port()
                    except Exception as exc:
                        logger.debug("Cleanup: close_port() error (ignored): %s", exc)
                    logger.debug("Cleanup: Virtual MIDI Port closed.")

        logger.debug("MidiThread stopped")

    def _handle_cc(self, cc: int, val: int) -> None:
        """Process a single MIDI Control Change message."""
        self._last_values[cc] = val

        # 1. Always emit for Learn handshake
        self.midi_cc_received.emit(cc, val)

        # 2. Check if mapped to a fader
        if cc in self._cc_map:
            ch_idx = self._cc_map[cc]
            # Convert 0-127 to 0.0-1.0
            vol = val / 127.0
            self.midi_volumes_changed.emit([(ch_idx, vol)])

        # 3. Check if mapped to a mute toggle (any non-zero value triggers)
        if cc in self._mute_cc_map and val > 0:
            self.midi_mute_toggled.emit(self._mute_cc_map[cc])

    def _sleep_checked(self, seconds: float) -> None:
        """Sleep while checking for thread stop request."""
        end_time = time.time() + seconds
        while self._running and time.time() < end_time:
            time.sleep(0.1)
