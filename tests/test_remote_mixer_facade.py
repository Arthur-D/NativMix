from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mido
import pytest
from PyQt6.QtCore import QSettings, Qt, pyqtSignal

from nativmix.audio.base import AudioBackendBase
from nativmix.audio.manager import PipeWireManager
from nativmix.gui import main_window, settings_panel
from nativmix.gui.main_window import ChannelWidget, MainWindow
from nativmix.gui.mixer_facade import RemoteMixerFacade, RemoteSyncSession
from nativmix.gui.settings_panel import SettingsPanel
from nativmix.hardware import midi as midi_module
from nativmix.hardware.midi import MidiThread, RemoteControllerOrigin
from nativmix.hardware.remote_midi import (
    DiscoveryChange,
    DiscoveryChangeKind,
    RemoteMidiTransport,
    peer_from_service,
)
from nativmix.main import wire_remote_mixer_control_plane
from nativmix.remote_sync.authority import ControlSessionMetadata, ReceiverMixerAuthority
from nativmix.remote_sync.protocol import (
    PROTOCOL_VERSION,
    AckMessage,
    CommandMessage,
    DeltaMessage,
    NackMessage,
    SnapshotMessage,
    SnapshotRequest,
)
from nativmix.remote_sync.schema import (
    SCHEMA_VERSION,
    ReceiverCapabilities,
    Snapshot,
    TargetInventoryItem,
    build_snapshot,
    normalize_profile,
    normalize_runtime_state,
)
from nativmix.remote_sync.target_inventory import ReceiverTargetInventory
from nativmix.utils.config_manager import ConfigManager
from nativmix.utils.profile_manager import ProfileManager

EPOCH = "00000000-0000-4000-8000-000000000001"
SESSION = "00000000-0000-4000-8000-000000000002"
PEER = "00000000-0000-4000-8000-000000000003"
PROFILE = "00000000-0000-4000-8000-000000000004"
CHANNEL = "00000000-0000-4000-8000-000000000005"


def _snapshot(
    *,
    revision: int = 0,
    label: str = "Music",
    volume: float = 0.4,
    saved_volume: float = 0.4,
    muted: bool = False,
    is_midi: bool = False,
    epoch: str = EPOCH,
    features: tuple[str, ...] = ("routing_pause", "remote_editing"),
    firefox_available: bool = True,
    mute_cc: int | None = 8,
    mute_channel: int = 3,
) -> Snapshot:
    profile = normalize_profile(
        {
            "id": PROFILE,
            "name": "Desktop",
            "channel_count": 1,
            "restore_fader_positions": True,
            "midi_switch_cc": 9,
            "channels": [
                {
                    "index": 0,
                    "label": label,
                    "is_midi": is_midi,
                    "mode": "app",
                    "app_names": ["Firefox"],
                    "routing_paused_apps": [],
                    "inverted": False,
                    "v_sink": False,
                    "midi_cc": 7,
                    "midi_channel": 2,
                    "midi_mute_cc": mute_cc,
                    "midi_mute_channel": mute_channel,
                    "volume": saved_volume,
                }
            ],
        },
        channel_ids=[CHANNEL],
    )
    runtime = normalize_runtime_state(
        {
            "channel_id": CHANNEL,
            "effective_volume": volume,
            "muted": muted,
            "available": True,
            "unresolved": False,
            "shared_target": False,
            "capability_state": "ok",
        }
    )
    return build_snapshot(
        epoch=epoch,
        revision=revision,
        profiles=[profile],
        active_profile_id=PROFILE,
        active_profile_name="Desktop",
        channel_order=[CHANNEL],
        runtime_states=[runtime],
        inventory=[
            TargetInventoryItem("pseudo:system-master", "System Master", "output", True),
            TargetInventoryItem("pseudo:other-apps", "Other Apps", "output", True),
            TargetInventoryItem("app:firefox", "Firefox", "output", firefox_available),
            TargetInventoryItem("app:missing", "Missing App", "output", False),
            TargetInventoryItem("device:headset", "USB Headset", "output", True),
        ],
        capabilities=ReceiverCapabilities(True, True, 32, features),
    )


def _envelope(message: Any, *, generation: int = 1, peer: str = PEER) -> SimpleNamespace:
    return SimpleNamespace(
        role="send",
        generation=generation,
        connected_peer_id=peer,
        transport_session_id=SESSION,
        message=message,
    )


def _snapshot_message(snapshot: Snapshot) -> SnapshotMessage:
    return SnapshotMessage(PROTOCOL_VERSION, SCHEMA_VERSION, SESSION, snapshot.to_canonical())


def _connected_model(
    *,
    clock: Callable[[], float] | None = None,
) -> tuple[RemoteMixerFacade, list[Any]]:
    sent: list[Any] = []
    kwargs = {"clock": clock} if clock is not None else {}
    model = RemoteMixerFacade(lambda message, _generation, _session: sent.append(message), **kwargs)
    model.begin_session(
        RemoteSyncSession(1, "send", PEER, PEER, "Studio PC", SESSION, True)
    )
    assert isinstance(sent.pop(), SnapshotRequest)
    model.handle_envelope(_envelope(_snapshot_message(_snapshot())))
    assert model.active
    return model, sent


def _last_command(sent: list[Any]) -> CommandMessage:
    assert len(sent) == 1
    assert isinstance(sent[0], CommandMessage)
    return sent[0]


def test_initial_snapshot_exposes_receiver_banner_state_and_inventory() -> None:
    model, _sent = _connected_model()

    assert model.sync_status == "Connected"
    assert model.sync_detail == "Controlling Studio PC - Desktop"
    assert model.active_profile_name == "Desktop"
    assert model.get_channel_label(0) == "Music"
    assert model.get_channel_volume(0) == 0.4
    assert [item.label for item in model.get_target_inventory("app")] == [
        "System Master",
        "Other Apps",
        "Firefox",
        "Missing App",
    ]
    assert [item.label for item in model.get_target_inventory("hardware")] == ["USB Headset"]


def test_receiver_target_availability_is_authoritative_on_sender() -> None:
    model, _sent = _connected_model()
    assert model.is_target_available("Firefox", "app") is True
    assert model.is_target_available("device:headset", "hardware") is True

    model.handle_envelope(
        _envelope(_snapshot_message(_snapshot(revision=1, firefox_available=False)))
    )

    assert model.is_target_available("Firefox", "app") is False
    assert model.is_target_available("Unknown old target", "app") is None


def test_legacy_snapshot_without_permission_metadata_keeps_prior_editable_semantics() -> None:
    sent: list[Any] = []
    model = RemoteMixerFacade(lambda message, _generation, _session: sent.append(message))
    model.begin_session(RemoteSyncSession(1, "send", PEER, PEER, "Legacy receiver", SESSION, True))
    sent.clear()
    model.handle_envelope(_envelope(_snapshot_message(_snapshot(features=()))))

    assert model.editing_allowed
    model.set_midi_mute_cc(0, 12, 1)
    assert _last_command(sent).command_type == "set_channel_mute_midi_binding"


