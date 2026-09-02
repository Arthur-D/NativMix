from __future__ import annotations

import logging
import socket
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

import nativmix.hardware.remote_midi as remote_midi
from nativmix.hardware.remote_midi import (
    APPLE_MIDI_SERVICE_TYPE,
    MAX_DATAGRAM,
    ControlCommand,
    DiscoveryChange,
    DiscoveryChangeKind,
    InvitationPacket,
    PeerRecord,
    RemoteMidiRole,
    RemoteMidiTransport,
    RtpCCPacket,
    SessionState,
    SyncPacket,
    decode_control_packet,
    decode_rtp_cc,
    encode_invitation,
    encode_rtp_cc,
    encode_sync,
    peer_from_service,
)


@dataclass
class Clock:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeDiscovery:
    def __init__(self, emit: Callable[[DiscoveryChange], None]) -> None:
        self.emit = emit
        self.advertisement: Mapping[str, Any] | None = None
        self.refreshes = 0
        self.closed = False

    def start(self, advertisement: Mapping[str, Any] | None) -> None:
        self.advertisement = advertisement

    def refresh(self) -> None:
        self.refreshes += 1

    def close(self) -> None:
        self.closed = True


def fake_discovery_factory(store: list[FakeDiscovery]) -> Callable[[Callable[[DiscoveryChange], None]], FakeDiscovery]:
    def factory(emit: Callable[[DiscoveryChange], None]) -> FakeDiscovery:
        backend = FakeDiscovery(emit)
        store.append(backend)
        return backend

    return factory


def test_invitation_and_sync_codecs_are_strict() -> None:
    invitation = encode_invitation(ControlCommand.INVITATION, 0x12345678, 0xABCDEF01, "Mixer 🎛")
    assert decode_control_packet(invitation) == InvitationPacket(
        ControlCommand.INVITATION, 0x12345678, 0xABCDEF01, "Mixer 🎛"
    )
    end = encode_invitation(ControlCommand.END, 4, 5)
    assert decode_control_packet(end) == InvitationPacket(ControlCommand.END, 4, 5, "")
    sync = encode_sync(9, 1, (10, 11, 12))
    assert decode_control_packet(sync) == SyncPacket(9, 1, (10, 11, 12))

    malformed = [
        b"",
        b"\xff\xfeIN",
        invitation[:-1],
        invitation + b"x",
        invitation[:4] + b"\x00\x00\x00\x01" + invitation[8:],
        b"\xff\xffZZ" + b"\0" * 20,
        b"x" * (MAX_DATAGRAM + 1),
        sync[:-1],
    ]
    for packet in malformed:
        with pytest.raises(ValueError, match="."):
            decode_control_packet(packet)


def test_rtp_cc_codec_and_malformed_packets() -> None:
    encoded = encode_rtp_cc(65535, 1234, 55, 15, 127, 0)
    assert decode_rtp_cc(encoded) == RtpCCPacket(65535, 1234, 55, 15, 127, 0)
    journal_packet = encoded[:12] + bytes((encoded[12] | 0x40,)) + encoded[13:] + b"journal bytes"
    assert decode_rtp_cc(journal_packet) == RtpCCPacket(65535, 1234, 55, 15, 127, 0)

    for replacement in (b"\x40", b"\x81", b"\x80\xe0"):
        with pytest.raises(ValueError, match="."):
            decode_rtp_cc(replacement + encoded[len(replacement) :])
    with pytest.raises(ValueError, match="."):
        decode_rtp_cc(encoded[:-1])
    with pytest.raises(ValueError, match="journal"):
        decode_rtp_cc(encoded + b"undeclared journal")
    with pytest.raises(ValueError, match="."):
        decode_rtp_cc(encoded[:13] + b"\x90\x01\x02")
    with pytest.raises(ValueError, match="."):
        decode_rtp_cc(b"x" * (MAX_DATAGRAM + 1))
    with pytest.raises(ValueError, match="."):
        encode_rtp_cc(0, 0, 0, 16, 0, 0)


