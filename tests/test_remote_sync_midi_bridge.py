from __future__ import annotations

import uuid

from nativmix.hardware.remote_midi import RemoteMidiTransport, SessionState
from nativmix.remote_sync.protocol import PROTOCOL_VERSION, Ping
from nativmix.remote_sync.schema import SCHEMA_VERSION


class _FakeSyncTransport:
    def __init__(self, message: Ping) -> None:
        self.transport_session_id = message.transport_session_id
        self.peer_instance_id = str(uuid.uuid4())
        self._messages = [message]
        self.sent: list[Ping] = []

    def poll(self, timeout: float) -> None:
        assert timeout == 0.0

    def is_sync_available(self) -> bool:
        return True

    def drain_status_events(self) -> list[object]:
        return []

    def drain_messages(self) -> list[Ping]:
        messages = self._messages
        self._messages = []
        return messages

    def send_message(self, message: Ping) -> bool:
        self.sent.append(message)
        return True


def _ping(session_id: str) -> Ping:
    return Ping(
        protocol_version=PROTOCOL_VERSION,
        schema_version=SCHEMA_VERSION,
        transport_session_id=session_id,
        ping_id=str(uuid.uuid4()),
    )


def test_sync_messages_are_correlated_to_current_remote_session() -> None:
    session_id = str(uuid.uuid4())
    peer_id = str(uuid.uuid4())
    received = []
    transport = RemoteMidiTransport(
        "receive",
        str(uuid.uuid4()),
        "Receiver",
        selected_peer_id=peer_id,
        on_sync_message=received.append,
    )
    sync_transport = _FakeSyncTransport(_ping(session_id))
    transport._sync_transport = sync_transport  # noqa: SLF001
    transport._state = SessionState.CONNECTED  # noqa: SLF001

    transport._poll_sync_transport()  # noqa: SLF001

    assert len(received) == 1
    envelope = received[0]
    assert envelope.generation == transport.sync_generation
    assert envelope.selected_peer_id == peer_id
    assert envelope.connected_peer_id == sync_transport.peer_instance_id
    assert envelope.transport_session_id == session_id


def test_sync_outbound_rejects_stale_generation_or_session() -> None:
    session_id = str(uuid.uuid4())
    transport = RemoteMidiTransport("receive", str(uuid.uuid4()), "Receiver")
    sync_transport = _FakeSyncTransport(_ping(session_id))
    transport._sync_transport = sync_transport  # noqa: SLF001
    transport._sync_available = True  # noqa: SLF001
    message = _ping(session_id)

    assert not transport.send_sync_message(
        message,
        expected_generation=transport.sync_generation + 1,
        expected_transport_session_id=session_id,
    )
    assert not transport.send_sync_message(
        message,
        expected_generation=transport.sync_generation,
        expected_transport_session_id=str(uuid.uuid4()),
    )
    assert transport.send_sync_message(
        message,
        expected_generation=transport.sync_generation,
        expected_transport_session_id=session_id,
    )
    assert sync_transport.sent == [message]


def test_sync_generation_does_not_change_for_ordinary_midi_activity() -> None:
    session_id = str(uuid.uuid4())
    transport = RemoteMidiTransport("receive", str(uuid.uuid4()), "Receiver")
    sync_transport = _FakeSyncTransport(_ping(session_id))
    transport._sync_transport = sync_transport  # noqa: SLF001
    transport._sync_available = True  # noqa: SLF001
    transport._sync_transport_session_id = session_id  # noqa: SLF001
    transport._sync_generation = 4  # noqa: SLF001

    transport._touch()  # noqa: SLF001
    assert transport.sync_generation == 4
    assert transport.send_sync_message(
        _ping(session_id),
        expected_generation=4,
        expected_transport_session_id=session_id,
    )