@pytest.mark.parametrize(
    ("invoke", "command_type", "payload"),
    [
        (lambda m: m.create_profile("Blank", 4), "create_profile", {"name": "Blank", "channel_count": 4}),
        (
            lambda m: m.duplicate_profile(PROFILE, "Copy"),
            "duplicate_profile",
            {"profile_id": PROFILE, "name": "Copy"},
        ),
        (lambda m: m.rename_profile(PROFILE, "New"), "rename_profile", {"profile_id": PROFILE, "name": "New"}),
        (lambda m: m.select_profile(PROFILE), "select_profile", {"profile_id": PROFILE}),
        (lambda m: m.delete_profile(PROFILE), "delete_profile", {"profile_id": PROFILE}),
        (
            lambda m: m.set_profile_restore_fader_positions(PROFILE, False),
            "set_profile_restore_fader_positions",
            {"profile_id": PROFILE, "enabled": False},
        ),
        (
            lambda m: m.set_profile_midi_switch_cc(PROFILE, 11),
            "set_profile_midi_switch_cc",
            {"profile_id": PROFILE, "cc": 11},
        ),
        (lambda m: m.add_midi_channel(), "add_midi_channel", {"profile_id": PROFILE}),
        (
            lambda m: m.remove_midi_channel(0),
            "delete_midi_channels",
            {"profile_id": PROFILE, "channel_ids": [CHANNEL]},
        ),
        (
            lambda m: m.set_channel_order([0]),
            "reorder_channels",
            {"profile_id": PROFILE, "channel_ids": [CHANNEL]},
        ),
        (
            lambda m: m.set_channel_label(0, "Voice"),
            "set_channel_label",
            {"profile_id": PROFILE, "channel_id": CHANNEL, "label": "Voice"},
        ),
        (
            lambda m: m.set_inverted(0, True),
            "set_channel_inverted",
            {"profile_id": PROFILE, "channel_id": CHANNEL, "inverted": True},
        ),
        (
            lambda m: m.change_channel_mode(0, "hardware"),
            "set_channel_mode",
            {"profile_id": PROFILE, "channel_id": CHANNEL, "mode": "hardware"},
        ),
        (
            lambda m: m.toggle_mapping(0, "pseudo:other-apps"),
            "set_channel_mappings",
            {
                "profile_id": PROFILE,
                "channel_id": CHANNEL,
                "target_keys": ["pseudo:other-apps"],
            },
        ),
        (
            lambda m: m.toggle_hardware_target(0, "device:headset"),
            "set_channel_hardware_target",
            {"profile_id": PROFILE, "channel_id": CHANNEL, "target_key": "device:headset"},
        ),
        (
            lambda m: m.set_app_routing_paused(0, "Firefox", True),
            "set_channel_routing_paused",
            {"profile_id": PROFILE, "channel_id": CHANNEL, "target_key": "app:firefox", "paused": True},
        ),
        (
            lambda m: m.set_v_sink_enabled(0, True),
            "set_channel_v_sink",
            {"profile_id": PROFILE, "channel_id": CHANNEL, "enabled": True},
        ),
        (
            lambda m: m.set_midi_cc(0, 12, 4),
            "set_channel_volume_midi_binding",
            {"profile_id": PROFILE, "channel_id": CHANNEL, "cc": 12, "midi_channel": 4},
        ),
        (
            lambda m: m.set_midi_mute_cc(0, 13, 5),
            "set_channel_mute_midi_binding",
            {"profile_id": PROFILE, "channel_id": CHANNEL, "cc": 13, "midi_channel": 5},
        ),
        (
            lambda m: m.set_channel_volume(0, 0.75),
            "set_channel_volume",
            {"profile_id": PROFILE, "channel_id": CHANNEL, "volume": 0.75},
        ),
        (
            lambda m: m.toggle_mute(0),
            "set_channel_mute",
            {"profile_id": PROFILE, "channel_id": CHANNEL, "muted": True},
        ),
    ],
)
def test_every_receiver_operation_uses_one_typed_exact_revision_command(
    invoke: Callable[[RemoteMixerFacade], None],
    command_type: str,
    payload: dict[str, Any],
) -> None:
    model, sent = _connected_model()

    invoke(model)

    command = _last_command(sent)
    assert command.command_type == command_type
    assert command.payload == payload
    assert command.receiver_epoch == EPOCH
    assert command.expected_revision == 0
    uuid.UUID(command.command_id)


def test_canonical_state_is_never_optimistic_and_needs_publication_and_ack() -> None:
    model, sent = _connected_model()
    pending: list[tuple[str, bool]] = []
    model.pending_changed.connect(lambda key, state: pending.append((key, state)))

    model.set_channel_label(0, "Receiver value")
    command = _last_command(sent)
    assert model.get_channel_label(0) == "Music"
    assert model.is_pending(model.control_key(0, "label"))

    model.handle_envelope(
        _envelope(AckMessage(PROTOCOL_VERSION, SCHEMA_VERSION, SESSION, command.command_id, 1))
    )
    assert model.is_pending(model.control_key(0, "label"))

    model.handle_envelope(_envelope(_snapshot_message(_snapshot(revision=1, label="Receiver value"))))
    assert model.get_channel_label(0) == "Receiver value"
    assert not model.is_pending(model.control_key(0, "label"))
    assert pending[-1] == (model.control_key(0, "label"), False)


def test_receiver_mute_binding_edit_waits_for_canonical_revision_and_survives_stale_rejection() -> None:
    model, sent = _connected_model()

    model.set_midi_mute_cc(0, 42, 5)
    command = _last_command(sent)
    assert command.command_type == "set_channel_mute_midi_binding"
    assert model.get_midi_mute_cc(0) == 8

    model.handle_envelope(
        _envelope(
            NackMessage(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                SESSION,
                command.command_id,
                "stale_revision",
                EPOCH,
                0,
            )
        )
    )
    assert model.get_midi_mute_cc(0) == 8

    model.handle_envelope(
        _envelope(_snapshot_message(_snapshot(revision=1, mute_cc=42, mute_channel=5)))
    )
    assert model.get_midi_mute_cc(0) == 42
    assert model.get_midi_mute_channel(0) == 5


def test_regular_mapping_replaces_an_exclusive_pseudo_target() -> None:
    snapshot = _snapshot()
    profile = snapshot.profiles[0]
    exclusive_channel = replace(profile.channels[0], mappings=("System Master",))
    exclusive_profile = replace(profile, channels=(exclusive_channel,))
    exclusive_snapshot = build_snapshot(
        epoch=snapshot.epoch,
        revision=snapshot.revision,
        profiles=[exclusive_profile],
        active_profile_id=snapshot.active_profile_id,
        active_profile_name=snapshot.active_profile_name,
        channel_order=snapshot.channel_order,
        runtime_states=snapshot.runtime_states,
        inventory=snapshot.inventory,
        capabilities=snapshot.capabilities,
    )
    sent: list[Any] = []
    model = RemoteMixerFacade(lambda message, _generation, _session: sent.append(message))
    model.begin_session(RemoteSyncSession(1, "send", PEER, PEER, "Studio PC", SESSION, True))
    sent.clear()
    model.handle_envelope(_envelope(_snapshot_message(exclusive_snapshot)))

    model.toggle_mapping(0, "app:firefox")

    assert _last_command(sent).payload["target_keys"] == ["app:firefox"]


def test_desktop_local_edit_wins_and_stale_sender_command_resynchronizes() -> None:
    model, sent = _connected_model()
    rejections: list[str] = []
    model.rejection.connect(rejections.append)
    model.set_channel_label(0, "Laptop edit")
    command = _last_command(sent)

    model.handle_envelope(_envelope(_snapshot_message(_snapshot(revision=1, label="Desktop edit"))))
    model.handle_envelope(
        _envelope(
            NackMessage(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                SESSION,
                command.command_id,
                "stale_revision",
                EPOCH,
                1,
            )
        )
    )

    assert model.get_channel_label(0) == "Desktop edit"
    assert model.sync_status == "Conflict/resynchronizing"
    assert "changed first" in rejections[-1]
    assert isinstance(sent[-1], SnapshotRequest)


def test_valid_volume_delta_updates_display_without_emitting_a_command() -> None:
    model, sent = _connected_model()
    next_snapshot = _snapshot(revision=1, volume=0.8)
    delta = DeltaMessage(
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
        SESSION,
        EPOCH,
        0,
        1,
        next_snapshot.content_hash,
        {"volumes": {CHANNEL: {"volume": 0.8}}},
    )

    model.handle_envelope(_envelope(delta))

    assert model.get_channel_volume(0) == 0.8
    assert sent == []


def test_adjacent_controller_ack_updates_exact_state_and_only_latest_material_correction_moves_motor() -> None:
    model, sent = _connected_model()
    corrections: list[tuple[int, float]] = []
    model.controller_correction_requested.connect(
        lambda channel, volume, _origin: corrections.append((channel, volume))
    )

    def origin(rtp_sequence: int, local_sequence: int, requested: float) -> RemoteControllerOrigin:
        return RemoteControllerOrigin(1, SESSION, PEER, rtp_sequence, local_sequence, 2, 7, 0, requested)

    def apply_delta(revision: int, volume: float, event: RemoteControllerOrigin) -> None:
        current = model._snapshot
        assert current is not None
        next_snapshot = _snapshot(revision=revision, volume=volume)
        model.handle_envelope(
            _envelope(
                DeltaMessage(
                    PROTOCOL_VERSION,
                    SCHEMA_VERSION,
                    SESSION,
                    EPOCH,
                    revision - 1,
                    revision,
                    next_snapshot.content_hash,
                    {
                        "volumes": {CHANNEL: {"volume": volume}},
                        "origins": {CHANNEL: event.to_wire()},
                    },
                )
            )
        )

    acknowledged = origin(10, 1, 79 / 127)
    model.note_local_controller_origin(acknowledged)
    apply_delta(1, 80 / 127, acknowledged)
    assert model.get_channel_volume(0) == 80 / 127
    assert corrections == []

    material = origin(11, 2, 79 / 127)
    model.note_local_controller_origin(material)
    apply_delta(2, 81 / 127, material)
    assert model.get_channel_volume(0) == 81 / 127
    assert corrections == [(0, 81 / 127)]

    stale = origin(12, 3, 0.7)
    latest = origin(13, 4, 0.9)
    model.note_local_controller_origin(stale)
    model.note_local_controller_origin(latest)
    apply_delta(3, 0.5, stale)
    assert corrections == [(0, 81 / 127)]

    apply_delta(4, 0.8, latest)
    assert corrections == [(0, 81 / 127), (0, 0.8)]
    assert sent == []