def _record(
    peer_id: str,
    name: str = "Desk",
    host: str = "127.0.0.1",
    control_port: int = 5004,
    data_port: int = 5005,
    service_name: str = "Desk._apple-midi._udp.local.",
) -> PeerRecord:
    return PeerRecord(peer_id, name, host, control_port, data_port, service_name)


def test_peer_record_validation() -> None:
    peer_id = str(uuid.uuid4())
    properties = {
        b"nativmix": b"1",
        b"protocol": b"1",
        b"role": b"send",
        b"instance_id": peer_id.encode(),
        b"name": b"Living room",
        b"data_port": b"6001",
    }
    peer = peer_from_service("service", ["::1", "127.0.0.1"], 6000, properties)
    assert peer == PeerRecord(peer_id, "Living room", "127.0.0.1", 6000, 6001, "service")
    assert peer_from_service("service", ["::1"], 6000, properties) is None
    assert peer_from_service("service", ["127.0.0.1"], 6000, {**properties, b"role": b"receive"}) is None
    assert peer_from_service("service", ["127.0.0.1"], 6000, {**properties, b"instance_id": b"bad"}) is None
    assert APPLE_MIDI_SERVICE_TYPE == "_apple-midi._udp.local."


def test_peer_record_sync_txt_is_optional_and_backward_compatible() -> None:
    peer_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    base = {
        b"nativmix": b"1",
        b"protocol": b"1",
        b"role": b"send",
        b"instance_id": peer_id.encode(),
        b"data_port": b"6001",
    }

    apple_only = peer_from_service("service", ["127.0.0.1"], 6000, base)
    assert apple_only is not None
    assert not apple_only.sync_capable

    sync_peer = peer_from_service(
        "service",
        ["127.0.0.1"],
        6000,
        {
            **base,
            b"sync": b"1",
            b"sync_protocol": b"1",
            b"sync_schema": b"1",
            b"sync_port": b"43210",
            b"sync_session": session_id.encode(),
        },
    )
    assert sync_peer is not None
    assert sync_peer.sync_capable
    assert sync_peer.sync_protocol_version == 1
    assert sync_peer.sync_schema_version == 1
    assert sync_peer.sync_port == 43210
    assert sync_peer.sync_session == session_id

    malformed = peer_from_service(
        "service",
        ["127.0.0.1"],
        6000,
        {**base, b"sync": b"1", b"sync_port": b"bad"},
    )
    assert malformed is not None
    assert not malformed.sync_capable


def test_discovery_add_update_remove_rename_and_explicit_selection() -> None:
    peer_id = str(uuid.uuid4())
    backends: list[FakeDiscovery] = []
    transport = RemoteMidiTransport(
        "receive",
        str(uuid.uuid4()),
        "Receiver",
        selected_peer_name="Desk",
        control_port=0,
        data_port=0,
        discovery_factory=fake_discovery_factory(backends),
    )
    transport.start()
    first = _record(peer_id)
    backends[0].emit(DiscoveryChange(DiscoveryChangeKind.ADD, first.service_name, first))
    transport.poll()
    assert transport.snapshot.selected_peer_id is None
    assert transport.snapshot.peers == (first,)
    transport.select_peer(peer_id, first.name)
    assert transport.snapshot.selected_peer_id == peer_id

    renamed = _record(peer_id, "Renamed", control_port=6000, data_port=6001, service_name=first.service_name)
    backends[0].emit(DiscoveryChange(DiscoveryChangeKind.UPDATE, first.service_name, renamed))
    transport.poll()
    assert transport.snapshot.peers == (renamed,)
    transport.select_peer(peer_id, "Renamed")
    assert transport.snapshot.selected_peer_id == peer_id

    backends[0].emit(DiscoveryChange(DiscoveryChangeKind.REMOVE, first.service_name))
    transport.poll()
    assert transport.snapshot.peers == ()
    assert transport.snapshot.selected_peer_id == peer_id
    transport.close()


