"""
MIDI hardware backend for NativMix.

Handles MIDI input devices (via mido/rtmidi) and maps Control Change (CC)
messages to volume levels. Supports a "Learn" mode for interactive setup.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import sys
import threading
import time
import types

import mido
from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot

logger = logging.getLogger(__name__)

# ALSA sequencer device nodes used for MIDI in Flatpak.  Access requires
# either --device=all or an explicit device permission in the manifest.
_ALSA_SEQ_DEVICES = ("/dev/snd/seq", "/dev/snd/midiC0D0")

# Set once at import time so the check result is available without a running
# MIDI session.
_IS_FLATPAK: bool = bool(
    os.environ.get("FLATPAK_ID") or os.path.exists("/.flatpak-info")
)


def check_alsa_sequencer_access() -> bool:
    """Return True if the ALSA sequencer is accessible.

    Checks whether the ALSA sequencer character device (``/dev/snd/seq``) is
    readable.  In a Flatpak sandbox this requires ``--device=all`` (or an
    explicit device rule) in the application manifest.  A False result means
    MIDI will not work via rtmidi/ALSA.
    """
    return os.access("/dev/snd/seq", os.R_OK | os.W_OK)


def warn_if_alsa_sequencer_inaccessible() -> None:
    """Emit a warning when running in Flatpak without ALSA sequencer access.

    Should be called once during MIDI initialisation.  The warning is
    suppressed outside of Flatpak because non-sandbox environments rarely
    need this hint.
    """
    if _IS_FLATPAK and not check_alsa_sequencer_access():
        logger.warning(
            "MIDI: ALSA sequencer device (/dev/snd/seq) is not accessible inside "
            "the Flatpak sandbox.  MIDI input will not work.  Add '--device=all' "
            "(or a specific device permission) to the Flatpak manifest's "
            "finish-args to grant sequencer access."
        )
_FADER_FEEDBACK_TOLERANCE = 0.05
_MIDO_PORTMIDI_DEFAULT_CANDIDATE = "libportmidi.so"


class _PortMidiState:
    """Process-wide PortMidi resolution/cache state."""

    def __init__(self) -> None:
        self.handle: ctypes.CDLL | None = None
        self.candidate: str | None = None
        self.failure_reported = False
        self.lock = threading.RLock()


_PORTMIDI = _PortMidiState()


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


def _get_portmidi_candidates() -> list[str]:
    """Return PortMidi library candidates in preferred order without duplicates."""
    candidates: list[str] = []
    for candidate in (
        ctypes.util.find_library("portmidi"),
        "libportmidi.so.0",
        _MIDO_PORTMIDI_DEFAULT_CANDIDATE,
    ):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _load_portmidi_library() -> ctypes.CDLL:
    """Load and return the first usable PortMidi shared library handle."""
    with _PORTMIDI.lock:
        if _PORTMIDI.handle is not None:
            return _PORTMIDI.handle

        candidates = _get_portmidi_candidates()

        last_error: OSError | None = None
        for candidate in candidates:
            try:
                _PORTMIDI.handle = ctypes.CDLL(candidate)
                _PORTMIDI.candidate = candidate
                _PORTMIDI.failure_reported = False
                logger.debug("Loaded PortMidi shared library: %s", candidate)
                return _PORTMIDI.handle
            except OSError as exc:
                logger.debug("PortMidi candidate load failed: %s (%s)", candidate, exc)
                last_error = exc

        if not _PORTMIDI.failure_reported:
            logger.warning(
                "Unable to load PortMidi library; attempted=%s; last_error=%s",
                candidates,
                last_error,
            )
            _PORTMIDI.failure_reported = True

        raise ImportError(
            f"Unable to load PortMidi library; attempted={candidates}; "
            f"last_error={str(last_error)}. "
            "Please ensure PortMidi is installed on your system."
        )


def _set_ctypes_signature(
    library_handle: ctypes.CDLL,
    name: str,
    restype,
    argtypes: list[type | ctypes._CFuncPtr] | None = None,
) -> None:
    """Set ctypes function signature metadata on a PortMidi symbol."""
    func = getattr(library_handle, name)
    func.restype = restype
    if argtypes is not None:
        func.argtypes = argtypes


def _build_mido_portmidi_init_module(
    library_handle: ctypes.CDLL,
    candidate: str,
) -> types.ModuleType:
    """Build a minimal mido.backends.portmidi_init module around a cached handle."""
    module = types.ModuleType("mido.backends.portmidi_init")
    module.__dict__["__package__"] = "mido.backends"
    module.dll_name = candidate
    module.lib = library_handle
    module.null = None
    module.false = 0
    module.true = 1
    module.PM_HOST_ERROR_MSG_LEN = 256

    def get_host_error_message() -> str:
        buf = ctypes.create_string_buffer(module.PM_HOST_ERROR_MSG_LEN)
        module.lib.Pm_GetHostErrorText(buf, module.PM_HOST_ERROR_MSG_LEN)
        return buf.value.decode()

    module.get_host_error_message = get_host_error_message
    module.PmError = ctypes.c_int
    module.pmNoError = 0
    module.pmHostError = -10000
    module.pmInvalidDeviceId = -9999
    module.pmInsufficientMemory = -9989
    module.pmBufferTooSmall = -9979
    module.pmBufferOverflow = -9969
    module.pmBadPtr = -9959
    module.pmBadData = -9994
    module.pmInternalError = -9993
    module.pmBufferMaxSize = -9992

    _set_ctypes_signature(module.lib, "Pm_Initialize", module.PmError)
    _set_ctypes_signature(module.lib, "Pm_Terminate", module.PmError)

    module.PmDeviceID = ctypes.c_int
    module.PortMidiStreamPtr = ctypes.c_void_p
    module.PmStreamPtr = module.PortMidiStreamPtr
    module.PortMidiStreamPtrPtr = ctypes.POINTER(module.PortMidiStreamPtr)

    _set_ctypes_signature(module.lib, "Pm_HasHostError", ctypes.c_int, [module.PortMidiStreamPtr])
    _set_ctypes_signature(module.lib, "Pm_GetErrorText", ctypes.c_char_p, [module.PmError])
    _set_ctypes_signature(module.lib, "Pm_GetHostErrorText", None, [ctypes.c_char_p, ctypes.c_uint])

    module.pmNoDevice = -1

    class PmDeviceInfo(ctypes.Structure):
        _fields_ = [
            ("structVersion", ctypes.c_int),
            ("interface", ctypes.c_char_p),
            ("name", ctypes.c_char_p),
            ("is_input", ctypes.c_int),
            ("is_output", ctypes.c_int),
            ("opened", ctypes.c_int),
        ]

    module.PmDeviceInfo = PmDeviceInfo
    module.PmDeviceInfoPtr = ctypes.POINTER(PmDeviceInfo)

    _set_ctypes_signature(module.lib, "Pm_CountDevices", ctypes.c_int)
    _set_ctypes_signature(module.lib, "Pm_GetDefaultOutputDeviceID", module.PmDeviceID)
    _set_ctypes_signature(module.lib, "Pm_GetDefaultInputDeviceID", module.PmDeviceID)

    module.PmTimestamp = ctypes.c_long
    module.PmTimeProcPtr = ctypes.CFUNCTYPE(module.PmTimestamp, ctypes.c_void_p)
    module.NullTimeProcPtr = ctypes.cast(module.null, module.PmTimeProcPtr)

    _set_ctypes_signature(module.lib, "Pm_GetDeviceInfo", module.PmDeviceInfoPtr, [module.PmDeviceID])
    _set_ctypes_signature(module.lib, "Pm_OpenInput", module.PmError, [
        module.PortMidiStreamPtrPtr,
        module.PmDeviceID,
        ctypes.c_void_p,
        ctypes.c_long,
        module.PmTimeProcPtr,
        ctypes.c_void_p,
    ])
    _set_ctypes_signature(module.lib, "Pm_OpenOutput", module.PmError, [
        module.PortMidiStreamPtrPtr,
        module.PmDeviceID,
        ctypes.c_void_p,
        ctypes.c_long,
        module.PmTimeProcPtr,
        ctypes.c_void_p,
        ctypes.c_long,
    ])
    _set_ctypes_signature(module.lib, "Pm_SetFilter", module.PmError, [module.PortMidiStreamPtr, ctypes.c_long])
    _set_ctypes_signature(module.lib, "Pm_SetChannelMask", module.PmError, [module.PortMidiStreamPtr, ctypes.c_int])
    _set_ctypes_signature(module.lib, "Pm_Abort", module.PmError, [module.PortMidiStreamPtr])
    _set_ctypes_signature(module.lib, "Pm_Close", module.PmError, [module.PortMidiStreamPtr])

    module.PmMessage = ctypes.c_long

    class PmEvent(ctypes.Structure):
        _fields_ = [("message", module.PmMessage), ("timestamp", module.PmTimestamp)]

    module.PmEvent = PmEvent
    module.PmEventPtr = ctypes.POINTER(PmEvent)

    _set_ctypes_signature(
        module.lib,
        "Pm_Read",
        module.PmError,
        [module.PortMidiStreamPtr, module.PmEventPtr, ctypes.c_long],
    )
    _set_ctypes_signature(module.lib, "Pm_Poll", module.PmError, [module.PortMidiStreamPtr])
    _set_ctypes_signature(
        module.lib,
        "Pm_Write",
        module.PmError,
        [module.PortMidiStreamPtr, module.PmEventPtr, ctypes.c_long],
    )
    _set_ctypes_signature(
        module.lib,
        "Pm_WriteShort",
        module.PmError,
        [module.PortMidiStreamPtr, module.PmTimestamp, ctypes.c_long],
    )
    _set_ctypes_signature(
        module.lib,
        "Pm_WriteSysEx",
        module.PmError,
        [module.PortMidiStreamPtr, module.PmTimestamp, ctypes.c_char_p],
    )

    module.PtError = ctypes.c_int
    module.ptNoError = 0
    module.ptHostError = -10000
    module.ptAlreadyStarted = -9999
    module.ptAlreadyStopped = -9998
    module.ptInsufficientMemory = -9997

    module.PtTimestamp = ctypes.c_long
    module.PtCallback = ctypes.CFUNCTYPE(module.PmTimestamp, ctypes.c_void_p)

    _set_ctypes_signature(module.lib, "Pt_Start", module.PtError, [ctypes.c_int, module.PtCallback, ctypes.c_void_p])
    _set_ctypes_signature(module.lib, "Pt_Stop", module.PtError)
    _set_ctypes_signature(module.lib, "Pt_Started", ctypes.c_int)
    _set_ctypes_signature(module.lib, "Pt_Time", module.PtTimestamp)
    return module


def _prime_mido_portmidi_init_module() -> None:
    """Preload mido.backends.portmidi_init with the resolved PortMidi handle."""
    library_handle = _load_portmidi_library()
    candidate = _PORTMIDI.candidate or _MIDO_PORTMIDI_DEFAULT_CANDIDATE

    module_name = "mido.backends.portmidi_init"
    existing_module = sys.modules.get(module_name)
    if (
        existing_module is not None
        and getattr(existing_module, "lib", None) is library_handle
        and getattr(existing_module, "dll_name", None) == candidate
    ):
        return

    module = _build_mido_portmidi_init_module(library_handle, candidate)
    sys.modules[module_name] = module


def _set_portmidi_backend() -> None:
    """Configure mido to use PortMidi while reusing the resolved library handle."""
    with _PORTMIDI.lock:
        _prime_mido_portmidi_init_module()
        mido.set_backend('mido.backends.portmidi')


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
                _set_portmidi_backend()
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
                        warn_if_alsa_sequencer_inaccessible()
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