@pytest.mark.parametrize(("canonical", "expected"), [(80 / 127, []), (81 / 127, [(0, 81 / 127)])])
def test_duplicate_sibling_provenance_dispatches_at_most_one_controller_correction(
    canonical: float,
    expected: list[tuple[int, float]],
) -> None:
    model, _sent = _connected_model()
    event = RemoteControllerOrigin(1, SESSION, PEER, 10, 1, 2, 7, 0, 79 / 127)
    model.note_local_controller_origin(event)
    corrections: list[tuple[int, float]] = []
    model.controller_correction_requested.connect(
        lambda channel, volume, _origin: corrections.append((channel, volume))
    )
    sibling = "00000000-0000-4000-8000-000000000006"
    origin_wire = event.to_wire()

    model._dispatch_controller_corrections(
        {CHANNEL: canonical, sibling: canonical},
        {CHANNEL: origin_wire, sibling: origin_wire},
        revision=1,
    )

    assert corrections == expected


def test_controller_origin_from_replaced_session_cannot_move_motor() -> None:
    model, _sent = _connected_model()
    stale = RemoteControllerOrigin(1, SESSION, PEER, 10, 1, 2, 7, 0, 0.6)
    model.note_local_controller_origin(stale)
    replacement_session = str(uuid.uuid4())
    model.begin_session(RemoteSyncSession(2, "send", PEER, PEER, "Receiver", replacement_session, True))
    corrections: list[float] = []
    model.controller_correction_requested.connect(
        lambda _channel, volume, _origin: corrections.append(volume)
    )

    model.note_local_controller_origin(stale)

    assert corrections == []
    assert not model._controller_origins


@pytest.mark.parametrize(
    "delta",
    [
        DeltaMessage(PROTOCOL_VERSION, SCHEMA_VERSION, SESSION, EPOCH, 2, 3, "bad", {"volumes": {}}),
        DeltaMessage(
            PROTOCOL_VERSION,
            SCHEMA_VERSION,
            SESSION,
            EPOCH,
            0,
            1,
            "bad",
            {"volumes": {CHANNEL: {"volume": 0.8}}},
        ),
    ],
)
def test_revision_gap_and_hash_mismatch_request_fresh_snapshot(delta: DeltaMessage) -> None:
    model, sent = _connected_model()

    model.handle_envelope(_envelope(delta))

    assert model.sync_status == "Conflict/resynchronizing"
    assert isinstance(sent[-1], SnapshotRequest)


def test_timeout_clears_pending_without_retrying_command() -> None:
    now = [0.0]
    model, sent = _connected_model(clock=lambda: now[0])
    model.set_channel_label(0, "Never acknowledged")
    command = _last_command(sent)
    now[0] = 6.0

    model.expire_pending()

    assert not model.is_pending(model.control_key(0, "label"))
    assert sent.count(command) == 1
    assert isinstance(sent[-1], SnapshotRequest)


def test_slider_updates_are_bounded_and_coalesced_while_one_is_in_flight() -> None:
    model, sent = _connected_model()

    model.set_channel_volume(0, 0.1)
    first = _last_command(sent)
    model.set_channel_volume(0, 0.2)
    model.set_channel_volume(0, 0.3)
    model.set_channel_volume(0, 0.9)

    assert len(sent) == 1
    assert len(model._queue) == 1
    assert model._queue[0].payload["volume"] == 0.9
    assert first.payload["volume"] == 0.1


def test_epoch_restart_disconnect_and_stale_generation_discard_remote_state() -> None:
    model, sent = _connected_model()
    model.begin_session(RemoteSyncSession(2, "send", PEER, PEER, "Studio PC", SESSION, False, "lost"))
    assert not model.active
    assert model.sync_status == "MIDI-only"

    model.begin_session(RemoteSyncSession(1, "send", PEER, PEER, "Old", SESSION, True))
    assert not model.active
    assert sent == []

    replacement_session = str(uuid.uuid4())
    model.begin_session(RemoteSyncSession(3, "send", PEER, PEER, "Studio PC", replacement_session, True))
    assert model.sync_status == "Syncing"
    assert isinstance(sent[-1], SnapshotRequest)


def test_permission_timeout_and_version_status_preserve_midi_only_mode() -> None:
    now = [0.0]
    sent: list[Any] = []
    model = RemoteMixerFacade(lambda message, _generation, _session: sent.append(message), clock=lambda: now[0])
    model.begin_session(RemoteSyncSession(1, "send", PEER, PEER, "Studio PC", SESSION, True))
    now[0] = 6.0
    model.expire_pending()
    assert model.sync_status == "Syncing"
    assert len(sent) == 2
    assert "AppleMIDI remains available" in model.sync_detail

    model.apply_transport_status("Version incompatible", "The receiver uses another version.")
    assert model.sync_status == "Version incompatible"
    assert "AppleMIDI remains available" in model.sync_detail


def test_permission_denial_keeps_mirror_read_only_and_fresh_snapshot_reenables_edits() -> None:
    model, sent = _connected_model()
    old_revision = model._snapshot.revision
    denial = NackMessage(
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
        SESSION,
        str(uuid.uuid4()),
        "permission_disabled",
        EPOCH,
        old_revision,
    )
    model.handle_envelope(_envelope(denial))
    assert model.active
    assert not model.editing_allowed
    assert model.sync_status == "Read-only"
    assert model._session is not None
    assert isinstance(sent[-1], SnapshotRequest)

    statuses: list[str] = []
    model.status_changed.connect(lambda status, _detail: statuses.append(status))
    fresh = _snapshot(revision=old_revision + 1, label="Receiver restored")
    model.handle_envelope(_envelope(_snapshot_message(fresh)))
    assert statuses == ["Connected"]
    assert model.active
    assert model.get_channel_label(0) == "Receiver restored"

    model.handle_envelope(_envelope(denial))
    assert model.active
    assert model.get_channel_label(0) == "Receiver restored"


def test_stale_generation_and_session_permission_denials_are_ignored() -> None:
    model, _sent = _connected_model()
    denial = NackMessage(
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
        SESSION,
        str(uuid.uuid4()),
        "permission_disabled",
        EPOCH,
        0,
    )
    model.handle_envelope(_envelope(denial, generation=0))
    model.handle_envelope(
        SimpleNamespace(
            role="send",
            generation=1,
            connected_peer_id=PEER,
            transport_session_id=str(uuid.uuid4()),
            message=denial,
        )
    )
    assert model.active
    assert model.sync_status == "Connected"


def test_duplicate_and_wrong_peer_publications_do_not_change_state() -> None:
    model, sent = _connected_model()
    changed = _snapshot(revision=1, label="Wrong peer")

    model.handle_envelope(_envelope(_snapshot_message(changed), peer=str(uuid.uuid4())))
    model.handle_envelope(_envelope(_snapshot_message(changed), generation=0))

    assert model.get_channel_label(0) == "Music"
    assert sent == []


def test_duplicate_delta_is_idempotently_ignored() -> None:
    model, sent = _connected_model()
    next_snapshot = _snapshot(revision=1, volume=0.8)
    delta = DeltaMessage(
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
        SESSION,
        EPOCH,
        0,
        1,
        next_snapshot.content_hash,
        {"volumes": {CHANNEL: {"volume": 0.8}}},
    )
    model.handle_envelope(_envelope(delta))
    model.handle_envelope(_envelope(delta))

    assert model.get_channel_volume(0) == 0.8
    assert model.sync_status == "Connected"
    assert sent == []