def test_transport_logs_discovery_and_connection_lifecycle(caplog) -> None:
    peer_id = str(uuid.uuid4())
    backends: list[FakeDiscovery] = []
    transport = RemoteMidiTransport(
        "receive",
        str(uuid.uuid4()),
        "Desktop",
        selected_peer_id=peer_id,
        selected_peer_name="Laptop",
        control_port=0,
        data_port=0,
        discovery_factory=fake_discovery_factory(backends),
    )
    caplog.set_level(logging.INFO, logger="nativmix.hardware.remote_midi")

    transport.start()
    peer = _record(peer_id)
    backends[0].emit(DiscoveryChange(DiscoveryChangeKind.ADD, peer.service_name, peer))
    transport.poll()
    backends[0].emit(DiscoveryChange(DiscoveryChangeKind.REMOVE, peer.service_name))
    transport.poll()
    transport.close()

    assert "Remote MIDI UDP sockets bound" in caplog.text
    assert "Remote MIDI peer discovered" in caplog.text
    assert "Remote MIDI connection attempt" in caplog.text
    assert "Remote MIDI peer removed" in caplog.text
    assert "Remote MIDI transport closing" in caplog.text


def test_outgoing_queue_is_bounded_with_explicit_overflow() -> None:
    backends: list[FakeDiscovery] = []
    transport = RemoteMidiTransport(
        "send",
        str(uuid.uuid4()),
        "Sender",
        control_port=0,
        data_port=0,
        outgoing_capacity=2,
        discovery_factory=fake_discovery_factory(backends),
    )
    transport.start()
    assert transport.send_cc(0, 1, 2)
    assert transport.send_cc(1, 3, 4)
    assert not transport.send_cc(2, 5, 6)
    assert transport.snapshot.outgoing_count == 2
    assert transport.snapshot.overflow_count == 1
    assert transport.snapshot.warning
    advertisement = backends[0].advertisement
    assert advertisement is not None
    assert advertisement["properties"][b"role"] == b"send"
    assert advertisement["properties"][b"protocol"] == b"1"
    assert advertisement["properties"][b"sync"] == b"1"
    assert advertisement["properties"][b"sync_protocol"] == b"1"
    assert advertisement["properties"][b"sync_schema"] == b"1"
    assert int(advertisement["properties"][b"sync_port"]) > 0
    uuid.UUID(advertisement["properties"][b"sync_session"].decode())
    transport.disconnect()
    assert transport.snapshot.outgoing_count == 0
    assert transport.snapshot.dropped_count == 2
    assert transport.snapshot.warning
    assert "Dropped 2" in transport.snapshot.warning
    transport.close()


