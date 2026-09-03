from __future__ import annotations

import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal

from nativmix.hardware.midi import MidiThread
from nativmix.remote_sync.authority import (
    RECEIVER_COMMAND_TYPES,
    AuthorityErrorCode,
    ControlSessionMetadata,
    ReceiverMixerAuthority,
    ValidatedCommandEnvelope,
)
from nativmix.remote_sync.protocol import (
    PROTOCOL_VERSION,
    AckMessage,
    CommandMessage,
    DeltaMessage,
    SnapshotMessage,
    SnapshotRequest,
)
from nativmix.remote_sync.schema import (
    SCHEMA_VERSION,
    ReceiverCapabilities,
    SchemaValueError,
    TargetInventoryItem,
)
from nativmix.utils.config_manager import ConfigManager
from nativmix.utils.profile_manager import ProfileManager


def _id() -> str:
    return str(uuid.uuid4())


class Backend:
    def __init__(self) -> None:
        self.volumes: list[tuple[int, float]] = []
        self.muted: dict[int, bool] = {}
        self.fail_volume = False

    def set_channel_volume(self, channel_index: int, volume: float) -> None:
        if self.fail_volume:
            raise RuntimeError("backend failed")
        self.volumes.append((channel_index, volume))

    def is_channel_muted(self, channel_index: int) -> bool:
        return self.muted.get(channel_index, False)

    def toggle_mute(self, channel_index: int) -> None:
        self.muted[channel_index] = not self.muted.get(channel_index, False)

    def apply_poti_volumes(self, volumes: list[float], *, force: bool = False) -> None:
        if force:
            self.volumes.extend(enumerate(volumes))


class SignalingBackend(QObject, Backend):
    channel_volume_changed = pyqtSignal(int, float)
    mute_state_changed = pyqtSignal(int, bool)

    def __init__(self) -> None:
        QObject.__init__(self)
        Backend.__init__(self)


@pytest.fixture
def authority(tmp_path: Path, qapp: QCoreApplication) -> ReceiverMixerAuthority:
    profiles_dir = tmp_path / "profiles"
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=profiles_dir)
    config.allow_remote_mixer_editing = True
    config.remote_midi_role = "receive"
    profiles = ProfileManager(profiles_dir=profiles_dir)
    profiles.set_active_silently(config.active_profile_id)
    config.apply_profile(profiles.load(config.active_profile_id))
    session = ControlSessionMetadata(_id(), _id(), generation=7)
    return ReceiverMixerAuthority(
        config,
        profiles,
        Backend(),
        capabilities_provider=ReceiverCapabilities(True, True, 32, ("routing_pause",)),
        inventory_provider=[
            TargetInventoryItem("pseudo:system-master", "System Master", "output", True),
            TargetInventoryItem("pseudo:other-apps", "Other Apps", "output", True),
            TargetInventoryItem("app:firefox", "Firefox", "output", True),
            TargetInventoryItem("device:headset", "USB Headset", "output", True),
        ],
        active_session=session,
    )


def _command(
    authority: ReceiverMixerAuthority,
    command_type: str,
    payload: dict[str, Any],
    *,
    command_id: str | None = None,
    revision: int | None = None,
    transport_session_id: str | None = None,
) -> CommandMessage:
    session = authority.active_session
    assert session is not None
    return CommandMessage(
        protocol_version=PROTOCOL_VERSION,
        schema_version=SCHEMA_VERSION,
        transport_session_id=transport_session_id or session.transport_session_id,
        control_session_id=session.control_session_id,
        command_id=command_id or _id(),
        receiver_epoch=authority.epoch,
        expected_revision=authority.revision if revision is None else revision,
        command_type=command_type,
        payload=payload,
    )


def _active_channel(authority: ReceiverMixerAuthority) -> tuple[str, str]:
    snapshot = authority.current_snapshot()
    return snapshot.active_profile_id, snapshot.channel_order[0]


def test_success_uses_stable_channel_id_and_canonical_publication(authority: ReceiverMixerAuthority) -> None:
    profile_id, channel_id = _active_channel(authority)
    result = authority.process_command(
        _command(
            authority,
            "set_channel_label",
            {"profile_id": profile_id, "channel_id": channel_id, "label": "Music"},
        ),
        generation=7,
    )
    assert result.accepted
    assert result.publication is not None
    assert result.publication.snapshot.content_hash
    assert result.revision == 1
    channel = result.publication.snapshot.profiles[0].channels[0]
    assert channel.id == channel_id
    assert channel.label == "Music"


