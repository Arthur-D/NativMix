from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nativmix.audio.manager import (
    PipeWireManager,
    _PipeWirePollerThread,
    _read_wpctl_default_sink_name,
    _read_wpctl_default_sink_state,
)
from nativmix.audio.volume_scheduler import VolumeIntentCoordinator
from nativmix.remote_sync.authority import ControlSessionMetadata, ReceiverMixerAuthority
from nativmix.remote_sync.protocol import PROTOCOL_VERSION, CommandMessage
from nativmix.remote_sync.schema import SCHEMA_VERSION
from nativmix.utils.config_manager import ConfigManager
from nativmix.utils.profile_manager import ProfileManager


def _sink(
    name: str,
    *,
    index: int = 1,
    volume: float = 1.0,
    muted: bool = False,
    device_api: str = "alsa",
) -> SimpleNamespace:
    return SimpleNamespace(
        index=index,
        name=name,
        description=name,
        proplist={"device.api": device_api} if device_api else {},
        volume=SimpleNamespace(value_flat=volume),
        mute=muted,
    )


def _alias_manager(tmp_path, *, feedback: bool = True) -> tuple[PipeWireManager, ConfigManager]:
    config = ConfigManager(
        config_path=tmp_path / "config.json",
        profiles_dir=tmp_path / "profiles",
    )
    config.set_channel_mode(0, "hardware")
    config.set_hardware_id(0, "sink:alsa_output.current")
    config.set_app_names(1, ["System Master"])
    config.midi_fader_feedback = feedback
    manager = PipeWireManager(config=config)
    manager.pw_only_mode = True
    manager.can_set_volume_pw = True
    manager._replace_effective_output_inventory(
        "alsa_output.current",
        [_sink("alsa_output.current")],
    )
    return manager, config


def test_system_master_and_default_hardware_sync_immediately_with_one_write(tmp_path):
    manager, config = _alias_manager(tmp_path)
    updates: list[tuple[int, float]] = []
    manager.channel_volume_changed.connect(lambda channel, volume: updates.append((channel, volume)))

    with (
        patch.object(manager, "_apply_hardware_volume") as apply_hardware,
        patch.object(manager, "_apply_system_master_volume") as apply_master,
    ):
        manager.set_channel_volume(0, 0.42)

    assert config.get_channel_volume(0) == pytest.approx(0.42)
    assert config.get_channel_volume(1) == pytest.approx(0.42)
    assert updates == [(1, pytest.approx(0.42)), (0, pytest.approx(0.42))]
    apply_hardware.assert_called_once_with("sink:alsa_output.current", 0.42, pulse=None)
    apply_master.assert_not_called()


def test_system_master_source_writes_default_once_and_syncs_hardware(tmp_path):
    manager, config = _alias_manager(tmp_path)
    updates: list[tuple[int, float]] = []
    manager.channel_volume_changed.connect(lambda channel, volume: updates.append((channel, volume)))

    with (
        patch.object(manager, "_apply_hardware_volume") as apply_hardware,
        patch.object(manager, "_apply_system_master_volume") as apply_master,
    ):
        manager.set_channel_volume(1, 0.37)

    assert config.get_channel_volume(0) == pytest.approx(0.37)
    assert updates == [(0, pytest.approx(0.37)), (1, pytest.approx(0.37))]
    apply_master.assert_called_once_with(0.37, pulse=None)
    apply_hardware.assert_not_called()


def test_backend_confirmation_dedupes_equal_and_propagates_correction(tmp_path):
    manager, config = _alias_manager(tmp_path)
    updates: list[tuple[int, float]] = []
    manager.channel_volume_changed.connect(lambda channel, volume: updates.append((channel, volume)))

    with patch.object(manager, "_apply_hardware_volume"):
        manager.set_channel_volume(0, 0.42)
    updates.clear()

    manager._on_master_volume_changed(0.42, False)
    assert updates == []

    manager._on_master_volume_changed(0.39, False)
    assert updates == [(0, pytest.approx(0.39)), (1, pytest.approx(0.39))]
    assert config.get_channel_volume(0) == pytest.approx(0.39)
    assert config.get_channel_volume(1) == pytest.approx(0.39)