def _pump(
    sender: RemoteMidiTransport,
    receiver: RemoteMidiTransport,
    *,
    attempts: int = 100,
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    from_sender: list[tuple[int, int, int]] = []
    from_receiver: list[tuple[int, int, int]] = []
    for _ in range(attempts):
        from_sender.extend(receiver.poll())
        from_receiver.extend(sender.poll())
        if from_sender and from_receiver:
            break
    return from_sender, from_receiver


def test_loopback_bidirectional_cc_and_by() -> None:
    sender_backends: list[FakeDiscovery] = []
    receiver_backends: list[FakeDiscovery] = []
    sender_id = str(uuid.uuid4())
    sender = RemoteMidiTransport(
        RemoteMidiRole.SEND,
        sender_id,
        "Send",
        bind_host="127.0.0.1",
        control_port=0,
        data_port=0,
        discovery_factory=fake_discovery_factory(sender_backends),
        random_u32=iter(range(100, 1000)).__next__,
    )
    receiver = RemoteMidiTransport(
        RemoteMidiRole.RECEIVE,
        str(uuid.uuid4()),
        "Receive",
        selected_peer_id=sender_id,
        bind_host="127.0.0.1",
        control_port=0,
        data_port=0,
        discovery_factory=fake_discovery_factory(receiver_backends),
        random_u32=iter(range(1000, 2000)).__next__,
    )
    sender.start()
    receiver.start()
    record = _record(sender_id, "Send", "127.0.0.1", sender.control_port, sender.data_port)
    receiver_backends[0].emit(DiscoveryChange(DiscoveryChangeKind.ADD, record.service_name, record))

    for _ in range(100):
        receiver.poll()
        sender.poll()
        if sender.snapshot.state is SessionState.CONNECTED and receiver.snapshot.state is SessionState.CONNECTED:
            break
    assert sender.snapshot.state is SessionState.CONNECTED
    assert receiver.snapshot.state is SessionState.CONNECTED
    assert not sender.snapshot.sync_available
    assert not receiver.snapshot.sync_available
    assert sender.snapshot.connected_peer_name == "Receive"
    assert receiver.snapshot.connected_peer_name == "Send"

    assert sender.send_cc(2, 7, 99)
    assert receiver.send_cc(3, 8, 100)
    from_sender, from_receiver = _pump(sender, receiver)
    assert from_sender == [(2, 7, 99)]
    assert from_receiver == [(3, 8, 100)]

    receiver.disconnect()
    for _ in range(20):
        sender.poll()
        if sender.snapshot.state is SessionState.IDLE:
            break
    assert sender.snapshot.state is SessionState.IDLE
    sender.close()
    receiver.close()


def test_duplicate_stale_and_sequence_wrap() -> None:
    sender_backends: list[FakeDiscovery] = []
    receiver_backends: list[FakeDiscovery] = []
    sender_id = str(uuid.uuid4())
    sender = RemoteMidiTransport(
        "send",
        sender_id,
        "Send",
        bind_host="127.0.0.1",
        control_port=0,
        data_port=0,
        discovery_factory=fake_discovery_factory(sender_backends),
    )
    receiver = RemoteMidiTransport(
        "receive",
        str(uuid.uuid4()),
        "Receive",
        selected_peer_id=sender_id,
        bind_host="127.0.0.1",
        control_port=0,
        data_port=0,
        discovery_factory=fake_discovery_factory(receiver_backends),
    )
    sender.start()
    receiver.start()
    peer = _record(sender_id, control_port=sender.control_port, data_port=sender.data_port)
    receiver_backends[0].emit(DiscoveryChange(DiscoveryChangeKind.ADD, peer.service_name, peer))
    for _ in range(100):
        receiver.poll()
        sender.poll()
        if receiver.snapshot.state is SessionState.CONNECTED:
            break
    assert receiver.snapshot.state is SessionState.CONNECTED

    assert sender._data_socket is not None  # noqa: SLF001 - focused wire-level protocol test
    assert sender._data_endpoint is not None  # noqa: SLF001
    packets = [
        encode_rtp_cc(65535, 1, sender._local_ssrc, 0, 10, 1),  # noqa: SLF001
        encode_rtp_cc(65535, 2, sender._local_ssrc, 0, 10, 2),  # noqa: SLF001
        encode_rtp_cc(0, 3, sender._local_ssrc, 0, 10, 3),  # noqa: SLF001
        encode_rtp_cc(65534, 4, sender._local_ssrc, 0, 10, 4),  # noqa: SLF001
    ]
    for packet in packets:
        sender._data_socket.sendto(packet, sender._data_endpoint)  # noqa: SLF001
    received: list[tuple[int, int, int]] = []
    for _ in range(20):
        received.extend(receiver.poll())
        if len(received) >= 2:
            break
    assert received == [(0, 10, 1), (0, 10, 3)]
    sender.close()
    receiver.close()


def test_sync_tcp_follows_matching_applemidi_session() -> None:
    sender_backends: list[FakeDiscovery] = []
    receiver_backends: list[FakeDiscovery] = []
    sender_id = str(uuid.uuid4())
    sender = RemoteMidiTransport(
        "send",
        sender_id,
        "Send",
        bind_host="127.0.0.1",
        control_port=0,
        data_port=0,
        discovery_factory=fake_discovery_factory(sender_backends),
    )
    receiver = RemoteMidiTransport(
        "receive",
        str(uuid.uuid4()),
        "Receive",
        selected_peer_id=sender_id,
        bind_host="127.0.0.1",
        control_port=0,
        data_port=0,
        discovery_factory=fake_discovery_factory(receiver_backends),
    )
    try:
        sender.start()
        receiver.start()
        advertisement = sender_backends[0].advertisement
        assert advertisement is not None
        peer = peer_from_service(
            str(advertisement["service_name"]),
            ["127.0.0.1"],
            sender.control_port,
            advertisement["properties"],
        )
        assert peer is not None
        assert peer.sync_capable
        receiver_backends[0].emit(DiscoveryChange(DiscoveryChangeKind.ADD, peer.service_name, peer))

        for _ in range(500):
            receiver.poll()
            sender.poll()
            if sender.snapshot.sync_available and receiver.snapshot.sync_available:
                break

        assert sender.snapshot.state is SessionState.CONNECTED
        assert receiver.snapshot.state is SessionState.CONNECTED
        assert sender.snapshot.sync_available
        assert receiver.snapshot.sync_available
        assert sender.snapshot.sync_error is None
        assert receiver.snapshot.sync_error is None
    finally:
        receiver.close()
        sender.close()


def test_sync_version_mismatch_keeps_applemidi_connected() -> None:
    sender_backends: list[FakeDiscovery] = []
    receiver_backends: list[FakeDiscovery] = []
    sender_id = str(uuid.uuid4())
    sender = RemoteMidiTransport(
        "send",
        sender_id,
        "Send",
        bind_host="127.0.0.1",
        control_port=0,
        data_port=0,
        discovery_factory=fake_discovery_factory(sender_backends),
    )
    receiver = RemoteMidiTransport(
        "receive",
        str(uuid.uuid4()),
        "Receive",
        selected_peer_id=sender_id,
        bind_host="127.0.0.1",
        control_port=0,
        data_port=0,
        discovery_factory=fake_discovery_factory(receiver_backends),
    )
    try:
        sender.start()
        receiver.start()
        advertisement = sender_backends[0].advertisement
        assert advertisement is not None
        properties = dict(advertisement["properties"])
        properties[b"sync_protocol"] = b"999"
        peer = peer_from_service(
            str(advertisement["service_name"]),
            ["127.0.0.1"],
            sender.control_port,
            properties,
        )
        assert peer is not None
        assert peer.sync_capable
        receiver_backends[0].emit(DiscoveryChange(DiscoveryChangeKind.ADD, peer.service_name, peer))

        for _ in range(200):
            receiver.poll()
            sender.poll()
            if receiver.snapshot.state is SessionState.CONNECTED:
                break

        assert sender.snapshot.state is SessionState.CONNECTED
        assert receiver.snapshot.state is SessionState.CONNECTED
        assert not receiver.snapshot.sync_available
        assert receiver.snapshot.sync_error
        assert "incompatible protocol/schema" in receiver.snapshot.sync_error
        assert sender.send_cc(0, 1, 64)
        from_sender, _ = _pump(sender, receiver)
        assert from_sender == [(0, 1, 64)]
        receiver.select_peer(None)
        assert receiver.snapshot.sync_error is None
    finally:
        receiver.close()
        sender.close()


def test_timeout_enters_deterministic_backoff_and_reconnects() -> None:
    clock = Clock()
    backends: list[FakeDiscovery] = []
    peer_id = str(uuid.uuid4())
    transport = RemoteMidiTransport(
        "receive",
        str(uuid.uuid4()),
        "Receive",
        selected_peer_id=peer_id,
        control_port=0,
        data_port=0,
        clock=clock,
        random_float=lambda: 0.5,
        discovery_factory=fake_discovery_factory(backends),
    )
    transport.start()
    peer = _record(peer_id)
    backends[0].emit(DiscoveryChange(DiscoveryChangeKind.ADD, peer.service_name, peer))
    transport.poll()
    assert transport.snapshot.state is SessionState.INVITING_CONTROL
    assert transport.send_cc(0, 1, 2)
    for _ in range(12):
        clock.advance(1.0)
        transport.poll()
    assert transport.snapshot.state is SessionState.BACKOFF
    assert transport.snapshot.reconnect_attempt == 1
    assert transport.snapshot.outgoing_count == 0
    assert transport.snapshot.dropped_count == 1
    assert transport.snapshot.warning
    assert "session failed" in transport.snapshot.warning
    clock.advance(1.0)
    transport.poll()
    assert transport.snapshot.state is SessionState.INVITING_CONTROL
    transport.close()


def test_clock_sync_uses_data_socket_immediately_and_every_ten_seconds() -> None:
    clock = Clock()
    backends: list[FakeDiscovery] = []
    peer_id = str(uuid.uuid4())
    peer_ssrc = 0x76543210
    control_peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    data_peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    control_peer.bind(("127.0.0.1", 0))
    data_peer.bind(("127.0.0.1", 0))
    control_peer.settimeout(0.2)
    data_peer.settimeout(0.2)
    transport = RemoteMidiTransport(
        "receive",
        str(uuid.uuid4()),
        "Controller",
        selected_peer_id=peer_id,
        bind_host="127.0.0.1",
        control_port=0,
        data_port=0,
        clock=clock,
        discovery_factory=fake_discovery_factory(backends),
        random_u32=iter(range(2000, 3000)).__next__,
    )
    transport.start()
    peer = _record(
        peer_id,
        "Desktop",
        control_port=control_peer.getsockname()[1],
        data_port=data_peer.getsockname()[1],
    )
    backends[0].emit(DiscoveryChange(DiscoveryChangeKind.ADD, peer.service_name, peer))
    transport.poll()
    control_invitation, control_source = control_peer.recvfrom(MAX_DATAGRAM)
    invitation = decode_control_packet(control_invitation)
    assert isinstance(invitation, InvitationPacket)
    control_peer.sendto(
        encode_invitation(ControlCommand.ACCEPT, invitation.token, peer_ssrc, "Desktop"),
        control_source,
    )
    transport.poll()
    data_invitation, data_source = data_peer.recvfrom(MAX_DATAGRAM)
    invitation = decode_control_packet(data_invitation)
    assert isinstance(invitation, InvitationPacket)
    data_peer.sendto(encode_invitation(ControlCommand.ACCEPT, invitation.token, peer_ssrc, "Desktop"), data_source)

    transport.poll()
    initial_sync, sync_source = data_peer.recvfrom(MAX_DATAGRAM)
    decoded_sync = decode_control_packet(initial_sync)
    assert isinstance(decoded_sync, SyncPacket)
    assert decoded_sync.count == 0
    assert sync_source == data_source
    assert transport.snapshot.connected_peer_name == "Desktop"

    last_activity = transport.snapshot.last_activity
    control_peer.sendto(encode_sync(peer_ssrc, 0, (1, 0, 0)), control_source)
    transport.poll()
    assert transport.snapshot.last_activity == last_activity
    with pytest.raises(TimeoutError):
        control_peer.recvfrom(MAX_DATAGRAM)

    clock.advance(10.0)
    transport.poll()
    periodic_sync, _ = data_peer.recvfrom(MAX_DATAGRAM)
    decoded_sync = decode_control_packet(periodic_sync)
    assert isinstance(decoded_sync, SyncPacket)
    assert decoded_sync.count == 0
    transport.close()
    control_peer.close()
    data_peer.close()


def test_missing_zeroconf_is_nonfatal_and_closes_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    created_sockets: list[socket.socket] = []

    def tracking_socket(family: int, socket_type: int) -> socket.socket:
        udp_socket = socket.socket(family, socket_type)
        created_sockets.append(udp_socket)
        return udp_socket

    def missing_module(name: str, package: str | None = None) -> Any:
        del package
        assert name == "zeroconf"
        raise ImportError("not installed")

    monkeypatch.setattr(remote_midi.importlib, "import_module", missing_module)
    transport = RemoteMidiTransport(
        "receive",
        str(uuid.uuid4()),
        "Receiver",
        control_port=0,
        data_port=0,
        socket_factory=tracking_socket,
    )
    snapshot = transport.start()
    assert snapshot.state is SessionState.UNAVAILABLE
    assert snapshot.error
    assert "zeroconf" in snapshot.error
    assert len(created_sockets) == 2
    assert all(udp_socket.fileno() == -1 for udp_socket in created_sockets)
    assert transport._control_socket is None  # noqa: SLF001
    assert transport._data_socket is None  # noqa: SLF001
    transport.close()
