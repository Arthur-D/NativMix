"""Nonblocking, poll-driven TCP transport for NativMix remote synchronization.

This module owns exactly one active TCP connection at a time (as either a
listening server or an outbound client), performs the hello/hello_ack
handshake, sends heartbeats, detects inactivity, and reconnects with
jittered exponential backoff. It never blocks indefinitely: callers drive
it by calling :meth:`TcpServerTransport.poll` / :meth:`TcpClientTransport.poll`
with an explicit, bounded timeout on their own event loop tick.

Sockets, wall-clock time, and randomness are all injectable so tests can
exercise handshake/backoff/heartbeat/timeout logic deterministically without
real sleeping, while still supporting real loopback TCP sockets for
end-to-end tests.

If the negotiated protocol/schema version is incompatible, or the peer
address/session/role does not match what is configured, this transport
represents that as "sync unavailable" (see :meth:`is_sync_available`) and
closes the connection — it never raises out of :meth:`poll` for a remote
peer's bad behavior, and it has no dependency on (and no effect on) the
MIDI layer.
"""

from __future__ import annotations

import errno
import random
import selectors
import socket
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from nativmix.remote_sync.protocol import (
    ALLOWED_ROLES,
    DecodeContext,
    FrameDecoder,
    FrameError,
    Hello,
    HelloAck,
    MalformedMessageError,
    Message,
    Ping,
    Pong,
    ProtocolError,
    RoleMismatchError,
    SessionMismatchError,
    VersionMismatchError,
    decode_message,
    encode_frame,
    encode_message,
)
from nativmix.remote_sync.schema import SchemaError
from nativmix.remote_sync.state import (
    MAX_PENDING_COMMANDS,
    PendingCommandTracker,
)

__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "INACTIVITY_TIMEOUT_SECONDS",
    "RECONNECT_MIN_SECONDS",
    "RECONNECT_MAX_SECONDS",
    "MAX_OUTBOUND_QUEUE",
    "MAX_INBOUND_QUEUE",
    "MAX_READ_BYTES_PER_POLL",
    "HANDSHAKE_TIMEOUT_SECONDS",
    "STATUS_RATE_LIMIT_SECONDS",
    "ConnectionStatus",
    "CloseReason",
    "StatusEvent",
    "OutboundQueueOverflowError",
    "SocketLike",
    "ReconnectBackoff",
    "TcpServerTransport",
    "TcpClientTransport",
]

#: Seconds between heartbeat (ping) sends on an idle, connected link.
HEARTBEAT_INTERVAL_SECONDS: float = 5.0

#: Seconds of no received traffic before a connected link is considered dead.
INACTIVITY_TIMEOUT_SECONDS: float = 15.0

#: Reconnect backoff bounds (client role only), with jitter in between.
RECONNECT_MIN_SECONDS: float = 0.5
RECONNECT_MAX_SECONDS: float = 30.0

#: Maximum number of not-yet-sent outbound frames buffered per connection.
MAX_OUTBOUND_QUEUE: int = 256

#: Maximum number of fully decoded, not-yet-drained inbound messages buffered
#: per connection. A peer sending faster than the caller drains messages via
#: ``drain_messages()`` overflows this bound and closes the connection.
MAX_INBOUND_QUEUE: int = 256

#: Maximum socket bytes consumed during one poll tick. Additional kernel
#: buffered data is handled on later ticks so a chatty peer cannot monopolize
#: the MIDI worker thread.
MAX_READ_BYTES_PER_POLL: int = 256 * 1024

#: Seconds a connection may remain in CONNECTING or HANDSHAKING before it is
#: closed as timed out. Each phase (CONNECTING then HANDSHAKING) gets its own
#: full budget; a server frees its accept slot and a client backs off.
HANDSHAKE_TIMEOUT_SECONDS: float = 5.0

#: Minimum interval between repeated structured status events of the same
#: (status, reason) pair, to avoid flooding a caller/log with duplicates.
STATUS_RATE_LIMIT_SECONDS: float = 1.0


