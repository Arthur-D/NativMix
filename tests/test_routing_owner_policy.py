"""
Tests for routing owner policy (PR #41).

Covers:
- ConfigManager.routing_owner getter/setter.
- detect_easyeffects() returns (detected, evidence) correctly.
- PipeWireManager._resolve_routing_owner() — auto + explicit + EE detection.
- Permission-aware write guard blocks writes on nodes with perms=['r','x'].
- on_mapping_changed() does not invoke reroute paths when owner != nativmix.
- enable_v_sink() is blocked when owner != nativmix.
- _apply_auto_reconnect() V-Sink routing is blocked when owner != nativmix.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import json

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))


# ---------------------------------------------------------------------------
# pulsectl stub — avoids libpulse.so requirement in CI
# ---------------------------------------------------------------------------

def _make_mock_pulsectl():
    mock_pa = MagicMock()
    mock_pulse_instance = MagicMock()
    mock_pulse_instance.__enter__ = MagicMock(return_value=mock_pulse_instance)
    mock_pulse_instance.__exit__ = MagicMock(return_value=False)
    mock_pulse_instance.sink_input_list.return_value = []
    mock_pulse_instance.sink_list.return_value = []
    mock_pulse_instance.source_list.return_value = []
    mock_pulse_instance.server_info.return_value = MagicMock(default_sink_name="alsa_output.default")
    mock_pa.Pulse.return_value = mock_pulse_instance
    mock_pa.PulseError = Exception
    mock_pa.PulseIndexError = Exception
    mock_pa.PulseLoopStop = StopIteration
    mock_pa.PulseEventFacilityEnum = MagicMock()
    mock_pa.PulseEventTypeEnum = MagicMock()
    return mock_pa


if "pulsectl" not in sys.modules:
    sys.modules["pulsectl"] = _make_mock_pulsectl()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pw_node(
    node_id: int = 1,
    app_name: str = "TestApp",
    node_name: str = "testapp",
    media_class: str = "Stream/Output/Audio",
    permissions: list[str] | None = None,
    props: dict | None = None,
):
    from nativmix.audio.pipewire_native import PipeWireNode
    return PipeWireNode(
        node_id=node_id,
        client_id=0,
        app_name=app_name,
        process_binary="",
        media_name="",
        media_class=media_class,
        app_id="",
        node_name=node_name,
        props=props or {},
        permissions=permissions if permissions is not None else ["r", "w", "x"],
    )


def _make_config(tmp_path, routing_owner="nativmix"):
    from nativmix.utils.config_manager import ConfigManager
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    cfg.routing_owner = routing_owner
    return cfg


# ---------------------------------------------------------------------------
# ConfigManager.routing_owner
# ---------------------------------------------------------------------------

class TestConfigRoutingOwner:
    def test_default_is_auto(self, tmp_path):
        from nativmix.utils.config_manager import ConfigManager
        cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
        assert cfg.routing_owner == "auto"

    def test_get_set_nativmix(self, tmp_path):
        cfg = _make_config(tmp_path, "nativmix")
        assert cfg.routing_owner == "nativmix"

    def test_get_set_easyeffects(self, tmp_path):
        cfg = _make_config(tmp_path, "easyeffects")
        assert cfg.routing_owner == "easyeffects"

    def test_get_set_none(self, tmp_path):
        cfg = _make_config(tmp_path, "none")
        assert cfg.routing_owner == "none"

    def test_invalid_value_raises(self, tmp_path):
        cfg = _make_config(tmp_path)
        with pytest.raises(ValueError):
            cfg.routing_owner = "invalid"

    def test_unknown_persisted_value_returns_auto(self, tmp_path):
        """Corrupt config value falls back to 'auto'."""
        from nativmix.utils.config_manager import ConfigManager
        cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
        cfg._data.setdefault("settings", {})["routing_owner"] = "garbage"
        assert cfg.routing_owner == "auto"

    def test_persisted_across_save_load(self, tmp_path):
        from nativmix.utils.config_manager import ConfigManager
        cfg = _make_config(tmp_path, "easyeffects")
        cfg.save()
        cfg2 = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
        assert cfg2.routing_owner == "easyeffects"


# ---------------------------------------------------------------------------
# detect_easyeffects
# ---------------------------------------------------------------------------

class TestDetectEasyEffects:
    def test_no_ee_returns_false(self):
        from nativmix.audio.pipewire_native import detect_easyeffects
        with (
            patch("nativmix.audio.pipewire_native._pw_dump_nodes", return_value=[]),
            patch("glob.iglob", return_value=iter([])),
        ):
            detected, evidence = detect_easyeffects()
        assert detected is False

    def test_ee_process_detected_via_proc(self, tmp_path):
        """Simulate /proc/<pid>/cmdline containing easyeffects."""
        import glob as _glob
        from nativmix.audio.pipewire_native import detect_easyeffects

        fake_cmdline = tmp_path / "cmdline"
        fake_cmdline.write_bytes(b"easyeffects\x00--gapplication-service\x00")

        with (
            patch("nativmix.audio.pipewire_native._pw_dump_nodes", return_value=[]),
            patch("glob.iglob", return_value=iter([str(fake_cmdline)])),
        ):
            detected, evidence = detect_easyeffects()
        assert detected is True
        assert "easyeffects" in evidence.lower()

    def test_ee_pw_node_detected(self):
        from nativmix.audio.pipewire_native import detect_easyeffects
        ee_node = _make_pw_node(node_id=100, app_name="easyeffects", node_name="easyeffects_sink")
        with (
            patch("nativmix.audio.pipewire_native._pw_dump_nodes", return_value=[ee_node]),
            patch("glob.iglob", return_value=iter([])),
        ):
            detected, evidence = detect_easyeffects()
        assert detected is True


# ---------------------------------------------------------------------------
# PipeWireManager._resolve_routing_owner
# ---------------------------------------------------------------------------

class TestResolveRoutingOwner:
    def _make_manager(self, tmp_path, routing_owner="auto"):
        from nativmix.audio.manager import PipeWireManager
        cfg = _make_config(tmp_path, routing_owner)
        mgr = PipeWireManager.__new__(PipeWireManager)
        mgr._config = cfg
        mgr.routing_owner = "nativmix"
        return mgr

    def test_explicit_nativmix_returned_as_is(self, tmp_path):
        mgr = self._make_manager(tmp_path, "nativmix")
        result = mgr._resolve_routing_owner()
        assert result == "nativmix"

    def test_explicit_easyeffects_returned_as_is(self, tmp_path):
        mgr = self._make_manager(tmp_path, "easyeffects")
        result = mgr._resolve_routing_owner()
        assert result == "easyeffects"

    def test_auto_ee_detected_flatpak_defaults_easyeffects(self, tmp_path):
        mgr = self._make_manager(tmp_path, "auto")
        with (
            patch("nativmix.audio.manager.IS_FLATPAK", True),
            patch("nativmix.audio.manager.detect_easyeffects", return_value=(True, "process cmdline contains easyeffects")),
        ):
            result = mgr._resolve_routing_owner()
        assert result == "easyeffects"

    def test_auto_no_ee_flatpak_defaults_nativmix(self, tmp_path):
        mgr = self._make_manager(tmp_path, "auto")
        with (
            patch("nativmix.audio.manager.IS_FLATPAK", True),
            patch("nativmix.audio.manager.detect_easyeffects", return_value=(False, "no evidence found")),
        ):
            result = mgr._resolve_routing_owner()
        assert result == "nativmix"

    def test_auto_non_flatpak_defaults_nativmix_even_with_ee(self, tmp_path):
        mgr = self._make_manager(tmp_path, "auto")
        with (
            patch("nativmix.audio.manager.IS_FLATPAK", False),
            patch("nativmix.audio.manager.detect_easyeffects", return_value=(True, "process")),
        ):
            result = mgr._resolve_routing_owner()
        assert result == "nativmix"

    def test_auto_persists_resolved_value(self, tmp_path):
        mgr = self._make_manager(tmp_path, "auto")
        with (
            patch("nativmix.audio.manager.IS_FLATPAK", False),
            patch("nativmix.audio.manager.detect_easyeffects", return_value=(False, "")),
            patch.object(mgr._config, "save"),
        ):
            result = mgr._resolve_routing_owner()
        assert mgr._config.routing_owner == result


# ---------------------------------------------------------------------------
# Permission-aware write guard
# ---------------------------------------------------------------------------

class TestPermissionAwareWriteGuard:
    def _make_manager_with_node(self, tmp_path, permissions, routing_owner="nativmix"):
        """Return a PipeWireManager with a single PW node having given permissions."""
        from nativmix.audio.manager import PipeWireManager
        cfg = _make_config(tmp_path, routing_owner)
        mgr = PipeWireManager.__new__(PipeWireManager)
        mgr._config = cfg
        mgr.routing_owner = routing_owner
        mgr._pw_nodes = {}
        mgr._pw_nodes_lock = threading.Lock()
        mgr._stable_ids = {}
        mgr._pw_identity = {}
        mgr._unresolved_targets = set()
        mgr._unresolved_lock = threading.Lock()
        mgr.unresolved_targets_changed = MagicMock()
        node = _make_pw_node(node_id=248, app_name="Spotify", permissions=permissions)
        mgr._pw_nodes[248] = node
        return mgr

    def test_writable_node_attempts_write(self, tmp_path):
        """Nodes with 'w' should attempt the actual write."""
        mgr = self._make_manager_with_node(tmp_path, permissions=["r", "w", "x"])
        with (
            patch("nativmix.audio.manager._wpctl_set_volume_traced",
                  return_value=(True, "wpctl set-volume 248 0.5", 0, "", "")) as mock_wpctl,
            patch("nativmix.audio.manager._wpctl_set_volume_default_sink", return_value=True),
        ):
            mgr._apply_volume_by_name_pw_only("Spotify", 0.5)
        mock_wpctl.assert_called_once_with(248, 0.5)

    def test_non_writable_node_skips_write(self, tmp_path):
        """Nodes with ['r','x'] (no 'w') must not be written to."""
        mgr = self._make_manager_with_node(tmp_path, permissions=["r", "x"])
        with (
            patch("nativmix.audio.manager._wpctl_set_volume_traced") as mock_wpctl,
            patch("nativmix.audio.manager._pw_set_volume_traced") as mock_pw,
        ):
            mgr._apply_volume_by_name_pw_only("Spotify", 0.5)
        mock_wpctl.assert_not_called()
        mock_pw.assert_not_called()

    def test_unknown_permissions_attempts_write(self, tmp_path):
        """Nodes with empty permissions list (unknown) should try writing (optimistic)."""
        mgr = self._make_manager_with_node(tmp_path, permissions=[])
        with (
            patch("nativmix.audio.manager._wpctl_set_volume_traced",
                  return_value=(True, "wpctl set-volume 248 0.5", 0, "", "")) as mock_wpctl,
            patch("nativmix.audio.manager._wpctl_set_volume_default_sink", return_value=True),
        ):
            mgr._apply_volume_by_name_pw_only("Spotify", 0.5)
        mock_wpctl.assert_called_once_with(248, 0.5)

    def test_write_guard_logs_info_on_block(self, tmp_path, caplog):
        """A blocked write must emit an INFO log with node_id and perms."""
        import logging
        mgr = self._make_manager_with_node(tmp_path, permissions=["r", "x"])
        with caplog.at_level(logging.INFO):
            with (
                patch("nativmix.audio.manager._wpctl_set_volume_traced") as mock_wpctl,
                patch("nativmix.audio.manager._pw_set_volume_traced") as mock_pw,
            ):
                mgr._apply_volume_by_name_pw_only("Spotify", 0.5)
        assert any("target_not_writable" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# on_mapping_changed — reroute blocked when owner != nativmix
# ---------------------------------------------------------------------------

class TestOnMappingChangedOwnerGuard:
    def _make_manager(self, tmp_path, routing_owner, pw_only=False):
        from nativmix.audio.manager import PipeWireManager
        cfg = _make_config(tmp_path, routing_owner)
        # Give config channel 0 with app 'Spotify'
        cfg.set_app_names(0, ["Spotify"])
        mgr = PipeWireManager.__new__(PipeWireManager)
        mgr._config = cfg
        mgr.routing_owner = routing_owner
        mgr.pw_only_mode = pw_only
        mgr._state_lock = threading.RLock()
        mgr._poti_volumes = {0: 0.7}
        mgr._prev_app_names = {0: ["Spotify"]}
        mgr._pw_nodes = {}
        mgr._pw_nodes_lock = threading.Lock()
        mgr._stable_ids = {}
        mgr._pw_identity = {}
        mgr._unresolved_targets = set()
        mgr._unresolved_lock = threading.Lock()
        mgr._last_applied_volumes = {}
        mgr._channel_muted = {}
        mgr._muted_at_volume = {}
        mgr._active_streams = {}
        mgr.unresolved_targets_changed = MagicMock()
        return mgr

    def test_nativmix_owner_proceeds_to_routing(self, tmp_path):
        """In nativmix mode, on_mapping_changed should proceed (not early-return on owner guard)."""
        mgr = self._make_manager(tmp_path, "nativmix", pw_only=False)
        with (
            patch.object(mgr, "_update_thread_states") as mock_update,
            patch("pulsectl.Pulse", side_effect=Exception("no pulse")),
        ):
            # Should not raise; routing is attempted (may fail without real PA)
            try:
                mgr.on_mapping_changed(0, ["Spotify"])
            except Exception:
                pass
        mock_update.assert_called()

    def test_easyeffects_owner_blocks_routing(self, tmp_path):
        """In easyeffects mode, on_mapping_changed must NOT proceed to move-sink/V-Sink steps."""
        mgr = self._make_manager(tmp_path, "easyeffects", pw_only=False)
        with (
            patch.object(mgr, "_update_thread_states") as mock_update,
            patch("pulsectl.Pulse") as mock_pulse_cls,
        ):
            mgr.on_mapping_changed(0, ["Spotify"])
        # Pulse must NOT be opened for routing
        mock_pulse_cls.assert_not_called()

    def test_none_owner_blocks_routing(self, tmp_path):
        """In none mode, on_mapping_changed must NOT proceed to routing steps."""
        mgr = self._make_manager(tmp_path, "none", pw_only=False)
        with (
            patch.object(mgr, "_update_thread_states"),
            patch("pulsectl.Pulse") as mock_pulse_cls,
        ):
            mgr.on_mapping_changed(0, ["Spotify"])
        mock_pulse_cls.assert_not_called()


# ---------------------------------------------------------------------------
# enable_v_sink — blocked when owner != nativmix
# ---------------------------------------------------------------------------

class TestEnableVSinkOwnerGuard:
    def _make_manager(self, tmp_path, routing_owner):
        from nativmix.audio.manager import PipeWireManager
        cfg = _make_config(tmp_path, routing_owner)
        mgr = PipeWireManager.__new__(PipeWireManager)
        mgr._config = cfg
        mgr.routing_owner = routing_owner
        mgr.pw_only_mode = False
        mgr._state_lock = threading.RLock()
        mgr._vsink_creating = set()
        return mgr

    def test_nativmix_proceeds(self, tmp_path):
        mgr = self._make_manager(tmp_path, "nativmix")
        with (
            patch.object(mgr, "_update_thread_states"),
            patch("pulsectl.Pulse", side_effect=Exception("no pulse")),
        ):
            try:
                mgr.enable_v_sink(0)
            except Exception:
                pass
        # _vsink_creating should have been populated (past the guard)
        assert 0 in mgr._vsink_creating

    def test_easyeffects_blocked(self, tmp_path):
        mgr = self._make_manager(tmp_path, "easyeffects")
        with patch("pulsectl.Pulse") as mock_pulse_cls:
            mgr.enable_v_sink(0)
        mock_pulse_cls.assert_not_called()
        assert 0 not in mgr._vsink_creating

    def test_none_blocked(self, tmp_path):
        mgr = self._make_manager(tmp_path, "none")
        with patch("pulsectl.Pulse") as mock_pulse_cls:
            mgr.enable_v_sink(0)
        mock_pulse_cls.assert_not_called()
        assert 0 not in mgr._vsink_creating


# ---------------------------------------------------------------------------
# _apply_auto_reconnect V-Sink routing — blocked when owner != nativmix
# ---------------------------------------------------------------------------

class TestApplyAutoReconnectOwnerGuard:
    def _make_thread(self, routing_owner, ch=0, app="Spotify"):
        from nativmix.audio.manager import _AudioListenerThread
        cfg = MagicMock()
        cfg.find_channel_for_app.return_value = ch
        cfg.get_channel_volume.return_value = 0.8
        cfg.is_v_sink_enabled.return_value = True
        thread = _AudioListenerThread.__new__(_AudioListenerThread)
        thread._config = cfg
        thread.routing_owner = routing_owner
        thread.channel_states = {}
        thread._states_lock = threading.Lock()
        thread._recently_routed = {}
        return thread

    def _make_stream_info(self, app="Spotify"):
        from nativmix.audio.base import StreamInfo
        info = MagicMock(spec=StreamInfo)
        info.app_name = app
        info.index = 42
        info.props = {}
        return info

    def test_nativmix_proceeds_to_vsink_lookup(self, tmp_path):
        """In nativmix mode the V-Sink routing path is entered (pulse.get_sink_by_name called)."""
        thread = self._make_thread("nativmix")
        pulse = MagicMock()
        pulse.get_sink_by_name.return_value = MagicMock(name="NativMix_CH_0")
        info = self._make_stream_info()
        thread._apply_auto_reconnect(pulse, info)
        pulse.get_sink_by_name.assert_called()

    def test_easyeffects_blocks_vsink_routing(self, tmp_path):
        thread = self._make_thread("easyeffects")
        pulse = MagicMock()
        info = self._make_stream_info()
        thread._apply_auto_reconnect(pulse, info)
        pulse.get_sink_by_name.assert_not_called()

    def test_none_blocks_vsink_routing(self, tmp_path):
        thread = self._make_thread("none")
        pulse = MagicMock()
        info = self._make_stream_info()
        thread._apply_auto_reconnect(pulse, info)
        pulse.get_sink_by_name.assert_not_called()


# ---------------------------------------------------------------------------
# PipeWireNode.permissions field
# ---------------------------------------------------------------------------

class TestPipeWireNodePermissions:
    def test_permissions_stored(self):
        from nativmix.audio.pipewire_native import PipeWireNode
        node = PipeWireNode(
            node_id=1, client_id=0, app_name="a", process_binary="",
            media_name="", media_class="Stream/Output/Audio", app_id="",
            node_name="", permissions=["r", "x"],
        )
        assert node.permissions == ["r", "x"]

    def test_permissions_defaults_to_empty(self):
        from nativmix.audio.pipewire_native import PipeWireNode
        node = PipeWireNode(
            node_id=1, client_id=0, app_name="a", process_binary="",
            media_name="", media_class="Stream/Output/Audio", app_id="",
            node_name="",
        )
        assert node.permissions == []

    def test_pw_dump_nodes_parses_permissions(self):
        """_pw_dump_nodes must carry permissions from raw pw-dump output."""
        from nativmix.audio.pipewire_native import _pw_dump_nodes
        fake_dump = json.dumps([{
            "id": 10,
            "type": "PipeWire:Interface:Node",
            "permissions": ["r", "x"],
            "info": {
                "props": {
                    "media.class": "Stream/Output/Audio",
                    "application.name": "Spotify",
                    "node.name": "spotify",
                }
            }
        }])
        import subprocess
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = fake_dump
        with (
            patch("nativmix.audio.pipewire_native.shutil.which", return_value="/usr/bin/pw-dump"),
            patch("nativmix.audio.pipewire_native.subprocess.run", return_value=mock_result),
        ):
            nodes = _pw_dump_nodes()
        assert len(nodes) == 1
        assert nodes[0].permissions == ["r", "x"]
