import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pulsectl
import pytest

from nativmix.audio.manager import PipeWireManager, _AudioListenerThread
from nativmix.utils.config_manager import ConfigManager


def _module(index, name, argument):
    return SimpleNamespace(index=index, name=name, argument=argument)


def _sink(index, name, owner_module=None):
    return SimpleNamespace(index=index, name=name, owner_module=owner_module)


def _source(index, name, owner_module=None):
    return SimpleNamespace(index=index, name=name, owner_module=owner_module)


class _Pulse:
    def __init__(self, *, modules=(), sinks=(), sources=None, sink_inputs=(), default="alsa_output.usb"):
        self.modules = list(modules)
        self.sinks = list(sinks)
        self.sources = None if sources is None else list(sources)
        self.sink_inputs = list(sink_inputs)
        self.default = default
        self.moves = []
        self.unloaded = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def module_list(self):
        return list(self.modules)

    def sink_list(self):
        return list(self.sinks)

    def source_list(self):
        if self.sources is not None:
            return list(self.sources)
        return [
            _source(1000 + sink.index, f"{sink.name}.monitor", sink.owner_module)
            for sink in self.sinks
            if sink.name.startswith("NativMix_")
        ]

    def sink_input_list(self):
        derived = []
        sinks_by_name = {sink.name: sink for sink in self.sinks}
        for module in self.modules:
            if module.name != "module-loopback":
                continue
            sink_name = PipeWireManager._module_argument_value(module.argument, "sink")
            sink = sinks_by_name.get(sink_name)
            if sink is not None:
                derived.append(
                    SimpleNamespace(
                        index=2000 + int(module.index),
                        sink=sink.index,
                        owner_module=int(module.index),
                    )
                )
        return list(self.sink_inputs) + derived

    def source_output_list(self):
        derived = []
        sources_by_name = {source.name: source for source in self.source_list()}
        for module in self.modules:
            if module.name != "module-loopback":
                continue
            source_name = PipeWireManager._module_argument_value(module.argument, "source")
            source = sources_by_name.get(source_name)
            if source is not None:
                derived.append(
                    SimpleNamespace(
                        index=3000 + int(module.index),
                        source=source.index,
                        owner_module=int(module.index),
                    )
                )
        return derived

    def server_info(self):
        return SimpleNamespace(default_sink_name=self.default)

    def sink_input_move(self, stream_index, sink_index):
        self.moves.append((stream_index, sink_index))

    def module_unload(self, module_index):
        self.unloaded.append(int(module_index))
        self.modules = [module for module in self.modules if int(module.index) != int(module_index)]


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
            _module(4, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output.usb dont-link=1"),
            _module(5, "module-loopback", "source=NativMix_CH_0.monitor-extra"),
            _module(6, "module-loopback", "source=Other.monitor"),
            _module(
                7,
                "module-loopback",
                "source=NativMix_CH_0.monitor "
                "sink_input_properties=application.name=UserManagedLoopback",
            ),
            _module(8, "module-loopback", "source=NativMix_CH_0.monitor sink=recorder_sink"),
            _module(
                9,
                "module-loopback",
                "source=NativMix_CH_1.monitor "
                "sink_input_properties=application.name=NativMixLoopback_CH_1",
            ),
        ]
    )

    null_sinks, loopbacks = manager._inventory_v_sink_modules(pulse)

    assert {channel: [module.index for module in modules] for channel, modules in null_sinks.items()} == {0: [1]}
    assert {channel: [module.index for module in modules] for channel, modules in loopbacks.items()} == {
        0: [4],
        1: [9],
    }