def test_rapid_values_ignore_superseded_confirmations_without_stale_jump(tmp_path):
    manager, config = _alias_manager(tmp_path)
    updates: list[tuple[int, float]] = []
    manager.channel_volume_changed.connect(lambda channel, volume: updates.append((channel, volume)))

    with patch.object(manager, "_apply_hardware_volume") as apply_hardware:
        for volume in (0.2, 0.4, 0.6):
            manager.set_channel_volume(0, volume)

    assert updates == [
        (1, pytest.approx(0.2)),
        (0, pytest.approx(0.2)),
        (1, pytest.approx(0.4)),
        (0, pytest.approx(0.4)),
        (1, pytest.approx(0.6)),
        (0, pytest.approx(0.6)),
    ]
    assert apply_hardware.call_count == 3
    updates.clear()

    manager._on_master_volume_changed(0.2, False)
    manager._on_master_volume_changed(0.4, False)
    manager._on_master_volume_changed(0.6, False)

    assert updates == []
    assert config.get_channel_volume(0) == pytest.approx(0.6)
    assert config.get_channel_volume(1) == pytest.approx(0.6)


def test_superseded_confirmation_stays_suppressed_until_latest_write_expires(tmp_path):
    manager, config = _alias_manager(tmp_path)
    with patch.object(manager, "_apply_hardware_volume", return_value=True):
        manager.set_channel_volume(0, 0.2)
        manager.set_channel_volume(0, 0.6)

    manager._on_master_volume_changed(0.2, False)
    assert config.get_channel_volume(0) == pytest.approx(0.6)

    manager._on_master_volume_changed(0.2, False)
    assert config.get_channel_volume(0) == pytest.approx(0.6)
    assert config.get_channel_volume(1) == pytest.approx(0.6)


def test_rapid_alternating_values_reject_delayed_middle_confirmation(tmp_path):
    manager, config = _alias_manager(tmp_path)
    with patch.object(manager, "_apply_hardware_volume", return_value=True):
        manager.set_channel_volume(0, 0.2)
        manager.set_channel_volume(0, 0.8)
        manager.set_channel_volume(0, 0.2)

    manager._on_master_volume_changed(0.2, False)
    manager._on_master_volume_changed(0.8, False)

    assert config.get_channel_volume(0) == pytest.approx(0.2)
    assert config.get_channel_volume(1) == pytest.approx(0.2)


def test_expired_pending_request_cannot_hide_later_legitimate_backend_value(tmp_path):
    manager, config = _alias_manager(tmp_path)
    with patch.object(manager, "_apply_hardware_volume", return_value=True):
        manager.set_channel_volume(0, 0.2)
        manager.set_channel_volume(0, 0.6)
    manager._pending_output_started_at["alsa_output.current"] -= 10.0

    manager._on_master_volume_changed(0.2, False)

    assert config.get_channel_volume(0) == pytest.approx(0.2)
    assert config.get_channel_volume(1) == pytest.approx(0.2)


def test_alias_mute_syncs_immediately_with_one_hardware_write(tmp_path):
    manager, _config = _alias_manager(tmp_path)
    manager.pw_only_mode = False
    mute_updates: list[tuple[int, bool]] = []
    manager.mute_state_changed.connect(lambda channel, muted: mute_updates.append((channel, muted)))
    pulse = MagicMock()
    pulse_context = MagicMock()
    pulse_context.__enter__.return_value = pulse

    with patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse_context):
        manager.toggle_mute(0)

    assert manager.is_channel_muted(0)
    assert manager.is_channel_muted(1)
    assert mute_updates == [(0, True), (1, True)]
    pulse.mute.assert_called_once()

    mute_updates.clear()
    manager._on_master_volume_changed(1.0, True)
    assert mute_updates == []


