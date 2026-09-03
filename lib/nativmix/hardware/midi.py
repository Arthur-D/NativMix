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
import re
import sys
import threading
import time
import types
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path

import mido
from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot

from nativmix.hardware.remote_midi import (
    REMOTE_SYNC_TCP_PORT,
    RemoteMidiRole,
    RemoteMidiTransport,
    RtpCCPacket,
    SessionState,
    SyncControlEnvelope,
    SyncSessionSnapshot,
    TransportSnapshot,
)
from nativmix.remote_sync.protocol import Message as SyncMessage
from nativmix.remote_sync.transport import CloseReason as SyncCloseReason
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
_MIDI_PORT_CHECK_INTERVAL_S = 0.5
_REMOTE_CONTROLLER_INFO_INTERVAL_S = 10.0
PendingFaderFeedback = float | tuple[float, frozenset[tuple[int, int]]]
_ALSA_SEQ_CLIENTS_PATH = "/proc/asound/seq/clients"
_ALSA_ENDPOINT_RE = re.compile(r"\s(\d+:\d+)\s*$")


def _example_led_cc_for_mute(mute_cc: int) -> int | None:
    """Map example mute CC 5-8 to LED hue CC 32-35."""
    if _EXAMPLE_MUTE_CC_MIN <= mute_cc <= _EXAMPLE_MUTE_CC_MAX:
        return _EXAMPLE_LED_CC_BASE + mute_cc - _EXAMPLE_MUTE_CC_MIN
    return None
_MIDO_PORTMIDI_DEFAULT_CANDIDATE = "libportmidi.so"
_MIDI_RECOVERABLE_ERRORS = (OSError, EOFError, RuntimeError, TypeError, ValueError)


class MidiEndpointDisconnected(OSError):
    """An opened physical MIDI endpoint disappeared or was replaced."""


@dataclass(frozen=True)
class RemoteControllerOrigin:
    """Provenance for one physical CC within an AppleMIDI/TCP session."""

    generation: int
    transport_session_id: str
    peer_id: str
    rtp_sequence: int
    local_sequence: int
    midi_channel: int
    control: int
    channel_index: int
    requested_volume: float

    def to_wire(self) -> dict[str, object]:
        return {
            "kind": "remote_controller",
            "generation": self.generation,
            "transport_session_id": self.transport_session_id,
            "peer_id": self.peer_id,
            "rtp_sequence": self.rtp_sequence,
            "midi_channel": self.midi_channel,
            "control": self.control,
            "requested_volume": self.requested_volume,
        }


@dataclass(frozen=True)
class FaderFeedbackRequest:
    mappings: tuple[tuple[int, float], ...]
    suppressed_bindings: frozenset[tuple[int, int]] = frozenset()
    reason: str = "canonical"


class _RemoteMidiOutput:
    """Mido-like output adapter backed by a connected remote transport."""

    def __init__(self, transport: RemoteMidiTransport) -> None:
        self._transport = transport

    def send(self, message) -> None:
        if message.type != "control_change":
            raise ValueError(f"Unsupported remote MIDI feedback type: {message.type}")
        self._transport.send_cc(int(message.channel), int(message.control), int(message.value))


def _alsa_endpoint_address(port_name: str) -> str | None:
    """Extract the volatile ALSA client:port address from an RtMidi name."""
    match = _ALSA_ENDPOINT_RE.search(port_name)
    return match.group(1) if match else None