class SocketLike(Protocol):
    """The minimal socket surface this transport depends on (for fakes)."""

    def fileno(self) -> int: ...

    def setblocking(self, flag: bool) -> None: ...

    def bind(self, address: tuple[str, int]) -> None: ...

    def connect_ex(self, address: tuple[str, int]) -> int: ...

    def send(self, data: bytes) -> int: ...

    def recv(self, bufsize: int) -> bytes: ...

    def close(self) -> None: ...

    def getpeername(self) -> tuple[str, int]: ...

    def setsockopt(self, level: int, optname: int, value: int) -> None: ...

    def getsockopt(self, level: int, optname: int) -> int: ...


class ListenerLike(Protocol):
    """The minimal listening-socket surface this transport depends on."""

    def fileno(self) -> int: ...

    def setblocking(self, flag: bool) -> None: ...

    def bind(self, address: tuple[str, int]) -> None: ...

    def listen(self, backlog: int) -> None: ...

    def accept(self) -> tuple[SocketLike, tuple[str, int]]: ...

    def getsockname(self) -> tuple[str, int]: ...

    def close(self) -> None: ...

    def setsockopt(self, level: int, optname: int, value: int) -> None: ...


class ConnectionStatus(str, Enum):
    """Lifecycle state of a transport's single managed connection."""

    IDLE = "idle"
    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    CONNECTED = "connected"
    CLOSED = "closed"


class CloseReason(str, Enum):
    """Exact reason a connection was closed (also used for status events)."""

    NONE = "none"
    LOCAL_CLOSE = "local_close"
    PEER_CLOSED = "peer_closed"
    MALFORMED_MESSAGE = "malformed_message"
    SESSION_MISMATCH = "session_mismatch"
    ADDRESS_REJECTED = "address_rejected"
    ROLE_MISMATCH = "role_mismatch"
    QUEUE_OVERFLOW = "queue_overflow"
    INACTIVITY_TIMEOUT = "inactivity_timeout"
    SOCKET_ERROR = "socket_error"
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
    HANDSHAKE_REJECTED = "handshake_rejected"
    HANDSHAKE_TIMEOUT = "handshake_timeout"


@dataclass(frozen=True)
class StatusEvent:
    """A single structured status report emitted by the transport."""

    timestamp: float
    status: ConnectionStatus
    reason: CloseReason
    detail: str


class OutboundQueueOverflowError(RuntimeError):
    """Raised internally when the bounded outbound queue would overflow."""


def _default_socket_factory() -> SocketLike:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    return sock


def _default_listener_factory() -> ListenerLike:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setblocking(False)
    return sock


class ReconnectBackoff:
    """Full-jitter exponential backoff bounded to [minimum, maximum] seconds."""

    def __init__(
        self,
        *,
        minimum: float = RECONNECT_MIN_SECONDS,
        maximum: float = RECONNECT_MAX_SECONDS,
        rng: random.Random | None = None,
    ) -> None:
        if minimum <= 0 or maximum < minimum:
            raise ValueError("invalid backoff bounds")
        self._minimum = minimum
        self._maximum = maximum
        self._rng = rng if rng is not None else random.Random()
        self._attempt = 0

    def reset(self) -> None:
        self._attempt = 0

    def next_delay(self) -> float:
        """Return the next jittered delay, bounded to [minimum, maximum]."""
        self._attempt += 1
        cap = min(self._maximum, self._minimum * (2 ** (self._attempt - 1)))
        if cap <= self._minimum:
            return self._minimum
        return self._rng.uniform(self._minimum, cap)


class _StatusRateLimiter:
    """Suppresses repeated identical (status, reason) events within a window."""

    def __init__(self, min_interval: float = STATUS_RATE_LIMIT_SECONDS) -> None:
        self._min_interval = min_interval
        self._last_emitted: dict[tuple[ConnectionStatus, CloseReason], float] = {}

    def allow(self, now: float, status: ConnectionStatus, reason: CloseReason) -> bool:
        key = (status, reason)
        last = self._last_emitted.get(key)
        if last is not None and (now - last) < self._min_interval:
            return False
        self._last_emitted[key] = now
        return True


