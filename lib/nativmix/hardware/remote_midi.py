"""Qt-free AppleMIDI transport for NativMix remote MIDI controllers.

The transport is deliberately poll-driven: DNS-SD callbacks only enqueue
immutable :class:`DiscoveryChange` objects, and all session state changes
happen in :meth:`RemoteMidiTransport.poll`.
"""

from __future__ import annotations

import importlib
import ipaddress
import logging
import random
import secrets
import socket
import struct
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from queue import Empty, SimpleQueue
from typing import Any, Protocol

logger = logging.getLogger(__name__)

APPLE_MIDI_SERVICE_TYPE = "_apple-midi._udp.local."
MAX_DATAGRAM = 2048
MAX_NAME_BYTES = 255
_PROTOCOL_VERSION = 2
_RTP_PAYLOAD_TYPE = 0x61
_SEQUENCE_WINDOW = 0x4000
_INVITATION_INTERVAL = 1.0
_MAX_INVITATIONS = 12
_SYNC_INTERVAL = 10.0
_INACTIVITY_TIMEOUT = 30.0
_MAX_BACKOFF = 60.0
_MAX_PACKETS_PER_POLL = 128
_MAX_INVITATION_RATE_ENTRIES = 128

_CONTROL_PREFIX = struct.Struct("!HH")
_INVITATION_HEADER = struct.Struct("!HHIII")
_END_HEADER = struct.Struct("!HHII")
_SYNC_PACKET = struct.Struct("!HHIB3xQQQ")
_RTP_HEADER = struct.Struct("!BBHII")
_SIGNATURE = 0xFFFF


class RemoteMidiRole(str, Enum):
    """Transport operating role."""

    OFF = "off"
    SEND = "send"
    RECEIVE = "receive"


class SessionState(str, Enum):
    """Current transport/session state."""

    STOPPED = "stopped"
    UNAVAILABLE = "unavailable"
    IDLE = "idle"
    INVITING_CONTROL = "inviting_control"
    INVITING_DATA = "inviting_data"
    CONNECTED = "connected"
    BACKOFF = "backoff"
    CLOSED = "closed"


class ControlCommand(bytes, Enum):
    """AppleMIDI control commands."""

    INVITATION = b"IN"
    ACCEPT = b"OK"
    REJECT = b"NO"
    END = b"BY"
    SYNC = b"CK"

    @property
    def code(self) -> int:
        """Return the command's network-order integer representation."""
        return int.from_bytes(self.value, "big")


@dataclass(frozen=True)
class InvitationPacket:
    """Decoded AppleMIDI invitation, response, rejection, or end packet."""

    command: ControlCommand
    token: int
    ssrc: int
    name: str


@dataclass(frozen=True)
class SyncPacket:
    """Decoded AppleMIDI clock synchronization packet."""

    ssrc: int
    count: int
    timestamps: tuple[int, int, int]


@dataclass(frozen=True)
class RtpCCPacket:
    """Decoded RTP-MIDI Control Change packet."""

    sequence: int
    timestamp: int
    ssrc: int
    channel: int
    control: int
    value: int


@dataclass(frozen=True)
class PeerRecord:
    """Validated NativMix send service, keyed by its stable UUID."""

    peer_id: str
    name: str
    host: str
    control_port: int
    data_port: int
    service_name: str = ""


class DiscoveryChangeKind(str, Enum):
    """A change emitted by a discovery backend."""

    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"


@dataclass(frozen=True)
class DiscoveryChange:
    """Immutable hand-off from a DNS-SD callback to the polling thread."""

    kind: DiscoveryChangeKind
    service_name: str
    peer: PeerRecord | None = None


@dataclass(frozen=True)
class TransportSnapshot:
    """Authoritative immutable view of discovery, queue, and session state."""

    generation: int
    role: RemoteMidiRole
    state: SessionState
    available: bool
    error: str | None
    peers: tuple[PeerRecord, ...]
    selected_peer_id: str | None
    connected_peer_id: str | None
    connected_peer_name: str | None
    outgoing_count: int
    outgoing_capacity: int
    overflow_count: int
    dropped_count: int
    warning: str | None
    reconnect_attempt: int
    last_activity: float | None


class DiscoveryBackend(Protocol):
    """Minimal injectable DNS-SD backend contract."""

    def start(self, advertisement: Mapping[str, Any] | None) -> None:
        """Start browsing and optionally advertising."""

    def refresh(self) -> None:
        """Refresh the browser."""

    def close(self) -> None:
        """Stop and release backend resources."""


def _bounded_utf8(value: str, limit: int = MAX_NAME_BYTES) -> tuple[str, bytes]:
    raw = value.replace("\x00", "").encode("utf-8")
    if len(raw) > limit:
        raw = raw[:limit]
        while raw:
            try:
                value = raw.decode("utf-8")
                break
            except UnicodeDecodeError:
                raw = raw[:-1]
        else:
            value = ""
    else:
        value = raw.decode("utf-8")
    return value, raw


