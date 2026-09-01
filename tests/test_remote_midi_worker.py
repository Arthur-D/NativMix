from __future__ import annotations

import uuid

import mido

from nativmix.hardware.midi import MidiThread, _RemoteMidiOutput
from nativmix.hardware.remote_midi import RemoteMidiRole, SessionState, TransportSnapshot


def _snapshot(
    state: SessionState,
    *,
    role: RemoteMidiRole = RemoteMidiRole.RECEIVE,
    connected_name: str | None = None,
) -> TransportSnapshot:
    return TransportSnapshot(
        generation=1,
        role=role,
        state=state,
        available=True,
        error=None,
        peers=(),
        selected_peer_id=None,
        connected_peer_id=None,
        connected_peer_name=connected_name,
        outgoing_count=0,
        outgoing_capacity=512,
        overflow_count=0,
        dropped_count=0,
        warning=None,
        reconnect_attempt=0,
        last_activity=None,
    )


class _FakeTransport:
    def __init__(
        self,
        state: SessionState = SessionState.CONNECTED,
        *,
        role: RemoteMidiRole = RemoteMidiRole.RECEIVE,
        received: list[tuple[int, int, int]] | None = None,
        accept_outgoing: bool = True,
    ) -> None:
        self.snapshot = _snapshot(state, role=role)
        self.received = list(received or [])
        self.sent: list[tuple[int, int, int]] = []
        self.accept_outgoing = accept_outgoing
        self.closed = False

    def poll(self) -> list[tuple[int, int, int]]:
        received, self.received = self.received, []
        return received

    def send_cc(self, channel: int, control: int, value: int) -> bool:
        self.sent.append((channel, control, value))
        return self.accept_outgoing

    def refresh_discovery(self) -> None:
        return

    def close(self) -> None:
        self.closed = True


class _Input:
    def __init__(self, thread: MidiThread, message: mido.Message) -> None:
        self.thread = thread
        self.message = message

    def receive(self, block: bool = False) -> mido.Message:
        assert block is False
        self.thread._running = False
        return self.message


class _Output:
    def __init__(self) -> None:
        self.messages: list[mido.Message] = []

    def send(self, message: mido.Message) -> None:
        self.messages.append(message)


def _install_transport(thread: MidiThread, transport: _FakeTransport) -> None:
    thread._remote_transport = transport  # type: ignore[assignment]
    thread._remote_transport_key = thread._remote_config_values()


def test_send_role_forwards_physical_cc_without_local_mapping() -> None:
    instance_id = str(uuid.uuid4())
    thread = MidiThread(
        device_name="Controller",
        input_mode="midi_only",
        remote_role="send",
        remote_instance_id=instance_id,
        remote_name="Laptop",
    )
    thread._running = True
    thread.update_mappings({(2, 7): 4})
    transport = _FakeTransport(role=RemoteMidiRole.SEND)
    _install_transport(thread, transport)
    volumes: list[tuple[int, float]] = []
    learned: list[tuple[int, int, int]] = []
    thread.midi_volumes_changed.connect(volumes.extend)
    thread.midi_cc_received.connect(lambda channel, cc, value: learned.append((channel, cc, value)))

    message = mido.Message("control_change", channel=2, control=7, value=90)
    thread._device_loop(_Input(thread, message), None, "Controller")

    assert transport.sent == [(2, 7, 90)]
    assert volumes == []
    assert learned == []


def test_receive_role_uses_existing_handle_cc_mapping_without_local_input() -> None:
    thread = MidiThread(
        input_mode="midi_only",
        remote_role="receive",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Desktop",
    )
    thread._running = True
    thread.update_mappings({(1, 9): 3})
    transport = _FakeTransport(received=[(1, 9, 64)])
    _install_transport(thread, transport)
    volumes: list[tuple[int, float]] = []
    thread.midi_volumes_changed.connect(volumes.extend)

    def poll_once() -> None:
        for channel, cc, value in transport.poll():
            thread._handle_cc(channel, cc, value)
        thread._running = False

    thread._poll_remote_transport = lambda _outport=None: poll_once()  # type: ignore[method-assign]
    thread._run_remote_receive_loop(transport)  # type: ignore[arg-type]

    assert volumes == [(3, 64 / 127.0)]


def test_remote_feedback_adapter_uses_existing_fader_binding() -> None:
    thread = MidiThread(
        input_mode="midi_only",
        remote_role="receive",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Desktop",
    )
    transport = _FakeTransport()
    thread.update_mappings({(4, 11): 2})
    thread._queue_fader_sync([(2, 0.5)])

    thread._process_pending_sync(_RemoteMidiOutput(transport))  # type: ignore[arg-type]

    assert transport.sent == [(4, 11, 64)]


def test_send_role_writes_remote_feedback_to_physical_output() -> None:
    thread = MidiThread(
        device_name="Controller",
        input_mode="midi_only",
        remote_role="send",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Laptop",
    )
    transport = _FakeTransport(role=RemoteMidiRole.SEND, received=[(3, 10, 91)])
    _install_transport(thread, transport)
    output = _Output()

    thread._poll_remote_transport(output)

    assert [(message.channel, message.control, message.value) for message in output.messages] == [(3, 10, 91)]
    assert thread._remote_feedback_cache == {(3, 10): 91}


def test_remote_feedback_overflow_drops_without_interrupting_receive() -> None:
    thread = MidiThread(
        input_mode="midi_only",
        remote_role="receive",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Desktop",
    )
    transport = _FakeTransport(accept_outgoing=False)
    thread.update_mappings({(4, 11): 2})
    thread._queue_fader_sync([(2, 0.5)])

    thread._process_pending_sync(_RemoteMidiOutput(transport))  # type: ignore[arg-type]

    assert transport.sent == [(4, 11, 64)]
    assert thread._pending_sync is None


def test_remote_transport_closes_when_disabled_or_sender_is_not_physical() -> None:
    thread = MidiThread(
        input_mode="midi_only",
        remote_role="send",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Laptop",
    )
    transport = _FakeTransport(role=RemoteMidiRole.SEND)
    _install_transport(thread, transport)

    assert thread._ensure_remote_transport() is None
    assert transport.closed

    transport = _FakeTransport(role=RemoteMidiRole.RECEIVE)
    thread._remote_role = "receive"
    thread._input_mode = "usb"
    _install_transport(thread, transport)

    assert thread._ensure_remote_transport() is None
    assert transport.closed


def test_remote_snapshot_emits_only_on_authoritative_state_changes() -> None:
    thread = MidiThread(
        input_mode="midi_only",
        remote_role="receive",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Desktop",
    )
    states: list[tuple[int, str, str]] = []
    thread.remote_state_changed.connect(
        lambda generation, _role, status, message, *_args: states.append((generation, status, message))
    )
    snapshot = _snapshot(SessionState.CONNECTED, connected_name="Laptop")

    thread._on_remote_snapshot(snapshot)
    thread._on_remote_snapshot(snapshot)

    assert len(states) == 1
    assert states[0][1:] == ("stable", "Remote controller connected: Laptop")