@dataclass
class _RecvBuffer:
    decoder: FrameDecoder = field(default_factory=FrameDecoder)


class _BaseTransport:
    """Shared connection-management logic for server and client transports."""

    def __init__(
        self,
        *,
        local_role: str,
        instance_id: str | None,
        session_token: str,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        if local_role not in ALLOWED_ROLES:
            raise ValueError(f"local_role must be one of {sorted(ALLOWED_ROLES)}")
        self._local_role = local_role
        self._instance_id = instance_id if instance_id is not None else str(uuid.uuid4())
        self._session_token = session_token
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()

        self._sock: SocketLike | None = None
        self._status = ConnectionStatus.IDLE
        self._close_reason = CloseReason.NONE
        self._recv = _RecvBuffer()
        self._outbound: deque[bytes] = deque()
        self._send_buffer = b""
        self._transport_session_id: str | None = None
        self._peer_instance_id: str | None = None
        self._last_sent_at: float = 0.0
        self._last_received_at: float = 0.0
        self._status_events: list[StatusEvent] = []
        self._rate_limiter = _StatusRateLimiter()
        self.pending_commands = PendingCommandTracker(max_size=MAX_PENDING_COMMANDS)
        self._events_this_poll: list[Message] = []
        self._handshake_deadline: float | None = None
        self._selector = selectors.DefaultSelector()

    # -- public introspection -------------------------------------------------

    @property
    def status(self) -> ConnectionStatus:
        return self._status

    @property
    def last_close_reason(self) -> CloseReason:
        return self._close_reason

    @property
    def transport_session_id(self) -> str | None:
        return self._transport_session_id

    @property
    def peer_instance_id(self) -> str | None:
        """Stable peer UUID from the completed hello exchange."""
        return self._peer_instance_id

    def is_sync_available(self) -> bool:
        """True only while a fully handshaked connection is active."""
        return self._status == ConnectionStatus.CONNECTED

    def drain_status_events(self) -> list[StatusEvent]:
        """Return and clear all status events queued since the last call."""
        events = self._status_events
        self._status_events = []
        return events

    def drain_messages(self) -> list[Message]:
        """Return and clear all fully decoded, non-transport-internal messages."""
        events = self._events_this_poll
        self._events_this_poll = []
        return events

    # -- outbound queue --------------------------------------------------------

    def send_message(self, message: Message) -> bool:
        """Enqueue *message* for sending. Returns False (and closes) on failure.

        Any failure to encode *message* (malformed framing, a schema
        violation such as a non-finite number or an out-of-range value, or a
        Unicode error from an unencodable string) must never propagate out of
        this transport into the caller's (e.g. MIDI) layer. It is reported as
        a sync-only close instead of raised.
        """
        try:
            frame = encode_frame(encode_message(message))
        except (FrameError, SchemaError, UnicodeError) as exc:
            self._close(CloseReason.MALFORMED_MESSAGE, f"failed to encode outbound message: {exc}")
            return False
        if len(self._outbound) >= MAX_OUTBOUND_QUEUE:
            self._close(CloseReason.QUEUE_OVERFLOW, "outbound queue overflow")
            return False
        self._outbound.append(frame)
        return True

    # -- shared helpers used by subclasses -------------------------------------

    def _report(self, status: ConnectionStatus, reason: CloseReason, detail: str) -> None:
        now = self._clock()
        if self._rate_limiter.allow(now, status, reason):
            self._status_events.append(StatusEvent(timestamp=now, status=status, reason=reason, detail=detail))

    def _close(self, reason: CloseReason, detail: str) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._status = ConnectionStatus.CLOSED
        self._close_reason = reason
        self._transport_session_id = None
        self._peer_instance_id = None
        self._outbound.clear()
        self._send_buffer = b""
        self._recv = _RecvBuffer()
        self._handshake_deadline = None
        self._report(ConnectionStatus.CLOSED, reason, detail)

    def _flush_outbound(self) -> None:
        if self._sock is None:
            return
        while self._send_buffer or self._outbound:
            if not self._send_buffer:
                self._send_buffer = self._outbound.popleft()
            try:
                sent = self._sock.send(self._send_buffer)
            except BlockingIOError:
                return
            except OSError as exc:
                self._close(CloseReason.SOCKET_ERROR, f"send failed: {exc}")
                return
            self._send_buffer = self._send_buffer[sent:]
            if self._send_buffer:
                return

    def _read_available(self) -> bytes | None:
        """Read all currently available bytes; None on orderly peer close."""
        if self._sock is None:
            return b""
        chunks: list[bytes] = []
        remaining = MAX_READ_BYTES_PER_POLL
        while remaining:
            try:
                chunk = self._sock.recv(min(65536, remaining))
            except BlockingIOError:
                break
            except OSError as exc:
                self._close(CloseReason.SOCKET_ERROR, f"recv failed: {exc}")
                return b""
            if chunk == b"":
                if not chunks:
                    return None
                break
            chunks.append(chunk)
            remaining -= len(chunk)
            if len(chunk) < 65536:
                break
        return b"".join(chunks)

    def _process_incoming(self, ctx: DecodeContext) -> None:
        if self._sock is None:
            return
        data = self._read_available()
        if data is None:
            self._close(CloseReason.PEER_CLOSED, "peer closed connection")
            return
        if not data:
            return
        self._last_received_at = self._clock()
        try:
            frames = self._recv.decoder.feed(data)
        except FrameError as exc:
            self._close(CloseReason.MALFORMED_MESSAGE, f"framing error: {exc}")
            return
        for frame in frames:
            try:
                message = decode_message(frame, ctx)
            except VersionMismatchError as exc:
                self._close(CloseReason.PROTOCOL_INCOMPATIBLE, str(exc))
                return
            except SessionMismatchError as exc:
                self._close(CloseReason.SESSION_MISMATCH, str(exc))
                return
            except RoleMismatchError as exc:
                self._close(CloseReason.ROLE_MISMATCH, str(exc))
                return
            except (MalformedMessageError, ProtocolError) as exc:
                self._close(CloseReason.MALFORMED_MESSAGE, str(exc))
                return
            self._handle_message(message)
            if self._status == ConnectionStatus.CLOSED:
                return

    def _handle_message(self, message: Message) -> None:
        if isinstance(message, Ping):
            self.send_message(
                Pong(
                    protocol_version=message.protocol_version,
                    schema_version=message.schema_version,
                    transport_session_id=message.transport_session_id,
                    ping_id=message.ping_id,
                )
            )
            return
        if isinstance(message, Pong):
            return
        if len(self._events_this_poll) >= MAX_INBOUND_QUEUE:
            self._close(CloseReason.QUEUE_OVERFLOW, "inbound message queue overflow")
            return
        self._events_this_poll.append(message)

    def _enforce_handshake_timeout(self) -> None:
        """Close a connection stuck in CONNECTING/HANDSHAKING past its deadline.

        A server frees its accept slot (the connection closes and a new peer
        may be accepted); a client's own :meth:`_close` override schedules a
        backoff retry. Neither role ever waits indefinitely for a handshake.
        """
        if self._status not in (ConnectionStatus.CONNECTING, ConnectionStatus.HANDSHAKING):
            return
        if self._handshake_deadline is None:
            return
        if self._clock() >= self._handshake_deadline:
            self._close(CloseReason.HANDSHAKE_TIMEOUT, "handshake did not complete within timeout")

    def _select_once(
        self, registrations: list[tuple[int, int, str]], timeout: float
    ) -> list[tuple[str, int]]:
        """Register *registrations* on the shared selector for one poll tick.

        File descriptors are registered immediately before the blocking
        ``select`` call and unregistered immediately after (in a ``finally``
        block), because descriptor numbers are reused by the OS across
        reconnects and must never be left dangling on the persistent
        selector between ticks. Passing an empty list still blocks for up to
        *timeout* seconds without spinning, which is used to honor a
        caller-supplied poll timeout while backing off with no socket open.
        """
        for fd, mask, data in registrations:
            self._selector.register(fd, mask, data=data)
        try:
            events = self._selector.select(timeout=timeout)
            return [(key.data, mask) for key, mask in events]
        finally:
            for fd, _mask, _data in registrations:
                self._selector.unregister(fd)

    def _heartbeat_and_timeout(self, ctx_session_id: str) -> None:
        if self._status != ConnectionStatus.CONNECTED:
            return
        now = self._clock()
        if now - self._last_received_at >= INACTIVITY_TIMEOUT_SECONDS:
            self._close(CloseReason.INACTIVITY_TIMEOUT, "no traffic within inactivity timeout")
            return
        if now - self._last_sent_at >= HEARTBEAT_INTERVAL_SECONDS:
            self._last_sent_at = now
            self.send_message(
                Ping(
                    protocol_version=self._protocol_version(),
                    schema_version=self._schema_version(),
                    transport_session_id=ctx_session_id,
                    ping_id=str(uuid.uuid4()),
                )
            )

    @staticmethod
    def _protocol_version() -> int:
        from nativmix.remote_sync.protocol import PROTOCOL_VERSION

        return PROTOCOL_VERSION

    @staticmethod
    def _schema_version() -> int:
        from nativmix.remote_sync.schema import SCHEMA_VERSION

        return SCHEMA_VERSION

    def close(self) -> None:
        """Close the active connection (if any) with reason LOCAL_CLOSE."""
        if self._sock is not None:
            self._close(CloseReason.LOCAL_CLOSE, "closed locally")
        self._selector.close()


class TcpServerTransport(_BaseTransport):
    """Listens for and accepts exactly one connection from an allowlisted peer."""

    def __init__(
        self,
        *,
        bind_address: tuple[str, int],
        allowed_peer_host: str = "",
        instance_id: str | None = None,
        session_token: str = "",
        expected_peer_instance_id: str | None = None,
        socket_factory: Callable[[], SocketLike] | None = None,
        listener_factory: Callable[[], ListenerLike] | None = None,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(
            local_role="controller", instance_id=instance_id, session_token=session_token, clock=clock, rng=rng
        )
        self._allowed_peer_host = allowed_peer_host
        self._expected_peer_instance_id = expected_peer_instance_id
        self._socket_factory = socket_factory if socket_factory is not None else _default_socket_factory
        listener_factory = listener_factory if listener_factory is not None else _default_listener_factory
        self._listener: ListenerLike = listener_factory()
        self._listener.bind(bind_address)
        self._listener.listen(1)

    def listening_address(self) -> tuple[str, int]:
        return self._listener.getsockname()

    def set_active_peer(self, host: str, instance_id: str | None = None) -> None:
        """Restrict acceptance to the active AppleMIDI peer."""
        normalized_id = str(uuid.UUID(instance_id)) if instance_id is not None else None
        if self._sock is not None and (
            host != self._allowed_peer_host
            or (
                normalized_id is not None
                and self._peer_instance_id is not None
                and normalized_id != self._peer_instance_id
            )
        ):
            self._close(CloseReason.ADDRESS_REJECTED, "active MIDI peer changed")
        self._allowed_peer_host = host
        self._expected_peer_instance_id = normalized_id

    def poll(self, timeout: float) -> None:
        """Advance the server one tick, waiting at most *timeout* seconds."""
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        self._enforce_handshake_timeout()
        registrations: list[tuple[int, int, str]] = [
            (self._listener.fileno(), selectors.EVENT_READ, "listener")
        ]
        if self._sock is not None:
            mask = selectors.EVENT_READ
            if self._send_buffer or self._outbound:
                mask |= selectors.EVENT_WRITE
            registrations.append((self._sock.fileno(), mask, "conn"))
        for data, _mask in self._select_once(registrations, timeout):
            if data == "listener":
                self._accept_if_possible()
            elif data == "conn":
                self._flush_outbound()
                if self._sock is not None:
                    self._process_incoming(self._decode_ctx())
        self._heartbeat_and_timeout(self._transport_session_id or "")

    def _decode_ctx(self) -> DecodeContext:
        return DecodeContext(expected_transport_session_id=self._transport_session_id, local_role=self._local_role)

    def _accept_if_possible(self) -> None:
        try:
            new_sock, addr = self._listener.accept()
        except (BlockingIOError, OSError):
            return
        if self._sock is not None:
            # Only one connection is supported at a time.
            try:
                new_sock.close()
            except OSError:
                pass
            self._report(ConnectionStatus.CONNECTED, CloseReason.ADDRESS_REJECTED, "rejected concurrent connection")
            return
        peer_host = addr[0]
        if peer_host != self._allowed_peer_host:
            try:
                new_sock.close()
            except OSError:
                pass
            self._report(ConnectionStatus.IDLE, CloseReason.ADDRESS_REJECTED, f"rejected peer address {peer_host!r}")
            return
        new_sock.setblocking(False)
        try:
            new_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._sock = new_sock
        self._status = ConnectionStatus.HANDSHAKING
        now = self._clock()
        self._last_received_at = now
        self._last_sent_at = now
        self._handshake_deadline = now + HANDSHAKE_TIMEOUT_SECONDS
        self._transport_session_id = str(uuid.uuid4())
        self._report(ConnectionStatus.HANDSHAKING, CloseReason.NONE, "accepted connection, awaiting hello")

    def _handle_message(self, message: Message) -> None:
        if isinstance(message, Hello) and self._status == ConnectionStatus.HANDSHAKING:
            if message.session_token != self._session_token:
                self._close(CloseReason.SESSION_MISMATCH, "hello session token does not match advertisement")
                return
            if (
                self._expected_peer_instance_id is not None
                and message.instance_id != self._expected_peer_instance_id
            ):
                self._close(CloseReason.SESSION_MISMATCH, "hello instance ID does not match active peer")
                return
            self._peer_instance_id = message.instance_id
            self._status = ConnectionStatus.CONNECTED
            self._handshake_deadline = None
            self.send_message(
                HelloAck(
                    protocol_version=message.protocol_version,
                    schema_version=message.schema_version,
                    role=self._local_role,
                    instance_id=self._instance_id,
                    transport_session_id=self._transport_session_id or "",
                    accepted=True,
                    reason=None,
                )
            )
            self._report(ConnectionStatus.CONNECTED, CloseReason.NONE, "handshake complete")
            return
        super()._handle_message(message)

    def close(self) -> None:
        """Close the active connection and the listening socket."""
        super().close()
        try:
            self._listener.close()
        except OSError:
            pass


class TcpClientTransport(_BaseTransport):
    """Connects (and reconnects, with jittered backoff) to a single server."""

    def __init__(
        self,
        *,
        server_address: tuple[str, int],
        instance_id: str | None = None,
        session_token: str = "",
        expected_server_instance_id: str | None = None,
        source_address: tuple[str, int] | None = None,
        socket_factory: Callable[[], SocketLike] | None = None,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        backoff: ReconnectBackoff | None = None,
    ) -> None:
        super().__init__(
            local_role="receiver", instance_id=instance_id, session_token=session_token, clock=clock, rng=rng
        )
        self._server_address = server_address
        self._source_address = source_address
        self._expected_server_instance_id = (
            str(uuid.UUID(expected_server_instance_id)) if expected_server_instance_id is not None else None
        )
        self._socket_factory = socket_factory if socket_factory is not None else _default_socket_factory
        self._backoff = backoff if backoff is not None else ReconnectBackoff(rng=self._rng)
        self._next_attempt_at: float = 0.0
        self._hello_sent = False

    def poll(self, timeout: float) -> None:
        """Advance the client one tick, waiting at most *timeout* seconds."""
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        self._enforce_handshake_timeout()
        now = self._clock()
        if self._sock is None and self._status != ConnectionStatus.CONNECTING:
            if now >= self._next_attempt_at:
                self._begin_connect()
            if self._sock is None:
                # Still waiting out the backoff window: honor the caller's
                # timeout with a real bounded wait (no registered file
                # descriptors) instead of returning immediately, which would
                # let a tight caller polling loop busy-spin the CPU.
                wait_for = min(timeout, max(0.0, self._next_attempt_at - self._clock()))
                if wait_for > 0:
                    self._select_once([], wait_for)
            self._heartbeat_and_timeout(self._transport_session_id or "")
            return

        if self._sock is not None:
            mask = selectors.EVENT_READ | selectors.EVENT_WRITE
            for _data, _mask in self._select_once([(self._sock.fileno(), mask, "conn")], timeout):
                if self._status == ConnectionStatus.CONNECTING:
                    self._complete_connect()
                else:
                    self._flush_outbound()
                    if self._sock is not None:
                        self._process_incoming(self._decode_ctx())
        self._heartbeat_and_timeout(self._transport_session_id or "")

    def _decode_ctx(self) -> DecodeContext:
        return DecodeContext(expected_transport_session_id=self._transport_session_id, local_role=self._local_role)

    def _begin_connect(self) -> None:
        sock = self._socket_factory()
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        if self._source_address is not None:
            try:
                sock.bind(self._source_address)
            except OSError as exc:
                try:
                    sock.close()
                except OSError:
                    pass
                self._schedule_retry(f"source bind {self._source_address[0]} failed: {exc}")
                return
        err = sock.connect_ex(self._server_address)
        # EINPROGRESS (or EWOULDBLOCK on some platforms) is expected for a
        # nonblocking connect; anything else is treated as an immediate
        # failure to retry with backoff.
        if err not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK):
            try:
                sock.close()
            except OSError:
                pass
            self._schedule_retry(f"connect failed immediately: errno {err}")
            return
        self._sock = sock
        self._status = ConnectionStatus.CONNECTING
        self._hello_sent = False
        self._handshake_deadline = self._clock() + HANDSHAKE_TIMEOUT_SECONDS
        self._report(ConnectionStatus.CONNECTING, CloseReason.NONE, "connecting")

    def _complete_connect(self) -> None:
        if self._sock is None:
            return
        try:
            error = self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        except (OSError, AttributeError):
            error = 0
        if error != 0:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._schedule_retry(f"connect failed: errno {error}")
            return
        now = self._clock()
        self._last_received_at = now
        self._last_sent_at = now
        self._status = ConnectionStatus.HANDSHAKING
        self._handshake_deadline = now + HANDSHAKE_TIMEOUT_SECONDS
        self._backoff.reset()
        self._report(ConnectionStatus.HANDSHAKING, CloseReason.NONE, "connected, sending hello")
        self.send_message(
            Hello(
                protocol_version=self._protocol_version(),
                schema_version=self._schema_version(),
                role=self._local_role,
                instance_id=self._instance_id,
                session_token=self._session_token,
            )
        )
        self._hello_sent = True
        self._flush_outbound()

    def _schedule_retry(self, detail: str) -> None:
        delay = self._backoff.next_delay()
        self._next_attempt_at = self._clock() + delay
        self._status = ConnectionStatus.IDLE
        self._handshake_deadline = None
        self._report(ConnectionStatus.IDLE, CloseReason.SOCKET_ERROR, f"{detail}; retrying in {delay:.2f}s")

    def _close(self, reason: CloseReason, detail: str) -> None:
        super()._close(reason, detail)
        delay = self._backoff.next_delay()
        self._next_attempt_at = self._clock() + delay

    def _handle_message(self, message: Message) -> None:
        if isinstance(message, HelloAck) and self._status == ConnectionStatus.HANDSHAKING:
            if not message.accepted:
                self._close(CloseReason.HANDSHAKE_REJECTED, message.reason or "handshake rejected by server")
                return
            if (
                self._expected_server_instance_id is not None
                and message.instance_id != self._expected_server_instance_id
            ):
                self._close(CloseReason.SESSION_MISMATCH, "hello_ack instance ID does not match selected peer")
                return
            self._peer_instance_id = message.instance_id
            self._transport_session_id = message.transport_session_id
            self._status = ConnectionStatus.CONNECTED
            self._handshake_deadline = None
            self._report(ConnectionStatus.CONNECTED, CloseReason.NONE, "handshake complete")
            return
        super()._handle_message(message)