def test_default_sink_change_reassigns_alias_component(tmp_path):
    manager, config = _alias_manager(tmp_path)
    config.set_channel_mode(2, "hardware")
    config.set_hardware_id(2, "sink:alsa_output.new")
    sinks = [_sink("alsa_output.current"), _sink("alsa_output.new", index=2)]

    manager._replace_effective_output_inventory("alsa_output.current", sinks)
    assert manager.get_effective_shared_target_channels(1) == [0, 1]

    manager._replace_effective_output_inventory("alsa_output.new", sinks)
    assert manager.get_effective_shared_target_channels(0) == [0]
    assert manager.get_effective_shared_target_channels(1) == [1, 2]


@pytest.mark.parametrize(
    ("default_sink", "sinks"),
    [
        ("default", [_sink("alsa_output.one"), _sink("alsa_output.two", index=2)]),
        ("alsa_output.duplicate", [_sink("alsa_output.duplicate"), _sink("alsa_output.duplicate", index=2)]),
        ("easyeffects_sink", [_sink("easyeffects_sink")]),
        ("NativMix_CH_0", [_sink("NativMix_CH_0")]),
        ("alsa_output.current.monitor", [_sink("alsa_output.current.monitor")]),
    ],
)
def test_unknown_ambiguous_virtual_and_monitor_defaults_never_alias(
    tmp_path,
    default_sink,
    sinks,
):
    manager, _config = _alias_manager(tmp_path)

    manager._replace_effective_output_inventory(default_sink, sinks)

    assert manager.get_effective_shared_target_channels(0) == [0]
    assert manager.get_effective_shared_target_channels(1) == [1]


def test_feedback_off_preserves_last_moved_volume_but_mute_still_aliases(tmp_path):
    manager, config = _alias_manager(tmp_path, feedback=False)

    with patch.object(manager, "_apply_hardware_volume"):
        manager.set_channel_volume(0, 0.25)

    assert config.get_channel_volume(0) == pytest.approx(0.25)
    assert config.get_channel_volume(1) == pytest.approx(1.0)
    assert manager.get_effective_shared_target_channels(0) == [0, 1]

    manager._on_master_volume_changed(0.25, False)
    assert config.get_channel_volume(1) == pytest.approx(0.25)


def test_same_default_inventory_refresh_preserves_stale_confirmation_order(tmp_path):
    manager, config = _alias_manager(tmp_path)
    manager._running = True
    with patch.object(manager, "_apply_hardware_volume"):
        manager.set_channel_volume(0, 0.2)
        manager.set_channel_volume(0, 0.6)

    pulse = MagicMock()
    pulse.server_info.return_value.default_sink_name = "alsa_output.current"
    pulse.sink_list.return_value = [_sink("alsa_output.current")]
    pulse_context = MagicMock()
    pulse_context.__enter__.return_value = pulse
    with patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse_context):
        manager.pw_only_mode = False
        manager.refresh_effective_output_aliases()

    manager._on_master_volume_changed(0.2, False)
    assert config.get_channel_volume(0) == pytest.approx(0.6)
    assert config.get_channel_volume(1) == pytest.approx(0.6)


def test_failed_latest_write_allows_last_real_confirmation_to_correct_state(tmp_path):
    manager, config = _alias_manager(tmp_path)
    with (
        patch.object(manager, "_apply_hardware_volume", side_effect=[True, False]),
        patch.object(manager, "_apply_system_master_volume") as apply_master,
    ):
        manager.set_channel_volume(0, 0.2)
        manager.set_channel_volume(0, 0.6)

    apply_master.assert_not_called()
    assert manager._pending_output_volumes == {"alsa_output.current": 0.2}
    manager._on_master_volume_changed(0.2, False)

    assert config.get_channel_volume(0) == pytest.approx(0.2)
    assert config.get_channel_volume(1) == pytest.approx(0.2)


def test_wpctl_default_parser_accepts_standard_tree_prefixes():
    output = """
Audio
 ├─ Devices:
 │      45. Built-in Audio
 │
 ├─ Sinks:
 │  *   48. alsa_output.usb-headset    [vol: 0.50]
 │      52. alsa_output.pci-speaker    [vol: 0.25]
 ├─ Sources:
 │      49. alsa_input.usb-headset     [vol: 1.00]
"""
    result = SimpleNamespace(returncode=0, stdout=output)

    with (
        patch("nativmix.audio.manager.shutil.which", return_value="/usr/bin/wpctl"),
        patch("nativmix.audio.manager.subprocess.run", return_value=result),
    ):
        assert _read_wpctl_default_sink_name() == "alsa_output.usb-headset"


