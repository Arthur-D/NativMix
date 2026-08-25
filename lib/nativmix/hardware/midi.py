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

from nativmix.utils.midi_ports import match_midi_port, normalize_midi_device_name
from nativmix.utils.proc_resolver import IS_FLATPAK

logger = logging.getLogger(__name__)

# ALSA sequencer device nodes used for MIDI in Flatpak.  Access requires
# either --device=all or an explicit device permission in the manifest.
_ALSA_SEQ_DEVICES = ("/dev/snd/seq", "/dev/snd/midiC0D0")

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
    if IS_FLATPAK and not check_alsa_sequencer_access():
        logger.warning(
            "MIDI: ALSA sequencer device (/dev/snd/seq) is not accessible inside "
            "the Flatpak sandbox.  MIDI input will not work.  Add '--device=all' "
            "(or a specific device permission) to the Flatpak manifest's "
            "finish-args to grant sequencer access."
        )


# Ignore inbound mapped fader CC while within this band of the last outbound sync.
_FADER_FEEDBACK_TOLERANCE = 0.05

# Arduino example controller LED hue encoding.
_LED_HUE_MUTED = 0
_LED_HUE_UNMUTED = 42
_EXAMPLE_MUTE_CC_MIN = 5
_EXAMPLE_MUTE_CC_MAX = 8
_EXAMPLE_LED_CC_BASE = 32
_MUTE_OUTBOUND_SUPPRESS_S = 0.15


def _example_led_cc_for_mute(mute_cc: int) -> int | None:
    """Map example mute CC 5-8 to LED hue CC 32-35."""
    if _EXAMPLE_MUTE_CC_MIN <= mute_cc <= _EXAMPLE_MUTE_CC_MAX:
        return _EXAMPLE_LED_CC_BASE + mute_cc - _EXAMPLE_MUTE_CC_MIN
    return None
_MIDO_PORTMIDI_DEFAULT_CANDIDATE = "libportmidi.so"
_MIDI_RECOVERABLE_ERRORS = (OSError, EOFError, RuntimeError, TypeError, ValueError)


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
    """Compatibility wrapper for stable backend-independent port matching."""
    return match_midi_port(names, device_key)


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


def _set_rtmidi_backend() -> None:
    """Configure mido to use python-rtmidi."""
    import rtmidi  # noqa: F401

    mido.set_backend("mido.backends.rtmidi")


def ensure_midi_backend() -> str | None:
    """Probe and set the best available mido backend.

    RtMidi is preferred on every platform because it handles device removal
    without PortMidi's unsafe native poll/read race. Native Linux installations
    may use PortMidi as an explicit compatibility fallback when RtMidi is not
    packaged. Flatpak never enables that fallback because a hot-unplug can
    segfault inside PortMidi before Python can recover.

    Returns the backend name ('rtmidi' or 'portmidi') or None if none is available.
    Idempotent — safe to call multiple times.
    """
    try:
        _set_rtmidi_backend()
        return "rtmidi"
    except (ImportError, OSError):
        if sys.platform == "win32" or IS_FLATPAK:
            return None

    try:
        _set_portmidi_backend()
    except (ImportError, OSError):
        return None

    logger.warning(
        "MIDI Backend fallback: PortMidi is active because python-rtmidi is unavailable. "
        "USB hot-unplug is unsafe with PortMidi; install python-rtmidi when possible."
    )
    return "portmidi"