def test_remote_facade_has_no_laptop_manager_or_audio_references(tmp_path: Path) -> None:
    sentinel = tmp_path / "local-profile.json"
    sentinel.write_text('{"label":"Laptop"}')
    before = sentinel.read_bytes()
    model, sent = _connected_model()

    model.set_channel_label(0, "Receiver only")

    assert _last_command(sent).command_type == "set_channel_label"
    assert sentinel.read_bytes() == before
    assert not hasattr(model, "config")
    assert not hasattr(model, "profiles")
    assert not hasattr(model, "backend")


class _Backend(AudioBackendBase):
    channel_volume_changed = pyqtSignal(int, float)
    other_apps_changed = pyqtSignal(list)
    unresolved_targets_changed = pyqtSignal(set)
    status_changed = pyqtSignal(str, str)
    capability_changed = pyqtSignal(str, bool)
    mute_state_changed = pyqtSignal(int, bool)
    target_inventory_changed = pyqtSignal()

    gain_control_supported = True
    v_sink_supported = True
    v_sink_capability_reason = ""

    def __init__(self) -> None:
        super().__init__()
        self.volumes: list[tuple[int, float]] = []
        self.muted: dict[int, bool] = {}
        self.shared_channels: dict[int, list[int]] = {}
        self.streams: list[Any] = []
        self.sinks: list[tuple[str, str]] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def get_real_sinks(self) -> list[Any]:
        return list(self.sinks)

    def get_real_sources(self) -> list[Any]:
        return []

    def get_active_streams(self) -> list[Any]:
        return list(self.streams)

    def get_unresolved_targets(self) -> set[str]:
        return set()

    def get_default_sink_name(self) -> None:
        return None

    def set_channel_volume(self, channel_index: int, volume: float) -> None:
        self.volumes.append((channel_index, volume))

    def get_effective_shared_target_channels(self, channel_index: int) -> list[int]:
        return self.shared_channels.get(channel_index, [channel_index])

    def is_channel_muted(self, channel_index: int) -> bool:
        return self.muted.get(channel_index, False)

    def toggle_mute(self, channel_index: int) -> None:
        self.muted[channel_index] = not self.muted.get(channel_index, False)


def test_receiver_inventory_signal_publishes_canonical_availability_change(
    tmp_path: Path,
) -> None:
    profiles_dir = tmp_path / "receiver-profiles"
    config = ConfigManager(config_path=tmp_path / "receiver.json", profiles_dir=profiles_dir)
    profiles = ProfileManager(profiles_dir=profiles_dir)
    profiles.set_active_silently(config.active_profile_id)
    config.apply_profile(profiles.load(config.active_profile_id))
    config.remote_midi_role = "receive"
    config.allow_remote_mixer_editing = True
    config.set_app_names(0, ["Firefox"])
    backend = _Backend()
    inventory = ReceiverTargetInventory(config, backend)
    sent: list[Any] = []
    authority = ReceiverMixerAuthority(
        config,
        profiles,
        backend,
        inventory_provider=inventory,
        protocol_message_sender=lambda message, _generation, _session: sent.append(message),
        active_session=ControlSessionMetadata(
            transport_session_id=SESSION,
            control_session_id=str(uuid.UUID(int=0)),
            generation=1,
            permission_enabled=True,
            selected_peer_id=PEER,
            connected_peer_id=PEER,
        ),
    )
    authority.prime_observed_state()
    authority.connect_local_sources()

    backend.streams = [SimpleNamespace(app_name="Firefox")]
    backend.target_inventory_changed.emit()

    assert isinstance(sent[-1], SnapshotMessage)
    firefox = next(item for item in sent[-1].snapshot["inventory"] if item["label"] == "Firefox")
    assert firefox["available"] is True


def test_channel_widget_programmatic_state_never_enters_user_command_path(
    tmp_path: Path,
    qtbot,
) -> None:
    profiles_dir = tmp_path / "profiles"
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=profiles_dir)
    backend = _Backend()
    widget = ChannelWidget(0, config, backend)
    qtbot.addWidget(widget)

    widget.set_volume(0.25)
    widget.set_mute_state(True)

    assert backend.volumes == []
    assert backend.muted == {}

    widget.set_mute_state(False)
    widget._slider.setFocus()
    qtbot.keyClick(widget._slider, Qt.Key.Key_Up)
    qtbot.mouseClick(widget._mute_btn, Qt.MouseButton.LeftButton)

    assert backend.volumes == [(0, pytest.approx(0.26))]
    assert backend.muted == {0: True}


def test_main_window_remote_banner_commands_and_exact_local_restoration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot,
) -> None:
    profiles_dir = tmp_path / "profiles"
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=profiles_dir)
    profiles = ProfileManager(profiles_dir=profiles_dir)
    profiles.set_active_silently(config.active_profile_id)
    local_profile = profiles.load(config.active_profile_id)
    config.apply_profile(local_profile)
    local_label = config.get_channel_label(0)
    local_file = next(profiles_dir.glob("*.json"))
    local_bytes = local_file.read_bytes()
    backend = _Backend()
    monkeypatch.setattr(settings_panel, "update_checks_supported", lambda: True)
    monkeypatch.setattr(
        main_window,
        "QSettings",
        lambda *_args: QSettings(
            str(tmp_path / "gui.ini"),
            QSettings.Format.IniFormat,
        ),
    )
    window = MainWindow(config=config, backend=backend, profile_manager=profiles)
    qtbot.addWidget(window)
    model, sent = _connected_model()

    window.set_mixer_facade(model)
    assert window._remote_banner.text() == "Remote mixer — Studio PC"
    assert not window._remote_banner.isHidden()
    assert window._channels[0]._ch_label.text() == "Music"
    assert not window.settings_panel._master_box.isEnabled()
    assert not window.settings_panel._panic_btn.isEnabled()

    window._channels[0]._on_slider_changed(75)
    assert _last_command(sent).command_type == "set_channel_volume"
    assert window._channels[0]._slider.value() == 40
    assert backend.volumes == []
    assert config.get_channel_label(0) == local_label
    assert local_file.read_bytes() == local_bytes

    window.set_mixer_facade(window._local_mixer)
    assert window._remote_banner.isHidden()
    assert window._channels[0]._ch_label.text() == (local_label or "CH 1")
    assert local_file.read_bytes() == local_bytes
    assert window.settings_panel._master_box.isEnabled()
    assert window.settings_panel._panic_btn.isEnabled()


def test_remote_strip_shows_receiver_mute_binding_and_receiver_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot,
) -> None:
    profiles_dir = tmp_path / "profiles"
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=profiles_dir)
    profiles = ProfileManager(profiles_dir=profiles_dir)
    profiles.set_active_silently(config.active_profile_id)
    config.apply_profile(profiles.load(config.active_profile_id))
    backend = _Backend()
    monkeypatch.setattr(settings_panel, "update_checks_supported", lambda: True)
    window = MainWindow(config=config, backend=backend, profile_manager=profiles)
    qtbot.addWidget(window)
    model, _sent = _connected_model()

    window.set_mixer_facade(model)
    channel = window._channels[0]
    assert not channel.is_midi_channel
    assert channel._mute_learn_btn.text() == "4:8"
    assert channel._mute_learn_btn.isEnabled()
    row = channel._app_list_layout.itemAt(0).widget()
    assert isinstance(row, main_window._AppRow)
    assert not row._name_label.font().italic()

    model.handle_envelope(
        _envelope(
            _snapshot_message(
                _snapshot(revision=1, features=("remote_permissions",), firefox_available=False)
            )
        )
    )
    row = window._channels[0]._app_list_layout.itemAt(0).widget()
    assert isinstance(row, main_window._AppRow)
    assert row._name_label.font().italic()
    assert "Receiver target" in row._name_label.toolTip()
    assert window._channels[0]._mute_learn_btn.text() == "4:8"
    assert not window._channels[0]._mute_learn_btn.isEnabled()
    assert window._remote_banner.text() == "Remote mixer — Studio PC"
    assert not window._remote_banner.styleSheet()