def test_remote_feedback_lineage_suppresses_equivalent_ack_for_shared_endpoint_component(
    authority: ReceiverMixerAuthority,
) -> None:
    session = authority.active_session
    assert session is not None
    authority._config.midi_fader_feedback = True
    authority._backend.get_effective_shared_target_channels = lambda _channel: [0, 1]
    authority._config.set_midi_cc(0, 10, midi_channel=3)
    authority._config.set_midi_cc(1, 11, midi_channel=4)
    origin = SimpleNamespace(
        generation=session.generation,
        transport_session_id=session.transport_session_id,
        midi_channel=3,
        control=10,
        channel_index=0,
        rtp_sequence=1,
        requested_volume=79 / 127,
    )

    authority.note_remote_controller_origin(origin)

    expected = frozenset({(3, 10), (4, 11)})
    assert authority.feedback_suppressed_bindings(0, 80 / 127) == expected
    assert authority.feedback_suppressed_bindings(1, 80 / 127) == expected
    assert authority.feedback_suppressed_bindings(0, 81 / 127) == frozenset()

    newer_origin = SimpleNamespace(
        **{
            **origin.__dict__,
            "rtp_sequence": 2,
            "requested_volume": 90 / 127,
        }
    )
    authority.note_remote_controller_origin(newer_origin)
    assert authority.feedback_suppressed_bindings(0, 80 / 127) == frozenset()
    assert authority.feedback_suppressed_bindings(0, 90 / 127) == expected

    authority.note_remote_controller_origin(origin)
    authority.begin_transport_session(
        SimpleNamespace(
            role="receive",
            generation=session.generation + 1,
            transport_session_id=str(uuid.uuid4()),
            available=True,
            selected_peer_id=None,
            connected_peer_id=None,
        )
    )
    assert authority.feedback_suppressed_bindings(0, 80 / 127) == frozenset()


def test_receiver_feedback_without_remote_provenance_emits_adjacent_cc(
    authority: ReceiverMixerAuthority,
) -> None:
    assert authority.feedback_suppressed_bindings(0, 80 / 127) == frozenset()


def test_local_feedback_lineage_suppresses_all_shared_bindings_only_on_origin_endpoint(
    authority: ReceiverMixerAuthority,
) -> None:
    authority._config.midi_fader_feedback = True
    authority._backend.get_effective_shared_target_channels = lambda _channel: [0, 1]
    authority._config.set_midi_cc(0, 10, midi_channel=3)
    authority._config.set_midi_cc(1, 11, midi_channel=4)
    origin = SimpleNamespace(
        generation=9,
        controller_id="local-controller-a",
        local_sequence=1,
        midi_channel=3,
        control=10,
        channel_index=0,
        requested_volume=79 / 127,
    )

    authority.note_remote_controller_origin(origin)

    suppressed = authority.feedback_suppressed_bindings(1, 80 / 127)
    assert suppressed == frozenset({(3, 10), (4, 11)})
    assert authority.feedback_suppressed_bindings(1, 80 / 127) == frozenset()

    class Output:
        def __init__(self) -> None:
            self.messages: list[Any] = []

        def send(self, message: Any) -> None:
            self.messages.append(message)

    origin_endpoint = MidiThread(input_mode="midi_only")
    origin_endpoint.set_fader_feedback_enabled(True)
    origin_endpoint.update_mappings({(3, 10): 0, (4, 11): 1})
    origin_endpoint.request_fader_sync(
        [(0, 80 / 127), (1, 80 / 127)],
        suppressed_bindings=suppressed,
    )
    origin_output = Output()
    origin_endpoint._process_pending_sync(origin_output)
    assert origin_output.messages == []

    other_endpoint = MidiThread(input_mode="midi_only")
    other_endpoint.set_fader_feedback_enabled(True)
    other_endpoint.update_mappings({(3, 10): 0, (4, 11): 1})
    other_endpoint.request_fader_sync([(0, 80 / 127), (1, 80 / 127)])
    other_output = Output()
    other_endpoint._process_pending_sync(other_output)
    assert [(message.control, message.value) for message in other_output.messages] == [(10, 80), (11, 80)]


def test_duplicate_uuid_returns_identical_cached_ack_without_reapply(authority: ReceiverMixerAuthority) -> None:
    profile_id, channel_id = _active_channel(authority)
    command = _command(
        authority,
        "set_channel_inverted",
        {"profile_id": profile_id, "channel_id": channel_id, "inverted": True},
    )
    first = authority.process_command(command, generation=7)
    second = authority.process_command(command, generation=999)
    assert second is first
    assert authority.revision == 1


def test_duplicate_uuid_after_reconnect_uses_current_transport_without_reapply(
    authority: ReceiverMixerAuthority,
) -> None:
    profile_id, channel_id = _active_channel(authority)
    command_id = _id()
    first = authority.process_command(
        _command(
            authority,
            "set_channel_label",
            {"profile_id": profile_id, "channel_id": channel_id, "label": "Cached"},
            command_id=command_id,
        ),
        generation=7,
    )
    replacement = ControlSessionMetadata(_id(), _id(), generation=8)
    authority.set_active_session(replacement)
    retry = authority.process_command(
        _command(
            authority,
            "set_channel_label",
            {"profile_id": profile_id, "channel_id": channel_id, "label": "Cached"},
            command_id=command_id,
        ),
        generation=8,
    )
    assert retry.accepted
    assert retry.revision == first.revision == 1
    assert retry.response.transport_session_id == replacement.transport_session_id
    assert authority.revision == 1