def test_duplicate_ch0_modules_are_deduplicated_without_loading(tmp_path):
    manager = _manager(tmp_path)
    manager._config.set_v_sink_enabled(0, True)
    pulse = _Pulse(
        modules=[
            _module(10, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(11, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(20, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output.usb dont-link=1"),
            _module(21, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output.usb dont-link=1"),
        ],
        sinks=[_sink(30, "NativMix_CH_0", 10), _sink(40, "alsa_output.usb")],
    )

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_unload_v_sink_modules", return_value=1) as unload,
        patch.object(manager, "_update_sink_metadata"),
        patch.object(manager, "_restore_hardware_default_sink"),
        patch.object(manager, "_unmute_module_streams"),
        patch.object(
            manager,
            "_wait_for_exact_v_sink_monitor",
            return_value=_source(1030, "NativMix_CH_0.monitor", 10),
        ),
        patch.object(manager, "_wait_for_loopback_node", return_value="loopback-20"),
        patch.object(manager, "_wait_for_exact_loopback_route", return_value=True),
        patch("nativmix.audio.manager.routing.clean_links"),
        patch("nativmix.audio.manager.routing.smart_link"),
        patch("nativmix.audio.manager.subprocess.run") as run,
    ):
        manager.enable_v_sink(0)

    assert [module.index for module in unload.call_args_list[0].args[0]] == [11]
    assert [module.index for module in unload.call_args_list[1].args[0]] == [21]
    assert not any("load-module" in call.args[0] for call in run.call_args_list)


def test_delayed_registration_and_concurrent_enable_load_once(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(sinks=[_sink(40, "alsa_output.usb")])
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
        sinks=[_sink(30, "NativMix_CH_0", 10), _sink(40, "alsa_output.usb")],
    )
    load_calls = []

    def run(command, **_kwargs):
        if command[1:3] == ["load-module", "module-loopback"]:
            load_calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="78")
        raise AssertionError(command)

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_wait_for_loopback_node", return_value="loopback-78"),
        patch.object(manager, "_wait_for_exact_loopback_route", return_value=True),
        patch.object(manager, "_update_sink_metadata"),
        patch.object(manager, "_restore_hardware_default_sink"),
        patch.object(manager, "_update_thread_states"),
        patch("nativmix.audio.manager.routing.clean_links"),
        patch("nativmix.audio.manager.routing.smart_link"),
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
        sinks=[_sink(30, "NativMix_CH_0", 10), _sink(40, "alsa_output.usb")],
    )
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[1:3] == ["load-module", "module-loopback"]:
            return subprocess.CompletedProcess(command, 0, stdout="78")
        return subprocess.CompletedProcess(command, 0, stdout="")

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_wait_for_loopback_node", return_value="loopback-78"),
        patch.object(manager, "_wait_for_exact_loopback_route", return_value=True),
        patch.object(manager, "_update_sink_metadata"),
        patch.object(manager, "_restore_hardware_default_sink"),
        patch.object(manager, "_update_thread_states"),
        patch("nativmix.audio.manager.routing.clean_links"),
        patch("nativmix.audio.manager.routing.smart_link"),
        patch("nativmix.audio.manager.subprocess.run", side_effect=run),
    ):
        manager.enable_v_sink(0)

    assert 20 in pulse.unloaded
    assert [
        "pactl",
        "load-module",
        "module-loopback",
        "source=NativMix_CH_0.monitor",
        "sink=alsa_output.usb",
        "sink_input_properties=application.name=NativMixLoopback_CH_0",
        "source_output_properties=application.name=NativMixLoopback_CH_0",
        "source_dont_move=1",
        "sink_dont_move=1",
        "dont-link=1",
    ] in commands


def test_stale_loopback_clears_pending_module_before_replacement(tmp_path):
    manager = _manager(tmp_path)
    manager._vsink_pending_loopback[0] = (20, time.monotonic())
    pulse = _Pulse(
        modules=[
            _module(10, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(20, "module-loopback", "source=NativMix_CH_0.monitor sink=old_output dont-link=1"),
        ],
        sinks=[_sink(30, "NativMix_CH_0", 10), _sink(40, "alsa_output.usb")],
    )
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[1:3] == ["load-module", "module-loopback"]:
            return subprocess.CompletedProcess(command, 0, stdout="78")
        return subprocess.CompletedProcess(command, 0, stdout="")

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_wait_for_loopback_node", return_value="loopback-78"),
        patch.object(manager, "_wait_for_exact_loopback_route", return_value=True),
        patch.object(manager, "_update_sink_metadata"),
        patch.object(manager, "_restore_hardware_default_sink"),
        patch.object(manager, "_update_thread_states"),
        patch("nativmix.audio.manager.routing.clean_links"),
        patch("nativmix.audio.manager.routing.smart_link"),
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
            _module(6, "module-loopback", "source=NativMix_CH_5.monitor dont-link=1"),
            _module(12, "module-null-sink", "sink_name=NativMix_CH_12"),
        ]
    )

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_disable_v_sink_locked") as disable,
    ):
        manager.reconcile_v_sinks()

    assert [call.args[0] for call in disable.call_args_list] == [5]
    assert pulse.unloaded == [12]