def test_wpctl_default_state_parser_reads_volume_and_mute():
    result = SimpleNamespace(returncode=0, stdout="Volume: 0.375 [MUTED]\n")

    with (
        patch("nativmix.audio.manager.shutil.which", return_value="/usr/bin/wpctl"),
        patch("nativmix.audio.manager.subprocess.run", return_value=result),
    ):
        assert _read_wpctl_default_sink_state() == (pytest.approx(0.375), True)


def test_pw_only_poller_detects_default_switch_without_stream_node_change():
    poller = _PipeWirePollerThread()
    defaults = iter(["alsa_output.one", "alsa_output.two"])
    stream_node = SimpleNamespace(node_id=10)
    sink_sets = [
        [_sink("alsa_output.one"), _sink("alsa_output.two", index=2)],
        [_sink("alsa_output.one"), _sink("alsa_output.two", index=2)],
    ]
    events: list[str] = []
    states: list[tuple[float, bool]] = []

    def capture(default_sink: str, _sinks: list[object]) -> None:
        events.append(default_sink)
        if len(events) == 2:
            poller._running = False

    poller.default_sink_changed.connect(capture)
    poller.master_volume_changed.connect(lambda volume, muted: states.append((volume, muted)))
    with (
        patch("nativmix.audio.manager._read_wpctl_default_sink_name", side_effect=defaults),
        patch(
            "nativmix.audio.manager._read_wpctl_default_sink_state",
            side_effect=[(0.5, False), (0.5, False)],
        ),
        patch(
            "nativmix.audio.manager._pw_dump_nodes",
            side_effect=[[stream_node], sink_sets[0], [stream_node], sink_sets[1]],
        ),
        patch("nativmix.audio.manager.time.sleep"),
    ):
        poller.run()

    assert events == ["alsa_output.one", "alsa_output.two"]
    assert states == [(0.5, False), (0.5, False)]


def test_profile_target_and_topology_changes_invalidate_or_refresh_aliases(tmp_path):
    manager, config = _alias_manager(tmp_path)
    target_events: list[None] = []
    config.channel_targets_changed.connect(lambda: target_events.append(None))

    config.set_hardware_id(0, "sink:alsa_output.other")
    assert target_events == [None]
    config.apply_profile(
        {
            "id": "profile-alias-test",
            "channel_count": config.num_channels,
            "channels": config.all_channels(),
        }
    )
    assert target_events == [None, None]

    manager.invalidate_effective_output_aliases()
    assert manager.get_effective_shared_target_channels(1) == [1]

    manager._running = True
    with (
        patch.object(manager, "refresh_effective_output_aliases") as refresh,
        patch.object(manager, "reconcile_v_sinks") as reconcile,
    ):
        manager._on_audio_topology_changed()

    refresh.assert_called_once_with()
    reconcile.assert_called_once_with(create_missing=False)

    config.set_hardware_id(0, "sink:alsa_output.current")
    manager._replace_effective_output_inventory(
        "alsa_output.current",
        [_sink("alsa_output.current")],
    )
    assert manager.get_effective_shared_target_channels(1) == [0, 1]
    with patch("nativmix.audio.manager.QTimer.singleShot"):
        manager._on_thread_finished()
    assert manager.get_effective_shared_target_channels(1) == [1]


def test_remote_authority_observes_alias_fanout_as_one_canonical_mutation(tmp_path, qapp):
    manager, config = _alias_manager(tmp_path)
    config.allow_remote_mixer_editing = True
    config.remote_midi_role = "receive"
    config._persist_active_profile_channels()
    profiles = ProfileManager(profiles_dir=tmp_path / "profiles")
    profiles.set_active_silently(config.active_profile_id)
    authority = ReceiverMixerAuthority(
        config,
        profiles,
        manager,
        active_session=ControlSessionMetadata(
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
            1,
        ),
    )
    authority.prime_observed_state()
    authority.connect_local_sources()
    publications = []
    authority.publication_ready.connect(publications.append)

    with patch.object(manager, "_apply_hardware_volume") as apply_hardware:
        manager.set_channel_volume(0, 0.44)
    qapp.processEvents()

    assert apply_hardware.call_count == 1
    assert len(publications) == 1
    assert publications[0].delta is not None
    assert set(publications[0].delta.changes["volumes"]) == set(
        publications[0].snapshot.channel_order[:2]
    )
    runtime = publications[0].snapshot.runtime_states
    assert [state.effective_volume for state in runtime[:2]] == pytest.approx([0.44, 0.44])