@pytest.mark.parametrize(
    ("configure", "code"),
    [
        ("permission", AuthorityErrorCode.PERMISSION_DISABLED),
        ("session", AuthorityErrorCode.SESSION_MISMATCH),
        ("generation", AuthorityErrorCode.GENERATION_MISMATCH),
        ("role", AuthorityErrorCode.ROLE_MISMATCH),
    ],
)
def test_session_permission_generation_and_role_rejections(
    authority: ReceiverMixerAuthority,
    configure: str,
    code: AuthorityErrorCode,
) -> None:
    command = _command(authority, "request_resync", {})
    generation = 7
    role = "receive"
    if configure == "permission":
        old = authority.active_session
        assert old is not None
        authority.set_active_session(
            ControlSessionMetadata(
                old.transport_session_id,
                old.control_session_id,
                old.generation,
                permission_enabled=False,
            )
        )
    elif configure == "session":
        command = _command(authority, "request_resync", {}, transport_session_id=_id())
    elif configure == "generation":
        generation = 8
    else:
        role = "send"
    result = authority.process_command(command, generation=generation, role=role)
    assert result.error_code is code
    assert authority.revision == 0


def test_stale_revision_and_unknown_command_are_stable_nacks(authority: ReceiverMixerAuthority) -> None:
    stale = authority.process_command(
        _command(authority, "request_resync", {}, revision=1),
        generation=7,
    )
    unknown = authority.process_command(_command(authority, "run_shell", {}), generation=7)
    assert stale.error_code is AuthorityErrorCode.STALE_REVISION
    assert stale.response.reason == "stale_revision"
    assert unknown.error_code is AuthorityErrorCode.UNKNOWN_COMMAND_TYPE
    assert unknown.response.reason == "unknown_command_type"


def test_invalid_mapping_is_prevalidated_without_mutation_or_revision(authority: ReceiverMixerAuthority) -> None:
    profile_id, channel_id = _active_channel(authority)
    before = authority.current_snapshot()
    result = authority.process_command(
        _command(
            authority,
            "set_channel_mappings",
            {
                "profile_id": profile_id,
                "channel_id": channel_id,
                "target_keys": ["pseudo:system-master", "app:firefox"],
            },
        ),
        generation=7,
    )
    assert result.error_code is AuthorityErrorCode.CONFLICT
    assert authority.revision == 0
    assert authority.current_snapshot().content_hash == before.content_hash


def test_profile_crud_select_and_stable_midi_channel_commands(authority: ReceiverMixerAuthority) -> None:
    original_profile, _ = _active_channel(authority)
    assert authority.process_command(
        _command(authority, "create_profile", {"name": "Blank", "channel_count": 2}),
        generation=7,
    ).accepted
    blank = next(profile for profile in authority.current_snapshot().profiles if profile.name == "Blank")
    assert authority.process_command(
        _command(authority, "duplicate_profile", {"profile_id": blank.id, "name": "Copy"}),
        generation=7,
    ).accepted
    copy_profile = next(profile for profile in authority.current_snapshot().profiles if profile.name == "Copy")
    assert authority.process_command(
        _command(authority, "rename_profile", {"profile_id": copy_profile.id, "name": "Renamed"}),
        generation=7,
    ).accepted
    assert authority.process_command(
        _command(authority, "select_profile", {"profile_id": blank.id}),
        generation=7,
    ).accepted

    added_id = _id()
    assert authority.process_command(
        _command(
            authority,
            "add_midi_channel",
            {"profile_id": blank.id, "channel_id": added_id},
        ),
        generation=7,
    ).accepted
    assert added_id in authority.current_snapshot().channel_order
    reversed_order = list(reversed(authority.current_snapshot().channel_order))
    assert authority.process_command(
        _command(
            authority,
            "reorder_channels",
            {"profile_id": blank.id, "channel_ids": reversed_order},
        ),
        generation=7,
    ).accepted
    assert list(authority.current_snapshot().channel_order) == reversed_order
    assert authority.process_command(
        _command(
            authority,
            "delete_midi_channels",
            {"profile_id": blank.id, "channel_ids": [added_id]},
        ),
        generation=7,
    ).accepted
    assert added_id not in authority.current_snapshot().channel_order
    assert authority.process_command(
        _command(authority, "delete_profile", {"profile_id": original_profile}),
        generation=7,
    ).accepted


def test_channel_mapping_capability_bindings_and_runtime_commands(authority: ReceiverMixerAuthority) -> None:
    profile_id, channel_id = _active_channel(authority)
    commands = [
        ("set_channel_label", {"label": "Browser"}),
        ("set_channel_inverted", {"inverted": True}),
        ("set_channel_mode", {"mode": "app"}),
        ("set_channel_mappings", {"target_keys": ["app:firefox"]}),
        ("set_channel_v_sink", {"enabled": True}),
        ("set_channel_routing_paused", {"target_key": "app:firefox", "paused": True}),
        ("set_channel_volume_midi_binding", {"cc": 12, "midi_channel": 3}),
        ("set_channel_mute_midi_binding", {"cc": 13, "midi_channel": 4}),
        ("set_channel_volume", {"volume": 0.25}),
        ("set_channel_mute", {"muted": True}),
    ]
    for command_type, values in commands:
        payload = {"profile_id": profile_id, "channel_id": channel_id, **values}
        assert authority.process_command(_command(authority, command_type, payload), generation=7).accepted

    channel = authority.current_snapshot().profiles[0].channels[0]
    runtime = authority.current_snapshot().runtime_states[0]
    assert channel.mappings == ("Firefox",)
    assert channel.v_sink
    assert channel.routing_paused_apps == ("Firefox",)
    assert (channel.volume_channel, channel.volume_cc) == (3, 12)
    assert (channel.mute_channel, channel.mute_cc) == (4, 13)
    assert channel.saved_fader_volume == 1.0
    assert runtime.effective_volume == 0.25
    assert runtime.muted