def _alsa_client_block(snapshot: str, client_name: str) -> str | None:
    """Return one named ALSA client block from /proc/asound/seq/clients."""
    pattern = re.compile(
        rf'^Client\s+\d+\s+:\s+"{re.escape(client_name)}".*?(?=^Client\s|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(snapshot)
    return match.group(0) if match else None


def _alsa_subscription_present(
    snapshot: str,
    client_name: str,
    relation: str,
    endpoint: str,
) -> bool:
    """Return whether a named local client has the expected ALSA subscription."""
    block = _alsa_client_block(snapshot, client_name)
    if block is None:
        return False
    return any(
        re.search(rf"(?<!\d){re.escape(endpoint)}(?![\d:])", line) is not None
        for line in block.splitlines()
        if line.strip().startswith(f"{relation}:")
    )


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
    fader_sync_requested = pyqtSignal(object)
    mute_feedback_requested = pyqtSignal(list)  # list[tuple[int, bool]] (channel, muted)
    remote_state_changed = pyqtSignal(int, str, str, str, list, str, str)
    remote_sync_status_changed = pyqtSignal(int, str, str)
    remote_sync_message_received = pyqtSignal(object)
    remote_sync_session_changed = pyqtSignal(object)
    remote_sync_send_requested = pyqtSignal(object, int, str)
    remote_controller_origin_sent = pyqtSignal(object)
    remote_controller_origin_received = pyqtSignal(object)
    remote_volume_batch_ready = pyqtSignal()
    remote_cc_batch_ready = pyqtSignal()

    def __init__(
        self,
        device_name: str = "",
        input_mode: str = "hybrid",
        remote_role: str = "off",
        remote_instance_id: str = "",
        remote_name: str = "",
        remote_peer_id: str = "",
        remote_peer_name: str = "",
        parent=None,
    ) -> None:
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
        self._active_generation: int | None = None
        self._active_input_name: str | None = None
        self._active_output_name: str | None = None
        self._active_input_client_name: str | None = None
        self._active_output_client_name: str | None = None
        self._active_subscription_confirmed = False
        self._first_cc_logged_generation: int | None = None
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
        self._pending_sync: list[tuple[int, PendingFaderFeedback]] | None = None
        self._pending_mute_feedback: list[tuple[int, bool]] | None = None
        self._mute_outbound_suppress_until: dict[tuple[int, int], float] = {}
        self._remote_lock = threading.RLock()
        self._remote_role = remote_role if remote_role in ("off", "send", "receive") else "off"
        self._remote_instance_id = remote_instance_id
        self._remote_name = remote_name
        self._remote_peer_id = remote_peer_id
        self._remote_peer_name = remote_peer_name
        self._remote_transport: RemoteMidiTransport | None = None
        self._remote_transport_key: tuple[str, str, str, str, str, str] | None = None
        self._remote_refresh_requested = False
        self._remote_state_generation = 0
        self._remote_session_connected = False
        self._remote_snapshot_signature: tuple[object, ...] | None = None
        self._remote_blocked_signature: tuple[str, str, str] | None = None
        self._remote_feedback_cache: dict[tuple[int, int], int] = {}
        self._remote_sync_outbound: deque[tuple[SyncMessage, int, str]] = deque()
        self._remote_sync_outbound_capacity = 256
        self._remote_sync_public_generation = 0
        self._remote_sync_local_generation = -1
        self._remote_sync_public_session_id: str | None = None
        self._last_remote_controller_info_at: float | None = None
        self._remote_cc_observation_deferred = False
        self._remote_origin_sequence = 0
        self._remote_volume_lock = threading.Lock()
        self._remote_pending_volumes: dict[int, tuple[float, RemoteControllerOrigin | None]] = {}
        self._remote_volume_notification_pending = False
        self._remote_volume_coalesced = 0
        self._last_remote_volume_diagnostic_at = time.monotonic()
        self._remote_pending_cc: dict[tuple[int, int], int] = {}
        self._remote_cc_notification_pending = False
        self.fader_sync_requested.connect(self._queue_fader_sync)
        self.mute_feedback_requested.connect(self._queue_mute_feedback)
        self.remote_sync_send_requested.connect(self._queue_remote_sync_message)

    def _feedback_output_enabled(self) -> bool:
        # A receiver must relay canonical state to its remote controller. A sender
        # writes its local motor/LED endpoint only when the machine-local preference
        # is enabled.
        return self._remote_role == "receive" or self._fader_feedback_enabled

    def _queue_remote_volume(
        self,
        channel_index: int,
        volume: float,
        origin: RemoteControllerOrigin | None,
    ) -> None:
        notify = False
        with self._remote_volume_lock:
            if channel_index in self._remote_pending_volumes:
                self._remote_volume_coalesced += 1
            self._remote_pending_volumes[channel_index] = (volume, origin)
            if not self._remote_volume_notification_pending:
                self._remote_volume_notification_pending = True
                notify = True
            now = time.monotonic()
            if self._remote_volume_coalesced and now - self._last_remote_volume_diagnostic_at >= 5.0:
                logger.debug(
                    "Remote MIDI latest-value queue coalesced=%d depth=%d",
                    self._remote_volume_coalesced,
                    len(self._remote_pending_volumes),
                )
                self._last_remote_volume_diagnostic_at = now
        if notify:
            self.remote_volume_batch_ready.emit()

    def take_remote_volume_batch(self) -> list[tuple[int, float, RemoteControllerOrigin | None]]:
        """Atomically drain the latest remote value per mapped channel."""
        with self._remote_volume_lock:
            batch = [
                (channel, volume, origin)
                for channel, (volume, origin) in self._remote_pending_volumes.items()
            ]
            self._remote_pending_volumes.clear()
            self._remote_volume_notification_pending = False
        return batch

    def clear_remote_volume_batch(self) -> None:
        with self._remote_volume_lock:
            self._remote_pending_volumes.clear()
            self._remote_volume_notification_pending = False
            self._remote_pending_cc.clear()
            self._remote_cc_notification_pending = False

    def _queue_remote_cc_observation(self, midi_channel: int, cc: int, value: int) -> None:
        notify = False
        with self._remote_volume_lock:
            self._remote_pending_cc[(midi_channel, cc)] = value
            if not self._remote_cc_notification_pending:
                self._remote_cc_notification_pending = True
                notify = True
        if notify:
            self.remote_cc_batch_ready.emit()

    def take_remote_cc_batch(self) -> list[tuple[int, int, int]]:
        with self._remote_volume_lock:
            batch = [
                (midi_channel, cc, value)
                for (midi_channel, cc), value in self._remote_pending_cc.items()
            ]
            self._remote_pending_cc.clear()
            self._remote_cc_notification_pending = False
        return batch

    def set_fader_feedback_enabled(self, enabled: bool) -> None:
        """Enable or disable outbound MIDI CC fader position sync."""
        was_output_enabled = self._feedback_output_enabled()
        if self._fader_feedback_enabled != enabled:
            logger.debug("MIDI fader feedback %s", "enabled" if enabled else "disabled")
        self._fader_feedback_enabled = enabled
        if was_output_enabled != self._feedback_output_enabled() and self.isRunning():
            self._panic_flag = True
        if not enabled and not self._feedback_output_enabled():
            with self._feedback_lock:
                self._feedback_takeover.clear()
                self._last_sent_cc_value.clear()
                self._pending_sync = None
                self._pending_mute_feedback = None
                self._mute_outbound_suppress_until.clear()

    @pyqtSlot(object)
    def _queue_fader_sync(self, request: FaderFeedbackRequest | list[tuple[int, float]]) -> None:
        """Queue outbound fader positions (thread-safe via queued signal)."""
        if isinstance(request, FaderFeedbackRequest):
            mappings = request.mappings
            suppressed = request.suppressed_bindings
        else:
            mappings = tuple(request)
            suppressed = frozenset()
        if not self._feedback_output_enabled() or not mappings:
            return
        with self._feedback_lock:
            pending: dict[int, PendingFaderFeedback] = dict(self._pending_sync or [])
            for channel, volume in mappings:
                pending[channel] = (volume, suppressed) if suppressed else volume
            self._pending_sync = list(pending.items())

    def request_fader_sync(
        self,
        mappings: list[tuple[int, float]],
        *,
        suppressed_bindings: frozenset[tuple[int, int]] = frozenset(),
        reason: str = "canonical",
    ) -> None:
        """Request outbound CC sync; safe to call from the GUI/main thread."""
        self.fader_sync_requested.emit(FaderFeedbackRequest(tuple(mappings), suppressed_bindings, reason))

    @pyqtSlot(list)
    def _queue_mute_feedback(self, states: list[tuple[int, bool]]) -> None:
        """Queue outbound mute and LED states."""
        if not self._feedback_output_enabled() or not states:
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
            logger.info("MIDI Mode changed: %s -> %s", self._input_mode, mode)
            self._input_mode = mode
            self._panic_flag = True

    def set_remote_config(
        self,
        role: str,
        instance_id: str,
        advertised_name: str,
        peer_id: str,
        peer_name: str,
    ) -> None:
        """Apply global remote-controller settings on the next worker loop."""
        normalized_role = role if role in ("off", "send", "receive") else "off"
        values = (normalized_role, instance_id, advertised_name, peer_id, peer_name)
        with self._remote_lock:
            current = (
                self._remote_role,
                self._remote_instance_id,
                self._remote_name,
                self._remote_peer_id,
                self._remote_peer_name,
            )
            if values == current:
                return
            (
                self._remote_role,
                self._remote_instance_id,
                self._remote_name,
                self._remote_peer_id,
                self._remote_peer_name,
            ) = values
            self._panic_flag = True
        logger.info(
            "Remote MIDI role/config transition: %s -> %s (mode=%s device=%r peer=%r)",
            current[0],
            normalized_role,
            self._input_mode,
            self._device_name,
            peer_name or peer_id or "",
        )

    def refresh_remote_peers(self) -> None:
        """Request a DNS-SD refresh from the MIDI worker."""
        with self._remote_lock:
            self._remote_refresh_requested = True

    def request_remote_sync_send(
        self,
        message: SyncMessage,
        generation: int,
        transport_session_id: str,
    ) -> None:
        """Queue a control message for the MIDI worker's active TCP transport."""
        self.remote_sync_send_requested.emit(message, generation, transport_session_id)

    @pyqtSlot(object, int, str)
    def _queue_remote_sync_message(
        self,
        message: SyncMessage,
        generation: int,
        transport_session_id: str,
    ) -> None:
        with self._remote_lock:
            if len(self._remote_sync_outbound) >= self._remote_sync_outbound_capacity:
                logger.warning("Remote sync outbound queue full; dropping newest message")
                return
            self._remote_sync_outbound.append((message, generation, transport_session_id))

    def _on_remote_sync_message(self, envelope: SyncControlEnvelope) -> None:
        """Marshal a worker-owned validated envelope to the Qt main thread."""
        with self._remote_lock:
            if (
                envelope.generation != self._remote_sync_local_generation
                or envelope.transport_session_id != self._remote_sync_public_session_id
            ):
                logger.debug("Discarding message from replaced remote mixer control transport")
                return
            generation = self._remote_sync_public_generation
        self.remote_sync_message_received.emit(replace(envelope, generation=generation))

    def _on_remote_sync_session(self, snapshot: SyncSessionSnapshot) -> None:
        """Marshal immutable control lifecycle state to the Qt main thread."""
        with self._remote_lock:
            self._remote_sync_public_generation += 1
            generation = self._remote_sync_public_generation
            self._remote_sync_local_generation = snapshot.generation
            self._remote_sync_public_session_id = (
                snapshot.transport_session_id if snapshot.available else None
            )
        logger.info(
            "Remote mixer control lifecycle: generation=%d role=%s available=%s session=%s",
            generation,
            snapshot.role.value,
            snapshot.available,
            snapshot.transport_session_id or "none",
        )
        self.remote_sync_session_changed.emit(replace(snapshot, generation=generation))

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

    def _remote_config_values(self) -> tuple[str, str, str, str, str]:
        with self._remote_lock:
            return (
                self._remote_role,
                self._remote_instance_id,
                self._remote_name,
                self._remote_peer_id,
                self._remote_peer_name,
            )

    def _remote_transport_identity(self) -> tuple[str, str, str, str, str, str]:
        role, instance_id, advertised_name, peer_id, peer_name = self._remote_config_values()
        controller_name = (
            normalize_midi_device_name(self._active_input_name or self._device_name)
            if role == "send"
            else ""
        )
        return role, instance_id, advertised_name, peer_id, peer_name, controller_name

    def _next_remote_state_generation(self) -> int:
        with self._generation_lock:
            self._remote_state_generation += 1
            return self._remote_state_generation

    def _remote_status(self, snapshot: TransportSnapshot) -> tuple[str, str]:
        peer_name = snapshot.connected_peer_name or self._remote_peer_name or "remote computer"
        if snapshot.state is SessionState.CONNECTED:
            return "stable", f"Remote controller connected: {peer_name}"
        if snapshot.state is SessionState.UNAVAILABLE:
            return "error_critical", snapshot.error or "Remote controller unavailable"
        if snapshot.state is SessionState.BACKOFF:
            return "error_temporary", f"{snapshot.error or 'Remote controller disconnected'} - retrying"
        if snapshot.role is RemoteMidiRole.SEND:
            return "connecting", "Waiting for a desktop to connect..."
        if not snapshot.selected_peer_id:
            return "warning", "Choose a discovered laptop, then press Connect"
        if not snapshot.peers:
            return "connecting", f"Waiting for {self._remote_peer_name or 'selected laptop'}..."
        return "connecting", f"Connecting to {self._remote_peer_name or 'selected laptop'}..."

    def _on_remote_snapshot(self, snapshot: TransportSnapshot) -> None:
        """Publish remote transport state from the MIDI worker thread."""
        connected = snapshot.state is SessionState.CONNECTED
        if connected and not self._remote_session_connected:
            self._prepare_feedback_connection()
        self._remote_session_connected = connected
        if snapshot.role is RemoteMidiRole.RECEIVE:
            self._set_connection_state(connected)

        signature = (
            snapshot.role,
            snapshot.state,
            snapshot.available,
            snapshot.error,
            snapshot.warning,
            snapshot.peers,
            snapshot.selected_peer_id,
            snapshot.connected_peer_id,
            snapshot.connected_peer_name,
            snapshot.overflow_count,
            snapshot.reconnect_attempt,
            snapshot.sync_available,
            snapshot.sync_error,
            snapshot.sync_terminal,
            snapshot.sync_close_reason,
        )
        if signature == self._remote_snapshot_signature:
            return
        self._remote_snapshot_signature = signature
        status_type, message = self._remote_status(snapshot)
        if snapshot.warning:
            status_type = "warning"
            message = snapshot.warning
        peers = [
            {
                "id": peer.peer_id,
                "name": peer.name,
                "host": peer.host,
                "controller_name": peer.controller_name,
            }
            for peer in snapshot.peers
        ]
        connected_marker = snapshot.connected_peer_id or (
            snapshot.connected_peer_name if snapshot.state is SessionState.CONNECTED else ""
        )
        generation = self._next_remote_state_generation()
        self.remote_state_changed.emit(
            generation,
            snapshot.role.value,
            status_type,
            message,
            peers,
            snapshot.selected_peer_id or "",
            connected_marker or "",
        )
        if snapshot.sync_available:
            sync_status = "Connected"
            sync_detail = "Remote mixer synchronization connected."
        elif snapshot.sync_close_reason is SyncCloseReason.PROTOCOL_INCOMPATIBLE or (
            snapshot.sync_error
            and any(
                marker in snapshot.sync_error.lower()
                for marker in (
                    "incompatible",
                    "protocol mismatch",
                    "schema mismatch",
                    "version mismatch",
                )
            )
        ):
            sync_status = "Version incompatible"
            sync_detail = snapshot.sync_error or "Remote mixer protocol or schema version is incompatible."
        elif snapshot.sync_terminal:
            sync_status = "Unavailable"
            sync_detail = snapshot.sync_error or "Remote mixer synchronization is unavailable."
        elif snapshot.state is SessionState.CONNECTED:
            sync_status = "Reconnecting"
            sync_detail = snapshot.sync_error or "Remote mixer synchronization is reconnecting."
        else:
            sync_status = "Syncing"
            sync_detail = snapshot.sync_error or "Waiting for the remote mixer control connection."
        self.remote_sync_status_changed.emit(generation, sync_status, sync_detail)

    def _close_remote_transport(self) -> None:
        transport = self._remote_transport
        self._remote_transport = None
        self._remote_transport_key = None
        self._remote_session_connected = False
        self._remote_snapshot_signature = None
        if transport is not None:
            logger.info(
                "Remote MIDI transport stopping: role=%s state=%s",
                transport.role.value,
                transport.snapshot.state.value,
            )
            transport.close()

    def _publish_remote_blocked(self, role: str, message: str) -> None:
        signature = (role, self._input_mode, self._device_name)
        if signature == self._remote_blocked_signature:
            return
        self._remote_blocked_signature = signature
        logger.info(
            "Remote MIDI %s blocked: mode=%s device=%r reason=%s",
            role,
            self._input_mode,
            self._device_name,
            message,
        )
        generation = self._next_remote_state_generation()
        self.remote_state_changed.emit(
            generation,
            role,
            "warning",
            message,
            [],
            self._remote_peer_id,
            "",
        )

    def _ensure_remote_transport(self) -> RemoteMidiTransport | None:
        role, instance_id, advertised_name, peer_id, peer_name, controller_name = (
            self._remote_transport_identity()
        )
        key = (role, instance_id, advertised_name, peer_id, peer_name, controller_name)
        if role not in ("send", "receive"):
            self._close_remote_transport()
            self._remote_blocked_signature = None
            return None
        if self._input_mode == "usb":
            self._close_remote_transport()
            self._publish_remote_blocked(
                role,
                f"Remote {role.title()} blocked: set Input Mode to USB + MIDI or MIDI Only.",
            )
            return None
        if role == "send" and self._device_name in ("", "VIRTUAL_PORT"):
            self._close_remote_transport()
            self._publish_remote_blocked(
                role,
                "Remote Send blocked: select a physical MIDI controller in MIDI Hardware.",
            )
            return None
        self._remote_blocked_signature = None
        if self._remote_transport is not None and self._remote_transport_key == key:
            return self._remote_transport

        self._close_remote_transport()
        logger.info(
            "Remote MIDI transport starting: role=%s mode=%s device=%r name=%r peer=%r",
            role,
            self._input_mode,
            self._device_name,
            advertised_name,
            peer_name or peer_id or "",
        )
        try:
            transport = RemoteMidiTransport(
                role,
                instance_id,
                advertised_name,
                selected_peer_id=peer_id or None,
                selected_peer_name=peer_name or None,
                controller_name=controller_name,
                sync_port=REMOTE_SYNC_TCP_PORT,
                on_snapshot=self._on_remote_snapshot,
                on_sync_message=self._on_remote_sync_message,
                on_sync_session=self._on_remote_sync_session,
            )
        except (TypeError, ValueError) as exc:
            generation = self._next_remote_state_generation()
            self.remote_state_changed.emit(
                generation,
                role,
                "error_critical",
                f"Invalid remote controller configuration: {exc}",
                [],
                peer_id,
                "",
            )
            return None
        self._remote_transport = transport
        self._remote_transport_key = key
        snapshot = transport.start()
        if snapshot.available:
            logger.info(
                "Remote MIDI transport ready: role=%s control_port=%d data_port=%d sync_port=%s controller=%r",
                role,
                transport.control_port,
                transport.data_port,
                transport.sync_listener_port or "discovery-client",
                controller_name or "Remote controller",
            )
        else:
            logger.warning("Remote MIDI transport unavailable: role=%s error=%s", role, snapshot.error)
        return transport

    def _remote_transport_connected(self) -> bool:
        transport = self._remote_transport
        return transport is not None and transport.snapshot.state is SessionState.CONNECTED

    def _flush_remote_feedback_cache(self, outport) -> None:
        if not self._feedback_output_enabled() or outport is None or not self._remote_feedback_cache:
            return
        for (midi_channel, cc), value in list(self._remote_feedback_cache.items()):
            self._send_remote_fader_feedback(outport, midi_channel, cc, value)

    def _send_remote_fader_feedback(self, outport, midi_channel: int, cc: int, value: int) -> None:
        """Write receiver feedback while suppressing a controller's input echo."""
        key = (midi_channel, cc)
        with self._map_lock:
            channel_index = self._cc_map.get(key)
        if channel_index is None:
            self._send_raw_cc(outport, midi_channel, cc, value)
            return
        self._send_fader_cc(outport, midi_channel, cc, channel_index, value / 127.0)

    def _remote_fader_input_suppressed(self, midi_channel: int, cc: int, value: int) -> bool:
        """Consume feedback takeover state before forwarding physical input."""
        key = (midi_channel, cc)
        with self._feedback_lock:
            takeover_volume = self._feedback_takeover.get(key)
            if _inbound_fader_suppressed(takeover_volume, value):
                return True
            if takeover_volume is not None:
                self._feedback_takeover.pop(key, None)
        return False

    def _forward_remote_cc(self, midi_channel: int, cc: int, value: int) -> None:
        """Forward one physical controller event unless it echoes feedback."""
        transport = self._remote_transport
        if (
            transport is not None
            and self._remote_transport_connected()
            and not self._remote_fader_input_suppressed(midi_channel, cc, value)
        ):
            send_with_sequence = getattr(transport, "send_cc_with_sequence", None)
            if not callable(send_with_sequence):
                transport.send_cc(midi_channel, cc, value)
                return
            rtp_sequence = send_with_sequence(midi_channel, cc, value)
            if rtp_sequence is None:
                return
            with self._map_lock:
                channel_index = self._cc_map.get((midi_channel, cc))
            if channel_index is None:
                return
            with self._remote_lock:
                generation = self._remote_sync_public_generation
                session_id = self._remote_sync_public_session_id
                peer_id = self._remote_instance_id
                self._remote_origin_sequence += 1
                local_sequence = self._remote_origin_sequence
            if session_id:
                self.remote_controller_origin_sent.emit(
                    RemoteControllerOrigin(
                        generation,
                        session_id,
                        peer_id,
                        int(rtp_sequence),
                        local_sequence,
                        midi_channel,
                        cc,
                        channel_index,
                        value / 127.0,
                    )
                )

    def _poll_remote_transport(self, outport=None) -> bool:
        transport = self._ensure_remote_transport()
        if transport is None:
            return False
        with self._remote_lock:
            refresh_requested = self._remote_refresh_requested
            self._remote_refresh_requested = False
        if refresh_requested:
            transport.refresh_discovery()

        received_remote_cc = False

        def handle_remote_packet(packet: RtpCCPacket) -> None:
            nonlocal received_remote_cc
            received_remote_cc = True
            midi_channel, cc, value = packet.channel, packet.control, packet.value
            if self._remote_role == "receive":
                now = time.monotonic()
                if (
                    self._last_remote_controller_info_at is None
                    or now - self._last_remote_controller_info_at >= _REMOTE_CONTROLLER_INFO_INTERVAL_S
                ):
                    logger.info(
                        "Remote controller path active: AppleMIDI CC -> receiver audio; TCP mixer sync is observational"
                    )
                    self._last_remote_controller_info_at = now
                if self._remote_fader_input_suppressed(midi_channel, cc, value):
                    logger.debug(
                        "Remote controller input suppressed as feedback echo: midi_ch=%d cc=%d sequence=%d",
                        midi_channel,
                        cc,
                        packet.sequence,
                    )
                    return
                with self._map_lock:
                    channel_index = self._cc_map.get((midi_channel, cc))
                with self._remote_lock:
                    generation = self._remote_sync_public_generation
                    session_id = self._remote_sync_public_session_id
                    peer_id = transport.snapshot.connected_peer_id or ""
                origin = None
                if channel_index is not None and session_id:
                    origin = RemoteControllerOrigin(
                        generation,
                        session_id,
                        peer_id,
                        packet.sequence,
                        -1,
                        midi_channel,
                        cc,
                        channel_index,
                        value / 127.0,
                    )
                if channel_index is not None:
                    self._queue_remote_volume(channel_index, value / 127.0, origin)
                self._queue_remote_cc_observation(midi_channel, cc, value)
                self._handle_cc(
                    midi_channel,
                    cc,
                    value,
                    throttle_volume=False,
                    check_feedback_takeover=False,
                    emit_volume=False,
                    emit_learn=False,
                )
            elif self._remote_role == "send":
                self._remote_feedback_cache[(midi_channel, cc)] = value
                if outport is not None and self._feedback_output_enabled():
                    self._send_remote_fader_feedback(outport, midi_channel, cc, value)

        if callable(getattr(transport, "send_cc_with_sequence", None)):
            transport.poll(cc_packet_handler=handle_remote_packet)
        else:
            transport.poll(
                lambda midi_channel, cc, value: handle_remote_packet(
                    RtpCCPacket(0, 0, 0, midi_channel, cc, value)
                )
            )
        if received_remote_cc and self._remote_role == "receive":
            if not self._remote_cc_observation_deferred:
                self._remote_cc_observation_deferred = True
                return True
            self._remote_cc_observation_deferred = False
        elif not received_remote_cc:
            self._remote_cc_observation_deferred = False
        with self._remote_lock:
            pending_sync = list(self._remote_sync_outbound)
            self._remote_sync_outbound.clear()
            public_generation = self._remote_sync_public_generation
            local_generation = self._remote_sync_local_generation
            public_session_id = self._remote_sync_public_session_id
        for message, generation, transport_session_id in pending_sync:
            if generation != public_generation or transport_session_id != public_session_id:
                logger.debug("Discarded remote sync message for a replaced control session")
                continue
            if not transport.send_sync_message(
                message,
                expected_generation=local_generation,
                expected_transport_session_id=transport_session_id,
            ):
                logger.debug("Discarded remote sync message for a stale control session")
        return received_remote_cc

    def _service_remote_feedback(self) -> None:
        """Flush receiver feedback after the current inbound CC/audio dispatch."""
        transport = self._remote_transport
        output = (
            _RemoteMidiOutput(transport)
            if transport is not None and transport.snapshot.state is SessionState.CONNECTED
            else None
        )
        self._process_pending_sync(output)
        self._process_pending_mute_feedback(output)

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
        """Enumerate ports without superseding the active stream generation."""
        try:
            self._available_ports = list(mido.get_input_names())
        except _MIDI_RECOVERABLE_ERRORS as exc:
            logger.warning("MidiThread: MIDI port refresh failed: %s", exc)
            self._available_ports = []
            self._publish_device_state(
                self._connection_generation,
                "error_temporary",
                "MIDI port refresh failed",
            )
            return

        virtual_connected = (
            self._virtual_client is not None
            and self._device_name in ("", "VIRTUAL_PORT")
            and self._input_mode != "usb"
        )
        physical_connected = (
            self._active_generation == self._connection_generation
            and self._active_input_name is not None
            and self._active_subscription_confirmed
        )
        self._publish_device_state(
            self._connection_generation,
            "stable" if virtual_connected or physical_connected else "connecting",
            (
                "Virtual MIDI Online"
                if virtual_connected
                else f"♫: {normalize_midi_device_name(self._device_name)}"
                if physical_connected
                else "MIDI ports refreshed"
            ),
            connected_name=(
                "VIRTUAL_PORT"
                if virtual_connected
                else self._active_input_name or ""
            ),
        )

    def _activate_physical_stream(
        self,
        generation: int,
        target_device: str,
        input_name: str,
        output_name: str | None,
        input_client_name: str | None,
        output_client_name: str | None,
        subscription_confirmed: bool,
    ) -> None:
        """Mark an opened physical input as the sole connected stream."""
        self._active_generation = generation
        self._active_input_name = input_name
        self._active_output_name = output_name
        self._active_input_client_name = input_client_name
        self._active_output_client_name = output_client_name
        self._active_subscription_confirmed = subscription_confirmed
        self._first_cc_logged_generation = None
        logger.info(
            "MIDI stream opened: generation=%d input=%r output=%r subscription_confirmed=%s",
            generation,
            input_name,
            output_name,
            subscription_confirmed,
        )
        self._prepare_feedback_connection()
        if subscription_confirmed:
            self._confirm_physical_stream(generation, target_device)
        else:
            self._set_connection_state(False)
            self._publish_device_state(
                generation,
                "connecting",
                f"Waiting for MIDI: {normalize_midi_device_name(target_device)}",
                configured_name=target_device,
            )

    def _confirm_physical_stream(self, generation: int, target_device: str) -> None:
        """Publish connected only for the currently opened, proven input stream."""
        if self._active_generation != generation or self._active_input_name is None:
            return
        self._active_subscription_confirmed = True
        self._set_connection_state(True)
        self._publish_device_state(
            generation,
            "stable",
            f"♫: {normalize_midi_device_name(target_device)}",
            configured_name=target_device,
            connected_name=self._active_input_name,
        )

    def _deactivate_physical_stream(
        self,
        generation: int,
        target_device: str,
        reason: str,
    ) -> None:
        """Clear stream ownership and publish disconnect from its generation."""
        if self._active_generation == generation:
            self._active_generation = None
            self._active_input_name = None
            self._active_output_name = None
            self._active_input_client_name = None
            self._active_output_client_name = None
            self._active_subscription_confirmed = False
        logger.info("MIDI stream inactive: generation=%d reason=%s", generation, reason)
        self._set_connection_state(False)
        self._publish_device_state(
            generation,
            "error_temporary",
            "MIDI Disconnected - Retrying...",
            configured_name=target_device,
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
                self._close_remote_transport()
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
        self._close_remote_transport()

    def _run_safe(self) -> None:
        """Inner loop for MIDI processing logic."""
        self._ensure_remote_transport()
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
            # Receive mode does not need a local MIDI backend; keep its LAN path responsive.
            while self._running and not self._panic_flag:
                if self._remote_role == "receive":
                    self._poll_remote_transport()
                    self._service_remote_feedback()
                    time.sleep(0.005)
                else:
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
            remote_transport = self._ensure_remote_transport()

            # Is MIDI even enabled?
            if self._input_mode == "usb":
                # USB-only: idle without closing the virtual port so ALSA
                # clients see one stable "NativMix:Input" across mode switches.
                if self._virtual_client is None:
                    self._set_connection_state(False)
                # Wait for setting changes
                while self._running and not self._panic_flag and self._input_mode == "usb":
                    self._sleep_checked(0.5)
                continue

            target_device = self._device_name if self._device_name else "VIRTUAL_PORT"
            generation = self._next_connection_generation()
            try:
                if self._critical_error:
                    self._sleep_checked(2.0)
                    continue

                if self._remote_role == "receive":
                    self._publish_device_state(
                        generation,
                        "connecting",
                        "Remote MIDI controller",
                    )
                    self._run_remote_receive_loop(remote_transport)
                    continue

                if self._remote_role == "send" and target_device == "VIRTUAL_PORT":
                    self._set_connection_state(False)
                    self._publish_device_state(
                        generation,
                        "disabled",
                        "Remote Send requires a physical MIDI controller",
                    )
                    self._sleep_checked(0.5)
                    continue

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
                        self._poll_remote_transport(None)

                        msg_data = self._virtual_client.get_message()
                        if msg_data:
                            msg, _ = msg_data
                            if len(msg) >= 3 and (msg[0] & 0xF0) == 0xB0:
                                if self._remote_role == "send":
                                    self._forward_remote_cc(msg[0] & 0x0F, msg[1], msg[2])
                                else:
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
                    if self._feedback_output_enabled():
                        try:
                            out_name = _match_midi_port(mido.get_output_names(), target_device)
                        except Exception as exc:
                            logger.debug("MidiThread: could not list MIDI outputs: %s", exc)
                        if out_name is None:
                            logger.warning(
                                "MIDI fader feedback enabled but no output port matched '%s'",
                                target_device,
                            )

                    try:
                        close_reason = self._run_physical_session(
                            generation,
                            target_device,
                            target_name,
                            out_name,
                        )
                    except MidiEndpointDisconnected as exc:
                        self._deactivate_physical_stream(generation, target_device, str(exc))
                        self._sleep_checked(0.5)
                        continue

                    if close_reason != "stopped":
                        self._deactivate_physical_stream(generation, target_device, close_reason)

            except _MIDI_RECOVERABLE_ERRORS as exc:
                logger.warning("MIDI Recoverable Error: %s", exc)
                if self._virtual_client is not None and self._device_name in ("", "VIRTUAL_PORT"):
                    self._close_virtual_client()
                self._deactivate_physical_stream(generation, target_device, str(exc))
                self._sleep_checked(5.0)

        logger.debug("MidiThread stopped")

    def _run_remote_receive_loop(self, transport: RemoteMidiTransport | None) -> None:
        """Poll a selected LAN controller without opening a local MIDI input."""
        while (
            self._running
            and not self._panic_flag
            and self._input_mode != "usb"
            and self._remote_role == "receive"
        ):
            transport = self._ensure_remote_transport() or transport
            self._poll_remote_transport()
            self._service_remote_feedback()
            time.sleep(0.005)
        if self._remote_role == "receive":
            self._set_connection_state(False)

    def _assert_physical_ports_current(
        self,
        inport,
        outport,
        target_device: str,
        connected_input_name: str,
        connected_output_name: str | None,
    ) -> None:
        """Raise when ALSA replaced or removed an opened physical endpoint."""
        input_rt = getattr(inport, "_rt", None)
        output_rt = getattr(outport, "_rt", None) if outport is not None else None
        input_names = (
            list(input_rt.get_ports())
            if input_rt is not None
            else list(mido.get_input_names())
        )
        self._available_ports = input_names
        if connected_input_name not in input_names:
            raise MidiEndpointDisconnected(
                f"input endpoint disappeared: {connected_input_name!r}; available={input_names!r}"
            )
        current_input_name = _match_midi_port(input_names, target_device)
        if current_input_name != connected_input_name:
            raise MidiEndpointDisconnected(
                f"input endpoint replaced: {connected_input_name!r} -> {current_input_name!r}"
            )

        if self._fader_feedback_enabled:
            output_names = (
                list(output_rt.get_ports())
                if output_rt is not None
                else list(mido.get_output_names())
            )
            if connected_output_name is not None and connected_output_name not in output_names:
                raise MidiEndpointDisconnected(
                    f"output endpoint disappeared: {connected_output_name!r}; available={output_names!r}"
                )
            current_output_name = _match_midi_port(output_names, target_device)
            if current_output_name != connected_output_name:
                raise MidiEndpointDisconnected(
                    f"output endpoint replaced: {connected_output_name!r} -> {current_output_name!r}"
                )
        self._assert_active_alsa_subscriptions(
            connected_input_name,
            connected_output_name,
        )

    def _prepare_rtmidi_subscription_check(
        self,
        inport,
        outport,
        generation: int,
        input_name: str,
        output_name: str | None,
    ) -> tuple[str | None, str | None, bool]:
        """Name RtMidi clients and verify their ALSA subscriptions when observable."""
        input_rt = getattr(inport, "_rt", None)
        output_rt = getattr(outport, "_rt", None) if outport is not None else None
        if input_rt is None:
            return None, None, True
        if not input_rt.is_port_open():
            raise MidiEndpointDisconnected("RtMidi input returned without an open port")
        if output_rt is not None and not output_rt.is_port_open():
            raise MidiEndpointDisconnected("RtMidi output returned without an open port")

        input_endpoint = _alsa_endpoint_address(input_name)
        output_endpoint = _alsa_endpoint_address(output_name or "")
        if sys.platform != "linux" or input_endpoint is None:
            return None, None, False

        input_client_name = f"NativMix MIDI In g{generation}"
        output_client_name = f"NativMix MIDI Out g{generation}" if output_rt is not None else None
        input_rt.set_client_name(input_client_name)
        if output_rt is not None and output_client_name is not None:
            output_rt.set_client_name(output_client_name)

        try:
            snapshot = Path(_ALSA_SEQ_CLIENTS_PATH).read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("MIDI ALSA subscription state unavailable: %s", exc)
            return input_client_name, output_client_name, False

        if not _alsa_subscription_present(
            snapshot,
            input_client_name,
            "Connected From",
            input_endpoint,
        ):
            raise MidiEndpointDisconnected(
                f"input subscription missing: client={input_client_name!r} endpoint={input_endpoint}"
            )
        if output_rt is not None and (
            output_endpoint is None
            or output_client_name is None
            or not _alsa_subscription_present(
                snapshot,
                output_client_name,
                "Connecting To",
                output_endpoint,
            )
        ):
            raise MidiEndpointDisconnected(
                f"output subscription missing: client={output_client_name!r} endpoint={output_endpoint}"
            )
        return input_client_name, output_client_name, True

    def _assert_active_alsa_subscriptions(
        self,
        input_name: str,
        output_name: str | None,
    ) -> None:
        """Detect fast unplug/replug even when ALSA reuses the endpoint address."""
        if (
            sys.platform != "linux"
            or self._active_input_client_name is None
            or _alsa_endpoint_address(input_name) is None
        ):
            return
        try:
            snapshot = Path(_ALSA_SEQ_CLIENTS_PATH).read_text(encoding="utf-8")
        except OSError:
            return

        input_endpoint = _alsa_endpoint_address(input_name)
        if input_endpoint is None or not _alsa_subscription_present(
            snapshot,
            self._active_input_client_name,
            "Connected From",
            input_endpoint,
        ):
            raise MidiEndpointDisconnected(
                f"input subscription lost: client={self._active_input_client_name!r} endpoint={input_endpoint}"
            )

        if output_name is not None and self._active_output_client_name is not None:
            output_endpoint = _alsa_endpoint_address(output_name)
            if output_endpoint is None or not _alsa_subscription_present(
                snapshot,
                self._active_output_client_name,
                "Connecting To",
                output_endpoint,
            ):
                raise MidiEndpointDisconnected(
                    "output subscription lost: "
                    f"client={self._active_output_client_name!r} endpoint={output_endpoint}"
                )

    def _run_physical_session(
        self,
        generation: int,
        target_device: str,
        input_name: str,
        output_name: str | None,
    ) -> str:
        """Open exact endpoints, run until transition, then close both contexts."""
        logger.info(
            "MIDI opening: generation=%d input=%r output=%r",
            generation,
            input_name,
            output_name,
        )
        try:
            with ExitStack() as stack:
                inport = stack.enter_context(mido.open_input(input_name))
                outport = (
                    stack.enter_context(mido.open_output(output_name))
                    if output_name is not None
                    else None
                )
                input_client_name, output_client_name, subscription_confirmed = (
                    self._prepare_rtmidi_subscription_check(
                        inport,
                        outport,
                        generation,
                        input_name,
                        output_name,
                    )
                )
                self._activate_physical_stream(
                    generation,
                    target_device,
                    input_name,
                    output_name,
                    input_client_name,
                    output_client_name,
                    subscription_confirmed,
                )
                return self._device_loop(
                    inport,
                    outport,
                    target_device,
                    connected_input_name=input_name,
                    connected_output_name=output_name,
                )
        finally:
            logger.info(
                "MIDI contexts closed: generation=%d input=%r output=%r",
                generation,
                input_name,
                output_name,
            )

    def _device_loop(
        self,
        inport,
        outport,
        target_device: str,
        connected_input_name: str | None = None,
        connected_output_name: str | None = None,
    ) -> str:
        """Poll a physical MIDI input (and optional output) until reconnect is needed."""
        next_port_check = time.monotonic() + _MIDI_PORT_CHECK_INTERVAL_S
        if self._remote_role == "send":
            self._flush_remote_feedback_cache(outport)
        while self._running and not self._panic_flag:
            if self._input_mode == "usb":
                return "input mode changed"
            if self._device_name != target_device:
                return "configured device changed"
            now = time.monotonic()
            if connected_input_name is not None and now >= next_port_check:
                self._assert_physical_ports_current(
                    inport,
                    outport,
                    target_device,
                    connected_input_name,
                    connected_output_name,
                )
                next_port_check = now + _MIDI_PORT_CHECK_INTERVAL_S
            msg = inport.receive(block=False)
            if msg is not None and msg.type == "control_change":
                if self._first_cc_logged_generation != self._active_generation:
                    logger.info(
                        "MIDI first CC: generation=%s input=%r channel=%d cc=%d",
                        self._active_generation,
                        connected_input_name,
                        int(msg.channel),
                        msg.control,
                    )
                    self._first_cc_logged_generation = self._active_generation
                if not self._active_subscription_confirmed and self._active_generation is not None:
                    self._confirm_physical_stream(self._active_generation, target_device)
                if self._remote_role == "send":
                    self._forward_remote_cc(int(msg.channel), msg.control, msg.value)
                else:
                    self._handle_cc(int(msg.channel), msg.control, msg.value)
            self._process_pending_sync(outport)
            self._process_pending_mute_feedback(outport)
            self._poll_remote_transport(outport)
            if msg is None:
                time.sleep(0.005 if self._remote_role == "send" else 0.05)
        return "stopped" if not self._running else "restart requested"

    def _process_pending_sync(self, outport) -> None:
        """Send queued outbound fader CC values when feedback is enabled."""
        if not self._feedback_output_enabled():
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
            for ch_idx, value in pending:
                suppressed_bindings: frozenset[tuple[int, int]]
                if isinstance(value, tuple):
                    volume, suppressed_bindings = value
                else:
                    volume = value
                    suppressed_bindings = frozenset()
                for midi_channel, cc in ch_to_bindings.get(ch_idx, []):
                    if (midi_channel, cc) in suppressed_bindings:
                        logger.debug(
                            "MIDI feedback suppressed: channel=%d midi_ch=%d cc=%d reason=origin_ack",
                            ch_idx,
                            midi_channel,
                            cc,
                        )
                        continue
                    self._send_fader_cc(outport, midi_channel, cc, ch_idx, volume)
        except _MIDI_RECOVERABLE_ERRORS:
            with self._feedback_lock:
                retry = dict(pending)
                retry.update(self._pending_sync or [])
                self._pending_sync = list(retry.items())
            raise

    def _process_pending_mute_feedback(self, outport) -> None:
        """Send queued mute state and the example controller's LED hue."""
        if not self._feedback_output_enabled():
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

    def _handle_cc(
        self,
        midi_channel: int,
        cc: int,
        val: int,
        *,
        throttle_volume: bool = True,
        check_feedback_takeover: bool = True,
        emit_volume: bool = True,
        emit_learn: bool = True,
    ) -> None:
        """Process a Control Change on a protocol MIDI channel."""
        midi_channel = max(0, min(15, int(midi_channel)))
        key = (midi_channel, cc)
        with self._map_lock:
            self._last_values[key] = val

        # 1. Always emit for Learn handshake
        if emit_learn:
            self.midi_cc_received.emit(midi_channel, cc, val)

        # 2. Check if mapped to a fader — throttled to 50 Hz per binding.
        # to prevent Qt signal queue flooding from misbehaving MIDI controllers.
        with self._map_lock:
            ch_idx = self._cc_map.get(key)
        if ch_idx is not None and emit_volume:
            if check_feedback_takeover and self._remote_fader_input_suppressed(midi_channel, cc, val):
                return
            now = time.monotonic()
            with self._map_lock:
                last_emit = self._last_vol_emit.get(key, 0.0)
            if not throttle_volume or now - last_emit >= 0.02:
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
        while self._running and not self._panic_flag and time.time() < end_time:
            self._poll_remote_transport()
            time.sleep(0.05)
