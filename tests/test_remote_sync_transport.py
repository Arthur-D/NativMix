"""Tests for nativmix.remote_sync.transport: the nonblocking, poll-driven TCP
transport (server + client), heartbeat/inactivity handling, jittered
reconnect backoff, bounded outbound queue, and closing on malformed/session/
address mismatches — including full loopback TCP handshakes.
"""

from __future__ import annotations

import random
import socket
import time
import uuid
from collections.abc import Callable

import pytest

from nativmix.remote_sync import protocol as p
from nativmix.remote_sync import transport as t


def _uuid() -> str:
    return str(uuid.uuid4())


class ManualClock:
    """A controllable fake clock shared by both endpoints in a test."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, delta: float) -> None:
        self.value += delta


class _NeverReadySocket:
    """Minimal stand-in used only to force a transport into CONNECTING/
    HANDSHAKING for handshake-timeout tests, without needing a real socket
    that would otherwise complete its handshake almost instantly on
    loopback. Only ``close()`` is exercised by the code paths under test.
    """

    def close(self) -> None:
        pass


def _make_command(session_id: str, *, command_type: str = "noop") -> p.CommandMessage:
    return p.CommandMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        control_session_id=_uuid(),
        command_id=_uuid(),
        receiver_epoch=_uuid(),
        expected_revision=0,
        command_type=command_type,
        payload={},
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_until(condition: Callable[[], bool], *transports: object, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for tr in transports:
            tr.poll(0.01)  # type: ignore[attr-defined]
        if condition():
            return True
    return condition()


# --------------------------------------------------------------------------
# ReconnectBackoff: bounds and jitter
# --------------------------------------------------------------------------


def test_reconnect_backoff_stays_within_bounds() -> None:
    backoff = t.ReconnectBackoff(minimum=0.5, maximum=30.0, rng=random.Random(1234))
    for _ in range(20):
        delay = backoff.next_delay()
        assert 0.5 <= delay <= 30.0


def test_reconnect_backoff_grows_then_caps() -> None:
    backoff = t.ReconnectBackoff(minimum=0.5, maximum=30.0, rng=random.Random(7))
    delays = [backoff.next_delay() for _ in range(12)]
    # Eventually the cap must be reached/held (later delays can hit the max).
    assert max(delays) <= 30.0
    assert any(d > 5.0 for d in delays[-3:])


def test_reconnect_backoff_reset_restarts_growth() -> None:
    backoff = t.ReconnectBackoff(minimum=0.5, maximum=30.0, rng=random.Random(0))
    for _ in range(10):
        backoff.next_delay()
    backoff.reset()
    first_after_reset = backoff.next_delay()
    assert 0.5 <= first_after_reset <= 30.0


def test_reconnect_backoff_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="invalid backoff bounds"):
        t.ReconnectBackoff(minimum=0.0, maximum=1.0)
    with pytest.raises(ValueError, match="invalid backoff bounds"):
        t.ReconnectBackoff(minimum=5.0, maximum=1.0)


def test_reconnect_backoff_deterministic_with_seeded_rng() -> None:
    backoff_a = t.ReconnectBackoff(rng=random.Random(42))
    backoff_b = t.ReconnectBackoff(rng=random.Random(42))
    seq_a = [backoff_a.next_delay() for _ in range(5)]
    seq_b = [backoff_b.next_delay() for _ in range(5)]
    assert seq_a == seq_b


# --------------------------------------------------------------------------
# Bounded outbound queue: overflow closes the connection
# --------------------------------------------------------------------------


def test_outbound_queue_overflow_closes_connection() -> None:
    clock = ManualClock()
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok", clock=clock)
    session_id = _uuid()
    for _ in range(t.MAX_OUTBOUND_QUEUE):
        ping = p.Ping(protocol_version=1, schema_version=1, transport_session_id=session_id, ping_id=_uuid())
        assert client.send_message(ping) is True
    overflow_ping = p.Ping(protocol_version=1, schema_version=1, transport_session_id=session_id, ping_id=_uuid())
    assert client.send_message(overflow_ping) is False
    assert client.last_close_reason == t.CloseReason.QUEUE_OVERFLOW
    events = client.drain_status_events()
    assert any(e.reason == t.CloseReason.QUEUE_OVERFLOW for e in events)


def test_outbound_queue_accepts_up_to_but_not_beyond_the_limit() -> None:
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok")
    session_id = _uuid()

    def make() -> p.Ping:
        return p.Ping(protocol_version=1, schema_version=1, transport_session_id=session_id, ping_id=_uuid())

    for i in range(t.MAX_OUTBOUND_QUEUE - 1):
        assert client.send_message(make()) is True, f"failed at {i}"
    # The (MAX_OUTBOUND_QUEUE)th message still fits exactly at capacity.
    assert client.send_message(make()) is True
    assert client.send_message(make()) is False


def test_socket_reads_are_bounded_per_poll_tick() -> None:
    class FloodSocket:
        def __init__(self) -> None:
            self.calls = 0

        def recv(self, size: int) -> bytes:
            self.calls += 1
            return b"x" * size

        def close(self) -> None:
            return

    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok")
    flood = FloodSocket()
    client._sock = flood  # type: ignore[assignment]  # noqa: SLF001
    try:
        data = client._read_available()  # noqa: SLF001
        assert data is not None
        assert len(data) == t.MAX_READ_BYTES_PER_POLL
        assert flood.calls == t.MAX_READ_BYTES_PER_POLL // 65536
    finally:
        client.close()


# --------------------------------------------------------------------------
# Status event rate limiting
# --------------------------------------------------------------------------


def test_status_events_are_rate_limited_for_repeated_identical_reason() -> None:
    clock = ManualClock()
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok", clock=clock)
    client._report(t.ConnectionStatus.IDLE, t.CloseReason.SOCKET_ERROR, "first")
    client._report(t.ConnectionStatus.IDLE, t.CloseReason.SOCKET_ERROR, "second-suppressed")
    events = client.drain_status_events()
    assert len(events) == 1
    assert events[0].detail == "first"
    clock.advance(t.STATUS_RATE_LIMIT_SECONDS + 0.1)
    client._report(t.ConnectionStatus.IDLE, t.CloseReason.SOCKET_ERROR, "third-allowed")
    events = client.drain_status_events()
    assert len(events) == 1
    assert events[0].detail == "third-allowed"


# --------------------------------------------------------------------------
# Real loopback TCP: full handshake, either startup ordering
# --------------------------------------------------------------------------


def test_loopback_handshake_server_starts_first() -> None:
    server = t.TcpServerTransport(bind_address=("127.0.0.1", 0), allowed_peer_host="127.0.0.1", session_token="tok")
    client = t.TcpClientTransport(server_address=server.listening_address(), session_token="tok")
    try:
        connected = _wait_until(lambda: server.is_sync_available() and client.is_sync_available(), server, client)
        assert connected
        assert server.transport_session_id == client.transport_session_id
    finally:
        client.close()
        server.close()


def test_loopback_handshake_client_starts_first() -> None:
    port = _free_port()
    clock = ManualClock()
    client = t.TcpClientTransport(server_address=("127.0.0.1", port), session_token="tok", clock=clock)
    server: t.TcpServerTransport | None = None
    try:
        # Client attempts to connect before any server is listening; it must
        # fail without raising and schedule a retry.
        for _ in range(5):
            client.poll(0.01)
        assert client.status in (t.ConnectionStatus.IDLE, t.ConnectionStatus.CONNECTING)
        assert not client.is_sync_available()

        # Skip the (fake-clock-controlled) backoff wait instantly, then start
        # the server bound to the same address the client is targeting.
        clock.advance(35.0)
        server = t.TcpServerTransport(
            bind_address=("127.0.0.1", port), allowed_peer_host="127.0.0.1", session_token="tok"
        )

        def _connected() -> bool:
            return server is not None and server.is_sync_available() and client.is_sync_available()

        connected = _wait_until(_connected, server, client)
        assert connected
    finally:
        client.close()
        if server is not None:
            server.close()


def test_loopback_only_one_active_connection_accepted() -> None:
    server = t.TcpServerTransport(bind_address=("127.0.0.1", 0), allowed_peer_host="127.0.0.1", session_token="tok")
    addr = server.listening_address()
    client1 = t.TcpClientTransport(server_address=addr, session_token="tok")
    try:
        assert _wait_until(lambda: server.is_sync_available() and client1.is_sync_available(), server, client1)

        # A second concurrent client must not disturb the first connection.
        client2 = t.TcpClientTransport(server_address=addr, session_token="tok")
        try:
            for _ in range(20):
                server.poll(0.01)
                client2.poll(0.01)
            assert server.is_sync_available()
            assert client1.is_sync_available()
            assert not client2.is_sync_available()
        finally:
            client2.close()
    finally:
        client1.close()
        server.close()


# --------------------------------------------------------------------------
# Wrong peer address rejected by server
# --------------------------------------------------------------------------


def test_server_rejects_non_allowlisted_peer_address() -> None:
    server = t.TcpServerTransport(bind_address=("127.0.0.1", 0), allowed_peer_host="10.10.10.10", session_token="tok")
    client = t.TcpClientTransport(server_address=server.listening_address(), session_token="tok")
    try:
        deadline = time.monotonic() + 3.0
        saw_rejection = False
        while time.monotonic() < deadline and not saw_rejection:
            server.poll(0.02)
            client.poll(0.02)
            saw_rejection = any(e.reason == t.CloseReason.ADDRESS_REJECTED for e in server.drain_status_events())
        assert saw_rejection
        assert not server.is_sync_available()
        assert not client.is_sync_available()
    finally:
        client.close()
        server.close()


# --------------------------------------------------------------------------
# AppleMIDI peer/session binding
# --------------------------------------------------------------------------


def test_server_listener_rejects_until_midi_peer_is_active() -> None:
    server = t.TcpServerTransport(bind_address=("127.0.0.1", 0), session_token="tok")
    client = t.TcpClientTransport(server_address=server.listening_address(), session_token="tok")
    try:
        for _ in range(20):
            server.poll(0.01)
            client.poll(0.01)
        assert not server.is_sync_available()
        assert any(event.reason == t.CloseReason.ADDRESS_REJECTED for event in server.drain_status_events())
    finally:
        client.close()
        server.close()


def test_advertised_session_and_stable_instance_are_required() -> None:
    server_id = _uuid()
    receiver_id = _uuid()
    server = t.TcpServerTransport(
        bind_address=("127.0.0.1", 0),
        allowed_peer_host="127.0.0.1",
        instance_id=server_id,
        session_token="expected",
        expected_peer_instance_id=receiver_id,
    )
    wrong_token = t.TcpClientTransport(
        server_address=server.listening_address(),
        instance_id=receiver_id,
        session_token="wrong",
        expected_server_instance_id=server_id,
    )
    try:
        for _ in range(30):
            server.poll(0.01)
            wrong_token.poll(0.01)
            if server.last_close_reason == t.CloseReason.SESSION_MISMATCH:
                break
        assert server.last_close_reason == t.CloseReason.SESSION_MISMATCH
        assert not server.is_sync_available()
    finally:
        wrong_token.close()
        server.close()

    server = t.TcpServerTransport(
        bind_address=("127.0.0.1", 0),
        allowed_peer_host="127.0.0.1",
        instance_id=server_id,
        session_token="expected",
    )
    wrong_server = t.TcpClientTransport(
        server_address=server.listening_address(),
        instance_id=receiver_id,
        session_token="expected",
        expected_server_instance_id=_uuid(),
    )
    try:
        assert _wait_until(
            lambda: wrong_server.last_close_reason == t.CloseReason.SESSION_MISMATCH,
            server,
            wrong_server,
        )
        assert not wrong_server.is_sync_available()
    finally:
        wrong_server.close()
        server.close()


# --------------------------------------------------------------------------
# Session mismatch closes the connection
# --------------------------------------------------------------------------


def test_session_mismatch_message_closes_connection() -> None:
    server = t.TcpServerTransport(bind_address=("127.0.0.1", 0), allowed_peer_host="127.0.0.1", session_token="tok")
    client = t.TcpClientTransport(server_address=server.listening_address(), session_token="tok")
    try:
        assert _wait_until(lambda: server.is_sync_available() and client.is_sync_available(), server, client)

        forged = p.Ping(protocol_version=1, schema_version=1, transport_session_id=_uuid(), ping_id=_uuid())
        assert client.send_message(forged) is True

        deadline = time.monotonic() + 3.0
        saw_mismatch = False
        while time.monotonic() < deadline and not saw_mismatch:
            client.poll(0.02)
            server.poll(0.02)
            saw_mismatch = any(e.reason == t.CloseReason.SESSION_MISMATCH for e in server.drain_status_events())
        assert saw_mismatch
        assert not server.is_sync_available()
    finally:
        client.close()
        server.close()


# --------------------------------------------------------------------------
# Malformed message closes connection
# --------------------------------------------------------------------------


def test_malformed_frame_closes_connection() -> None:
    server = t.TcpServerTransport(bind_address=("127.0.0.1", 0), allowed_peer_host="127.0.0.1", session_token="tok")
    client = t.TcpClientTransport(server_address=server.listening_address(), session_token="tok")
    try:
        assert _wait_until(lambda: server.is_sync_available() and client.is_sync_available(), server, client)

        # Reach into the underlying real socket to inject a malformed frame
        # (valid length header, invalid JSON payload) directly on the wire.
        assert client._sock is not None
        bad_payload = b"not valid json at all"
        client._sock.send(len(bad_payload).to_bytes(4, "big") + bad_payload)

        deadline = time.monotonic() + 3.0
        saw_malformed = False
        while time.monotonic() < deadline and not saw_malformed:
            server.poll(0.02)
            saw_malformed = any(e.reason == t.CloseReason.MALFORMED_MESSAGE for e in server.drain_status_events())
        assert saw_malformed
        assert not server.is_sync_available()
    finally:
        client.close()
        server.close()


# --------------------------------------------------------------------------
# Heartbeat + inactivity timeout (fake shared clock, real sockets)
# --------------------------------------------------------------------------


def test_heartbeat_keeps_connection_alive_across_inactivity_window() -> None:
    clock = ManualClock()
    server = t.TcpServerTransport(
        bind_address=("127.0.0.1", 0), allowed_peer_host="127.0.0.1", session_token="tok", clock=clock
    )
    client = t.TcpClientTransport(server_address=server.listening_address(), session_token="tok", clock=clock)
    try:
        assert _wait_until(lambda: server.is_sync_available() and client.is_sync_available(), server, client)

        # Advance time in heartbeat-sized steps well past the inactivity
        # timeout; because both sides send/receive heartbeats each step, the
        # link must never be flagged inactive.
        for _ in range(6):
            clock.advance(t.HEARTBEAT_INTERVAL_SECONDS + 0.1)
            for _ in range(5):
                client.poll(0.01)
                server.poll(0.01)
        assert client.is_sync_available()
        assert server.is_sync_available()
    finally:
        client.close()
        server.close()


def test_inactivity_timeout_closes_connection_when_no_heartbeats_arrive() -> None:
    # Two independent clocks: the server's clock jumps far ahead by itself,
    # simulating the client having gone silent (e.g. a frozen/partitioned
    # peer) without the server itself doing anything to keep the link open.
    server_clock = ManualClock()
    server = t.TcpServerTransport(
        bind_address=("127.0.0.1", 0), allowed_peer_host="127.0.0.1", session_token="tok", clock=server_clock
    )
    client = t.TcpClientTransport(server_address=server.listening_address(), session_token="tok")
    try:
        assert _wait_until(lambda: server.is_sync_available() and client.is_sync_available(), server, client)
        client.close()  # simulate the peer vanishing without a clean FIN observed in time

        server_clock.advance(t.INACTIVITY_TIMEOUT_SECONDS + 1.0)
        deadline = time.monotonic() + 3.0
        saw_timeout_or_peer_closed = False
        while time.monotonic() < deadline and not saw_timeout_or_peer_closed:
            server.poll(0.02)
            saw_timeout_or_peer_closed = any(
                e.reason in (t.CloseReason.INACTIVITY_TIMEOUT, t.CloseReason.PEER_CLOSED)
                for e in server.drain_status_events()
            )
        assert saw_timeout_or_peer_closed
        assert not server.is_sync_available()
    finally:
        server.close()


# --------------------------------------------------------------------------
# is_sync_available never raises and reflects incompatible protocol version
# --------------------------------------------------------------------------


def test_is_sync_available_false_before_handshake() -> None:
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok")
    assert client.is_sync_available() is False


def test_protocol_incompatible_reported_as_sync_unavailable_without_raising() -> None:
    server = t.TcpServerTransport(bind_address=("127.0.0.1", 0), allowed_peer_host="127.0.0.1", session_token="tok")
    client = t.TcpClientTransport(server_address=server.listening_address(), session_token="tok")
    try:
        # Make the client's initial hello advertise a different protocol while
        # leaving its decoder able to consume the server's rejection.
        client._protocol_version = lambda: 999  # type: ignore[method-assign]

        deadline = time.monotonic() + 3.0
        server_events: list[t.StatusEvent] = []
        client_events: list[t.StatusEvent] = []
        while time.monotonic() < deadline and not (
            any(event.reason == t.CloseReason.PROTOCOL_INCOMPATIBLE for event in server_events)
            and any(event.reason == t.CloseReason.PROTOCOL_INCOMPATIBLE for event in client_events)
        ):
            client.poll(0.02)
            server.poll(0.02)
            server_events.extend(server.drain_status_events())
            client_events.extend(client.drain_status_events())
        assert any(event.reason == t.CloseReason.PROTOCOL_INCOMPATIBLE for event in server_events)
        assert any(event.reason == t.CloseReason.PROTOCOL_INCOMPATIBLE for event in client_events)
        assert server.is_sync_available() is False
        assert client.is_sync_available() is False
    finally:
        client.close()
        server.close()


# --------------------------------------------------------------------------
# Review item #1: send_message never raises into the caller (e.g. MIDI);
# encode-time SchemaError/UnicodeError must close (sync-only) instead.
# --------------------------------------------------------------------------


def test_send_message_with_nonfinite_payload_closes_without_raising() -> None:
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok")
    base = _make_command(_uuid())
    bad_command = p.CommandMessage(
        protocol_version=base.protocol_version,
        schema_version=base.schema_version,
        transport_session_id=base.transport_session_id,
        control_session_id=base.control_session_id,
        command_id=base.command_id,
        receiver_epoch=base.receiver_epoch,
        expected_revision=base.expected_revision,
        command_type=base.command_type,
        payload={"volume": float("nan")},
    )
    result = client.send_message(bad_command)
    assert result is False
    assert client.last_close_reason == t.CloseReason.MALFORMED_MESSAGE
    events = client.drain_status_events()
    assert any(e.reason == t.CloseReason.MALFORMED_MESSAGE for e in events)


def test_send_message_with_lone_surrogate_payload_closes_without_raising() -> None:
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok")
    base = _make_command(_uuid())
    bad_command = p.CommandMessage(
        protocol_version=base.protocol_version,
        schema_version=base.schema_version,
        transport_session_id=base.transport_session_id,
        control_session_id=base.control_session_id,
        command_id=base.command_id,
        receiver_epoch=base.receiver_epoch,
        expected_revision=base.expected_revision,
        command_type=base.command_type,
        payload={"label": "\ud800"},
    )
    result = client.send_message(bad_command)
    assert result is False
    assert client.last_close_reason == t.CloseReason.MALFORMED_MESSAGE
    events = client.drain_status_events()
    assert any(e.reason == t.CloseReason.MALFORMED_MESSAGE for e in events)


# --------------------------------------------------------------------------
# Review item #7: bounded inbound decoded-message queue; overflow closes.
# --------------------------------------------------------------------------


def test_inbound_message_queue_accepts_up_to_the_limit() -> None:
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok")
    session_id = _uuid()
    for _ in range(t.MAX_INBOUND_QUEUE):
        client._handle_message(_make_command(session_id))
    assert client.status != t.ConnectionStatus.CLOSED
    assert len(client.drain_messages()) == t.MAX_INBOUND_QUEUE


def test_inbound_message_queue_overflow_closes_connection() -> None:
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok")
    session_id = _uuid()
    for _ in range(t.MAX_INBOUND_QUEUE):
        client._handle_message(_make_command(session_id))
    assert client.status != t.ConnectionStatus.CLOSED

    client._handle_message(_make_command(session_id))
    assert client.last_close_reason == t.CloseReason.QUEUE_OVERFLOW
    events = client.drain_status_events()
    assert any(e.reason == t.CloseReason.QUEUE_OVERFLOW for e in events)


def test_inbound_message_queue_ping_pong_do_not_count_toward_bound() -> None:
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok")
    session_id = _uuid()
    for _ in range(t.MAX_INBOUND_QUEUE + 10):
        client._handle_message(
            p.Ping(protocol_version=1, schema_version=1, transport_session_id=session_id, ping_id=_uuid())
        )
        client._outbound.clear()  # drop the auto Pong replies; irrelevant to this assertion
    # Pings are answered with an auto Pong (queued outbound) but never
    # occupy the bounded inbound message queue, and never close the link.
    assert client.status != t.ConnectionStatus.CLOSED
    assert client.drain_messages() == []


# --------------------------------------------------------------------------
# Review item #2: bounded CONNECTING/HANDSHAKING timeout; server frees its
# slot and a client backs off.
# --------------------------------------------------------------------------


def test_client_connecting_handshake_timeout_closes_and_schedules_backoff() -> None:
    clock = ManualClock()
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok", clock=clock)
    client._sock = _NeverReadySocket()  # type: ignore[assignment]
    client._status = t.ConnectionStatus.CONNECTING
    client._handshake_deadline = clock.value + t.HANDSHAKE_TIMEOUT_SECONDS
    clock.advance(t.HANDSHAKE_TIMEOUT_SECONDS + 0.1)
    client.poll(0.01)
    assert client.last_close_reason == t.CloseReason.HANDSHAKE_TIMEOUT
    assert client._sock is None
    assert client._next_attempt_at > clock.value  # a backoff retry was scheduled
    events = client.drain_status_events()
    assert any(e.reason == t.CloseReason.HANDSHAKE_TIMEOUT for e in events)


def test_client_handshaking_timeout_closes_and_schedules_backoff() -> None:
    clock = ManualClock()
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok", clock=clock)
    client._sock = _NeverReadySocket()  # type: ignore[assignment]
    client._status = t.ConnectionStatus.HANDSHAKING
    client._handshake_deadline = clock.value + t.HANDSHAKE_TIMEOUT_SECONDS
    clock.advance(t.HANDSHAKE_TIMEOUT_SECONDS + 0.1)
    client.poll(0.01)
    assert client.last_close_reason == t.CloseReason.HANDSHAKE_TIMEOUT
    assert client._sock is None
    assert client._next_attempt_at > clock.value


def test_handshake_not_yet_expired_does_not_close() -> None:
    clock = ManualClock()
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok", clock=clock)
    client._sock = _NeverReadySocket()  # type: ignore[assignment]
    client._status = t.ConnectionStatus.HANDSHAKING
    client._handshake_deadline = clock.value + t.HANDSHAKE_TIMEOUT_SECONDS
    clock.advance(t.HANDSHAKE_TIMEOUT_SECONDS - 0.5)
    client._enforce_handshake_timeout()
    assert client._sock is not None
    assert client.last_close_reason == t.CloseReason.NONE


def test_server_handshaking_timeout_frees_slot_and_accepts_new_client() -> None:
    clock = ManualClock()
    server = t.TcpServerTransport(
        bind_address=("127.0.0.1", 0), allowed_peer_host="127.0.0.1", session_token="tok", clock=clock
    )
    addr = server.listening_address()
    try:
        # Simulate a peer that opened a connection but never completed hello.
        server._sock = _NeverReadySocket()  # type: ignore[assignment]
        server._status = t.ConnectionStatus.HANDSHAKING
        server._transport_session_id = _uuid()
        server._handshake_deadline = clock.value + t.HANDSHAKE_TIMEOUT_SECONDS
        clock.advance(t.HANDSHAKE_TIMEOUT_SECONDS + 0.1)
        server.poll(0.01)
        assert server.last_close_reason == t.CloseReason.HANDSHAKE_TIMEOUT
        assert server._sock is None  # slot freed

        client = t.TcpClientTransport(server_address=addr, session_token="tok")
        try:
            assert _wait_until(lambda: server.is_sync_available() and client.is_sync_available(), server, client)
        finally:
            client.close()
    finally:
        server.close()


# --------------------------------------------------------------------------
# Review item #13: client honors a positive poll timeout while backing off,
# without busy-spinning.
# --------------------------------------------------------------------------


def test_client_backoff_honors_poll_timeout_without_busy_spin() -> None:
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok")
    client._next_attempt_at = client._clock() + 1_000.0
    try:
        started = time.monotonic()
        client.poll(0.2)
        elapsed = time.monotonic() - started
        assert elapsed >= 0.15
        assert elapsed < 2.0
    finally:
        client.close()


def test_client_backoff_wait_bounded_by_remaining_backoff_not_only_timeout() -> None:
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok")
    client._next_attempt_at = client._clock() + 0.05
    try:
        started = time.monotonic()
        client.poll(5.0)  # generous poll timeout; must not actually wait 5s
        elapsed = time.monotonic() - started
        assert elapsed < 2.0
    finally:
        client.close()


# --------------------------------------------------------------------------
# Review item #15: persistent selector per transport (not rebuilt every
# tick); close() cleans it up.
# --------------------------------------------------------------------------


def test_selector_is_persistent_across_poll_calls() -> None:
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok")
    client._next_attempt_at = client._clock() + 1_000.0
    selector = client._selector
    try:
        client.poll(0.01)
        client.poll(0.01)
        assert client._selector is selector
    finally:
        client.close()

    server = t.TcpServerTransport(bind_address=("127.0.0.1", 0), allowed_peer_host="127.0.0.1", session_token="tok")
    server_selector = server._selector
    try:
        server.poll(0.01)
        server.poll(0.01)
        assert server._selector is server_selector
    finally:
        server.close()


def test_close_cleans_up_the_selector() -> None:
    client = t.TcpClientTransport(server_address=("127.0.0.1", 1), session_token="tok")
    client.close()
    with pytest.raises(ValueError, match="closed"):
        client._selector.select(timeout=0)

    server = t.TcpServerTransport(bind_address=("127.0.0.1", 0), allowed_peer_host="127.0.0.1", session_token="tok")
    server.close()
    with pytest.raises(ValueError, match="closed"):
        server._selector.select(timeout=0)


def test_reconnect_after_close_does_not_leak_stale_selector_registrations() -> None:
    server = t.TcpServerTransport(bind_address=("127.0.0.1", 0), allowed_peer_host="127.0.0.1", session_token="tok")
    addr = server.listening_address()
    try:
        client1 = t.TcpClientTransport(server_address=addr, session_token="tok")
        try:
            assert _wait_until(lambda: server.is_sync_available() and client1.is_sync_available(), server, client1)
        finally:
            client1.close()
        for _ in range(20):
            server.poll(0.01)
        assert not server.is_sync_available()

        # A fresh connection (whose OS-level file descriptor numbers are
        # very likely reused from the just-closed one) must handshake
        # cleanly; a naive persistent-selector implementation that failed
        # to unregister stale fds between ticks would raise or misbehave
        # here.
        client2 = t.TcpClientTransport(server_address=addr, session_token="tok")
        try:
            assert _wait_until(lambda: server.is_sync_available() and client2.is_sync_available(), server, client2)
        finally:
            client2.close()
    finally:
        server.close()