def test_active_profile_mapping_command_emits_live_backend_signal(authority: ReceiverMixerAuthority) -> None:
    emitted = []
    authority._config.mapping_changed.connect(lambda index, mappings: emitted.append((index, mappings)))
    profile_id, channel_id = _active_channel(authority)
    result = authority.process_command(
        _command(
            authority,
            "set_channel_mappings",
            {"profile_id": profile_id, "channel_id": channel_id, "target_keys": ["app:firefox"]},
        ),
        generation=7,
    )
    assert result.accepted
    assert emitted == [(0, ["Firefox"])]


def test_active_profile_edit_and_reorder_emit_live_settings_refresh(
    authority: ReceiverMixerAuthority,
) -> None:
    emissions = []
    authority._config.settings_changed.connect(lambda: emissions.append(None))
    profile_id, channel_id = _active_channel(authority)
    assert authority.process_command(
        _command(
            authority,
            "set_channel_inverted",
            {"profile_id": profile_id, "channel_id": channel_id, "inverted": True},
        ),
        generation=7,
    ).accepted
    order = list(reversed(authority.current_snapshot().channel_order))
    assert authority.process_command(
        _command(
            authority,
            "reorder_channels",
            {"profile_id": profile_id, "channel_ids": order},
        ),
        generation=7,
    ).accepted
    assert emissions == [None, None]


def test_hardware_targets_use_advertised_keys(authority: ReceiverMixerAuthority) -> None:
    profile_id, channel_id = _active_channel(authority)
    accepted = authority.process_command(
        _command(
            authority,
            "set_channel_mode",
            {"profile_id": profile_id, "channel_id": channel_id, "mode": "hardware"},
        ),
        generation=7,
    )
    assert accepted.accepted
    accepted = authority.process_command(
        _command(
            authority,
            "set_channel_hardware_target",
            {"profile_id": profile_id, "channel_id": channel_id, "target_key": "device:headset"},
        ),
        generation=7,
    )
    assert accepted.accepted
    assert authority.current_snapshot().profiles[0].channels[0].hardware_target_key == "device:headset"

    rejected = authority.process_command(
        _command(
            authority,
            "set_channel_hardware_target",
            {"profile_id": profile_id, "channel_id": channel_id, "target_key": "device:raw-id"},
        ),
        generation=7,
    )
    assert rejected.error_code is AuthorityErrorCode.NOT_FOUND

    assert authority.process_command(
        _command(
            authority,
            "set_channel_mode",
            {"profile_id": profile_id, "channel_id": channel_id, "mode": "app"},
        ),
        generation=7,
    ).accepted
    channel = authority.current_snapshot().profiles[0].channels[0]
    assert channel.hardware_target_key is None


def test_mode_transition_clears_incompatible_assignments(authority: ReceiverMixerAuthority) -> None:
    profile_id, channel_id = _active_channel(authority)
    assert authority.process_command(
        _command(
            authority,
            "set_channel_mappings",
            {"profile_id": profile_id, "channel_id": channel_id, "target_keys": ["app:firefox"]},
        ),
        generation=7,
    ).accepted
    assert authority.process_command(
        _command(
            authority,
            "set_channel_v_sink",
            {"profile_id": profile_id, "channel_id": channel_id, "enabled": True},
        ),
        generation=7,
    ).accepted
    assert authority.process_command(
        _command(
            authority,
            "set_channel_mode",
            {"profile_id": profile_id, "channel_id": channel_id, "mode": "hardware"},
        ),
        generation=7,
    ).accepted
    channel = authority.current_snapshot().profiles[0].channels[0]
    assert channel.mode == "hardware"
    assert channel.mappings == ()
    assert not channel.v_sink


def test_capability_failure_is_prevalidated(authority: ReceiverMixerAuthority) -> None:
    authority._capabilities_provider = ReceiverCapabilities(False, True, 32, ())
    profile_id, channel_id = _active_channel(authority)
    result = authority.process_command(
        _command(
            authority,
            "set_channel_v_sink",
            {"profile_id": profile_id, "channel_id": channel_id, "enabled": True},
        ),
        generation=7,
    )
    assert result.error_code is AuthorityErrorCode.CONFLICT
    assert authority.revision == 0


