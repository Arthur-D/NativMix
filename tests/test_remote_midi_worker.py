from __future__ import annotations

import logging
import uuid
from dataclasses import replace

import mido
import pytest

import nativmix.hardware.midi as midi_module
from nativmix.hardware.midi import MidiThread, _RemoteMidiOutput
from nativmix.hardware.remote_midi import (
    RemoteMidiRole,
    SessionState,
    SyncControlEnvelope,
    SyncSessionSnapshot,
    TransportSnapshot,
)
from nativmix.remote_sync.protocol import PROTOCOL_VERSION, Ping
from nativmix.remote_sync.schema import SCHEMA_VERSION


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
        self.role = role
        self.snapshot = _snapshot(state, role=role)
        self.received = list(received or [])
        self.sent: list[tuple[int, int, int]] = []
        self.accept_outgoing = accept_outgoing
        self.closed = False
        self.sync_sends: list[tuple[object, int, str]] = []

    def poll(self, cc_handler=None) -> list[tuple[int, int, int]]:
        received, self.received = self.received, []
        if cc_handler is not None:
            for channel, control, value in received:
                cc_handler(channel, control, value)
            return []
        return received

    def send_cc(self, channel: int, control: int, value: int) -> bool:
        self.sent.append((channel, control, value))
        return self.accept_outgoing

    def send_sync_message(
        self,
        message: object,
        *,
        expected_generation: int,
        expected_transport_session_id: str,
    ) -> bool:
        self.sync_sends.append((message, expected_generation, expected_transport_session_id))
        return True

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
    thread._remote_transport_key = thread._remote_transport_identity()


def test_control_plane_is_started_before_optional_local_midi_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    thread = MidiThread(
        input_mode="midi_only",
        remote_role="receive",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Desktop",
    )
    thread._running = False
    monkeypatch.setattr(thread, "_ensure_remote_transport", lambda: events.append("control-plane"))
    monkeypatch.setattr(
        midi_module,
        "ensure_midi_backend",
        lambda: events.append("midi-backend") or None,
    )

    thread._run_safe()

    assert events == ["control-plane", "midi-backend"]


def test_receive_control_plane_keeps_fast_polling_without_local_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BackendFreeTransport(_FakeTransport):
        def poll(self, cc_handler=None) -> list[tuple[int, int, int]]:
            thread._running = False
            return super().poll(cc_handler)

    thread = MidiThread(
        input_mode="midi_only",
        remote_role="receive",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Desktop",
    )
    thread._running = True
    thread.update_mappings({(4, 11): 2})
    thread._queue_fader_sync([(2, 0.5)])
    transport = BackendFreeTransport()
    _install_transport(thread, transport)
    monkeypatch.setattr(midi_module, "ensure_midi_backend", lambda: None)
    monkeypatch.setattr(midi_module.time, "sleep", lambda _seconds: None)

    thread._run_safe()

    assert transport.sent == [(4, 11, 64)]


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


def test_sender_physical_cc_is_enqueued_before_tcp_control_poll() -> None:
    events: list[str] = []

    class OrderedTransport(_FakeTransport):
        def send_cc(self, channel: int, control: int, value: int) -> bool:
            events.append("applemidi-enqueue")
            return super().send_cc(channel, control, value)

        def poll(self, cc_handler=None) -> list[tuple[int, int, int]]:
            events.append("tcp-control-poll")
            return super().poll(cc_handler)

    class OrderedInput(_Input):
        def receive(self, block: bool = False) -> mido.Message:
            events.append("physical-read")
            return super().receive(block)

    thread = MidiThread(
        device_name="Controller",
        input_mode="midi_only",
        remote_role="send",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Laptop",
    )
    thread._running = True
    transport = OrderedTransport(role=RemoteMidiRole.SEND)
    _install_transport(thread, transport)
    message = mido.Message("control_change", channel=2, control=7, value=90)

    thread._device_loop(OrderedInput(thread, message), None, "Controller")

    assert events == ["physical-read", "applemidi-enqueue", "tcp-control-poll"]
    assert transport.sent == [(2, 7, 90)]


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


def test_remote_controller_cc_applies_once_before_tcp_observation() -> None:
    events: list[str] = []

    class OrderedTransport(_FakeTransport):
        def poll(self, cc_handler=None) -> list[tuple[int, int, int]]:
            events.append("applemidi-receive")
            assert cc_handler is not None
            cc_handler(1, 9, 64)
            events.append("tcp-observation")
            return []

    thread = MidiThread(
        input_mode="midi_only",
        remote_role="receive",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Desktop",
    )
    thread.update_mappings({(1, 9): 3})
    transport = OrderedTransport()
    _install_transport(thread, transport)
    applied: list[tuple[int, float]] = []

    def apply_volume(changes: list[tuple[int, float]]) -> None:
        events.append("receiver-audio")
        applied.extend(changes)

    thread.midi_volumes_changed.connect(apply_volume)
    thread._poll_remote_transport()

    assert events == ["applemidi-receive", "receiver-audio", "tcp-observation"]
    assert applied == [(3, 64 / 127.0)]