def test_reconcile_retains_one_pair_for_enabled_ch0(tmp_path):
    manager = _manager(tmp_path)
    manager._config.set_v_sink_enabled(0, True)
    pulse = _Pulse(
        modules=[
            _module(1, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(2, "module-loopback", "source=NativMix_CH_0.monitor dont-link=1"),
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


def test_profile_config_change_removes_orphaned_old_channel(tmp_path):
    manager = _manager(tmp_path)
    manager._config.set_v_sink_enabled(0, True)
    pulse = _Pulse(modules=[_module(1, "module-null-sink", "sink_name=NativMix_CH_0")])
    manager._config.set_v_sink_enabled(0, False)

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_disable_v_sink_locked") as disable,
    ):
        manager.reconcile_v_sinks()

    disable.assert_not_called()
    assert pulse.unloaded == [1]


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
        _module(3, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output.usb dont-link=1"),
        _module(4, "module-loopback", "source=NativMix_CH_0.monitor dont-link=1"),
    ]
    pulse = _Pulse(
        modules=modules,
        sinks=[
            _sink(30, "NativMix_CH_0", 1),
            _sink(31, "NativMix_CH_0", 2),
            _sink(40, "alsa_output.usb"),
        ],
        sink_inputs=inputs,
    )

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_seamless_move") as move,
        patch.object(manager, "_update_thread_states"),
        patch.object(manager, "_apply_volume_by_name"),
        patch("nativmix.audio.manager.time.sleep"),
    ):
        manager.disable_v_sink(0)

    assert {call.args[1] for call in move.call_args_list} == {100, 101}
    assert set(pulse.unloaded) == {1, 2, 3, 4}


def test_enable_evacuates_duplicate_module_stream_before_unload(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(
        modules=[
            _module(1, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(2, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(3, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output.usb dont-link=1"),
        ],
        sinks=[
            _sink(30, "NativMix_CH_0", 1),
            _sink(31, "NativMix_CH_0", 2),
            _sink(40, "alsa_output.usb"),
        ],
        sink_inputs=[SimpleNamespace(index=100, sink=31)],
    )

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_seamless_move") as move,
        patch.object(manager, "_wait_for_single_v_sink", return_value=_sink(30, "NativMix_CH_0", 1)),
        patch.object(manager, "_update_sink_metadata"),
        patch.object(manager, "_restore_hardware_default_sink"),
        patch.object(manager, "_unmute_module_streams"),
        patch.object(manager, "_wait_for_loopback_node", return_value="loopback-3"),
        patch.object(manager, "_wait_for_exact_loopback_route", return_value=True),
    ):
        manager.enable_v_sink(0)

    move.assert_called_once()
    assert move.call_args.args[1:3] == (100, 30)


def test_disable_unloads_modules_that_have_not_registered_yet(tmp_path):
    manager = _manager(tmp_path)
    manager._vsink_pending_null[0] = (77, 1.0)
    manager._vsink_pending_loopback[0] = (78, 1.0)
    pulse = _Pulse(sinks=[_sink(40, "alsa_output.usb")])

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_update_thread_states"),
        patch.object(manager, "_apply_volume_by_name"),
    ):
        manager.disable_v_sink(0)

    assert set(pulse.unloaded) == {77, 78}
    assert 0 not in manager._vsink_pending_null
    assert 0 not in manager._vsink_pending_loopback


def test_normal_pair_create_destroy_is_idempotent(tmp_path):
    manager = _manager(tmp_path)
    manager._config.set_v_sink_enabled(0, True)
    pulse = _Pulse(sinks=[_sink(40, "alsa_output.usb")])
    load_commands = []

    def run(command, **_kwargs):
        if command[1] != "load-module":
            raise AssertionError(command)
        load_commands.append(command)
        if command[2] == "module-null-sink":
            pulse.modules.append(_module(77, command[2], " ".join(command[3:])))
            pulse.sinks.append(_sink(30, "NativMix_CH_0", 77))
            return subprocess.CompletedProcess(command, 0, stdout="77")
        pulse.modules.append(_module(78, command[2], " ".join(command[3:])))
        return subprocess.CompletedProcess(command, 0, stdout="78")

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_update_sink_metadata"),
        patch.object(manager, "_wait_for_loopback_node", return_value="loopback-78"),
        patch.object(manager, "_unmute_module_streams"),
        patch.object(manager, "_move_apps_to_sink"),
        patch.object(manager, "_set_v_sink_volume"),
        patch.object(manager, "_restore_hardware_default_sink"),
        patch("nativmix.audio.manager.routing.clean_links"),
        patch("nativmix.audio.manager.routing.smart_link"),
        patch("nativmix.audio.manager.subprocess.run", side_effect=run),
    ):
        manager.enable_v_sink(0)
        manager.enable_v_sink(0)
        manager.disable_v_sink(0)
        manager.disable_v_sink(0)

    assert [command[2] for command in load_commands] == ["module-null-sink", "module-loopback"]
    assert {module.name for module in pulse.modules} == set()
    assert pulse.unloaded == [78, 77]


def test_exact_live_orphan_loopback_is_removed_at_startup(tmp_path):
    manager = _manager(tmp_path)
    leaked = _module(
        536870919,
        "module-loopback",
        "source=NativMix_CH_0.monitor sink=alsa_output dont-link=1",
    )
    unrelated = _module(
        42,
        "module-loopback",
        "source=UserMix.monitor sink=alsa_output.usb",
    )
    pulse = _Pulse(modules=[leaked, unrelated], sinks=[_sink(40, "alsa_output.usb")])

    with patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse):
        assert manager._recover_orphaned_v_sink_modules("startup") is True

    assert pulse.unloaded == [536870919]
    assert pulse.modules == [unrelated]


def test_orphan_null_sink_without_loopback_is_removed(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(
        modules=[_module(77, "module-null-sink", "sink_name=NativMix_CH_0")],
        sinks=[_sink(30, "NativMix_CH_0", 77), _sink(40, "alsa_output.usb")],
    )

    with patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse):
        assert manager._recover_orphaned_v_sink_modules("startup") is True

    assert pulse.unloaded == [77]
    assert pulse.modules == []


def test_shutdown_cleanup_uses_live_inventory_when_memory_ids_are_stale(tmp_path):
    manager = _manager(tmp_path)
    manager._vsink_pending_null[0] = (901, time.monotonic())
    manager._vsink_pending_loopback[0] = (902, time.monotonic())
    pulse = _Pulse(
        modules=[
            _module(77, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(78, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output.usb dont-link=1"),
        ],
        sinks=[_sink(30, "NativMix_CH_0", 77), _sink(40, "alsa_output.usb")],
    )

    with patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse):
        assert manager._cleanup_all_owned_v_sink_modules("shutdown") is True

    assert set(pulse.unloaded) == {77, 78, 901, 902}
    assert manager._vsink_pending_null == {}
    assert manager._vsink_pending_loopback == {}


def test_delayed_reconcile_generation_cannot_run_after_shutdown(tmp_path):
    manager = _manager(tmp_path)
    manager._running = True
    callbacks = []

    with patch(
        "nativmix.audio.manager.QTimer.singleShot",
        side_effect=lambda _delay, callback: callbacks.append(callback),
    ):
        manager._schedule_v_sink_reconcile(0)
        manager._schedule_v_sink_reconcile(0)

    assert len(callbacks) == 1
    with (
        patch.object(manager, "_cleanup_all_owned_v_sink_modules", return_value=True),
        patch.object(manager._sink_poll_thread, "stop"),
        patch.object(manager._config, "save"),
    ):
        manager.stop()

    with patch.object(manager, "reconcile_v_sinks") as reconcile:
        callbacks[0]()

    reconcile.assert_not_called()


def test_shutdown_cleanup_retries_after_pulse_reconnect(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(
        modules=[
            _module(77, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(78, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output.usb dont-link=1"),
        ],
        sinks=[_sink(30, "NativMix_CH_0", 77), _sink(40, "alsa_output.usb")],
    )

    with patch(
        "nativmix.audio.manager.pulsectl.Pulse",
        side_effect=[pulsectl.PulseError("reconnecting"), pulse, pulse, pulse],
    ):
        assert manager._cleanup_all_owned_v_sink_modules("shutdown") is True

    assert set(pulse.unloaded) == {77, 78}


def test_shutdown_catches_loopback_recreated_with_new_module_id(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(
        modules=[
            _module(77, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(78, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output.usb dont-link=1"),
        ],
        sinks=[_sink(30, "NativMix_CH_0", 77), _sink(40, "alsa_output.usb")],
    )
    calls = 0

    def pulse_factory(_name):
        nonlocal calls
        calls += 1
        if calls == 3:
            pulse.modules.extend(
                [
                    _module(79, "module-null-sink", "sink_name=NativMix_CH_0"),
                    _module(
                        80,
                        "module-loopback",
                        "source=NativMix_CH_0.monitor sink=alsa_output.usb "
                        "sink_input_properties=application.name=NativMixLoopback_CH_0",
                    ),
                ]
            )
        return pulse

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", side_effect=pulse_factory),
        patch("nativmix.audio.manager.time.sleep"),
    ):
        assert manager._cleanup_all_owned_v_sink_modules("shutdown") is True

    assert set(pulse.unloaded) == {77, 78, 79, 80}
    assert calls == 6


def test_shutdown_retains_null_sink_when_loopback_unload_fails(tmp_path):
    class _LoopbackUnloadFailurePulse(_Pulse):
        def module_unload(self, module_index):
            self.unloaded.append(int(module_index))
            if int(module_index) == 78:
                raise pulsectl.PulseError("loopback busy")
            super().module_unload(module_index)

    manager = _manager(tmp_path)
    pulse = _LoopbackUnloadFailurePulse(
        modules=[
            _module(77, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(78, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output.usb dont-link=1"),
        ],
        sinks=[_sink(30, "NativMix_CH_0", 77), _sink(40, "alsa_output.usb")],
    )

    with patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse):
        assert manager._cleanup_all_owned_v_sink_modules("shutdown") is False

    assert {module.index for module in pulse.modules} == {77, 78}
    assert 77 not in pulse.unloaded


@pytest.mark.parametrize("new_owner", ["easyeffects", "none"])
def test_owner_switch_removes_owned_pair(tmp_path, new_owner):
    manager = _manager(tmp_path)
    manager._running = True
    manager._config.set_v_sink_enabled(0, True)
    pulse = _Pulse(
        modules=[
            _module(77, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(78, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output.usb dont-link=1"),
        ],
        sinks=[_sink(30, "NativMix_CH_0", 77), _sink(40, "alsa_output.usb")],
    )

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_refresh_owned_gain_paths"),
        patch.object(manager, "_refresh_virtual_processing_sinks"),
        patch.object(manager, "_update_gain_control_capability"),
        patch.object(manager, "_update_v_sink_capability"),
        patch.object(manager, "_update_thread_states"),
        patch.object(manager, "_apply_channel_volume"),
        patch.object(manager, "_publish_routing_owner_status"),
        patch.object(manager, "_restore_hardware_default_sink"),
    ):
        manager._activate_routing_owner(new_owner)

    assert pulse.modules == []
    assert set(pulse.unloaded) == {77, 78}


def test_flatpak_owner_none_cleans_prior_pair_without_pactl(tmp_path):
    manager = _manager(tmp_path)
    manager.effective_routing_owner = "none"
    pulse = _Pulse(
        modules=[
            _module(77, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(78, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output.usb dont-link=1"),
        ],
        sinks=[_sink(30, "NativMix_CH_0", 77), _sink(40, "alsa_output.usb")],
    )

    with (
        patch("nativmix.audio.manager.IS_FLATPAK", True),
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_restore_hardware_default_sink"),
        patch("nativmix.audio.manager.subprocess.run") as run,
    ):
        manager.reconcile_v_sinks()

    assert pulse.modules == []
    assert set(pulse.unloaded) == {77, 78}
    run.assert_not_called()


def test_ambiguous_default_and_multiple_hardware_sinks_refuse_loopback(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(
        sinks=[
            _sink(40, "alsa_output.usb-hyperx"),
            _sink(41, "alsa_output.usb-scarlett"),
            _sink(42, "easyeffects_sink"),
        ],
        default="alsa_output",
    )

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch("nativmix.audio.manager.subprocess.run") as run,
    ):
        manager.enable_v_sink(0)

    assert pulse.modules == []
    assert not any(call.args[0][1:3] == ["load-module", "module-loopback"] for call in run.call_args_list)


def test_ambiguous_target_removes_existing_owned_pair(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(
        modules=[
            _module(77, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(78, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output dont-link=1"),
        ],
        sinks=[
            _sink(30, "NativMix_CH_0", 77),
            _sink(40, "alsa_output.usb-hyperx"),
            _sink(41, "alsa_output.usb-scarlett"),
            _sink(42, "easyeffects_sink"),
        ],
        default="alsa_output",
    )

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_restore_hardware_default_sink"),
        patch("nativmix.audio.manager.subprocess.run") as run,
    ):
        manager.enable_v_sink(0)

    assert pulse.modules == []
    assert set(pulse.unloaded) == {77, 78}
    assert not any(call.args[0][1:3] == ["load-module", "module-loopback"] for call in run.call_args_list)


def test_missing_exact_monitor_unloads_new_null_sink_without_creating_loopback(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(sinks=[_sink(40, "alsa_output.usb")], sources=[])
    load_commands = []

    def run(command, **_kwargs):
        load_commands.append(command)
        if command[1:3] == ["load-module", "module-null-sink"]:
            pulse.modules.append(_module(77, command[2], " ".join(command[3:])))
            pulse.sinks.append(_sink(30, "NativMix_CH_0", 77))
            return subprocess.CompletedProcess(command, 0, stdout="77")
        raise AssertionError(command)

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_wait_for_exact_v_sink_monitor", return_value=None),
        patch.object(manager, "_update_sink_metadata"),
        patch.object(manager, "_restore_hardware_default_sink"),
        patch("nativmix.audio.manager.subprocess.run", side_effect=run),
    ):
        manager.enable_v_sink(0)

    assert [command[2] for command in load_commands] == ["module-null-sink"]
    assert pulse.modules == []
    assert pulse.unloaded == [77]


def test_monitor_with_wrong_owner_module_is_not_accepted(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(
        sources=[_source(100, "NativMix_CH_0.monitor", owner_module=999)],
    )

    assert manager._wait_for_exact_v_sink_monitor(
        pulse,
        "NativMix_CH_0.monitor",
        {77},
        timeout=0,
    ) is None


def test_physical_monitor_fallback_route_is_rejected(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(
        modules=[
            _module(77, "module-null-sink", "sink_name=NativMix_CH_0"),
            _module(78, "module-loopback", "source=NativMix_CH_0.monitor sink=alsa_output.usb dont-link=1"),
        ],
        sinks=[_sink(30, "NativMix_CH_0", 77), _sink(40, "alsa_output.usb")],
    )
    pulse.source_output_list = lambda: [
        SimpleNamespace(index=3078, source=999, owner_module=78),
    ]

    assert manager._wait_for_exact_loopback_route(
        pulse,
        module_id=78,
        source_index=1030,
        sink_index=40,
        timeout=0,
    ) is False


def test_source_removal_event_requests_topology_reconcile(tmp_path):
    manager = _manager(tmp_path)
    listener = _AudioListenerThread(manager._config)
    listener._pulse = object()
    emitted = []
    listener.topology_changed.connect(lambda: emitted.append(True))
    event = SimpleNamespace(
        facility=pulsectl.PulseEventFacilityEnum.source,
        t=pulsectl.PulseEventTypeEnum.remove,
        index=1030,
    )

    listener._on_event(event)

    assert emitted == [True]


def test_source_property_change_does_not_trigger_topology_reconcile(tmp_path):
    manager = _manager(tmp_path)
    listener = _AudioListenerThread(manager._config)
    listener._pulse = object()
    emitted = []
    listener.topology_changed.connect(lambda: emitted.append(True))
    event = SimpleNamespace(
        facility=pulsectl.PulseEventFacilityEnum.source,
        t=pulsectl.PulseEventTypeEnum.change,
        index=1030,
    )

    listener._on_event(event)

    assert emitted == []


def test_live_topology_change_reconciles_owned_pairs(tmp_path):
    manager = _manager(tmp_path)
    manager._running = True

    with patch.object(manager, "reconcile_v_sinks") as reconcile:
        manager._on_audio_topology_changed()

    reconcile.assert_called_once_with(create_missing=False)


def test_topology_reconcile_does_not_recreate_a_missing_pair(tmp_path):
    manager = _manager(tmp_path)
    manager._config.set_v_sink_enabled(0, True)
    pulse = _Pulse(sinks=[_sink(40, "alsa_output.usb")])

    with (
        patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse),
        patch.object(manager, "_recover_orphaned_v_sink_modules", return_value=True),
        patch.object(manager, "_enable_v_sink_locked") as enable,
        patch.object(manager, "_disable_v_sink_locked") as disable,
        patch.object(manager, "_restore_hardware_default_sink"),
    ):
        manager.reconcile_v_sinks(create_missing=False)

    enable.assert_not_called()
    disable.assert_not_called()


def test_easyeffects_default_resolves_sole_concrete_hardware_sink(tmp_path):
    manager = _manager(tmp_path)
    pulse = _Pulse(
        sinks=[
            _sink(40, "alsa_output.usb-scarlett"),
            _sink(42, "easyeffects_sink"),
        ],
        default="easyeffects_sink",
    )

    assert manager._get_master_hardware_sink(pulse) == "alsa_output.usb-scarlett"