def test_midi_only_delete_and_last_profile_safeguards(authority: ReceiverMixerAuthority) -> None:
    profile_id, channel_id = _active_channel(authority)
    channel_delete = authority.process_command(
        _command(
            authority,
            "delete_midi_channels",
            {"profile_id": profile_id, "channel_ids": [channel_id]},
        ),
        generation=7,
    )
    profile_delete = authority.process_command(
        _command(authority, "delete_profile", {"profile_id": profile_id}),
        generation=7,
    )
    assert channel_delete.error_code is AuthorityErrorCode.CONFLICT
    assert profile_delete.error_code is AuthorityErrorCode.CONFLICT
    assert authority.revision == 0


def test_profile_direct_midi_cc_is_remote_but_global_ccs_are_excluded(authority: ReceiverMixerAuthority) -> None:
    profile_id, _ = _active_channel(authority)
    result = authority.process_command(
        _command(authority, "set_profile_midi_switch_cc", {"profile_id": profile_id, "cc": 42}),
        generation=7,
    )
    assert result.accepted
    canonical = result.publication.snapshot.to_canonical()
    assert canonical["profiles"][0]["midi_switch_cc"] == 42
    assert "profile_midi_next_cc" not in repr(canonical)
    assert "profile_midi_prev_cc" not in repr(canonical)


def test_declared_layer3_command_api_is_allowlisted_only() -> None:
    assert {"add_midi_channel", "delete_midi_channels", "set_channel_mappings"} <= RECEIVER_COMMAND_TYPES
    assert {"add_channel", "delete_channel", "set_config", "run_shell"}.isdisjoint(RECEIVER_COMMAND_TYPES)


def test_command_and_destructive_rate_limits(tmp_path: Path, qapp: QCoreApplication) -> None:
    profiles_dir = tmp_path / "profiles"
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=profiles_dir)
    config.allow_remote_mixer_editing = True
    config.remote_midi_role = "receive"
    profiles = ProfileManager(profiles_dir=profiles_dir)
    profiles.set_active_silently(config.active_profile_id)
    second = profiles.create("Second", 1)
    session = ControlSessionMetadata(_id(), _id(), 1)
    limited = ReceiverMixerAuthority(
        config,
        profiles,
        active_session=session,
        command_rate_limit=1,
        destructive_rate_limit=1,
    )
    first = limited.process_command(_command(limited, "rename_profile", {"profile_id": second, "name": "B"}))
    second_result = limited.process_command(
        _command(limited, "rename_profile", {"profile_id": second, "name": "C"})
    )
    assert first.accepted
    assert second_result.error_code is AuthorityErrorCode.RATE_LIMITED

    destructive = ReceiverMixerAuthority(
        config,
        profiles,
        active_session=session,
        command_rate_limit=10,
        destructive_rate_limit=1,
    )
    third = profiles.create("Third", 1)
    assert destructive.process_command(
        _command(destructive, "delete_profile", {"profile_id": second})
    ).accepted
    rejected = destructive.process_command(
        _command(destructive, "delete_profile", {"profile_id": third})
    )
    assert rejected.error_code is AuthorityErrorCode.DESTRUCTIVE_RATE_LIMITED


def test_backend_failure_rolls_back_and_does_not_advance(authority: ReceiverMixerAuthority) -> None:
    backend = authority._backend
    assert isinstance(backend, Backend)
    backend.fail_volume = True
    profile_id, channel_id = _active_channel(authority)
    result = authority.process_command(
        _command(
            authority,
            "set_channel_volume",
            {"profile_id": profile_id, "channel_id": channel_id, "volume": 0.2},
        ),
        generation=7,
    )
    assert result.error_code is AuthorityErrorCode.APPLY_FAILED
    assert authority.revision == 0
    assert authority.current_snapshot().profiles[0].channels[0].saved_fader_volume == 1.0


def test_volume_coalescing_flushes_before_structural_publication(
    tmp_path: Path,
    qapp: QCoreApplication,
) -> None:
    now = [0.0]
    profiles_dir = tmp_path / "profiles"
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=profiles_dir)
    config.allow_remote_mixer_editing = True
    config.remote_midi_role = "receive"
    profiles = ProfileManager(profiles_dir=profiles_dir)
    profiles.set_active_silently(config.active_profile_id)
    config.apply_profile(profiles.load(config.active_profile_id))
    session = ControlSessionMetadata(_id(), _id(), 1)
    receiver = ReceiverMixerAuthority(
        config,
        profiles,
        Backend(),
        active_session=session,
        clock=lambda: now[0],
    )
    publications = []
    receiver.publication_ready.connect(publications.append)
    profile_id, channel_id = _active_channel(receiver)

    first = receiver.process_command(
        _command(
            receiver,
            "set_channel_volume",
            {"profile_id": profile_id, "channel_id": channel_id, "volume": 0.8},
        )
    )
    second = receiver.process_command(
        _command(
            receiver,
            "set_channel_volume",
            {"profile_id": profile_id, "channel_id": channel_id, "volume": 0.7},
        )
    )
    assert first.publication is not None
    assert second.publication is None
    structural = receiver.process_command(
        _command(receiver, "rename_profile", {"profile_id": profile_id, "name": "Renamed"})
    )
    assert structural.accepted
    assert [publication.revision for publication in publications] == [1, 2, 3]
    assert "volumes" in publications[1].delta.changes
    assert publications[2].kind == "snapshot"