@pytest.mark.parametrize("structured_reason", [True, False])
def test_real_hello_version_mismatch_precedes_generic_terminal_sync_status(
    structured_reason: bool,
) -> None:
    thread = MidiThread(
        input_mode="midi_only",
        remote_role="receive",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Desktop",
    )
    statuses: list[tuple[int, str, str]] = []
    thread.remote_sync_status_changed.connect(
        lambda generation, status, detail: statuses.append((generation, status, detail))
    )
    snapshot = replace(
        _snapshot(SessionState.CONNECTED),
        sync_error="Remote sync unavailable: hello version mismatch: got protocol=999 schema=1",
        sync_terminal=True,
        sync_close_reason=(
            midi_module.SyncCloseReason.PROTOCOL_INCOMPATIBLE if structured_reason else None
        ),
    )

    thread._on_remote_snapshot(snapshot)

    assert statuses[-1][1] == "Version incompatible"
    assert "hello version mismatch" in statuses[-1][2]


def test_continuous_receiver_cc_services_feedback_after_audio_without_duplicates() -> None:
    events: list[str] = []

    class BusyTransport(_FakeTransport):
        def poll(self, cc_handler=None) -> list[tuple[int, int, int]]:
            events.append("applemidi-receive")
            assert cc_handler is not None
            cc_handler(1, 9, 64)
            thread._running = False
            return []

        def send_cc(self, channel: int, control: int, value: int) -> bool:
            events.append(f"feedback:{channel}:{control}:{value}")
            return super().send_cc(channel, control, value)

    thread = MidiThread(
        input_mode="midi_only",
        remote_role="receive",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Desktop",
    )
    thread._running = True
    thread.update_mappings({(1, 9): 3, (4, 11): 2})
    thread.update_mute_mappings({(5, 12): 4})
    thread._queue_fader_sync([(2, 0.5)])
    thread._queue_mute_feedback([(4, True)])
    thread.midi_volumes_changed.connect(lambda _changes: events.append("receiver-audio"))
    transport = BusyTransport()
    _install_transport(thread, transport)

    thread._run_remote_receive_loop(transport)  # type: ignore[arg-type]

    assert events == [
        "applemidi-receive",
        "receiver-audio",
        "feedback:4:11:64",
        "feedback:5:12:127",
    ]
    assert transport.sent == [(4, 11, 64), (5, 12, 127)]


def test_receiver_audio_and_slider_dispatch_precede_queued_canonical_tcp_work() -> None:
    events: list[str] = []
    thread = MidiThread(
        input_mode="midi_only",
        remote_role="receive",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Desktop",
    )
    thread.update_mappings({(1, 9): 3})
    transport = _FakeTransport(received=[(1, 9, 64)])
    _install_transport(thread, transport)
    session_id = str(uuid.uuid4())
    thread._on_remote_sync_session(
        SyncSessionSnapshot(4, RemoteMidiRole.RECEIVE, None, None, "Laptop", session_id, True)
    )
    canonical = Ping(PROTOCOL_VERSION, SCHEMA_VERSION, session_id, str(uuid.uuid4()))
    thread._queue_remote_sync_message(canonical, 1, session_id)
    thread.midi_volumes_changed.connect(lambda _changes: events.append("receiver-audio"))
    thread.midi_volumes_changed.connect(lambda _changes: events.append("receiver-slider"))

    assert thread._poll_remote_transport()
    assert events == ["receiver-audio", "receiver-slider"]
    assert transport.sync_sends == []

    assert not thread._poll_remote_transport()
    assert transport.sync_sends == [(canonical, 4, session_id)]


def test_control_generation_stays_monotonic_across_transport_recreation() -> None:
    thread = MidiThread(
        input_mode="midi_only",
        remote_role="receive",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Desktop",
    )
    peer_id = str(uuid.uuid4())
    old_session = str(uuid.uuid4())
    new_session = str(uuid.uuid4())
    sessions = []
    messages = []
    thread.remote_sync_session_changed.connect(sessions.append)
    thread.remote_sync_message_received.connect(messages.append)

    thread._on_remote_sync_session(
        SyncSessionSnapshot(5, RemoteMidiRole.RECEIVE, peer_id, peer_id, "Laptop", old_session, True)
    )
    thread._on_remote_sync_session(
        SyncSessionSnapshot(1, RemoteMidiRole.RECEIVE, peer_id, peer_id, "Laptop", new_session, True)
    )
    ping = Ping(PROTOCOL_VERSION, SCHEMA_VERSION, new_session, str(uuid.uuid4()))
    thread._on_remote_sync_message(
        SyncControlEnvelope(1, RemoteMidiRole.RECEIVE, peer_id, peer_id, new_session, ping, 0.0)
    )
    stale_ping = Ping(PROTOCOL_VERSION, SCHEMA_VERSION, old_session, str(uuid.uuid4()))
    thread._on_remote_sync_message(
        SyncControlEnvelope(5, RemoteMidiRole.RECEIVE, peer_id, peer_id, old_session, stale_ping, 0.0)
    )

    assert [snapshot.generation for snapshot in sessions] == [1, 2]
    assert len(messages) == 1
    assert messages[0].generation == 2
    assert messages[0].transport_session_id == new_session