def _valid_u32(value: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{field} must be an unsigned 32-bit integer")
    return value


def encode_invitation(command: ControlCommand, token: int, ssrc: int, name: str = "") -> bytes:
    """Encode a strict AppleMIDI IN/OK/NO/BY packet."""
    if command not in {
        ControlCommand.INVITATION,
        ControlCommand.ACCEPT,
        ControlCommand.REJECT,
        ControlCommand.END,
    }:
        raise ValueError("not an invitation exchange command")
    token = _valid_u32(token, "token")
    ssrc = _valid_u32(ssrc, "ssrc")
    if command is ControlCommand.END:
        return _END_HEADER.pack(_SIGNATURE, command.code, token, ssrc)
    _, encoded_name = _bounded_utf8(name)
    return _INVITATION_HEADER.pack(_SIGNATURE, command.code, _PROTOCOL_VERSION, token, ssrc) + encoded_name + b"\0"


def encode_sync(ssrc: int, count: int, timestamps: tuple[int, int, int]) -> bytes:
    """Encode an AppleMIDI CK packet."""
    ssrc = _valid_u32(ssrc, "ssrc")
    if not 0 <= count <= 2:
        raise ValueError("sync count must be 0, 1, or 2")
    timestamps_invalid = len(timestamps) != 3 or any(
        not isinstance(item, int) or not 0 <= item <= 0xFFFFFFFFFFFFFFFF for item in timestamps
    )
    if timestamps_invalid:
        raise ValueError("sync timestamps must be three unsigned 64-bit integers")
    return _SYNC_PACKET.pack(_SIGNATURE, ControlCommand.SYNC.code, ssrc, count, *timestamps)


def decode_control_packet(data: bytes) -> InvitationPacket | SyncPacket:
    """Decode a strict bounded AppleMIDI control packet."""
    if not 4 <= len(data) <= MAX_DATAGRAM:
        raise ValueError("invalid control packet length")
    signature, command_code = _CONTROL_PREFIX.unpack_from(data)
    if signature != _SIGNATURE:
        raise ValueError("invalid AppleMIDI signature")
    try:
        command = ControlCommand(command_code.to_bytes(2, "big"))
    except ValueError as exc:
        raise ValueError("unknown AppleMIDI command") from exc

    if command is ControlCommand.SYNC:
        if len(data) != _SYNC_PACKET.size:
            raise ValueError("invalid sync packet length")
        _, _, ssrc, count, t1, t2, t3 = _SYNC_PACKET.unpack(data)
        if count > 2:
            raise ValueError("invalid sync count")
        return SyncPacket(ssrc, count, (t1, t2, t3))

    if command is ControlCommand.END:
        if len(data) != _END_HEADER.size:
            raise ValueError("invalid end packet length")
        _, _, token, ssrc = _END_HEADER.unpack(data)
        return InvitationPacket(command, token, ssrc, "")

    if len(data) < _INVITATION_HEADER.size + 1:
        raise ValueError("truncated invitation packet")
    _, _, version, token, ssrc = _INVITATION_HEADER.unpack_from(data)
    if version != _PROTOCOL_VERSION:
        raise ValueError("unsupported AppleMIDI protocol version")
    name_bytes = data[_INVITATION_HEADER.size :]
    if not name_bytes.endswith(b"\0") or b"\0" in name_bytes[:-1] or len(name_bytes) - 1 > MAX_NAME_BYTES:
        raise ValueError("invalid invitation name")
    try:
        name = name_bytes[:-1].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("invitation name is not UTF-8") from exc
    return InvitationPacket(command, token, ssrc, name)


def encode_rtp_cc(
    sequence: int,
    timestamp: int,
    ssrc: int,
    channel: int,
    control: int,
    value: int,
) -> bytes:
    """Encode one complete MIDI 1.0 CC command in an RTP v2 datagram."""
    if not 0 <= sequence <= 0xFFFF:
        raise ValueError("sequence must be an unsigned 16-bit integer")
    timestamp = _valid_u32(timestamp, "timestamp")
    ssrc = _valid_u32(ssrc, "ssrc")
    if not 0 <= channel <= 15:
        raise ValueError("MIDI channel must be in 0..15")
    if not 0 <= control <= 127 or not 0 <= value <= 127:
        raise ValueError("MIDI control and value must be in 0..127")
    header = _RTP_HEADER.pack(0x80, 0x80 | _RTP_PAYLOAD_TYPE, sequence, timestamp, ssrc)
    return header + bytes((3, 0xB0 | channel, control, value))


def decode_rtp_cc(data: bytes) -> RtpCCPacket:
    """Decode one complete non-running-status CC, ignoring a declared trailing journal."""
    if not _RTP_HEADER.size + 4 <= len(data) <= MAX_DATAGRAM:
        raise ValueError("invalid RTP-MIDI packet length")
    first, second, sequence, timestamp, ssrc = _RTP_HEADER.unpack_from(data)
    if first != 0x80:
        raise ValueError("RTP packet must be version 2 without extensions")
    if second != 0x80 | _RTP_PAYLOAD_TYPE:
        raise ValueError("invalid RTP marker or payload type")

    offset = _RTP_HEADER.size
    command_header = data[offset]
    offset += 1
    if command_header & 0x30:
        raise ValueError("unsupported RTP-MIDI command flags")
    if command_header & 0x80:
        if offset >= len(data):
            raise ValueError("truncated long MIDI command length")
        command_length = ((command_header & 0x0F) << 8) | data[offset]
        offset += 1
    else:
        command_length = command_header & 0x0F
    command_end = offset + command_length
    if command_length != 3 or command_end > len(data):
        raise ValueError("RTP-MIDI packet must contain exactly one complete CC")
    has_journal = bool(command_header & 0x40)
    if (has_journal and command_end == len(data)) or (not has_journal and command_end != len(data)):
        raise ValueError("invalid RTP-MIDI journal declaration")
    status, control, value = data[offset:command_end]
    if status & 0xF0 != 0xB0 or control > 127 or value > 127:
        raise ValueError("RTP-MIDI command is not a valid Control Change")
    return RtpCCPacket(sequence, timestamp, ssrc, status & 0x0F, control, value)


def _text_properties(properties: Mapping[Any, Any]) -> dict[str, str] | None:
    result: dict[str, str] = {}
    total = 0
    for raw_key, raw_value in properties.items():
        try:
            key = raw_key.decode("ascii") if isinstance(raw_key, bytes) else str(raw_key)
            value = raw_value.decode("utf-8") if isinstance(raw_value, bytes) else str(raw_value)
        except (UnicodeDecodeError, UnicodeEncodeError):
            return None
        total += len(key.encode("utf-8")) + len(value.encode("utf-8")) + 2
        if total > 1024 or len(key) > 64 or len(value.encode("utf-8")) > MAX_NAME_BYTES:
            return None
        result[key.lower()] = value
    return result


def peer_from_service(
    service_name: str,
    addresses: Iterable[str],
    port: int,
    properties: Mapping[Any, Any],
) -> PeerRecord | None:
    """Validate and convert a DNS-SD record, accepting IPv4 NativMix send records only."""
    props = _text_properties(properties)
    if props is None or props.get("nativmix") != "1" or props.get("protocol") != "1" or props.get("role") != "send":
        return None
    peer_id = props.get("instance_id", props.get("instance", ""))
    try:
        peer_id = str(uuid.UUID(peer_id))
    except (ValueError, AttributeError):
        return None
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return None
    try:
        data_port = int(props.get("data_port", str(port + 1)))
    except ValueError:
        return None
    if not 1 <= data_port <= 65535:
        return None
    host = ""
    for candidate in addresses:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv4Address) and not address.is_unspecified and not address.is_multicast:
            host = str(address)
            break
    if not host:
        return None
    display_name = props.get("name") or service_name.removesuffix(APPLE_MIDI_SERVICE_TYPE).rstrip(".")
    display_name, encoded = _bounded_utf8(display_name)
    if not encoded:
        return None
    return PeerRecord(peer_id, display_name, host, port, data_port, service_name)