def test_read_only_receiver_binding_rejects_edits_without_laptop_persistence(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "sender.json"
    profiles_dir = tmp_path / "profiles"
    config = ConfigManager(config_path=config_path, profiles_dir=profiles_dir)
    config.save()
    before_config = config_path.read_bytes()
    before_profiles = {path.name: path.read_bytes() for path in profiles_dir.glob("*.json")}
    sent: list[Any] = []
    model = RemoteMixerFacade(lambda message, _generation, _session: sent.append(message))
    model.begin_session(RemoteSyncSession(1, "send", PEER, PEER, "Studio PC", SESSION, True))
    sent.clear()
    model.handle_envelope(
        _envelope(_snapshot_message(_snapshot(features=("remote_permissions",))))
    )

    model.set_midi_mute_cc(0, 42, 5)

    assert sent == []
    assert model.get_midi_mute_cc(0) == 8
    assert config_path.read_bytes() == before_config
    assert {path.name: path.read_bytes() for path in profiles_dir.glob("*.json")} == before_profiles


def test_receiver_binding_survives_reconnect_until_new_canonical_snapshot() -> None:
    model, _sent = _connected_model()
    assert model.get_midi_mute_cc(0) == 8
    new_session = str(uuid.uuid4())

    model.begin_session(RemoteSyncSession(2, "send", PEER, PEER, "Studio PC", new_session, True))
    assert model.get_midi_mute_cc(0) is None
    fresh = _snapshot(revision=0)
    model.handle_envelope(
        SimpleNamespace(
            role="send",
            generation=2,
            connected_peer_id=PEER,
            transport_session_id=new_session,
            message=SnapshotMessage(PROTOCOL_VERSION, SCHEMA_VERSION, new_session, fresh.to_canonical()),
        )
    )

    assert model.get_midi_mute_cc(0) == 8


def test_local_backend_capability_events_cannot_override_remote_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot,
) -> None:
    profiles_dir = tmp_path / "profiles"
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=profiles_dir)
    profiles = ProfileManager(profiles_dir=profiles_dir)
    profiles.set_active_silently(config.active_profile_id)
    config.apply_profile(profiles.load(config.active_profile_id))
    backend = _Backend()
    monkeypatch.setattr(settings_panel, "update_checks_supported", lambda: True)
    window = MainWindow(config=config, backend=backend, profile_manager=profiles)
    qtbot.addWidget(window)
    model, _sent = _connected_model()
    window.set_mixer_facade(model)

    backend.capability_changed.emit("gain_control_supported", False)
    backend.capability_changed.emit("v_sink_supported", False)
    backend.mute_state_changed.emit(0, True)

    assert window._channels[0]._slider.isEnabled()
    assert window._channels[0]._vsink_cb.isEnabled()
    assert not window._channels[0]._muted


def test_remote_midi_learn_label_renders_only_canonical_binding(qtbot) -> None:
    sent: list[Any] = []
    model = RemoteMixerFacade(lambda message, _generation, _session: sent.append(message))
    model.begin_session(RemoteSyncSession(1, "send", PEER, PEER, "Studio PC", SESSION, True))
    sent.clear()
    model.handle_envelope(_envelope(_snapshot_message(_snapshot(is_midi=True))))
    backend = _Backend()
    widget = ChannelWidget(0, model, backend, is_midi=True)
    qtbot.addWidget(widget)

    model.set_midi_cc(0, 42, 5)
    command = _last_command(sent)
    widget.update_midi_cc(42, 5)
    assert widget._learn_btn.text() == "3:7"

    model.handle_envelope(
        _envelope(
            NackMessage(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                SESSION,
                command.command_id,
                "stale_revision",
                EPOCH,
                0,
            )
        )
    )
    widget.refresh()
    assert widget._learn_btn.text() == "3:7"


def test_remote_direct_profile_cc_clear_does_not_save_laptop_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot,
) -> None:
    profiles_dir = tmp_path / "profiles"
    config_path = tmp_path / "config.json"
    config = ConfigManager(config_path=config_path, profiles_dir=profiles_dir)
    profiles = ProfileManager(profiles_dir=profiles_dir)
    profiles.set_active_silently(config.active_profile_id)
    config.apply_profile(profiles.load(config.active_profile_id))
    config.save()
    before = config_path.read_bytes()
    monkeypatch.setattr(settings_panel, "update_checks_supported", lambda: True)
    model, sent = _connected_model()
    panel = SettingsPanel(config, profile_manager=profiles, mixer_facade=model)
    qtbot.addWidget(panel)
    panel.set_mixer_facade(model)

    panel._clear_profile_cc("direct")

    assert _last_command(sent).command_type == "set_profile_midi_switch_cc"
    assert config_path.read_bytes() == before


def test_in_process_sender_receiver_converges_and_receiver_remains_authoritative(
    tmp_path: Path,
    qapp,
) -> None:
    profiles_dir = tmp_path / "receiver-profiles"
    config = ConfigManager(config_path=tmp_path / "receiver.json", profiles_dir=profiles_dir)
    config.allow_remote_mixer_editing = True
    config.remote_midi_role = "receive"
    profiles = ProfileManager(profiles_dir=profiles_dir)
    profiles.set_active_silently(config.active_profile_id)
    config.apply_profile(profiles.load(config.active_profile_id))
    backend = _Backend()
    model_holder: dict[str, RemoteMixerFacade] = {}

    def publish(message: Any, generation: int, _session: str) -> None:
        model_holder["model"].handle_envelope(_envelope(message, generation=generation))

    authority = ReceiverMixerAuthority(
        config,
        profiles,
        backend,
        capabilities_provider=ReceiverCapabilities(True, True, 32, ("routing_pause",)),
        inventory_provider=[
            TargetInventoryItem("app:firefox", "Firefox", "output", True),
            TargetInventoryItem("device:headset", "USB Headset", "output", True),
        ],
        protocol_message_sender=publish,
        active_session=ControlSessionMetadata(
            SESSION,
            str(uuid.UUID(int=0)),
            generation=1,
            selected_peer_id=PEER,
            connected_peer_id=PEER,
        ),
    )

    def send_to_receiver(message: Any, generation: int, _session: str) -> None:
        if isinstance(message, SnapshotRequest):
            publish(_snapshot_message(authority.current_snapshot()), generation, SESSION)
            return
        assert isinstance(message, CommandMessage)
        result = authority.process_command(
            message,
            generation=generation,
            selected_peer_id=PEER,
            connected_peer_id=PEER,
            envelope_transport_session_id=SESSION,
        )
        if result.publication is not None:
            publish(result.publication.to_protocol_message(SESSION), generation, SESSION)
        publish(result.response, generation, SESSION)

    model = RemoteMixerFacade(send_to_receiver)
    model_holder["model"] = model
    model.begin_session(RemoteSyncSession(1, "send", PEER, PEER, "Receiver", SESSION, True))
    receiver_profile_id = model.active_profile_id

    model.set_channel_label(0, "From laptop")
    assert model.get_channel_label(0) == "From laptop"
    assert config.get_channel_label(0) == "From laptop"

    config.set_channel_label(0, "Desktop wins")
    config.save()
    authority.capture_local_mutation("label")
    assert model.get_channel_label(0) == "Desktop wins"

    old_epoch = authority.epoch
    model.dispose("Network interrupted.")
    replacement_epoch = str(uuid.uuid4())
    current = authority.current_snapshot()
    fresh = build_snapshot(
        epoch=replacement_epoch,
        revision=0,
        profiles=current.profiles,
        active_profile_id=current.active_profile_id,
        active_profile_name=current.active_profile_name,
        channel_order=current.channel_order,
        runtime_states=current.runtime_states,
        inventory=current.inventory,
        capabilities=current.capabilities,
    )
    new_session = str(uuid.uuid4())
    model.begin_session(RemoteSyncSession(2, "send", PEER, PEER, "Receiver", new_session, True))
    model.handle_envelope(
        SimpleNamespace(
            role="send",
            generation=2,
            connected_peer_id=PEER,
            transport_session_id=new_session,
            message=SnapshotMessage(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                new_session,
                fresh.to_canonical(),
            ),
        )
    )
    assert old_epoch != replacement_epoch
    assert model.active_profile_id == receiver_profile_id
    assert model.get_channel_label(0) == "Desktop wins"