class MidiThread(QThread):
    """
    Background thread that listens for MIDI CC messages from a specific device.

    Signals
    -------
    midi_volumes_changed(list[tuple[int, float]])
        Emitted when mapped MIDI CC values change.
        List of (channel_index, volume_0_to_1).
    midi_cc_received(int, int, int)
        Emitted for the "Learn" handshake: (protocol_channel, control_number, value).
    connection_changed(bool)
        Emitted when the device is opened (True) or closed/missing (False).
    device_state_changed(int, str, str, str, list, str)
        Authoritative generation, status, message, configured device, inventory,
        and successfully opened backend port for GUI reconciliation.
    """

    midi_volumes_changed = pyqtSignal(list)  # list[tuple[int, float]]
    midi_cc_received = pyqtSignal(int, int, int)
    midi_mute_toggled = pyqtSignal(int)  # channel_index
    connection_changed = pyqtSignal(bool)
    device_state_changed = pyqtSignal(int, str, str, str, list, str)
    # Status signal: (status_type, display_message)
    # Types: "connecting", "stable", "warning", "error_temporary", "error_critical"
    status_changed = pyqtSignal(str, str)
    profile_switch_requested = pyqtSignal(str)  # "next", "prev", or profile_id
    fader_sync_requested = pyqtSignal(list)  # list[tuple[int, float]] (channel, volume)
    mute_feedback_requested = pyqtSignal(list)  # list[tuple[int, bool]] (channel, muted)

    def __init__(self, device_name: str = "", input_mode: str = "hybrid", parent=None) -> None:
        super().__init__(parent)
        self._device_name: str = device_name
        self._input_mode: str = input_mode  # "usb", "hybrid", "midi_only"
        self._running: bool = False
        self._panic_flag: bool = False
        self._critical_error: bool = False
        self._error_count: int = 0
        self._connection_state: bool | None = None
        self._connection_generation = 0
        self._generation_lock = threading.Lock()
        self._available_ports: list[str] = []
        self._refresh_requested = False
        self._cc_map: dict[tuple[int, int], int] = {}
        self._mute_cc_map: dict[tuple[int, int], int] = {}
        self._map_lock = threading.RLock()
        self._last_values: dict[tuple[int, int], int] = {}
        self._last_vol_emit: dict[tuple[int, int], float] = {}
        # Persistent virtual port – kept alive across USB ↔ hybrid mode
        # switches so ALSA clients see one stable "NativMix:Input" port.
        self._virtual_client = None
        self._profile_next_cc: int | None = None
        self._profile_prev_cc: int | None = None
        self._profile_direct_map: dict[int, str] = {}  # cc -> profile_id
        self._fader_feedback_enabled: bool = False
        self._feedback_lock = threading.Lock()
        self._feedback_takeover: dict[tuple[int, int], float] = {}
        self._last_sent_cc_value: dict[tuple[int, int], int] = {}
        self._pending_sync: list[tuple[int, float]] | None = None
        self._pending_mute_feedback: list[tuple[int, bool]] | None = None
        self._mute_outbound_suppress_until: dict[tuple[int, int], float] = {}
        self.fader_sync_requested.connect(self._queue_fader_sync)
        self.mute_feedback_requested.connect(self._queue_mute_feedback)

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
                self._pending_mute_feedback = None
                self._mute_outbound_suppress_until.clear()

    @pyqtSlot(list)
    def _queue_fader_sync(self, mappings: list[tuple[int, float]]) -> None:
        """Queue outbound fader positions (thread-safe via queued signal)."""
        if not self._fader_feedback_enabled or not mappings:
            return
        with self._feedback_lock:
            pending = dict(self._pending_sync or [])
            pending.update(mappings)
            self._pending_sync = list(pending.items())

    def request_fader_sync(self, mappings: list[tuple[int, float]]) -> None:
        """Request outbound CC sync; safe to call from the GUI/main thread."""
        self.fader_sync_requested.emit(mappings)

    @pyqtSlot(list)
    def _queue_mute_feedback(self, states: list[tuple[int, bool]]) -> None:
        """Queue outbound mute and LED states."""
        if not self._fader_feedback_enabled or not states:
            return
        with self._feedback_lock:
            pending = dict(self._pending_mute_feedback or [])
            pending.update(states)
            self._pending_mute_feedback = list(pending.items())

    def request_mute_feedback(self, states: list[tuple[int, bool]]) -> None:
        """Request outbound mute feedback from the owning Qt thread."""
        self.mute_feedback_requested.emit(states)

    def _prepare_feedback_connection(self) -> None:
        """Forget delivery state so a newly opened output receives a full sync."""
        with self._feedback_lock:
            self._feedback_takeover.clear()
            self._last_sent_cc_value.clear()
            self._mute_outbound_suppress_until.clear()

    def set_device(self, name: str) -> None:
        """Update the target MIDI device. Reconnects on the next loop cycle."""
        name = normalize_midi_device_name(name)
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

    def update_mappings(self, mappings: dict[tuple[int, int], int]) -> None:
        """
        Update the CC -> Channel mappings.
        Args:
            mappings: (protocol channel, CC) -> NativMix channel index.
        """
        with self._map_lock:
            self._cc_map = dict(mappings)
        logger.debug("MIDI CC mappings updated: %s", mappings)

    def update_mute_mappings(self, mappings: dict[tuple[int, int], int]) -> None:
        """
        Update the mute-CC -> Channel mappings.
        Args:
            mappings: (protocol channel, CC) -> NativMix channel index.
        """
        with self._map_lock:
            self._mute_cc_map = dict(mappings)
        logger.debug("MIDI Mute CC mappings updated: %s", mappings)

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
        with self._map_lock:
            items = list(self._cc_map.items())
            last_values = dict(self._last_values)
        for key, ch_idx in items:
            if key in last_values:
                val = last_values[key]
                results.append((ch_idx, val / 127.0))
        return results

    def refresh_ports(self) -> None:
        """Trigger a re-scan of MIDI ports (Hot-Plug support)."""
        logger.info("MIDI Refresh requested (Hot-Plug).")
        self._refresh_requested = True
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
            except Exception as exc:
                logger.debug("MidiThread: virtual port cleanup failed: %s", exc)
            self._virtual_client = None

    def _set_connection_state(self, connected: bool) -> None:
        """Emit connection changes only when the state actually changes."""
        if self._connection_state == connected:
            return
        self._connection_state = connected
        self.connection_changed.emit(connected)

    def _next_connection_generation(self) -> int:
        """Start a new connection attempt and return its monotonic generation."""
        with self._generation_lock:
            self._connection_generation += 1
            return self._connection_generation

    def _publish_device_state(
        self,
        generation: int,
        status_type: str,
        message: str,
        *,
        configured_name: str | None = None,
        connected_name: str = "",
    ) -> None:
        """Publish one authoritative, generation-tagged worker snapshot."""
        stable_configured_name = normalize_midi_device_name(
            self._device_name if configured_name is None else configured_name
        )
        self.status_changed.emit(status_type, message)
        self.device_state_changed.emit(
            generation,
            status_type,
            message,
            stable_configured_name,
            list(self._available_ports),
            connected_name,
        )

    def _refresh_port_inventory(self) -> None:
        """Enumerate ports in the worker regardless of the active input mode."""
        generation = self._next_connection_generation()
        try:
            self._available_ports = list(mido.get_input_names())
        except _MIDI_RECOVERABLE_ERRORS as exc:
            logger.warning("MidiThread: MIDI port refresh failed: %s", exc)
            self._available_ports = []
            self._publish_device_state(
                generation,
                "error_temporary",
                "MIDI port refresh failed",
            )
            return

        virtual_connected = (
            self._virtual_client is not None
            and self._device_name in ("", "VIRTUAL_PORT")
            and self._input_mode != "usb"
        )
        self._publish_device_state(
            generation,
            "stable" if virtual_connected else "connecting",
            "Virtual MIDI Online" if virtual_connected else "MIDI ports refreshed",
            connected_name="VIRTUAL_PORT" if virtual_connected else "",
        )

    def _close_virtual_client(self) -> None:
        """Close the persistent RtMidi virtual client from the MIDI worker."""
        client = self._virtual_client
        self._virtual_client = None
        if client is None:
            return
        try:
            client.close_port()
        except _MIDI_RECOVERABLE_ERRORS as exc:
            logger.debug("MidiThread: virtual port cleanup failed: %s", exc)

    def restart_midi(self) -> None:
        """Manual reset to clear critical errors and restart the backend."""
        logger.info("MIDI Restart requested by user/system.")
        self._critical_error = False
        self._error_count = 0
        generation = self._next_connection_generation()
        self._refresh_requested = True
        self._panic_flag = True
        self._publish_device_state(generation, "connecting", "Restarting MIDI...")

    def run(self) -> None:
        """Main loop with Circuit Breaker protection."""
        self._running = True
        self._panic_flag = False
        self._critical_error = False
        self._error_count = 0
        self._connection_state = None

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
                    self._publish_device_state(
                        self._connection_generation,
                        "error_critical",
                        f"MIDI Error: {str(exc)}",
                    )
                    logger.error(
                        "MIDI Circuit Breaker: Backend disabled after %d consecutive failures.",
                        self._error_count,
                    )
                else:
                    self._publish_device_state(
                        self._connection_generation,
                        "error_temporary",
                        "MIDI Backend crashed - Recovering...",
                    )

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
            if IS_FLATPAK:
                logger.error("CRITICAL: python-rtmidi is required for MIDI in Flatpak; PortMidi fallback is disabled.")
                status_message = "RtMidi is required for Flatpak MIDI."
            else:
                logger.error("CRITICAL: No MIDI backend (rtmidi or portmidi) found! MIDI will not work.")
                status_message = "No MIDI backend found."
            self._set_connection_state(False)
            self._publish_device_state(self._connection_generation, "error_critical", status_message)
            # Stay in loop but idle
            while self._running and not self._panic_flag:
                self._sleep_checked(1.0)
            return

        self._error_count = 0 # Reset on successful backend load
        if backend_found == "portmidi":
            self._publish_device_state(
                self._connection_generation,
                "warning",
                "PortMidi fallback: do not hot-unplug",
            )
        else:
            self._publish_device_state(self._connection_generation, "connecting", "MIDI Ready")

        _vport_warning_logged = False
        while self._running:
            if self._panic_flag:
                self._panic_flag = False
                logger.debug("MidiThread: Internally restarting due to flag.")
            if self._refresh_requested:
                self._refresh_requested = False
                self._refresh_port_inventory()

            # Is MIDI even enabled?
            if self._input_mode == "usb":
                # USB-only: idle without closing the virtual port so ALSA
                # clients see one stable "NativMix:Input" across mode switches.
                if self._virtual_client is None:
                    self._set_connection_state(False)
                # Wait for setting changes
                while self._running and not self._panic_flag and self._input_mode == "usb":
                    time.sleep(0.5)
                continue

            try:
                if self._critical_error:
                    self._sleep_checked(2.0)
                    continue

                target_device = self._device_name if self._device_name else "VIRTUAL_PORT"
                generation = self._next_connection_generation()

                if target_device == "VIRTUAL_PORT":
                    if sys.platform == "win32":
                        # WinMM does not support virtual MIDI ports.
                        if not _vport_warning_logged:
                            logger.warning("MidiThread: Virtual Port is not supported on Windows (WinMM).")
                            _vport_warning_logged = True
                        self._set_connection_state(False)
                        self._publish_device_state(
                            generation,
                            "disabled",
                            "Virtual Port: not supported on Windows",
                        )
                        self._sleep_checked(5.0)
                        continue

                    if backend_found != "rtmidi":
                        if not _vport_warning_logged:
                            logger.info(
                                "MidiThread: Virtual Port requires rtmidi, but %s is loaded"
                                " — compatibility fallback cannot create virtual ports. Skipping.",
                                backend_found,
                            )
                            _vport_warning_logged = True
                        self._set_connection_state(False)
                        self._publish_device_state(generation, "disabled", "Virtual Port needs rtmidi")
                        self._sleep_checked(5.0)
                        continue

                    # Reuse the existing virtual port if already open so ALSA
                    # clients see one stable port across USB ↔ hybrid switches.
                    if self._virtual_client is None:
                        logger.debug("MidiThread: Opening Virtual Port 'NativMix:Input'...")
                        self._publish_device_state(generation, "connecting", "Opening Virtual Port...")
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
                            self._set_connection_state(False)
                            self._publish_device_state(
                                generation,
                                "error_temporary",
                                "Virtual Port failed - retrying...",
                            )
                            self._sleep_checked(5.0)
                            continue
                    else:
                        logger.debug("MidiThread: Reusing existing Virtual Port 'NativMix:Input'.")

                    self._prepare_feedback_connection()
                    self._set_connection_state(True)
                    self._publish_device_state(
                        generation,
                        "stable",
                        "Virtual MIDI Online",
                        connected_name="VIRTUAL_PORT",
                    )

                    while self._running and not self._panic_flag:
                        # Only exit if switching to a physical device; a mode
                        # change to USB keeps the port alive (handled above).
                        if self._device_name not in ("", "VIRTUAL_PORT"):
                            self._close_virtual_client()
                            logger.debug("MidiThread: Virtual Port closed (device change).")
                            break

                        # In USB mode just idle – don't process MIDI events.
                        if self._input_mode == "usb":
                            time.sleep(0.01)
                            continue

                        self._process_pending_sync(None)
                        self._process_pending_mute_feedback(None)

                        msg_data = self._virtual_client.get_message()
                        if msg_data:
                            msg, _ = msg_data
                            if len(msg) >= 3 and (msg[0] & 0xF0) == 0xB0:
                                self._handle_cc(msg[0] & 0x0F, msg[1], msg[2])

                        time.sleep(0.01)

                else:
                    # Physical Device Mode
                    logger.info("MidiThread: Connecting to physical device: %s", target_device)
                    names = mido.get_input_names()
                    self._available_ports = list(names)
                    logger.info("MidiThread: Available MIDI ports: %s", names)
                    target_name = _match_midi_port(names, target_device)

                    if not target_name:
                        logger.warning(
                            "MidiThread: Device '%s' not found. Available: %s",
                            target_device, names
                        )
                        self._set_connection_state(False)
                        self._publish_device_state(
                            generation,
                            "error_temporary",
                            f"Device '{target_device}' not found",
                            configured_name=target_device,
                        )
                        self._sleep_checked(5.0)
                        continue

                    self._publish_device_state(
                        generation,
                        "connecting",
                        f"Connecting: {normalize_midi_device_name(target_device)}",
                        configured_name=target_device,
                    )

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
                            self._prepare_feedback_connection()
                            self._set_connection_state(True)
                            self._publish_device_state(
                                generation,
                                "stable",
                                f"♫: {normalize_midi_device_name(target_device)}",
                                configured_name=target_device,
                                connected_name=target_name,
                            )
                            self._device_loop(inport, outport, target_device)
                    else:
                        with mido.open_input(target_name) as inport:
                            logger.info("MidiThread: Connected to %s", target_name)
                            self._prepare_feedback_connection()
                            self._set_connection_state(True)
                            self._publish_device_state(
                                generation,
                                "stable",
                                f"♫: {normalize_midi_device_name(target_device)}",
                                configured_name=target_device,
                                connected_name=target_name,
                            )
                            self._device_loop(inport, None, target_device)

            except _MIDI_RECOVERABLE_ERRORS as exc:
                logger.warning("MIDI Recoverable Error: %s", exc)
                if self._virtual_client is not None and self._device_name in ("", "VIRTUAL_PORT"):
                    self._close_virtual_client()
                self._set_connection_state(False)
                self._publish_device_state(
                    generation,
                    "error_temporary",
                    "MIDI Disconnected - Retrying...",
                    configured_name=target_device,
                )
                self._sleep_checked(5.0)

        logger.debug("MidiThread stopped")

    def _device_loop(self, inport, outport, target_device: str) -> None:
        """Poll a physical MIDI input (and optional output) until reconnect is needed."""
        while self._running and not self._panic_flag:
            if self._input_mode == "usb" or self._device_name != target_device:
                break
            self._process_pending_sync(outport)
            self._process_pending_mute_feedback(outport)
            msg = inport.receive(block=False)
            if msg is None:
                time.sleep(0.05)
                continue
            if msg.type == "control_change":
                self._handle_cc(int(msg.channel), msg.control, msg.value)

    def _process_pending_sync(self, outport) -> None:
        """Send queued outbound fader CC values when feedback is enabled."""
        if not self._fader_feedback_enabled:
            return
        with self._feedback_lock:
            pending = self._pending_sync
            self._pending_sync = None
        if not pending or outport is None:
            return

        with self._map_lock:
            items = list(self._cc_map.items())
        ch_to_bindings: dict[int, list[tuple[int, int]]] = {}
        for key, ch_idx in items:
            ch_to_bindings.setdefault(ch_idx, []).append(key)
        try:
            for ch_idx, volume in pending:
                for midi_channel, cc in ch_to_bindings.get(ch_idx, []):
                    self._send_fader_cc(outport, midi_channel, cc, ch_idx, volume)
        except _MIDI_RECOVERABLE_ERRORS:
            with self._feedback_lock:
                retry = dict(pending)
                retry.update(self._pending_sync or [])
                self._pending_sync = list(retry.items())
            raise

    def _process_pending_mute_feedback(self, outport) -> None:
        """Send queued mute state and the example controller's LED hue."""
        if not self._fader_feedback_enabled:
            return
        with self._feedback_lock:
            pending = self._pending_mute_feedback
            self._pending_mute_feedback = None
        if not pending or outport is None:
            return

        with self._map_lock:
            channel_bindings = {ch_idx: key for key, ch_idx in self._mute_cc_map.items()}
        now = time.monotonic()
        try:
            for ch_idx, muted in pending:
                binding = channel_bindings.get(ch_idx)
                if binding is None:
                    continue
                midi_channel, cc = binding
                self._send_raw_cc(outport, midi_channel, cc, 127 if muted else 0)
                with self._feedback_lock:
                    self._mute_outbound_suppress_until[binding] = now + _MUTE_OUTBOUND_SUPPRESS_S
                led_cc = _example_led_cc_for_mute(cc)
                if led_cc is not None:
                    self._send_raw_cc(
                        outport,
                        midi_channel,
                        led_cc,
                        _LED_HUE_MUTED if muted else _LED_HUE_UNMUTED,
                    )
        except _MIDI_RECOVERABLE_ERRORS:
            with self._feedback_lock:
                retry = dict(pending)
                retry.update(self._pending_mute_feedback or [])
                self._pending_mute_feedback = list(retry.items())
            raise

    def _send_raw_cc(self, outport, midi_channel: int, cc: int, value: int) -> None:
        """Send one outbound CC without fader takeover."""
        cc_value = max(0, min(127, int(value)))
        key = (midi_channel, cc)
        with self._feedback_lock:
            if self._last_sent_cc_value.get(key) == cc_value:
                return
        outport.send(
            mido.Message(
                "control_change",
                channel=max(0, min(15, midi_channel)),
                control=cc,
                value=cc_value,
            )
        )
        with self._feedback_lock:
            self._last_sent_cc_value[key] = cc_value
        with self._map_lock:
            self._last_values[key] = cc_value

    def _send_fader_cc(
        self,
        outport,
        midi_channel: int,
        cc: int,
        ch_idx: int,
        volume: float,
    ) -> None:
        """Send one outbound volume CC and arm takeover suppression for that channel."""
        cc_value = max(0, min(127, int(round(max(0.0, min(1.0, volume)) * 127))))
        key = (midi_channel, cc)
        with self._feedback_lock:
            if self._last_sent_cc_value.get(key) == cc_value:
                return
        outport.send(
            mido.Message(
                "control_change",
                channel=max(0, min(15, midi_channel)),
                control=cc,
                value=cc_value,
            )
        )
        with self._feedback_lock:
            self._last_sent_cc_value[key] = cc_value
            self._feedback_takeover[key] = cc_value / 127.0
        with self._map_lock:
            self._last_values[key] = cc_value
        logger.debug(
            "MIDI fader feedback: nmix_ch=%d midi_ch=%d cc=%d value=%d",
            ch_idx,
            midi_channel,
            cc,
            cc_value,
        )

    def _handle_cc(self, midi_channel: int, cc: int, val: int) -> None:
        """Process a Control Change on a protocol MIDI channel."""
        midi_channel = max(0, min(15, int(midi_channel)))
        key = (midi_channel, cc)
        with self._map_lock:
            self._last_values[key] = val

        # 1. Always emit for Learn handshake
        self.midi_cc_received.emit(midi_channel, cc, val)

        # 2. Check if mapped to a fader — throttled to 50 Hz per binding.
        # to prevent Qt signal queue flooding from misbehaving MIDI controllers.
        with self._map_lock:
            ch_idx = self._cc_map.get(key)
        if ch_idx is not None:
            with self._feedback_lock:
                takeover_vol = self._feedback_takeover.get(key)
            if _inbound_fader_suppressed(takeover_vol, val):
                return
            if takeover_vol is not None:
                with self._feedback_lock:
                    self._feedback_takeover.pop(key, None)
            now = time.monotonic()
            with self._map_lock:
                last_emit = self._last_vol_emit.get(key, 0.0)
            if now - last_emit >= 0.02:
                with self._map_lock:
                    self._last_vol_emit[key] = now
                vol = val / 127.0
                self.midi_volumes_changed.emit([(ch_idx, vol)])

        # 3. Mute toggle on button-on, suppressing echoes of outbound state.
        if val == 127:
            with self._map_lock:
                mute_channel = self._mute_cc_map.get(key)
            if mute_channel is not None:
                with self._feedback_lock:
                    suppress_until = self._mute_outbound_suppress_until.get(key, 0.0)
                if time.monotonic() >= suppress_until:
                    self.midi_mute_toggled.emit(mute_channel)

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
