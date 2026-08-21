import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from nativmix.audio.manager import PipeWireManager
from nativmix.utils.config_manager import ConfigManager


def _module(index, name, argument):
    return SimpleNamespace(index=index, name=name, argument=argument)


def _sink(index, name, owner_module=None):
    return SimpleNamespace(index=index, name=name, owner_module=owner_module)


class _Pulse:
    def __init__(self, *, modules=(), sinks=(), sink_inputs=(), default="alsa_output"):
        self.modules = list(modules)
        self.sinks = list(sinks)
        self.sink_inputs = list(sink_inputs)
        self.default = default
        self.moves = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def module_list(self):
        return list(self.modules)

    def sink_list(self):
        return list(self.sinks)

    def sink_input_list(self):
        return list(self.sink_inputs)

    def server_info(self):
        return SimpleNamespace(default_sink_name=self.default)

    def sink_input_move(self, stream_index, sink_index):
        self.moves.append((stream_index, sink_index))


def _manager(tmp_path):
    config = ConfigManager(
        config_path=tmp_path / "config.json",
        profiles_dir=tmp_path / "profiles",
    )
    manager = PipeWireManager(config=config)
    manager.routing_owner = "nativmix"
    manager.effective_routing_owner = "nativmix"
    manager.v_sink_supported = True
    manager.pw_only_mode = False
    return manager