def test_coalesced_volume_ack_waits_for_resulting_delta(
    tmp_path: Path,
    qapp: QCoreApplication,
) -> None:
    now = [0.0]
    profiles_dir = tmp_path / "profiles"
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=profiles_dir)
    config.allow_remote_mixer_editing = True
    config.remote_midi_role = "receive"
    profiles = ProfileManager(profiles_dir=profiles_dir)
    profiles.set_active_silently(config.active_profile_id)
    config.apply_profile(profiles.load(config.active_profile_id))
    session = ControlSessionMetadata(_id(), _id(), 1)
    sent = []
    receiver = ReceiverMixerAuthority(
        config,
        profiles,
        Backend(),
        active_session=session,
        clock=lambda: now[0],
        protocol_message_sender=lambda message, generation, session_id: sent.append(message),
    )
    profile_id, channel_id = _active_channel(receiver)

    first = _command(
        receiver,
        "set_channel_volume",
        {"profile_id": profile_id, "channel_id": channel_id, "volume": 0.8},
    )
    receiver.queue_validated(ValidatedCommandEnvelope(first, generation=1))
    qapp.processEvents()
    assert [type(message) for message in sent] == [DeltaMessage, AckMessage]

    second = _command(
        receiver,
        "set_channel_volume",
        {"profile_id": profile_id, "channel_id": channel_id, "volume": 0.7},
    )
    receiver.queue_validated(ValidatedCommandEnvelope(second, generation=1))
    qapp.processEvents()
    assert len(sent) == 2
    now[0] = 0.04
    receiver.flush_volume_publication()
    assert [type(message) for message in sent[-2:]] == [DeltaMessage, AckMessage]
    assert sent[-1].revision == sent[-2].revision


def test_inventory_rejects_machine_local_data(authority: ReceiverMixerAuthority) -> None:
    authority._inventory_provider = [
        {"key": "app:music", "label": "Music", "kind": "output", "available": True, "pid": 123}
    ]
    with pytest.raises(SchemaValueError):
        authority.current_snapshot()


def test_config_permission_is_checked_at_command_time(authority: ReceiverMixerAuthority) -> None:
    authority._config.allow_remote_mixer_editing = False
    result = authority.process_command(_command(authority, "request_resync", {}), generation=7)
    assert result.error_code is AuthorityErrorCode.PERMISSION_DISABLED
    assert authority.revision == 0


def test_midi_sync_envelope_adapter_sends_protocol_messages(
    authority: ReceiverMixerAuthority,
    qapp: QCoreApplication,
) -> None:
    sent = []
    authority.set_protocol_message_sender(
        lambda message, generation, session_id: sent.append((message, generation, session_id))
    )
    session = authority.active_session
    assert session is not None
    command = _command(authority, "request_resync", {})
    peer_id = _id()
    envelope = SimpleNamespace(
        generation=session.generation,
        role=SimpleNamespace(value="receive"),
        selected_peer_id=peer_id,
        connected_peer_id=peer_id,
        transport_session_id=session.transport_session_id,
        message=command,
    )

    authority.queue_control_envelope(envelope)
    qapp.processEvents()

    assert isinstance(sent[0][0], SnapshotMessage)
    assert isinstance(sent[1][0], AckMessage)
    assert all(item[1:] == (session.generation, session.transport_session_id) for item in sent)


def test_snapshot_request_establishes_selected_receiver_session(
    authority: ReceiverMixerAuthority,
) -> None:
    sent = []
    authority.set_active_session(None)
    authority.set_protocol_message_sender(
        lambda message, generation, session_id: sent.append((message, generation, session_id))
    )
    peer_id = _id()
    session_id = _id()
    request = SnapshotRequest(PROTOCOL_VERSION, SCHEMA_VERSION, session_id, _id())
    envelope = SimpleNamespace(
        generation=12,
        role=SimpleNamespace(value="receive"),
        selected_peer_id=peer_id,
        connected_peer_id=peer_id,
        transport_session_id=session_id,
        message=request,
    )
    authority.queue_control_envelope(envelope)
    assert authority.active_session is not None
    assert authority.active_session.generation == 12
    assert isinstance(sent[0][0], SnapshotMessage)
    assert authority.revision == 1

    authority.queue_control_envelope(envelope)
    assert authority.revision == 1
    assert len(sent) == 2
    assert sent[1][0] == sent[0][0]