def test_two_peer_gui_renders_live_permission_as_editable_or_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot,
) -> None:
    receiver_profiles_dir = tmp_path / "receiver-profiles"
    receiver_config = ConfigManager(
        config_path=tmp_path / "receiver.json",
        profiles_dir=receiver_profiles_dir,
    )
    receiver_config.remote_midi_role = "receive"
    receiver_profiles = ProfileManager(profiles_dir=receiver_profiles_dir)
    receiver_profiles.set_active_silently(receiver_config.active_profile_id)
    receiver_config.apply_profile(receiver_profiles.load(receiver_config.active_profile_id))
    receiver_config.set_channel_label(0, "Receiver channel")
    receiver_backend = _Backend()
    model_holder: dict[str, RemoteMixerFacade] = {}

    def publish(message: Any, generation: int, session_id: str) -> None:
        model_holder["model"].handle_envelope(
            SimpleNamespace(
                role="send",
                generation=generation,
                connected_peer_id=PEER,
                transport_session_id=session_id,
                message=message,
            )
        )

    authority = ReceiverMixerAuthority(
        receiver_config,
        receiver_profiles,
        receiver_backend,
        capabilities_provider=ReceiverCapabilities(True, True, 32, ("routing_pause",)),
        inventory_provider=[],
        protocol_message_sender=publish,
    )
    authority.prime_observed_state()
    authority.connect_local_sources()
    authority.begin_transport_session(
        SimpleNamespace(
            generation=1,
            role="receive",
            selected_peer_id=PEER,
            connected_peer_id=PEER,
            connected_peer_name="Laptop",
            transport_session_id=SESSION,
            available=True,
        )
    )

    def send_to_receiver(message: Any, generation: int, session_id: str) -> None:
        authority.queue_control_envelope(
            SimpleNamespace(
                generation=generation,
                role="receive",
                selected_peer_id=PEER,
                connected_peer_id=PEER,
                transport_session_id=session_id,
                message=message,
            )
        )

    model = RemoteMixerFacade(send_to_receiver)
    model_holder["model"] = model

    sender_profiles_dir = tmp_path / "sender-profiles"
    sender_config = ConfigManager(
        config_path=tmp_path / "sender.json",
        profiles_dir=sender_profiles_dir,
    )
    sender_profiles = ProfileManager(profiles_dir=sender_profiles_dir)
    sender_profiles.set_active_silently(sender_config.active_profile_id)
    sender_config.apply_profile(sender_profiles.load(sender_config.active_profile_id))
    sender_config.set_channel_label(0, "Laptop channel")
    monkeypatch.setattr(settings_panel, "update_checks_supported", lambda: True)
    monkeypatch.setattr(
        main_window,
        "QSettings",
        lambda *_args: QSettings(
            str(tmp_path / "sender-gui.ini"),
            QSettings.Format.IniFormat,
        ),
    )
    window = MainWindow(
        config=sender_config,
        backend=_Backend(),
        profile_manager=sender_profiles,
    )
    qtbot.addWidget(window)
    model.active_changed.connect(
        lambda active: window.set_mixer_facade(model if active else window._local_mixer)
    )

    model.begin_session(RemoteSyncSession(1, "send", PEER, PEER, "Receiver", SESSION, True))
    assert model.sync_status == "Read-only"
    assert window.mixer_facade is model
    assert window._channels[0]._ch_label.text() == "Receiver channel"
    assert not window._channels[0]._mute_learn_btn.isEnabled()

    receiver_config.allow_remote_mixer_editing = True
    assert model.active
    assert model.editing_allowed
    assert window.mixer_facade is model
    assert window._channels[0]._ch_label.text() == "Receiver channel"
    assert window._channels[0]._mute_learn_btn.isEnabled()

    receiver_config.allow_remote_mixer_editing = False
    assert model.active
    assert not model.editing_allowed
    assert model.sync_status == "Read-only"
    assert window.mixer_facade is model
    assert window._channels[0]._ch_label.text() == "Receiver channel"