def test_inventory_matches_exact_owned_arguments(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(
        modules=[
            _module(1, "module-null-sink", "sink_name=NativMix_CH_0 sink_properties=x"),
            _module(2, "module-null-sink", "sink_name=NativMix_CH_0_extra"),
            _module(3, "module-null-sink", "foo=sink_name=NativMix_CH_5"),
            _module(4, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output dont-link=1"),
            _module(5, "module-loopback", "source=NativMix_CH_0.monitor-extra"),
            _module(6, "module-loopback", "source=Other.monitor"),
        ]
    )

    null_sinks, loopbacks = manager._inventory_v_sink_modules(pulse)

    assert {channel: [module.index for module in modules] for channel, modules in null_sinks.items()} == {0: [1]}
    assert {channel: [module.index for module in modules] for channel, modules in loopbacks.items()} == {0: [4]}


def test_duplicate_ch0_modules_are_deduplicated_without_loading(tmp_path):
    manager = _manager(tmp_path)
    manager._config.set_v_sink_enabled(0, True)
    pulse = _Pulse(
        modules=[
            _module(10, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(11, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(20, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output"),
            _module(21, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output"),
        ],
        sinks=[_sink(30, "NativMix_CH_0", 10), _sink(40, "alsa_output")],
    )

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_unload_v_sink_modules", return_value=1) as unload,
        patch.object(manager, "_update_sink_metadata"),
        patch.object(manager, "_restore_hardware_default_sink"),
        patch.object(manager, "_unmute_module_streams"),
        patch.object(manager, "_wait_for_loopback_node", return_value=None),
        patch("nativmix.audio.manager.subprocess.run") as run,
    ):
        manager.enable_v_sink(0)

    assert [module.index for module in unload.call_args_list[0].args[0]] == [11]
    assert [module.index for module in unload.call_args_list[1].args[0]] == [21]
    assert not any("load-module" in call.args[0] for call in run.call_args_list)


def test_delayed_registration_and_concurrent_enable_load_once(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(sinks=[_sink(40, "alsa_output")])
    load_calls = []

    def run(command, **_kwargs):
        if command[1:3] == ["load-module", "module-null-sink"]:
            load_calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="77")
        raise AssertionError(command)

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_wait_for_single_v_sink", return_value=None),
        patch.object(manager, "_update_thread_states"),
        patch("nativmix.audio.manager.subprocess.run", side_effect=run),
    ):
        threads = [threading.Thread(target=manager.enable_v_sink, args=(0,)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert len(load_calls) == 1
    assert manager._vsink_pending_null[0][0] == 77


def test_delayed_loopback_registration_does_not_load_duplicate(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(
        modules=[_module(10, "module-null-sink", "sink_name=NativMix_CH_0")],
        sinks=[_sink(30, "NativMix_CH_0", 10), _sink(40, "alsa_output")],
    )
    load_calls = []

    def run(command, **_kwargs):
        if command[1:3] == ["load-module", "module-loopback"]:
            load_calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="78")
        raise AssertionError(command)

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_wait_for_loopback_node", return_value=None),
        patch.object(manager, "_update_sink_metadata"),
        patch.object(manager, "_restore_hardware_default_sink"),
        patch.object(manager, "_update_thread_states"),
        patch("nativmix.audio.manager.subprocess.run", side_effect=run),
    ):
        manager.enable_v_sink(0)
        manager.enable_v_sink(0)

    assert len(load_calls) == 1
    assert manager._vsink_pending_loopback[0][0] == 78


def test_legacy_loopback_without_sink_target_is_replaced(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(
        modules=[
            _module(10, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(20, "module-loopback", "source=NativMix_CH_0.monitor dont-link=1"),
        ],
        sinks=[_sink(30, "NativMix_CH_0", 10), _sink(40, "alsa_output")],
    )
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[1:3] == ["load-module", "module-loopback"]:
            return subprocess.CompletedProcess(command, 0, stdout="78")
        return subprocess.CompletedProcess(command, 0, stdout="")

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_wait_for_loopback_node", return_value=None),
        patch.object(manager, "_update_sink_metadata"),
        patch.object(manager, "_restore_hardware_default_sink"),
        patch.object(manager, "_update_thread_states"),
        patch("nativmix.audio.manager.subprocess.run", side_effect=run),
    ):
        manager.enable_v_sink(0)

    assert ["pactl", "unload-module", "20"] in commands
    assert [
        "pactl",
        "load-module",
        "module-loopback",
        "source=NativMix_CH_0.monitor",
        "sink=alsa_output",
        "dont-link=1",
    ] in commands


def test_stale_loopback_clears_pending_module_before_replacement(tmp_path):
    manager = _manager(tmp_path)
    manager._vsink_pending_loopback[0] = (20, time.monotonic())
    pulse = _Pulse(
        modules=[
            _module(10, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(20, "module-loopback", "source=NativMix_CH_0.monitor sink=old_output"),
        ],
        sinks=[_sink(30, "NativMix_CH_0", 10), _sink(40, "alsa_output")],
    )
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[1:3] == ["load-module", "module-loopback"]:
            return subprocess.CompletedProcess(command, 0, stdout="78")
        return subprocess.CompletedProcess(command, 0, stdout="")

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_wait_for_loopback_node", return_value=None),
        patch.object(manager, "_update_sink_metadata"),
        patch.object(manager, "_restore_hardware_default_sink"),
        patch.object(manager, "_update_thread_states"),
        patch("nativmix.audio.manager.subprocess.run", side_effect=run),
    ):
        manager.enable_v_sink(0)

    assert any(command[1:3] == ["load-module", "module-loopback"] for command in commands)
    assert manager._vsink_pending_loopback[0][0] == 78


def test_reconcile_removes_stale_channels_when_active_profile_has_none(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(
        modules=[
            _module(5, "module-null-sink", "sink_name=NativMix_CH_5"),
            _module(6, "module-loopback", "source=NativMix_CH_5.monitor"),
            _module(12, "module-null-sink", "sink_name=NativMix_CH_12"),
        ]
    )

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_disable_v_sink_locked") as disable,
    ):
        manager.reconcile_v_sinks()

    assert [call.args[0] for call in disable.call_args_list] == [5, 12]


def test_reconcile_retains_one_pair_for_enabled_ch0(tmp_path):
    manager = _manager(tmp_path)
    manager._config.set_v_sink_enabled(0, True)
    pulse = _Pulse(
        modules=[
            _module(1, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(2, "module-loopback", "source=NativMix_CH_0.monitor"),
        ]
    )

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_enable_v_sink_locked") as enable,
        patch.object(manager, "_disable_v_sink_locked") as disable,
    ):
        manager.reconcile_v_sinks()

    enable.assert_called_once_with(0)
    disable.assert_not_called()


def test_profile_config_change_reconciles_old_enabled_channel(tmp_path):
    manager = _manager(tmp_path)
    manager._config.set_v_sink_enabled(0, True)
    pulse = _Pulse(modules=[_module(1, "module-null-sink", "sink_name=NativMix_CH_0")])
    manager._config.set_v_sink_enabled(0, False)

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_disable_v_sink_locked") as disable,
    ):
        manager.reconcile_v_sinks()

    disable.assert_called_once_with(0)


def test_routing_owner_transition_reconciles_to_zero(tmp_path):
    manager = _manager(tmp_path)
    manager._running = True
    manager._poti_volumes = {}

    with (
        patch.object(manager, "reconcile_v_sinks") as reconcile,
        patch.object(manager, "_refresh_owned_gain_paths"),
        patch.object(manager, "_refresh_virtual_processing_sinks"),
        patch.object(manager, "_update_gain_control_capability"),
        patch.object(manager, "_update_v_sink_capability"),
        patch.object(manager, "_update_thread_states"),
        patch.object(manager, "_apply_channel_volume"),
        patch.object(manager, "_publish_routing_owner_status"),
    ):
        manager._activate_routing_owner("none")

    reconcile.assert_called_once_with()


def test_disable_evacuates_all_duplicate_sinks_and_unloads_every_module(tmp_path):
    manager = _manager(tmp_path)
    inputs = [
        SimpleNamespace(index=100, sink=30),
        SimpleNamespace(index=101, sink=31),
    ]
    modules = [
        _module(1, "module-null-sink", "sink_name=NativMix_CH_0"),
        _module(2, "module-null-sink", "sink_name=NativMix_CH_0"),
        _module(3, "module-loopback", "source=NativMix_CH_0.monitor"),
        _module(4, "module-loopback", "source=NativMix_CH_0.monitor"),
    ]
    pulse = _Pulse(
        modules=modules,
        sinks=[
            _sink(30, "NativMix_CH_0", 1),
            _sink(31, "NativMix_CH_0", 2),
            _sink(40, "alsa_output"),
        ],
        sink_inputs=inputs,
    )

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_seamless_move") as move,
        patch.object(manager, "_unload_v_sink_modules", return_value=4) as unload,
        patch.object(manager, "_update_thread_states"),
        patch.object(manager, "_apply_volume_by_name"),
        patch("nativmix.audio.manager.time.sleep"),
    ):
        manager.disable_v_sink(0)

    assert {call.args[1] for call in move.call_args_list} == {100, 101}
    assert {module.index for module in unload.call_args.args[0]} == {1, 2, 3, 4}


def test_enable_evacuates_duplicate_module_stream_before_unload(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(
        modules=[
            _module(1, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(2, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(3, "module-loopback", "source=NativMix_CH_0.monitor"),
        ],
        sinks=[
            _sink(30, "NativMix_CH_0", 1),
            _sink(31, "NativMix_CH_0", 2),
            _sink(40, "alsa_output"),
        ],
        sink_inputs=[SimpleNamespace(index=100, sink=31)],
    )

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_seamless_move") as move,
        patch.object(manager, "_unload_v_sink_modules", return_value=1),
        patch.object(manager, "_wait_for_single_v_sink", return_value=_sink(30, "NativMix_CH_0", 1)),
        patch.object(manager, "_update_sink_metadata"),
        patch.object(manager, "_restore_hardware_default_sink"),
        patch.object(manager, "_unmute_module_streams"),
        patch.object(manager, "_wait_for_loopback_node", return_value=None),
    ):
        manager.enable_v_sink(0)

    move.assert_called_once()
    assert move.call_args.args[1:3] == (100, 30)


def test_disable_unloads_modules_that_have_not_registered_yet(tmp_path):
    manager = _manager(tmp_path)
    manager._vsink_pending_null[0] = (77, 1.0)
    manager._vsink_pending_loopback[0] = (78, 1.0)
    pulse = _Pulse(sinks=[_sink(40, "alsa_output")])

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_unload_v_sink_modules", return_value=2) as unload,
        patch.object(manager, "_update_thread_states"),
        patch.object(manager, "_apply_volume_by_name"),
    ):
        manager.disable_v_sink(0)

    assert {module.index for module in unload.call_args.args[0]} == {77, 78}
    assert 0 not in manager._vsink_pending_null
    assert 0 not in manager._vsink_pending_loopback