def test_snapshot_permission_and_version_failures_do_not_affect_midi_transport(
    authority: ReceiverMixerAuthority,
) -> None:
    sent = []
    authority.set_active_session(None)
    authority.set_protocol_message_sender(
        lambda message, generation, session_id: sent.append((message, generation, session_id))
    )
    peer_id = _id()
    session_id = _id()
    envelope = SimpleNamespace(
        generation=12,
        role=SimpleNamespace(value="receive"),
        selected_peer_id=peer_id,
        connected_peer_id=peer_id,
        transport_session_id=session_id,
        message=SnapshotRequest(PROTOCOL_VERSION + 1, SCHEMA_VERSION, session_id, _id()),
    )
    authority.queue_control_envelope(envelope)
    assert authority.status is not None
    assert authority.status.value == "Version incompatible"
    assert not sent

    authority._config.allow_remote_mixer_editing = False
    envelope.message = SnapshotRequest(PROTOCOL_VERSION, SCHEMA_VERSION, session_id, _id())
    authority.queue_control_envelope(envelope)
    assert authority.status is not None
    assert authority.status.value == "Permission disabled"
    assert len(sent) == 1
    assert isinstance(sent[0][0], SnapshotMessage)
    assert "remote_editing" not in sent[0][0].snapshot["capabilities"]["features"]


def test_live_permission_enable_serves_snapshot_without_reconnect(
    authority: ReceiverMixerAuthority,
    qapp: QCoreApplication,
) -> None:
    sent: list[tuple[object, int, str]] = []
    authority.set_protocol_message_sender(
        lambda message, generation, session_id: sent.append((message, generation, session_id))
    )
    authority.prime_observed_state()
    authority.connect_local_sources()
    session_id = _id()
    peer_id = _id()
    authority.begin_transport_session(
        SimpleNamespace(
            generation=20,
            role="receive",
            selected_peer_id=peer_id,
            connected_peer_id=peer_id,
            transport_session_id=session_id,
            available=True,
        )
    )
    authority._config.allow_remote_mixer_editing = False
    request = SnapshotRequest(PROTOCOL_VERSION, SCHEMA_VERSION, session_id, _id())
    authority.queue_control_envelope(
        SimpleNamespace(
            generation=20,
            role="receive",
            selected_peer_id=peer_id,
            connected_peer_id=peer_id,
            transport_session_id=session_id,
            message=request,
        )
    )
    assert isinstance(sent[-1][0], SnapshotMessage)
    assert "remote_editing" not in sent[-1][0].snapshot["capabilities"]["features"]
    assert authority.active_session is not None
    assert not authority.active_session.permission_enabled

    authority._config.allow_remote_mixer_editing = True
    qapp.processEvents()

    assert isinstance(sent[-1][0], SnapshotMessage)
    assert sent[-1][1:] == (20, session_id)
    assert authority.active_session is not None
    assert authority.active_session.permission_enabled
    assert authority.status is not None
    assert authority.status.value == "Syncing"


def test_live_permission_disable_revokes_publication_and_rapid_toggle_recovers(
    authority: ReceiverMixerAuthority,
    qapp: QCoreApplication,
) -> None:
    sent: list[tuple[object, int, str]] = []
    authority.set_protocol_message_sender(
        lambda message, generation, session_id: sent.append((message, generation, session_id))
    )
    authority.prime_observed_state()
    authority.connect_local_sources()

    authority._config.allow_remote_mixer_editing = False
    assert isinstance(sent[-1][0], SnapshotMessage)
    denial_revision = sent[-1][0].snapshot["revision"]
    sent.clear()
    authority._config.set_channel_label(0, "Published read-only")
    qapp.processEvents()
    assert isinstance(sent[-1][0], SnapshotMessage)
    assert sent[-1][0].snapshot["profiles"][0]["channels"][0]["label"] == "Published read-only"
    sent.clear()

    authority._config.allow_remote_mixer_editing = True
    authority._config.allow_remote_mixer_editing = False
    authority._config.allow_remote_mixer_editing = True
    qapp.processEvents()

    assert [type(item[0]) for item in sent[:3]] == [SnapshotMessage, SnapshotMessage, SnapshotMessage]
    assert sent[-1][0].snapshot["revision"] > denial_revision


def test_reconnect_rejects_stale_snapshot_request_and_serves_fresh_session(
    authority: ReceiverMixerAuthority,
) -> None:
    sent: list[tuple[object, int, str]] = []
    authority.set_protocol_message_sender(
        lambda message, generation, session_id: sent.append((message, generation, session_id))
    )
    old_session = authority.active_session
    assert old_session is not None
    new_session_id = _id()
    peer_id = _id()
    authority.begin_transport_session(
        SimpleNamespace(
            generation=8,
            role="receive",
            selected_peer_id=peer_id,
            connected_peer_id=peer_id,
            transport_session_id=new_session_id,
            available=True,
        )
    )
    stale_request = SnapshotRequest(
        PROTOCOL_VERSION,
        SCHEMA_VERSION,
        old_session.transport_session_id,
        _id(),
    )
    authority.queue_control_envelope(
        SimpleNamespace(
            generation=7,
            role="receive",
            selected_peer_id=peer_id,
            connected_peer_id=peer_id,
            transport_session_id=old_session.transport_session_id,
            message=stale_request,
        )
    )
    assert sent == []

    fresh_request = SnapshotRequest(PROTOCOL_VERSION, SCHEMA_VERSION, new_session_id, _id())
    authority.queue_control_envelope(
        SimpleNamespace(
            generation=8,
            role="receive",
            selected_peer_id=peer_id,
            connected_peer_id=peer_id,
            transport_session_id=new_session_id,
            message=fresh_request,
        )
    )
    assert isinstance(sent[-1][0], SnapshotMessage)
    assert sent[-1][1:] == (8, new_session_id)