def test_composition_wiring_distinct_hosts_live_permission_replaces_sender_strips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot,
) -> None:
    class Discovery:
        def __init__(self, emit: Callable[[DiscoveryChange], None]) -> None:
            self.emit = emit
            self.advertisement: Mapping[str, Any] | None = None

        def start(self, advertisement: Mapping[str, Any] | None) -> None:
            self.advertisement = advertisement

        def refresh(self) -> None:
            return

        def close(self) -> None:
            return

    class StatusPanel:
        def __init__(self) -> None:
            self.states: list[tuple[int, str, str]] = []

        def apply_remote_sync_status(self, generation: int, status: str, detail: str) -> None:
            self.states.append((generation, status, detail))

    sender_profiles_dir = tmp_path / "sender-profiles"
    sender_config_path = tmp_path / "sender.json"
    sender_config = ConfigManager(config_path=sender_config_path, profiles_dir=sender_profiles_dir)
    sender_profiles = ProfileManager(profiles_dir=sender_profiles_dir)
    sender_profiles.set_active_silently(sender_config.active_profile_id)
    sender_config.apply_profile(sender_profiles.load(sender_config.active_profile_id))
    sender_config.set_channel_label(0, "Laptop local channel")
    sender_config.remote_midi_role = "send"
    sender_config.input_mode = "midi_only"
    sender_config.midi_device = "ROTO-CONTROL MIDI 1 24:0"
    sender_config.save()
    sender_before = sender_config_path.read_bytes()
    sender_profile_before = {path.name: path.read_bytes() for path in sender_profiles_dir.glob("*.json")}

    receiver_profiles_dir = tmp_path / "receiver-profiles"
    receiver_config = ConfigManager(
        config_path=tmp_path / "receiver.json",
        profiles_dir=receiver_profiles_dir,
    )
    receiver_profiles = ProfileManager(profiles_dir=receiver_profiles_dir)
    receiver_profile_id = receiver_profiles.create("Eighteen channels", channel_count=18)
    receiver_profiles.set_active_silently(receiver_profile_id)
    receiver_config.active_profile_id = receiver_profile_id
    receiver_config.apply_profile(receiver_profiles.load(receiver_profile_id))
    receiver_config.set_channel_label(0, "Receiver authoritative channel")
    receiver_config.set_app_names(0, ["Firefox"])
    receiver_config.set_app_names(1, ["Spotify"])
    receiver_config.remote_midi_role = "receive"
    receiver_config.allow_remote_mixer_editing = False
    receiver_backend = PipeWireManager(receiver_config)
    receiver_backend.pw_only_mode = True
    receiver_backend.effective_routing_owner = "none"
    audio_writes: list[tuple[int, float]] = []
    audio_channels = {"Firefox": 0, "Spotify": 1}
    monkeypatch.setattr(
        receiver_backend,
        "_apply_volume_by_name_pw_only",
        lambda app_name, volume: audio_writes.append((audio_channels[app_name], volume)),
    )
    authority = ReceiverMixerAuthority(
        receiver_config,
        receiver_profiles,
        receiver_backend,
        capabilities_provider=ReceiverCapabilities(True, True, 32, ("routing_pause",)),
        inventory_provider=[],
        protocol_message_sender=lambda message, generation, session: receiver_midi.request_remote_sync_send(
            message, generation, session
        ),
    )
    authority.prime_observed_state()
    authority.connect_local_sources()

    sender_midi = MidiThread(
        device_name=sender_config.midi_device,
        input_mode="midi_only",
        remote_role="send",
        remote_instance_id=sender_config.remote_midi_instance_id,
        remote_name="Laptop",
    )
    receiver_midi = MidiThread(
        input_mode="midi_only",
        remote_role="receive",
        remote_instance_id=receiver_config.remote_midi_instance_id,
        remote_name="Desktop",
        remote_peer_id=sender_config.remote_midi_instance_id,
        remote_peer_name="Laptop",
    )
    receiver_midi.set_fader_feedback_enabled(True)
    sender_model = RemoteMixerFacade(sender_midi.request_remote_sync_send)
    receiver_unused_model = RemoteMixerFacade(receiver_midi.request_remote_sync_send)

    monkeypatch.setattr(settings_panel, "update_checks_supported", lambda: True)
    monkeypatch.setattr(
        main_window,
        "QSettings",
        lambda *_args: QSettings(str(tmp_path / "sender-gui.ini"), QSettings.Format.IniFormat),
    )
    window = MainWindow(config=sender_config, backend=_Backend(), profile_manager=sender_profiles)
    qtbot.addWidget(window)
    receiver_window = MainWindow(
        config=receiver_config,
        backend=receiver_backend,
        profile_manager=receiver_profiles,
    )
    qtbot.addWidget(receiver_window)
    receiver_backend.channel_volume_changed.connect(receiver_window.on_channel_volume_changed)
    receiver_backend.mute_state_changed.connect(receiver_window.on_mute_state_changed)
    receiver_panel = StatusPanel()
    sender_wiring = wire_remote_mixer_control_plane(
        sender_config,
        sender_midi,
        sender_model,
        authority,
        window.settings_panel,
        lambda active: window.set_mixer_facade(sender_model if active else window._local_mixer),
    )
    receiver_wiring = wire_remote_mixer_control_plane(
        receiver_config,
        receiver_midi,
        receiver_unused_model,
        authority,
        receiver_panel,
        lambda _active: None,
    )
    assert sender_wiring
    assert receiver_wiring

    sender_discoveries: list[Discovery] = []
    receiver_discoveries: list[Discovery] = []

    def sender_discovery(emit: Callable[[DiscoveryChange], None]) -> Discovery:
        discovery = Discovery(emit)
        sender_discoveries.append(discovery)
        return discovery

    def receiver_discovery(emit: Callable[[DiscoveryChange], None]) -> Discovery:
        discovery = Discovery(emit)
        receiver_discoveries.append(discovery)
        return discovery

    def production_transport_factory(role: str, *args: Any, **kwargs: Any) -> RemoteMidiTransport:
        kwargs["bind_host"] = "127.0.0.2" if role == "send" else "127.0.0.3"
        kwargs["control_port"] = 0
        kwargs["data_port"] = 0
        kwargs["sync_port"] = 0
        kwargs["discovery_factory"] = sender_discovery if role == "send" else receiver_discovery
        return RemoteMidiTransport(role, *args, **kwargs)

    monkeypatch.setattr(midi_module, "RemoteMidiTransport", production_transport_factory)
    sender_transport = sender_midi._ensure_remote_transport()
    receiver_transport = receiver_midi._ensure_remote_transport()
    assert sender_transport is not None
    assert receiver_transport is not None

    try:
        advertisement = sender_discoveries[0].advertisement
        assert advertisement is not None
        peer = peer_from_service(
            str(advertisement["service_name"]),
            ["127.0.0.2"],
            sender_transport.control_port,
            advertisement["properties"],
        )
        assert peer is not None
        assert peer.controller_name == "ROTO-CONTROL MIDI 1"
        assert peer.sync_port == sender_transport.sync_listener_port
        receiver_discoveries[0].emit(DiscoveryChange(DiscoveryChangeKind.ADD, peer.service_name, peer))

        for _ in range(1000):
            receiver_midi._poll_remote_transport()
            sender_midi._poll_remote_transport()
            qtbot.wait(0)
            if sender_model.sync_status == "Read-only":
                break
        assert sender_transport.snapshot.sync_available
        assert receiver_transport.snapshot.sync_available
        assert sender_model.active
        assert not sender_model.editing_allowed
        assert window._channels[0]._ch_label.text() == "Receiver authoritative channel"

        receiver_config.allow_remote_mixer_editing = True
        for _ in range(1000):
            receiver_midi._poll_remote_transport()
            sender_midi._poll_remote_transport()
            qtbot.wait(0)
            if sender_model.editing_allowed:
                break

        assert sender_model.active
        assert sender_model.editing_allowed
        assert sender_model.num_channels == 18
        assert window.mixer_facade is sender_model
        assert window._channels[0]._ch_label.text() == "Receiver authoritative channel"
        assert sender_config_path.read_bytes() == sender_before
        assert {path.name: path.read_bytes() for path in sender_profiles_dir.glob("*.json")} == sender_profile_before

        receiver_midi.update_mappings({(3, 10): 0, (4, 11): 1})
        sender_midi.update_mappings({(3, 10): 0, (4, 11): 1})
        def apply_remote_volume_batch() -> None:
            batch = receiver_midi.take_remote_volume_batch()
            for _channel, _volume, origin in batch:
                if origin is not None:
                    authority.note_remote_controller_origin(origin)
            receiver_backend.apply_midi_volumes(
                [(channel, volume) for channel, volume, _origin in batch]
            )

        receiver_midi.remote_volume_batch_ready.connect(
            apply_remote_volume_batch,
            Qt.ConnectionType.QueuedConnection,
        )
        receiver_backend.channel_volume_changed.connect(
            lambda channel, volume: receiver_midi.request_fader_sync(
                [(channel, volume)],
                suppressed_bindings=authority.feedback_suppressed_bindings(channel, volume),
            )
        )
        sender_model.controller_correction_requested.connect(
            lambda channel, volume, _origin: sender_midi.request_fader_sync([(channel, volume)])
        )

        class PhysicalInput:
            def __init__(self, messages: list[mido.Message]) -> None:
                self.messages = iter(messages)

            def receive(self, block: bool = False) -> mido.Message | None:
                assert not block
                message = next(self.messages, None)
                if message is None:
                    sender_midi._running = False
                return message

        class PhysicalOutput:
            def __init__(self) -> None:
                self.messages: list[mido.Message] = []

            def send(self, message: mido.Message) -> None:
                self.messages.append(message)

        physical_values = [(3, 10, 10), (3, 10, 90), (4, 11, 30), (3, 10, 20)]
        sender_midi._active_generation = 10
        sender_midi._prepare_feedback_connection()
        sender_midi._running = True
        physical_output = PhysicalOutput()
        sender_midi._device_loop(
            PhysicalInput(
                [
                    mido.Message("control_change", channel=channel, control=cc, value=value)
                    for channel, cc, value in physical_values
                ]
            ),
            physical_output,
            sender_config.midi_device,
        )

        for _ in range(1000):
            receiver_midi._poll_remote_transport()
            receiver_midi._service_remote_feedback()
            sender_midi._poll_remote_transport(physical_output)
            qtbot.wait(1)
            if (
                len(audio_writes) >= 2
                and sender_model.get_channel_volume(0) == pytest.approx(20 / 127)
            ):
                break
        assert audio_writes[-2:] == [
            (0, pytest.approx(20 / 127)),
            (1, pytest.approx(30 / 127)),
        ]
        assert sender_model.get_channel_volume(0) == pytest.approx(20 / 127)
        assert sender_model.get_channel_volume(1) == pytest.approx(30 / 127)
        assert window._channels[0]._slider.value() == int(20 / 127 * 100)
        assert sender_model.sync_status == "Connected"
        assert physical_output.messages == []

        # Programmatic UI refresh and a matching delayed backend confirmation
        # retain remote causality and cannot become a reverse motor command.
        receiver_window.on_channel_volume_changed(0, 20 / 127)
        receiver_backend.channel_volume_changed.emit(0, 20 / 127)
        for _ in range(20):
            receiver_midi._service_remote_feedback()
            sender_midi._poll_remote_transport(physical_output)
            qtbot.wait(0)
        assert len(audio_writes) == 2
        assert physical_output.messages == []

        # The machine-local fail-safe blocks the sender motor only. Re-enabling
        # restores a genuinely external receiver change.
        receiver_config.set_channel_volume(0, 40 / 127)
        receiver_backend.channel_volume_changed.emit(0, 40 / 127)
        for _ in range(20):
            receiver_midi._service_remote_feedback()
            sender_midi._poll_remote_transport(physical_output)
            qtbot.wait(0)
        assert physical_output.messages == []

        sender_midi.set_fader_feedback_enabled(True)
        receiver_config.set_channel_volume(0, 41 / 127)
        receiver_backend.channel_volume_changed.emit(0, 41 / 127)
        for _ in range(1000):
            receiver_midi._service_remote_feedback()
            receiver_midi._poll_remote_transport()
            sender_midi._poll_remote_transport(physical_output)
            qtbot.wait(1)
            if physical_output.messages:
                break
        echoed = physical_output.messages[-1]
        writes_before_echo = len(audio_writes)
        sender_midi._running = True
        sender_midi._device_loop(
            PhysicalInput([echoed]),
            physical_output,
            sender_config.midi_device,
        )
        for _ in range(20):
            receiver_midi._poll_remote_transport()
            qtbot.wait(0)
        assert len(audio_writes) == writes_before_echo

        # Physical RtMidi reopen generations are independent of the TCP model
        # generation and must not prevent the next canonical acknowledgement.
        sender_midi._active_generation = 11
        sender_midi._prepare_feedback_connection()
        sender_midi._running = True
        sender_midi._device_loop(
            PhysicalInput([mido.Message("control_change", channel=3, control=10, value=70)]),
            physical_output,
            sender_config.midi_device,
        )
        for _ in range(1000):
            receiver_midi._poll_remote_transport()
            receiver_midi._service_remote_feedback()
            sender_midi._poll_remote_transport(physical_output)
            qtbot.wait(1)
            if (
                len(audio_writes) == writes_before_echo + 1
                and sender_model.get_channel_volume(0) == pytest.approx(70 / 127)
            ):
                break
        assert audio_writes[-1] == (0, pytest.approx(70 / 127))
        assert sender_model.get_channel_volume(0) == pytest.approx(70 / 127)

        old_model_session = sender_model._session
        assert old_model_session is not None
        assert receiver_transport._sync_transport is not None
        receiver_transport._close_sync_transport()
        receiver_transport._start_sync_client()
        for _ in range(4000):
            qtbot.wait(1)
            receiver_midi._poll_remote_transport()
            sender_midi._poll_remote_transport(physical_output)
            if (
                sender_model.active
                and sender_model._session is not None
                and sender_model._session.transport_session_id != old_model_session.transport_session_id
            ):
                break
        assert sender_model.active
        assert sender_model._session is not None
        assert sender_model._session.generation > old_model_session.generation
        assert sender_model._session.transport_session_id != old_model_session.transport_session_id
        assert len(audio_writes) == writes_before_echo + 1

        writes_before_local = len(audio_writes)
        receiver_backend.set_channel_volume(1, 75 / 127)
        for _ in range(1000):
            qtbot.wait(1)
            receiver_midi._poll_remote_transport()
            sender_midi._poll_remote_transport(physical_output)
            if sender_model.get_channel_volume(1) == pytest.approx(75 / 127):
                break
        assert sender_model.get_channel_volume(1) == pytest.approx(75 / 127)
        assert audio_writes[writes_before_local:] == [(1, pytest.approx(75 / 127))]

        # A receiver keyboard edit traverses the actual ChannelWidget user path:
        # one backend/audio write, one canonical publication, and one motor move.
        writes_before_gui = len(audio_writes)
        messages_before_gui = len(physical_output.messages)
        receiver_window._channels[1]._slider.setFocus()
        qtbot.keyClick(receiver_window._channels[1]._slider, Qt.Key.Key_Up)
        for _ in range(1000):
            receiver_midi._service_remote_feedback()
            receiver_midi._poll_remote_transport()
            sender_midi._poll_remote_transport(physical_output)
            qtbot.wait(1)
            if len(physical_output.messages) > messages_before_gui:
                break
        assert len(audio_writes) == writes_before_gui + 1
        assert len(physical_output.messages) == messages_before_gui + 1
        gui_publication = authority.flush_volume_publication()
        assert gui_publication is not None
        for _ in range(1000):
            receiver_midi._poll_remote_transport()
            sender_midi._poll_remote_transport(physical_output)
            qtbot.wait(0)
            if sender_model._snapshot.revision >= gui_publication.revision:
                break
        assert sender_model._snapshot.revision == gui_publication.revision

        current_revision = sender_model._snapshot.revision
        sender_model.handle_envelope(
            SimpleNamespace(
                role="send",
                generation=old_model_session.generation,
                connected_peer_id=old_model_session.connected_peer_id,
                transport_session_id=old_model_session.transport_session_id,
                message=_snapshot_message(_snapshot(revision=current_revision + 10, volume=0.0)),
            )
        )
        assert sender_model._snapshot.revision == current_revision
        assert sender_model.get_channel_volume(0) == pytest.approx(70 / 127)

        initial_revision = sender_model._snapshot.revision
        first_id, alias_id = sender_model._snapshot.channel_order[:2]
        monkeypatch.setattr(receiver_backend, "get_effective_shared_target_channels", lambda _channel: [0, 1])
        receiver_config.set_channel_volume(0, 64 / 127)
        receiver_config.set_channel_volume(1, 64 / 127)
        formerly_mismatched = authority._build_snapshot(initial_revision + 1)
        receiver_midi.request_remote_sync_send(
            DeltaMessage(
                PROTOCOL_VERSION,
                SCHEMA_VERSION,
                sender_model._session.transport_session_id,
                authority.epoch,
                initial_revision,
                initial_revision + 1,
                formerly_mismatched.content_hash,
                {"volumes": {first_id: {"volume": 64 / 127}}},
            ),
            sender_model._session.generation,
            sender_model._session.transport_session_id,
        )
        for _ in range(1000):
            receiver_midi._poll_remote_transport()
            sender_midi._poll_remote_transport()
            qtbot.wait(0)
            if sender_model._snapshot_requested_at is not None:
                break
        assert sender_model.active
        assert sender_model._snapshot.revision == initial_revision
        assert sender_model._snapshot_requested_at is not None

        for _ in range(1000):
            receiver_midi._poll_remote_transport()
            sender_midi._poll_remote_transport()
            qtbot.wait(0)
            if sender_model._snapshot_requested_at is None:
                break
        assert sender_model._snapshot.revision == initial_revision + 1
        assert sender_model.get_channel_volume(0) == pytest.approx(64 / 127)
        assert sender_model.get_channel_volume(1) == pytest.approx(64 / 127)

        receiver_config.set_channel_volume(0, 65 / 127)
        receiver_config.set_channel_volume(1, 65 / 127)
        publication = authority.capture_runtime_volume(0, 65 / 127)
        assert publication is not None
        assert publication.delta is not None
        assert set(publication.delta.changes["volumes"]) == {first_id, alias_id}
        for _ in range(1000):
            receiver_midi._poll_remote_transport()
            sender_midi._poll_remote_transport()
            qtbot.wait(0)
            if sender_model._snapshot.revision > initial_revision + 1:
                break
        assert sender_model.get_channel_volume(0) == pytest.approx(65 / 127)
        assert sender_model.get_channel_volume(1) == pytest.approx(65 / 127)
        assert sender_model.sync_status == "Connected"

        receiver_wiring[0](999, "Version incompatible", "hello version mismatch")
        assert receiver_panel.states[-1] == (999, "Version incompatible", "hello version mismatch")
        receiver_wiring[3]("Permission disabled")
        assert receiver_panel.states[-1] == (999, "Version incompatible", "hello version mismatch")

        receiver_wiring[0](1000, "Reconnecting", "retrying")
        receiver_wiring[0](998, "Version incompatible", "stale mismatch")
        receiver_wiring[3]("Permission disabled")
        assert receiver_panel.states[-1][1] == "Permission disabled"

        sender_wiring[0](2000, "Reconnecting", "new transport state")
        sender_wiring[0](1999, "Version incompatible", "stale transport state")
        assert sender_model.sync_status == "Reconnecting"
        assert window.settings_panel._remote_sync_status_label.fullText() == "Mixer sync: Reconnecting"

        sender_wiring[0](2001, "Version incompatible", "current mismatch")
        sender_wiring[2]("MIDI-only", "model session unavailable")
        assert window.settings_panel._remote_sync_status_label.fullText() == "Mixer sync: Version incompatible"

        sender_wiring[0](2002, "Reconnecting", "newer transport state")
        sender_wiring[2]("MIDI-only", "model session unavailable")
        assert window.settings_panel._remote_sync_status_label.fullText() == "Mixer sync: MIDI-only"
    finally:
        receiver_transport.close()
        sender_transport.close()