def test_async_alias_commit_publishes_all_channels_when_motor_feedback_is_off(tmp_path, qtbot):
    manager, config = _alias_manager(tmp_path, feedback=False)
    config.allow_remote_mixer_editing = True
    config._persist_active_profile_channels()
    profiles = ProfileManager(profiles_dir=tmp_path / "profiles")
    profiles.set_active_silently(config.active_profile_id)
    authority = ReceiverMixerAuthority(
        config,
        profiles,
        manager,
        active_session=ControlSessionMetadata(
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
            1,
        ),
    )
    authority.prime_observed_state()
    publications = []
    authority.publication_ready.connect(publications.append)
    coordinator = VolumeIntentCoordinator(
        manager,
        config,
        key_provider=manager.get_effective_shared_target_channels,
    )
    coordinator.committed.connect(
        lambda intent: authority.capture_runtime_volume(intent.channel, intent.volume)
    )

    try:
        with patch.object(manager, "_apply_hardware_volume", return_value=True) as apply_hardware:
            coordinator.submit_gui(0, 0.44)
            qtbot.waitUntil(lambda: len(publications) == 1)

        apply_hardware.assert_called_once()
        assert publications[0].delta is not None
        assert set(publications[0].delta.changes["volumes"]) == set(
            publications[0].snapshot.channel_order[:2]
        )
    finally:
        coordinator.stop()


def test_remote_command_publishes_all_alias_volumes_in_one_delta(tmp_path, qapp):
    manager, config = _alias_manager(tmp_path)
    config.allow_remote_mixer_editing = True
    config.remote_midi_role = "receive"
    config._persist_active_profile_channels()
    profiles = ProfileManager(profiles_dir=tmp_path / "profiles")
    profiles.set_active_silently(config.active_profile_id)
    session = ControlSessionMetadata(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        1,
    )
    authority = ReceiverMixerAuthority(config, profiles, manager, active_session=session)
    snapshot = authority.current_snapshot()
    publications = []
    authority.publication_ready.connect(publications.append)
    command = CommandMessage(
        protocol_version=PROTOCOL_VERSION,
        schema_version=SCHEMA_VERSION,
        transport_session_id=session.transport_session_id,
        control_session_id=session.control_session_id,
        command_id="33333333-3333-4333-8333-333333333333",
        receiver_epoch=authority.epoch,
        expected_revision=authority.revision,
        command_type="set_channel_volume",
        payload={
            "profile_id": snapshot.active_profile_id,
            "channel_id": snapshot.channel_order[0],
            "volume": 0.48,
        },
    )

    with patch.object(manager, "_apply_hardware_volume") as apply_hardware:
        result = authority.process_command(command, generation=1)
    qapp.processEvents()

    assert result.accepted
    assert result.publication is not None
    assert result.publication.delta is not None
    assert apply_hardware.call_count == 1
    assert set(result.publication.delta.changes["volumes"]) == set(snapshot.channel_order[:2])
    assert len(publications) == 1


def test_alias_snapshot_reads_are_lock_safe_during_invalidation(tmp_path):
    manager, _config = _alias_manager(tmp_path)
    errors: list[BaseException] = []
    physical = [_sink("alsa_output.current")]

    def replace_and_invalidate() -> None:
        try:
            for _ in range(100):
                manager._replace_effective_output_inventory("alsa_output.current", physical)
                manager.invalidate_effective_output_aliases()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=replace_and_invalidate)
    worker.start()
    for _ in range(100):
        assert manager.get_effective_shared_target_channels(0) in ([0], [0, 1])
    worker.join()

    assert errors == []