class _ZeroconfListener:
    def __init__(self, backend: _ZeroconfDiscovery) -> None:
        self._backend = backend

    def add_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        self._backend.resolve(zeroconf, service_type, name, DiscoveryChangeKind.ADD)

    def update_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        self._backend.resolve(zeroconf, service_type, name, DiscoveryChangeKind.UPDATE)

    def remove_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        del zeroconf, service_type
        self._backend.emit(DiscoveryChange(DiscoveryChangeKind.REMOVE, name))


class _ZeroconfDiscovery:
    """Lazy optional zeroconf adapter. Its methods may execute on library threads."""

    def __init__(
        self,
        emit: Callable[[DiscoveryChange], None],
        zeroconf_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.emit = emit
        self._zeroconf_factory = zeroconf_factory
        self._zeroconf: Any = None
        self._browser: Any = None
        self._browser_type: Any = None
        self._ip_version: Any = None
        self._listener = _ZeroconfListener(self)
        self._registered_info: Any = None

    def start(self, advertisement: Mapping[str, Any] | None) -> None:
        zeroconf_module = importlib.import_module("zeroconf")
        IPVersion = zeroconf_module.IPVersion
        ServiceBrowser = zeroconf_module.ServiceBrowser
        ServiceInfo = zeroconf_module.ServiceInfo
        Zeroconf = zeroconf_module.Zeroconf

        self._browser_type = ServiceBrowser
        self._ip_version = IPVersion
        self._zeroconf = self._zeroconf_factory() if self._zeroconf_factory else Zeroconf(ip_version=IPVersion.V4Only)
        if advertisement is not None:
            address = _advertised_ipv4(str(advertisement["bind_host"]))
            self._registered_info = ServiceInfo(
                APPLE_MIDI_SERVICE_TYPE,
                str(advertisement["service_name"]),
                addresses=[socket.inet_aton(address)],
                port=int(advertisement["control_port"]),
                properties=advertisement["properties"],
                server=f"{socket.gethostname().split('.')[0][:63] or 'nativmix'}.local.",
            )
            self._zeroconf.register_service(self._registered_info)
            logger.info(
                "Remote MIDI DNS-SD advertising %r on %s:%d",
                advertisement["service_name"],
                address,
                advertisement["control_port"],
            )
        self._browser = ServiceBrowser(self._zeroconf, APPLE_MIDI_SERVICE_TYPE, self._listener)
        logger.info("Remote MIDI DNS-SD browsing for %s", APPLE_MIDI_SERVICE_TYPE)

    def resolve(self, zeroconf: Any, service_type: str, name: str, kind: DiscoveryChangeKind) -> None:
        try:
            info = zeroconf.get_service_info(service_type, name, timeout=1000)
            if info is None:
                return
            try:
                addresses = info.parsed_addresses(self._ip_version.V4Only)
            except TypeError:
                addresses = info.parsed_addresses()
            peer = peer_from_service(name, addresses, info.port, info.properties)
            if peer is not None:
                self.emit(DiscoveryChange(kind, name, peer))
            else:
                self.emit(DiscoveryChange(DiscoveryChangeKind.REMOVE, name))
        except (OSError, RuntimeError, ValueError) as exc:
            logger.debug("Remote MIDI service resolution failed for %s: %s", name, exc)

    def refresh(self) -> None:
        if self._zeroconf is None:
            return
        if self._browser is not None:
            self._browser.cancel()
        self._browser = self._browser_type(self._zeroconf, APPLE_MIDI_SERVICE_TYPE, self._listener)

    def close(self) -> None:
        if self._browser is not None:
            self._browser.cancel()
            self._browser = None
        if self._zeroconf is not None:
            if self._registered_info is not None:
                try:
                    self._zeroconf.unregister_service(self._registered_info)
                except (OSError, RuntimeError) as exc:
                    logger.debug("Remote MIDI service unregister failed: %s", exc)
            self._zeroconf.close()
            self._zeroconf = None


def _advertised_ipv4(bind_host: str) -> str:
    if bind_host != "0.0.0.0":
        return bind_host
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return str(probe.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        probe.close()


SocketFactory = Callable[[int, int], socket.socket]
DiscoveryFactory = Callable[[Callable[[DiscoveryChange], None]], DiscoveryBackend]
SnapshotCallback = Callable[[TransportSnapshot], None]


class RemoteMidiTransport:
    """Poll-driven, IPv4 AppleMIDI session and CC transport."""

    def __init__(
        self,
        role: RemoteMidiRole | str,
        instance_id: str,
        advertised_name: str,
        selected_peer_id: str | None = None,
        selected_peer_name: str | None = None,
        bind_host: str = "0.0.0.0",
        control_port: int = 5004,
        data_port: int = 5005,
        outgoing_capacity: int = 512,
        *,
        clock: Callable[[], float] = time.monotonic,
        socket_factory: SocketFactory = socket.socket,
        random_u32: Callable[[], int] = lambda: secrets.randbits(32),
        random_float: Callable[[], float] = random.random,
        discovery_factory: DiscoveryFactory | None = None,
        zeroconf_factory: Callable[[], Any] | None = None,
        on_snapshot: SnapshotCallback | None = None,
    ) -> None:
        self.role = RemoteMidiRole(role)
        self.instance_id = str(uuid.UUID(instance_id))
        self.advertised_name, encoded_name = _bounded_utf8(advertised_name)
        if not encoded_name:
            raise ValueError("advertised_name must not be empty")
        try:
            bind_address = ipaddress.ip_address(bind_host)
        except ValueError as exc:
            raise ValueError("bind_host must be a literal IPv4 address") from exc
        if not isinstance(bind_address, ipaddress.IPv4Address):
            raise ValueError("bind_host must be IPv4")
        if not 0 <= control_port <= 65535 or not 0 <= data_port <= 65535:
            raise ValueError("ports must be in 0..65535")
        if outgoing_capacity <= 0:
            raise ValueError("outgoing_capacity must be positive")

        self.bind_host = bind_host
        self.control_port = control_port
        self.data_port = data_port
        self.outgoing_capacity = outgoing_capacity
        self._clock = clock
        self._socket_factory = socket_factory
        self._random_u32 = random_u32
        self._random_float = random_float
        self._on_snapshot = on_snapshot
        self._discovery_factory = discovery_factory
        self._zeroconf_factory = zeroconf_factory

        self._state = SessionState.STOPPED
        self._available = self.role is not RemoteMidiRole.OFF
        self._error: str | None = None
        self._warning: str | None = None
        self._generation = 0
        self._started = False
        self._closed = False
        self._control_socket: socket.socket | None = None
        self._data_socket: socket.socket | None = None
        self._discovery: DiscoveryBackend | None = None
        self._discovery_changes: SimpleQueue[DiscoveryChange] = SimpleQueue()
        self._peers: dict[str, PeerRecord] = {}
        self._services: dict[str, str] = {}
        self._selected_peer_id = self._normalize_optional_uuid(selected_peer_id)
        self._selected_peer_name = selected_peer_name
        self._connected_peer_id: str | None = None
        self._manual_disconnect = False

        self._outgoing: deque[tuple[int, int, int]] = deque()
        self._overflow_count = 0
        self._dropped_count = 0
        self._local_ssrc = self._new_nonzero_u32()
        self._token = 0
        self._remote_ssrc: int | None = None
        self._remote_name = ""
        self._control_endpoint: tuple[str, int] | None = None
        self._data_endpoint: tuple[str, int] | None = None
        self._sequence = self._random_u32() & 0xFFFF
        self._last_sequence: int | None = None
        self._invitation_tries = 0
        self._next_invitation = 0.0
        self._reconnect_attempt = 0
        self._backoff_until = 0.0
        self._last_received: float | None = None
        self._next_sync = 0.0
        self._invitation_sources: dict[tuple[str, int], float] = {}

    @staticmethod
    def _normalize_optional_uuid(value: str | None) -> str | None:
        return str(uuid.UUID(value)) if value else None

    def _new_nonzero_u32(self) -> int:
        value = self._random_u32() & 0xFFFFFFFF
        return value or 1

    @property
    def snapshot(self) -> TransportSnapshot:
        """Return the current immutable transport snapshot."""
        peers = tuple(sorted(self._peers.values(), key=lambda peer: (peer.name.casefold(), peer.peer_id)))
        return TransportSnapshot(
            generation=self._generation,
            role=self.role,
            state=self._state,
            available=self._available,
            error=self._error,
            peers=peers,
            selected_peer_id=self._selected_peer_id,
            connected_peer_id=self._connected_peer_id,
            connected_peer_name=self._remote_name if self._state is SessionState.CONNECTED else None,
            outgoing_count=len(self._outgoing),
            outgoing_capacity=self.outgoing_capacity,
            overflow_count=self._overflow_count,
            dropped_count=self._dropped_count,
            warning=self._warning,
            reconnect_attempt=self._reconnect_attempt,
            last_activity=self._last_received,
        )

    def _touch(self) -> None:
        self._generation += 1
        if self._on_snapshot is not None:
            try:
                self._on_snapshot(self.snapshot)
            except Exception:
                logger.exception("Remote MIDI snapshot callback failed")

    def start(self) -> TransportSnapshot:
        """Bind nonblocking sockets and start optional DNS-SD."""
        if self._closed:
            raise RuntimeError("transport is closed")
        if self._started:
            return self.snapshot
        self._started = True
        if self.role is RemoteMidiRole.OFF:
            self._state = SessionState.IDLE
            self._available = False
            self._touch()
            return self.snapshot

        try:
            control_socket = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
            data_socket = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
            for udp_socket, port in ((control_socket, self.control_port), (data_socket, self.data_port)):
                udp_socket.setblocking(False)
                udp_socket.bind((self.bind_host, port))
            self._control_socket = control_socket
            self._data_socket = data_socket
            self.control_port = int(control_socket.getsockname()[1])
            self.data_port = int(data_socket.getsockname()[1])
            self._state = SessionState.IDLE
            logger.info(
                "Remote MIDI UDP sockets bound: role=%s host=%s control=%d data=%d",
                self.role.value,
                self.bind_host,
                self.control_port,
                self.data_port,
            )
        except OSError as exc:
            if "control_socket" in locals():
                control_socket.close()
            if "data_socket" in locals():
                data_socket.close()
            self._available = False
            self._state = SessionState.UNAVAILABLE
            self._error = f"Unable to bind remote MIDI UDP sockets: {exc}"
            logger.warning("%s", self._error)
            self._touch()
            return self.snapshot

        try:
            if self._discovery_factory is not None:
                self._discovery = self._discovery_factory(self.enqueue_discovery_change)
            else:
                self._discovery = _ZeroconfDiscovery(self.enqueue_discovery_change, self._zeroconf_factory)
            advertisement = self._advertisement() if self.role is RemoteMidiRole.SEND else None
            self._discovery.start(advertisement)
        except ImportError:
            self._close_failed_discovery()
            self._discovery = None
            self._close_udp_sockets()
            self._available = False
            self._error = "Remote MIDI discovery unavailable: install the optional 'zeroconf' package"
            self._state = SessionState.UNAVAILABLE
            logger.warning("%s", self._error)
        except (OSError, RuntimeError, ValueError) as exc:
            self._close_failed_discovery()
            self._discovery = None
            self._close_udp_sockets()
            self._available = False
            self._error = f"Remote MIDI discovery unavailable: {exc}"
            self._state = SessionState.UNAVAILABLE
            logger.warning("Remote MIDI discovery startup failed: %s", exc)
        self._touch()
        return self.snapshot

    def _close_failed_discovery(self) -> None:
        if self._discovery is None:
            return
        try:
            self._discovery.close()
        except (OSError, RuntimeError) as exc:
            logger.debug("Partially started remote MIDI discovery failed to close: %s", exc)

    def _close_udp_sockets(self) -> None:
        for udp_socket in (self._control_socket, self._data_socket):
            if udp_socket is not None:
                udp_socket.close()
        self._control_socket = None
        self._data_socket = None

    def _advertisement(self) -> Mapping[str, Any]:
        label, _ = _bounded_utf8(self.advertised_name, 40)
        service_name = f"{label}-{self.instance_id[:8]}.{APPLE_MIDI_SERVICE_TYPE}"
        host, _ = _bounded_utf8(socket.gethostname(), 63)
        properties = {
            b"nativmix": b"1",
            b"protocol": b"1",
            b"instance_id": self.instance_id.encode("ascii"),
            b"role": b"send",
            b"name": self.advertised_name.encode("utf-8"),
            b"host": host.encode("utf-8"),
            b"data_port": str(self.data_port).encode("ascii"),
        }
        return {
            "service_name": service_name,
            "bind_host": self.bind_host,
            "control_port": self.control_port,
            "properties": properties,
        }

    def enqueue_discovery_change(self, change: DiscoveryChange) -> None:
        """Thread-safe callback target for a discovery backend."""
        if not isinstance(change, DiscoveryChange):
            raise TypeError("change must be a DiscoveryChange")
        self._discovery_changes.put(change)

    def refresh_discovery(self) -> None:
        """Request a DNS-SD browse refresh."""
        if self._discovery is not None:
            try:
                self._discovery.refresh()
            except (OSError, RuntimeError) as exc:
                self._error = f"Remote MIDI discovery refresh failed: {exc}"
                self._touch()

    def select_peer(self, peer_id: str | None, peer_name: str | None = None) -> TransportSnapshot:
        """Select a discovered send peer by stable UUID; ``None`` disables initiation."""
        normalized = self._normalize_optional_uuid(peer_id)
        if normalized == self._selected_peer_id and peer_name == self._selected_peer_name:
            self._manual_disconnect = False
            return self.snapshot
        self._send_end()
        self._drop_outgoing("peer selection changed")
        self._clear_session()
        self._selected_peer_id = normalized
        self._selected_peer_name = peer_name
        self._manual_disconnect = False
        self._reconnect_attempt = 0
        self._state = SessionState.IDLE if self._available else SessionState.UNAVAILABLE
        self._touch()
        return self.snapshot

    def send_cc(self, channel: int, control: int, value: int) -> bool:
        """Append one ordered CC to the bounded outbound queue."""
        if not 0 <= channel <= 15 or not 0 <= control <= 127 or not 0 <= value <= 127:
            raise ValueError("invalid MIDI Control Change")
        if len(self._outgoing) >= self.outgoing_capacity:
            self._overflow_count += 1
            self._warning = "Remote MIDI outgoing queue full; newest CC dropped"
            self._touch()
            return False
        self._outgoing.append((channel, control, value))
        self._touch()
        return True

    def poll(self) -> list[tuple[int, int, int]]:
        """Advance discovery/session state and return newly received ``(channel, control, value)`` tuples."""
        if not self._started or self._closed:
            return []
        self._apply_discovery_changes()
        received: list[tuple[int, int, int]] = []
        if self._control_socket is not None:
            self._drain_socket(self._control_socket, False, received)
        if self._data_socket is not None:
            self._drain_socket(self._data_socket, True, received)
        now = self._clock()
        self._advance_timers(now)
        self._flush_outgoing(now)
        return received

    def _apply_discovery_changes(self) -> None:
        changed = False
        while True:
            try:
                change = self._discovery_changes.get_nowait()
            except Empty:
                break
            if change.kind is DiscoveryChangeKind.REMOVE:
                peer_id = self._services.pop(change.service_name, None)
                if peer_id is not None and peer_id not in self._services.values():
                    removed_peer = self._peers.pop(peer_id, None)
                    changed = True
                    logger.info(
                        "Remote MIDI peer removed: name=%r id=%s",
                        removed_peer.name if removed_peer is not None else change.service_name,
                        peer_id,
                    )
                    active_selected = (
                        self.role is RemoteMidiRole.RECEIVE
                        and peer_id == self._selected_peer_id
                        and self._state
                        in {
                            SessionState.INVITING_CONTROL,
                            SessionState.INVITING_DATA,
                            SessionState.CONNECTED,
                        }
                    )
                    if active_selected:
                        self._end_for_failure("Selected remote MIDI service disappeared")
                continue
            peer = change.peer
            if peer is None:
                continue
            old_peer_id = self._services.get(change.service_name)
            previous = self._peers.get(peer.peer_id)
            self._services[change.service_name] = peer.peer_id
            if old_peer_id and old_peer_id != peer.peer_id and old_peer_id not in self._services.values():
                self._peers.pop(old_peer_id, None)
            self._peers[peer.peer_id] = peer
            changed = changed or previous != peer
            if previous != peer:
                logger.info(
                    "Remote MIDI peer %s: name=%r id=%s host=%s control=%d data=%d",
                    "discovered" if previous is None else "updated",
                    peer.name,
                    peer.peer_id,
                    peer.host,
                    peer.control_port,
                    peer.data_port,
                )
            active_selected = (
                self.role is RemoteMidiRole.RECEIVE
                and peer.peer_id == self._selected_peer_id
                and self._state
                in {
                    SessionState.INVITING_CONTROL,
                    SessionState.INVITING_DATA,
                    SessionState.CONNECTED,
                }
            )
            if (
                previous is not None
                and previous != peer
                and active_selected
                and (previous.host, previous.control_port, previous.data_port)
                != (peer.host, peer.control_port, peer.data_port)
            ):
                self._end_for_failure("Selected remote MIDI endpoint changed")

        if changed:
            self._touch()

    def _drain_socket(
        self,
        udp_socket: socket.socket,
        is_data_socket: bool,
        received: list[tuple[int, int, int]],
    ) -> None:
        for _ in range(_MAX_PACKETS_PER_POLL):
            try:
                data, address = udp_socket.recvfrom(MAX_DATAGRAM + 1)
            except BlockingIOError:
                return
            except OSError as exc:
                self._error = f"Remote MIDI receive failed: {exc}"
                self._touch()
                return
            endpoint = (str(address[0]), int(address[1]))
            if len(data) > MAX_DATAGRAM:
                continue
            if data.startswith(b"\xff\xff"):
                self._handle_control(data, endpoint, is_data_socket)
            elif is_data_socket:
                cc = self._handle_rtp(data, endpoint)
                if cc is not None:
                    received.append(cc)

    def _handle_control(self, data: bytes, endpoint: tuple[str, int], is_data_socket: bool) -> None:
        try:
            packet = decode_control_packet(data)
        except ValueError:
            return
        now = self._clock()
        if isinstance(packet, SyncPacket):
            self._handle_sync(packet, endpoint, is_data_socket, now)
            return
        if packet.command is ControlCommand.INVITATION:
            self._handle_invitation(packet, endpoint, is_data_socket, now)
        elif packet.command is ControlCommand.ACCEPT:
            self._handle_accept(packet, endpoint, is_data_socket, now)
        elif packet.command is ControlCommand.REJECT:
            if self._matches_active(packet, endpoint, is_data_socket):
                self._end_for_failure("Remote MIDI invitation rejected")
        elif packet.command is ControlCommand.END and self._matches_active(packet, endpoint, is_data_socket):
            self._end_for_failure("Remote MIDI peer ended the session", send_end=False)

    def _handle_invitation(
        self,
        packet: InvitationPacket,
        endpoint: tuple[str, int],
        is_data_socket: bool,
        now: float,
    ) -> None:
        if self.role is not RemoteMidiRole.SEND or not self._available:
            self._send_invitation_response(ControlCommand.REJECT, packet, endpoint, is_data_socket)
            return
        if packet.token == 0 or packet.ssrc == 0 or packet.ssrc == self._local_ssrc:
            self._send_invitation_response(ControlCommand.REJECT, packet, endpoint, is_data_socket)
            return
        existing_endpoint = self._data_endpoint if is_data_socket else self._control_endpoint
        expected_host = existing_endpoint[0] if existing_endpoint is not None else None
        if is_data_socket and expected_host is None and self._control_endpoint is not None:
            expected_host = self._control_endpoint[0]
        same_session = (
            packet.token == self._token
            and packet.ssrc == self._remote_ssrc
            and expected_host is not None
            and endpoint[0] == expected_host
        )
        if same_session:
            if is_data_socket:
                self._data_endpoint = endpoint
                if self._state is SessionState.CONNECTED:
                    self._last_received = now
                else:
                    self._mark_connected(now)
            else:
                self._last_received = now
            self._send_invitation_response(ControlCommand.ACCEPT, packet, endpoint, is_data_socket)
            return
        if len(self._invitation_sources) >= _MAX_INVITATION_RATE_ENTRIES:
            oldest = min(self._invitation_sources, key=self._invitation_sources.__getitem__)
            self._invitation_sources.pop(oldest)
        last = self._invitation_sources.get(endpoint, -1e9)
        self._invitation_sources[endpoint] = now
        if now - last < 0.1 or self._remote_ssrc is not None or is_data_socket:
            self._send_invitation_response(ControlCommand.REJECT, packet, endpoint, is_data_socket)
            return
        self._token = packet.token
        self._remote_ssrc = packet.ssrc
        self._remote_name = packet.name
        self._control_endpoint = endpoint
        self._data_endpoint = None
        self._last_received = now
        self._state = SessionState.INVITING_DATA
        logger.info(
            "Remote MIDI incoming session accepted: peer=%r host=%s; waiting for data invitation",
            packet.name,
            endpoint[0],
        )
        self._send_invitation_response(ControlCommand.ACCEPT, packet, endpoint, False)
        self._touch()

    def _send_invitation_response(
        self,
        command: ControlCommand,
        packet: InvitationPacket,
        endpoint: tuple[str, int],
        is_data_socket: bool,
    ) -> None:
        udp_socket = self._data_socket if is_data_socket else self._control_socket
        if udp_socket is None:
            return
        response = encode_invitation(command, packet.token, self._local_ssrc, self.advertised_name)
        self._sendto(udp_socket, response, endpoint)

    def _handle_accept(
        self,
        packet: InvitationPacket,
        endpoint: tuple[str, int],
        is_data_socket: bool,
        now: float,
    ) -> None:
        if self.role is not RemoteMidiRole.RECEIVE or packet.token != self._token:
            return
        peer = self._selected_peer()
        if peer is None or packet.ssrc == 0:
            return
        if not is_data_socket and self._state is SessionState.INVITING_CONTROL:
            if endpoint != (peer.host, peer.control_port):
                return
            self._remote_ssrc = packet.ssrc
            self._remote_name = packet.name
            self._control_endpoint = endpoint
            self._data_endpoint = (peer.host, peer.data_port)
            self._state = SessionState.INVITING_DATA
            self._invitation_tries = 0
            self._send_current_invitation(now)
            self._touch()
        elif is_data_socket and self._state is SessionState.INVITING_DATA:
            if endpoint != (peer.host, peer.data_port) or packet.ssrc != self._remote_ssrc:
                return
            self._data_endpoint = endpoint
            self._mark_connected(now)

    def _matches_active(
        self,
        packet: InvitationPacket,
        endpoint: tuple[str, int],
        is_data_socket: bool,
    ) -> bool:
        expected = self._data_endpoint if is_data_socket else self._control_endpoint
        return (
            expected == endpoint
            and packet.token == self._token
            and self._remote_ssrc is not None
            and packet.ssrc == self._remote_ssrc
        )

    def _handle_sync(
        self,
        packet: SyncPacket,
        endpoint: tuple[str, int],
        is_data_socket: bool,
        now: float,
    ) -> None:
        if not is_data_socket:
            return
        if (
            self._state is not SessionState.CONNECTED
            or self._data_endpoint != endpoint
            or self._remote_ssrc is None
            or packet.ssrc != self._remote_ssrc
        ):
            return
        self._last_received = now
        stamp = int(now * 10_000) & 0xFFFFFFFFFFFFFFFF
        if self._data_socket is None:
            return
        if packet.count == 0:
            response = encode_sync(self._local_ssrc, 1, (packet.timestamps[0], stamp, 0))
            self._sendto(self._data_socket, response, endpoint)
        elif packet.count == 1:
            response = encode_sync(self._local_ssrc, 2, (packet.timestamps[0], packet.timestamps[1], stamp))
            self._sendto(self._data_socket, response, endpoint)
        self._touch()

    def _handle_rtp(self, data: bytes, endpoint: tuple[str, int]) -> tuple[int, int, int] | None:
        if self._state is not SessionState.CONNECTED or endpoint != self._data_endpoint:
            return None
        try:
            packet = decode_rtp_cc(data)
        except ValueError:
            return None
        if packet.ssrc != self._remote_ssrc or not self._sequence_is_new(packet.sequence):
            return None
        self._last_sequence = packet.sequence
        self._last_received = self._clock()
        self._touch()
        return packet.channel, packet.control, packet.value

    def _sequence_is_new(self, sequence: int) -> bool:
        if self._last_sequence is None:
            return True
        distance = (sequence - self._last_sequence) & 0xFFFF
        return 0 < distance <= _SEQUENCE_WINDOW

    def _advance_timers(self, now: float) -> None:
        if self._state is SessionState.CONNECTED:
            if self._last_received is not None and now - self._last_received >= _INACTIVITY_TIMEOUT:
                self._end_for_failure("Remote MIDI session timed out")
                return
            if self.role is RemoteMidiRole.RECEIVE and now >= self._next_sync:
                self._send_sync(now)
            return
        if (
            self.role is RemoteMidiRole.SEND
            and self._state is SessionState.INVITING_DATA
            and self._last_received is not None
            and now - self._last_received >= _INACTIVITY_TIMEOUT
        ):
            self._end_for_failure("Remote MIDI data invitation timed out")
            return
        if self.role is not RemoteMidiRole.RECEIVE or self._manual_disconnect or not self._available:
            return
        if self._state is SessionState.BACKOFF:
            if now < self._backoff_until:
                return
            self._state = SessionState.IDLE
        if self._state is SessionState.IDLE:
            if self._selected_peer() is not None:
                self._begin_invitation(now)
            return
        if self._state in {SessionState.INVITING_CONTROL, SessionState.INVITING_DATA} and now >= self._next_invitation:
            if self._invitation_tries >= _MAX_INVITATIONS:
                self._end_for_failure("Remote MIDI invitation timed out")
            else:
                self._send_current_invitation(now)

    def _selected_peer(self) -> PeerRecord | None:
        return self._peers.get(self._selected_peer_id) if self._selected_peer_id else None

    def _begin_invitation(self, now: float) -> None:
        peer = self._selected_peer()
        if peer is None:
            return
        self._clear_session()
        self._token = self._new_nonzero_u32()
        self._control_endpoint = (peer.host, peer.control_port)
        self._data_endpoint = (peer.host, peer.data_port)
        self._state = SessionState.INVITING_CONTROL
        logger.info(
            "Remote MIDI connection attempt: peer=%r id=%s host=%s control=%d data=%d",
            peer.name,
            peer.peer_id,
            peer.host,
            peer.control_port,
            peer.data_port,
        )
        self._send_current_invitation(now)
        self._touch()

    def _send_current_invitation(self, now: float) -> None:
        is_data = self._state is SessionState.INVITING_DATA
        udp_socket = self._data_socket if is_data else self._control_socket
        endpoint = self._data_endpoint if is_data else self._control_endpoint
        if udp_socket is None or endpoint is None:
            return
        packet = encode_invitation(ControlCommand.INVITATION, self._token, self._local_ssrc, self.advertised_name)
        self._sendto(udp_socket, packet, endpoint)
        self._invitation_tries += 1
        self._next_invitation = now + _INVITATION_INTERVAL

    def _mark_connected(self, now: float) -> None:
        self._state = SessionState.CONNECTED
        self._connected_peer_id = self._selected_peer_id if self.role is RemoteMidiRole.RECEIVE else None
        self._last_received = now
        self._next_sync = 0.0
        self._last_sequence = None
        self._reconnect_attempt = 0
        self._error = None
        logger.info(
            "Remote MIDI session connected: role=%s peer=%r control=%s data=%s",
            self.role.value,
            self._remote_name,
            self._control_endpoint,
            self._data_endpoint,
        )
        if self.role is RemoteMidiRole.RECEIVE:
            self._send_sync(now)
        self._touch()

    def _send_sync(self, now: float) -> None:
        if self._data_socket is None or self._data_endpoint is None:
            return
        stamp = int(now * 10_000) & 0xFFFFFFFFFFFFFFFF
        self._sendto(self._data_socket, encode_sync(self._local_ssrc, 0, (stamp, 0, 0)), self._data_endpoint)
        self._next_sync = now + _SYNC_INTERVAL

    def _flush_outgoing(self, now: float) -> None:
        if self._state is not SessionState.CONNECTED or self._data_socket is None or self._data_endpoint is None:
            return
        changed = False
        while self._outgoing:
            channel, control, value = self._outgoing[0]
            packet = encode_rtp_cc(
                self._sequence,
                int(now * 10_000) & 0xFFFFFFFF,
                self._local_ssrc,
                channel,
                control,
                value,
            )
            if not self._sendto(self._data_socket, packet, self._data_endpoint):
                break
            self._outgoing.popleft()
            self._sequence = (self._sequence + 1) & 0xFFFF
            changed = True
        if changed:
            self._touch()

    def _sendto(self, udp_socket: socket.socket, data: bytes, endpoint: tuple[str, int]) -> bool:
        try:
            return udp_socket.sendto(data, endpoint) == len(data)
        except (BlockingIOError, OSError) as exc:
            self._error = f"Remote MIDI send failed: {exc}"
            logger.debug("Remote MIDI UDP send failed to %s: %s", endpoint, exc)
            self._touch()
            return False

    def _end_for_failure(self, message: str, *, send_end: bool = True) -> None:
        logger.warning(
            "Remote MIDI session failed: role=%s peer=%r reason=%s",
            self.role.value,
            self._remote_name or self._selected_peer_name or "",
            message,
        )
        if send_end:
            self._send_end()
        self._drop_outgoing("session failed")
        self._clear_session()
        self._error = message
        if self.role is RemoteMidiRole.RECEIVE and self._selected_peer_id and not self._manual_disconnect:
            self._reconnect_attempt += 1
            base = min(_MAX_BACKOFF, float(2 ** min(self._reconnect_attempt - 1, 6)))
            jitter = 0.8 + min(max(self._random_float(), 0.0), 1.0) * 0.4
            self._backoff_until = self._clock() + min(_MAX_BACKOFF, base * jitter)
            self._state = SessionState.BACKOFF
        else:
            self._state = SessionState.IDLE if self._available else SessionState.UNAVAILABLE
        self._touch()

    def _send_end(self) -> None:
        if self._token == 0:
            return
        packet = encode_invitation(ControlCommand.END, self._token, self._local_ssrc)
        if self._control_socket is not None and self._control_endpoint is not None:
            self._sendto(self._control_socket, packet, self._control_endpoint)
        if self._data_socket is not None and self._data_endpoint is not None:
            self._sendto(self._data_socket, packet, self._data_endpoint)

    def _drop_outgoing(self, reason: str) -> None:
        dropped = len(self._outgoing)
        if not dropped:
            return
        self._outgoing.clear()
        self._dropped_count += dropped
        suffix = "" if dropped == 1 else "s"
        self._warning = f"Dropped {dropped} queued remote MIDI CC event{suffix}: {reason}"

    def _clear_session(self) -> None:
        self._token = 0
        self._remote_ssrc = None
        self._remote_name = ""
        self._control_endpoint = None
        self._data_endpoint = None
        self._connected_peer_id = None
        self._last_received = None
        self._last_sequence = None
        self._invitation_tries = 0
        self._next_invitation = 0.0

    def disconnect(self) -> TransportSnapshot:
        """Send BY and stop the current session until selection is explicitly renewed."""
        self._send_end()
        self._drop_outgoing("transport disconnected")
        self._clear_session()
        self._manual_disconnect = True
        self._state = SessionState.IDLE if self._available else SessionState.UNAVAILABLE
        self._touch()
        return self.snapshot

    def close(self) -> None:
        """Send BY, stop DNS-SD, and close both UDP sockets."""
        if self._closed:
            return
        logger.info("Remote MIDI transport closing: role=%s state=%s", self.role.value, self._state.value)
        self._send_end()
        self._drop_outgoing("transport closed")
        self._clear_session()
        if self._discovery is not None:
            try:
                self._discovery.close()
            except (OSError, RuntimeError) as exc:
                logger.debug("Remote MIDI discovery close failed: %s", exc)
            self._discovery = None
        self._close_udp_sockets()
        self._closed = True
        self._state = SessionState.CLOSED
        self._available = False
        self._touch()

    def __enter__(self) -> RemoteMidiTransport:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        del exc_type, exc_value, traceback
        self.close()