def test_unanswered_snapshot_request_retries_in_current_session() -> None:
    now = [0.0]
    sent: list[tuple[Any, int, str]] = []
    model = RemoteMixerFacade(
        lambda message, generation, session: sent.append((message, generation, session)),
        clock=lambda: now[0],
    )
    model.begin_session(RemoteSyncSession(3, "send", PEER, PEER, "Receiver", SESSION, True))
    first_request = sent[-1][0]

    now[0] = 5.1
    model.expire_pending()

    assert len(sent) == 2
    assert sent[-1][0].request_id == first_request.request_id
    assert model.sync_status == "Syncing"


def test_stale_snapshot_cannot_cancel_recovery_or_replace_visible_model() -> None:
    model, sent = _connected_model()
    current = model._snapshot
    assert current is not None
    mismatched = DeltaMessage(
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
        SESSION,
        current.epoch,
        current.revision,
        current.revision + 1,
        "0" * 64,
        {"volumes": {current.channel_order[0]: {"volume": 0.75}}},
    )

    model.handle_envelope(_envelope(mismatched))
    request_id = sent[-1].request_id
    model.handle_envelope(_envelope(_snapshot_message(current)))

    assert model.active
    assert model._snapshot == current
    assert model._snapshot_requested_at is not None
    assert model._snapshot_request_id == request_id