def test_connected_local_sources_publish_each_canonical_change_once(
    authority: ReceiverMixerAuthority,
    qapp: QCoreApplication,
) -> None:
    publications = []
    authority.publication_ready.connect(publications.append)
    authority.prime_observed_state()
    authority.connect_local_sources()
    authority._config.set_channel_label(0, "Desktop edit")
    authority._config.settings_changed.emit()
    qapp.processEvents()
    assert len(publications) == 1
    assert publications[0].kind == "snapshot"

    authority._profiles.create("Local profile", 1)
    qapp.processEvents()
    assert len(publications) == 2
    assert publications[1].kind == "snapshot"

    authority._profiles.set_channel_order(list(reversed(authority._profiles.get_channel_order())))
    qapp.processEvents()
    assert len(publications) == 3

    profile = authority._profiles.active_profile
    profile["restore_fader_positions"] = True
    authority._profiles.save_profile(profile)
    qapp.processEvents()
    assert len(publications) == 4


def test_external_volume_capture_waits_for_config_synchronization(
    tmp_path: Path,
    qapp: QCoreApplication,
) -> None:
    profiles_dir = tmp_path / "profiles"
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=profiles_dir)
    config.allow_remote_mixer_editing = True
    config.remote_midi_role = "receive"
    profiles = ProfileManager(profiles_dir=profiles_dir)
    profiles.set_active_silently(config.active_profile_id)
    config.apply_profile(profiles.load(config.active_profile_id))
    backend = SignalingBackend()
    receiver = ReceiverMixerAuthority(
        config,
        profiles,
        backend,
        active_session=ControlSessionMetadata(_id(), _id(), 1),
    )
    receiver.prime_observed_state()
    receiver.connect_local_sources()
    publications = []
    events: list[str] = []

    def capture_publication(publication: object) -> None:
        events.append("canonical-publication")
        publications.append(publication)

    receiver.publication_ready.connect(capture_publication)

    events.append("receiver-audio")
    backend.channel_volume_changed.emit(0, 0.4)
    config.set_channel_volume(0, 0.4)
    assert events == ["receiver-audio"]
    qapp.processEvents()
    assert events == ["receiver-audio", "canonical-publication"]
    assert len(publications) == 1
    assert publications[0].delta is not None
    assert publications[0].snapshot.runtime_states[0].effective_volume == 0.4


def test_stale_queued_generation_is_discarded_before_mutation(
    authority: ReceiverMixerAuthority,
    qapp: QCoreApplication,
) -> None:
    command = _command(authority, "request_resync", {})
    results = []
    authority.command_completed.connect(results.append)
    authority.queue_validated(ValidatedCommandEnvelope(command, generation=7))
    old = authority.active_session
    assert old is not None
    authority.set_active_session(ControlSessionMetadata(_id(), _id(), generation=8))
    qapp.processEvents()
    assert results[0].error_code in {
        AuthorityErrorCode.SESSION_MISMATCH,
        AuthorityErrorCode.GENERATION_MISMATCH,
    }
    assert authority.revision == 0


def test_wrong_thread_rejected_and_queued_bridge_applies_on_main_thread(
    authority: ReceiverMixerAuthority,
    qapp: QCoreApplication,
) -> None:
    direct_results = []
    command = _command(authority, "request_resync", {})

    thread = threading.Thread(target=lambda: direct_results.append(authority.process_command(command, generation=7)))
    thread.start()
    thread.join()
    assert direct_results[0].error_code is AuthorityErrorCode.WRONG_THREAD
    assert authority.revision == 0

    queued_results = []
    authority.command_completed.connect(queued_results.append)
    authority.queue_validated(ValidatedCommandEnvelope(command, generation=7))
    qapp.processEvents()
    assert queued_results[0].accepted
    assert authority.revision == 1


def test_queued_bridge_enforces_five_second_deadline(
    tmp_path: Path,
    qapp: QCoreApplication,
) -> None:
    now = [0.0]
    profiles_dir = tmp_path / "profiles"
    config = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=profiles_dir)
    config.allow_remote_mixer_editing = True
    config.remote_midi_role = "receive"
    profiles = ProfileManager(profiles_dir=profiles_dir)
    profiles.set_active_silently(config.active_profile_id)
    session = ControlSessionMetadata(_id(), _id(), 1)
    receiver = ReceiverMixerAuthority(config, profiles, active_session=session, clock=lambda: now[0])
    command = _command(receiver, "request_resync", {})
    results = []
    receiver.command_completed.connect(results.append)
    receiver.queue_control_envelope(
        SimpleNamespace(
            generation=1,
            role=SimpleNamespace(value="receive"),
            selected_peer_id=None,
            connected_peer_id=None,
            transport_session_id=session.transport_session_id,
            message=command,
            received_at=0.0,
        )
    )
    now[0] = 5.1
    qapp.processEvents()
    assert results[0].error_code is AuthorityErrorCode.DEADLINE_EXCEEDED
    assert receiver.revision == 0