def test_public_control_generation_translates_to_active_transport_generation() -> None:
    thread = MidiThread(
        input_mode="midi_only",
        remote_role="receive",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Desktop",
    )
    transport = _FakeTransport()
    _install_transport(thread, transport)
    session_id = str(uuid.uuid4())
    thread._on_remote_sync_session(
        SyncSessionSnapshot(3, RemoteMidiRole.RECEIVE, None, None, "Laptop", session_id, True)
    )
    message = Ping(PROTOCOL_VERSION, SCHEMA_VERSION, session_id, str(uuid.uuid4()))
    thread._queue_remote_sync_message(message, 1, session_id)

    thread._poll_remote_transport()

    assert transport.sync_sends == [(message, 3, session_id)]


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


def test_remote_feedback_echo_is_not_sent_back_to_receiver_audio() -> None:
    thread = MidiThread(
        device_name="Controller",
        input_mode="midi_only",
        remote_role="send",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Laptop",
    )
    thread.update_mappings({(3, 10): 0})
    transport = _FakeTransport(role=RemoteMidiRole.SEND, received=[(3, 10, 91)])
    _install_transport(thread, transport)
    output = _Output()
    volume_events: list[list[tuple[int, float]]] = []
    thread.midi_volumes_changed.connect(volume_events.append)

    thread._poll_remote_transport(output)
    thread._forward_remote_cc(3, 10, 91)
    thread._forward_remote_cc(3, 10, 40)

    assert [(message.channel, message.control, message.value) for message in output.messages] == [(3, 10, 91)]
    assert transport.sent == [(3, 10, 40)]
    assert volume_events == []


def test_remote_receiver_applies_every_rapid_cc_without_local_throttle() -> None:
    thread = MidiThread(
        input_mode="midi_only",
        remote_role="receive",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Desktop",
    )
    thread.update_mappings({(3, 10): 0})
    transport = _FakeTransport(role=RemoteMidiRole.RECEIVE, received=[(3, 10, 10), (3, 10, 90), (3, 10, 20)])
    _install_transport(thread, transport)
    volume_events: list[list[tuple[int, float]]] = []
    audio_writes: list[tuple[int, float]] = []
    thread.midi_volumes_changed.connect(volume_events.append)
    thread.midi_volumes_changed.connect(lambda mappings: audio_writes.extend(mappings))

    thread._poll_remote_transport()

    assert volume_events == [
        [(0, pytest.approx(10 / 127.0))],
        [(0, pytest.approx(90 / 127.0))],
        [(0, pytest.approx(20 / 127.0))],
    ]
    assert audio_writes == [
        (0, pytest.approx(10 / 127.0)),
        (0, pytest.approx(90 / 127.0)),
        (0, pytest.approx(20 / 127.0)),
    ]


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


def test_usb_blank_sender_publishes_actionable_block_and_wakes(caplog, monkeypatch) -> None:
    thread = MidiThread(
        input_mode="usb",
        remote_role="off",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Laptop",
    )
    states: list[tuple[str, str, str]] = []
    thread.remote_state_changed.connect(
        lambda _generation, role, status, message, *_args: states.append((role, status, message))
    )
    caplog.set_level(logging.INFO, logger="nativmix.hardware.midi")

    thread.set_remote_config("send", thread._remote_instance_id, "Laptop", "", "")

    assert thread._panic_flag
    thread._running = True
    monkeypatch.setattr(
        midi_module.time,
        "sleep",
        lambda _seconds: pytest.fail("A remote role change must interrupt the worker sleep immediately"),
    )
    thread._sleep_checked(10.0)
    assert thread._ensure_remote_transport() is None
    assert states[-1] == (
        "send",
        "warning",
        "Remote Send blocked: set Input Mode to USB + MIDI or MIDI Only.",
    )
    assert "Remote MIDI role/config transition: off -> send" in caplog.text
    assert "Remote MIDI send blocked" in caplog.text


def test_receive_transition_bypasses_stale_local_physical_device(monkeypatch) -> None:
    thread = MidiThread(
        device_name="ROTO-CONTROL MIDI 1",
        input_mode="midi_only",
        remote_role="off",
        remote_instance_id=str(uuid.uuid4()),
        remote_name="Desktop",
    )
    transport = _FakeTransport(role=RemoteMidiRole.RECEIVE)
    received: list[object] = []

    def fail_local_enumeration() -> list[str]:
        pytest.fail("Receive mode must not enumerate or open the stale local MIDI device")

    def receive_once(active_transport) -> None:
        received.append(active_transport)
        thread._running = False

    monkeypatch.setattr(midi_module, "ensure_midi_backend", lambda: "rtmidi")
    monkeypatch.setattr(midi_module.mido, "get_input_names", fail_local_enumeration)
    monkeypatch.setattr(thread, "_ensure_remote_transport", lambda: transport)
    monkeypatch.setattr(thread, "_run_remote_receive_loop", receive_once)
    thread._running = True

    thread.set_remote_config("receive", thread._remote_instance_id, "Desktop", "", "")
    thread._run_safe()

    assert received == [transport]


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
