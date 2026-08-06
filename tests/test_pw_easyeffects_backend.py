"""
Tests for the EasyEffects virtual-sink backend.

Covers:
- discover_virtual_processing_sinks() finds easyeffects_sink/source and
  NativMix equivalents, preferring Easy Effects.
- _pw_move_node_to_target() uses the PipeWire-native pw-metadata path.
- _resolve_routing_owner() prefers Easy Effects when its nodes exist.
- Bound app streams are routed to the virtual sink and gain is applied on the
  backend node (not on the app stream nodes).
- Explicit "No virtual processing sink available" notice when none exists.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))


def _make_mock_pulsectl():
    mock_pa = MagicMock()
    mock_pulse_instance = MagicMock()
    mock_pulse_instance.__enter__ = MagicMock(return_value=mock_pulse_instance)
    mock_pulse_instance.__exit__ = MagicMock(return_value=False)
    mock_pulse_instance.sink_input_list.return_value = []
    mock_pa.Pulse.return_value = mock_pulse_instance
    mock_pa.PulseError = Exception
    mock_pa.PulseIndexError = Exception
    mock_pa.PulseLoopStop = StopIteration
    mock_pa.PulseEventFacilityEnum = MagicMock()
    mock_pa.PulseEventTypeEnum = MagicMock()
    return mock_pa


if "pulsectl" not in sys.modules:
    sys.modules["pulsectl"] = _make_mock_pulsectl()


def _pw_dump_payload() -> str:
    def node(node_id, name, media_class, perms=None):
        return {
            "id": node_id,
            "type": "PipeWire:Interface:Node",
            "permissions": perms if perms is not None else ["r", "w", "x"],
            "info": {"props": {"node.name": name, "media.class": media_class}},
        }

    return json.dumps([
        node(30, "alsa_output.pci-0000_00_1f.3.analog-stereo", "Audio/Sink"),
        node(40, "easyeffects_sink", "Audio/Sink"),
        node(41, "easyeffects_source", "Audio/Source"),
        node(50, "nativmix_vsink_ch0", "Audio/Sink"),
        node(60, "spotify", "Stream/Output/Audio", ["r", "x"]),
    ])


def _make_pw_node(node_id, app_name="Spotify", node_name="spotify", props=None, perms=None):
    from nativmix.audio.pipewire_native import PipeWireNode
    return PipeWireNode(
        node_id=node_id,
        client_id=0,
        app_name=app_name,
        process_binary="",
        media_name="",
        media_class="Stream/Output/Audio",
        app_id="",
        node_name=node_name,
        props=props or {},
        permissions=perms if perms is not None else ["r", "x"],
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscoverVirtualProcessingSinks:
    def _discover(self):
        from nativmix.audio import pipewire_native as pwn
        result = MagicMock(returncode=0, stdout=_pw_dump_payload())
        with (
            patch.object(pwn.shutil, "which", return_value="/usr/bin/pw-dump"),
            patch.object(pwn.subprocess, "run", return_value=result),
        ):
            return pwn.discover_virtual_processing_sinks()

    def test_finds_easyeffects_and_nativmix_nodes(self):
        sinks = self._discover()
        names = [s.node_name for s in sinks]
        assert "easyeffects_sink" in names
        assert "easyeffects_source" in names
        assert "nativmix_vsink_ch0" in names

    def test_ignores_plain_hardware_sinks(self):
        sinks = self._discover()
        assert all("alsa_output" not in s.node_name for s in sinks)

    def test_easyeffects_is_preferred_first(self):
        sinks = self._discover()
        assert sinks[0].node_name == "easyeffects_sink"
        assert sinks[0].backend == "easyeffects"
        assert sinks[0].direction == "sink"

    def test_nativmix_nodes_classified_as_nativmix_backend(self):
        sinks = self._discover()
        nm = [s for s in sinks if s.node_name == "nativmix_vsink_ch0"][0]
        assert nm.backend == "nativmix"

    def test_returns_empty_without_pw_dump(self):
        from nativmix.audio import pipewire_native as pwn
        with patch.object(pwn.shutil, "which", return_value=None):
            assert pwn.discover_virtual_processing_sinks() == []

    def test_stream_node_enumeration_still_defaults_to_stream_output(self):
        from nativmix.audio import pipewire_native as pwn
        result = MagicMock(returncode=0, stdout=_pw_dump_payload())
        with (
            patch.object(pwn.shutil, "which", return_value="/usr/bin/pw-dump"),
            patch.object(pwn.subprocess, "run", return_value=result),
        ):
            nodes = pwn._pw_dump_nodes()
        assert [n.node_name for n in nodes] == ["spotify"]


# ---------------------------------------------------------------------------
# PipeWire-native move
# ---------------------------------------------------------------------------

class TestPwMoveNodeToTarget:
    def test_uses_pw_metadata(self):
        from nativmix.audio import pipewire_native as pwn
        with (
            patch.object(pwn.shutil, "which", return_value="/usr/bin/pw-metadata"),
            patch.object(pwn.subprocess, "run", return_value=MagicMock(returncode=0)) as run_mock,
        ):
            assert pwn._pw_move_node_to_target(60, "easyeffects_sink") is True
        cmd = run_mock.call_args.args[0]
        assert cmd[:4] == ["pw-metadata", "60", "target.object", "easyeffects_sink"]

    def test_missing_tool_returns_false(self):
        from nativmix.audio import pipewire_native as pwn
        with patch.object(pwn.shutil, "which", return_value=None):
            assert pwn._pw_move_node_to_target(60, "easyeffects_sink") is False

    def test_missing_arguments_return_false(self):
        from nativmix.audio import pipewire_native as pwn
        assert pwn._pw_move_node_to_target(0, "easyeffects_sink") is False
        assert pwn._pw_move_node_to_target(60, "") is False


# ---------------------------------------------------------------------------
# Manager integration
# ---------------------------------------------------------------------------

def _make_manager(tmp_path, routing_owner="easyeffects"):
    from nativmix.audio.manager import PipeWireManager
    from nativmix.utils.config_manager import ConfigManager

    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    cfg.routing_owner = routing_owner
    cfg.set_app_names(0, ["Spotify"])

    mgr = PipeWireManager.__new__(PipeWireManager)
    mgr._config = cfg
    mgr.routing_owner = routing_owner
    mgr.pw_only_mode = True
    mgr._pw_nodes = {}
    mgr._pw_nodes_lock = threading.Lock()
    mgr._stable_ids = {}
    mgr._pw_identity = {}
    mgr._owned_gain_paths = {}
    mgr._owned_route_paths = {}
    mgr._pw_owned_path_status = "inactive"
    mgr._pw_owned_path_reason = ""
    mgr._virtual_sinks = []
    mgr._virtual_sink_status = "inactive"
    mgr._backend_routed_nodes = {}
    mgr._unresolved_targets = set()
    mgr._unresolved_lock = threading.Lock()
    mgr.unresolved_targets_changed = MagicMock()
    mgr.status_changed = MagicMock()
    return mgr


def _vsink(node_id=40, node_name="easyeffects_sink", backend="easyeffects", direction="sink"):
    from nativmix.audio.pipewire_native import VirtualProcessingSink
    return VirtualProcessingSink(
        node_id=node_id,
        node_name=node_name,
        media_class="Audio/Sink" if direction == "sink" else "Audio/Source",
        backend=backend,
        direction=direction,
    )


class TestManagerBackendSelection:
    def test_refresh_emits_ready_status(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("nativmix.audio.manager.discover_virtual_processing_sinks",
                   return_value=[_vsink()]):
            sinks = mgr._refresh_virtual_processing_sinks()
        assert [s.node_name for s in sinks] == ["easyeffects_sink"]
        mgr.status_changed.emit.assert_called_with(
            "pw_only", "Virtual processing sink: easyeffects_sink (backend=easyeffects)"
        )

    def test_refresh_emits_explicit_absence_notice(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("nativmix.audio.manager.discover_virtual_processing_sinks", return_value=[]):
            assert mgr._refresh_virtual_processing_sinks() == []
        mgr.status_changed.emit.assert_called_with(
            "degraded", "No virtual processing sink available"
        )

    def test_refresh_silent_when_emit_status_false(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("nativmix.audio.manager.discover_virtual_processing_sinks", return_value=[]):
            mgr._refresh_virtual_processing_sinks(emit_status=False)
        mgr.status_changed.emit.assert_not_called()

    def test_select_prefers_playback_endpoint(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr._virtual_sinks = [
            _vsink(node_id=41, node_name="easyeffects_source", direction="source"),
            _vsink(node_id=40, node_name="easyeffects_sink"),
        ]
        assert mgr._select_virtual_processing_sink().node_name == "easyeffects_sink"

    def test_select_returns_none_when_absent(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("nativmix.audio.manager.discover_virtual_processing_sinks", return_value=[]):
            assert mgr._select_virtual_processing_sink() is None


class TestResolveRoutingOwnerPrefersEasyEffects:
    def test_auto_prefers_easyeffects_when_nodes_exist(self, tmp_path):
        mgr = _make_manager(tmp_path, routing_owner="auto")
        with (
            patch("nativmix.audio.manager.IS_FLATPAK", False),
            patch("nativmix.audio.manager.detect_easyeffects", return_value=(False, "")),
            patch("nativmix.audio.manager.discover_virtual_processing_sinks",
                  return_value=[_vsink()]),
            patch.object(mgr._config, "save"),
        ):
            assert mgr._resolve_routing_owner() == "easyeffects"

    def test_auto_falls_back_to_nativmix_without_ee_nodes(self, tmp_path):
        mgr = _make_manager(tmp_path, routing_owner="auto")
        with (
            patch("nativmix.audio.manager.IS_FLATPAK", False),
            patch("nativmix.audio.manager.detect_easyeffects", return_value=(False, "")),
            patch("nativmix.audio.manager.discover_virtual_processing_sinks",
                  return_value=[_vsink(node_id=50, node_name="nativmix_vsink_ch0", backend="nativmix")]),
            patch.object(mgr._config, "save"),
        ):
            assert mgr._resolve_routing_owner() == "nativmix"


class TestBackendRoutingAndGain:
    def test_bound_app_stream_is_routed_to_virtual_sink(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr._pw_nodes = {60: _make_pw_node(60)}
        with patch("nativmix.audio.manager._pw_move_node_to_target", return_value=True) as move:
            routed = mgr._route_app_to_virtual_sink("Spotify", _vsink())
        assert routed == 1
        move.assert_called_once_with(60, "easyeffects_sink")

    def test_already_routed_node_is_not_moved_again(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr._pw_nodes = {60: _make_pw_node(60, props={"target.object": "easyeffects_sink"})}
        with patch("nativmix.audio.manager._pw_move_node_to_target", return_value=True) as move:
            assert mgr._route_app_to_virtual_sink("Spotify", _vsink()) == 1
            assert mgr._route_app_to_virtual_sink("Spotify", _vsink()) == 1
        move.assert_not_called()

    def test_gain_applied_on_backend_node_not_app_stream(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr._pw_nodes = {60: _make_pw_node(60)}
        with (
            patch("nativmix.audio.manager.discover_virtual_processing_sinks",
                  return_value=[_vsink()]),
            patch("nativmix.audio.manager._pw_move_node_to_target", return_value=True),
            patch("nativmix.audio.manager._wpctl_set_volume_traced",
                  return_value=(True, ["wpctl"], 0, "", "")) as wpctl,
            patch("nativmix.audio.manager._pw_set_volume_traced") as pw,
        ):
            mgr._apply_volume_by_name_pw_only("Spotify", 0.4)
        wpctl.assert_called_once_with(40, 0.4)
        pw.assert_not_called()
        assert "Spotify" not in mgr._unresolved_targets

    def test_gain_falls_back_to_pw_cli(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr._virtual_sinks = [_vsink()]
        with (
            patch("nativmix.audio.manager._pw_move_node_to_target", return_value=True),
            patch("nativmix.audio.manager._wpctl_set_volume_traced",
                  return_value=(False, [], None, "", "")),
            patch("nativmix.audio.manager._pw_set_volume_traced",
                  return_value=(True, ["pw-cli"], 0, "", "")) as pw,
        ):
            assert mgr._apply_volume_via_backend_sink("Spotify", 0.4) is True
        pw.assert_called_once_with(40, 0.4)

    def test_missing_backend_marks_target_unresolved(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr._pw_nodes = {60: _make_pw_node(60)}
        with (
            patch("nativmix.audio.manager.discover_virtual_processing_sinks", return_value=[]),
            patch("nativmix.audio.manager._wpctl_set_volume_traced") as wpctl,
            patch("nativmix.audio.manager._pw_set_volume_traced") as pw,
        ):
            mgr._apply_volume_by_name_pw_only("Spotify", 0.4)
        wpctl.assert_not_called()
        pw.assert_not_called()
        assert "Spotify" in mgr._unresolved_targets
        mgr.status_changed.emit.assert_called_with(
            "degraded", "No virtual processing sink available"
        )

    def test_system_master_still_uses_default_sink(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with (
            patch("nativmix.audio.manager._wpctl_set_volume_default_sink",
                  return_value=True) as default_sink,
            patch("nativmix.audio.manager._pw_move_node_to_target") as move,
        ):
            mgr._apply_volume_by_name_pw_only("System Master", 0.4)
        default_sink.assert_called_once_with(0.4)
        move.assert_not_called()
