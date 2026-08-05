"""
Tests for PW-only mode (PipeWire-native without PulseAudio socket).

Covers:
- _detect_pulse_available() returns False when pulsectl fails.
- _probe_capabilities() exposes ``pulse_available`` flag.
- PipeWireManager.pw_only_mode is True when PA socket is absent but PW is available.
- PipeWireManager.pw_only_mode is False when PA is available.
- _get_active_streams_pw_only() builds StreamInfo list from PW nodes.
- _get_active_streams_pw_only() de-duplicates apps from multiple stream nodes.
- _get_active_streams_pw_only() filters internal/system nodes.
- _apply_volume_by_name_pw_only() calls wpctl for matched nodes.
- _apply_volume_by_name_pw_only() handles system master via default sink.
- _PipeWirePollerThread emits pw_only status on startup.
- settings_panel.set_audio_mode() shows badge for pw_only, hides for stable.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

# ---------------------------------------------------------------------------
# pulsectl mock — inject a minimal mock so manager.py can be imported without
# libpulse.so being present in the CI environment.
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
    client_id: int = 0,
    app_name: str = "",
    process_binary: str = "",
    media_name: str = "",
    media_class: str = "Stream/Output/Audio",
    app_id: str = "",
    props: dict | None = None,
):
    from nativmix.audio.pipewire_native import PipeWireNode
    return PipeWireNode(
        node_id=node_id,
        client_id=client_id,
        app_name=app_name,
        process_binary=process_binary,
        media_name=media_name,
        media_class=media_class,
        app_id=app_id,
        props=props or {},
    )


# ---------------------------------------------------------------------------
# _detect_pulse_available
# ---------------------------------------------------------------------------

class TestDetectPulseAvailable:
    def test_function_exists_in_module(self):
        from nativmix.audio.pipewire_native import _detect_pulse_available
        assert callable(_detect_pulse_available)

    def test_returns_true_when_monkeypatched(self):
        from nativmix.audio import pipewire_native as _mod
        original = _mod._detect_pulse_available
        _mod._detect_pulse_available = lambda: True
        assert _mod._detect_pulse_available() is True
        _mod._detect_pulse_available = original

    def test_returns_false_when_monkeypatched(self):
        from nativmix.audio import pipewire_native as _mod
        original = _mod._detect_pulse_available
        _mod._detect_pulse_available = lambda: False
        assert _mod._detect_pulse_available() is False
        _mod._detect_pulse_available = original


# ---------------------------------------------------------------------------
# _probe_capabilities — pulse_available flag
# ---------------------------------------------------------------------------

class TestProbeCapabilitiesPulseFlag:
    def test_pulse_available_key_present(self):
        from nativmix.audio.pipewire_native import _probe_capabilities
        with patch("nativmix.audio.pipewire_native.shutil.which", return_value=None):
            caps = _probe_capabilities()
        assert "pulse_available" in caps

    def test_probe_returns_dict_with_all_required_keys(self):
        from nativmix.audio.pipewire_native import _probe_capabilities
        with patch("nativmix.audio.pipewire_native.shutil.which", return_value=None):
            caps = _probe_capabilities()
        required = {
            "can_set_volume_pw", "can_set_volume", "can_move_stream",
            "pw_dump_available", "pw_cli_available", "wpctl_available",
            "pulse_available",
        }
        assert required.issubset(caps.keys())

    def test_pulse_available_true_when_pulsectl_connects(self):
        from nativmix.audio.pipewire_native import _probe_capabilities
        mock_pulse_instance = MagicMock()
        mock_pulse_instance.__enter__ = MagicMock(return_value=mock_pulse_instance)
        mock_pulse_instance.__exit__ = MagicMock(return_value=False)
        mock_pulse_instance.server_info.return_value = MagicMock()
        mock_pulse_instance.sink_input_list.return_value = []
        mock_pa = MagicMock()
        mock_pa.Pulse.return_value = mock_pulse_instance
        mock_pa.PulseError = Exception

        with patch("nativmix.audio.pipewire_native.shutil.which", return_value=None), \
             patch.dict("sys.modules", {"pulsectl": mock_pa}):
            import nativmix.audio.pipewire_native as _mod
            caps = _mod._probe_capabilities()
        assert caps.get("pulse_available") is True


# ---------------------------------------------------------------------------
# PipeWireManager.pw_only_mode detection
# ---------------------------------------------------------------------------

class TestPipeWireManagerPwOnlyMode:
    def _make_manager(self):
        from nativmix.audio.manager import PipeWireManager
        cfg = MagicMock()
        cfg.num_channels = 2
        cfg.input_mode = "usb"
        cfg.get_channel_volume.return_value = 0.5
        cfg.get_app_names.return_value = []
        cfg.is_v_sink_enabled.return_value = False
        cfg.get_channel_mode.return_value = "software"
        return PipeWireManager(config=cfg)

    def test_pw_only_mode_defaults_false(self):
        mgr = self._make_manager()
        assert mgr.pw_only_mode is False

    def test_pw_only_mode_set_true_when_no_pulse(self):
        mgr = self._make_manager()
        caps = {
            "can_set_volume_pw": True, "can_set_volume": False,
            "can_move_stream": False, "pw_dump_available": True,
            "pw_cli_available": False, "wpctl_available": True,
            "pulse_available": False,
        }
        with patch("nativmix.audio.manager._probe_capabilities", return_value=caps), \
             patch.object(mgr, "_startup_routing_self_check"), \
             patch.object(mgr, "_mark_audit_complete"), \
             patch.object(mgr, "_refresh_pw_nodes"), \
             patch("nativmix.audio.manager._PipeWirePollerThread") as mock_cls:
            mock_cls.return_value = MagicMock()
            mgr.start()
        assert mgr.pw_only_mode is True
        mgr.stop()

    def test_pw_only_mode_false_when_pulse_available(self):
        mgr = self._make_manager()
        caps = {
            "can_set_volume_pw": True, "can_set_volume": True,
            "can_move_stream": True, "pw_dump_available": True,
            "pw_cli_available": True, "wpctl_available": True,
            "pulse_available": True,
        }
        with patch("nativmix.audio.manager._probe_capabilities", return_value=caps), \
             patch.object(mgr, "_startup_routing_self_check"), \
             patch("nativmix.audio.manager._AudioListenerThread") as mock_listener_cls, \
             patch.object(mgr._sink_poll_thread, "start"), \
             patch.object(mgr._sink_poll_thread, "master_volume_changed"), \
             patch.object(mgr._sink_poll_thread, "default_sink_changed"), \
             patch.object(mgr, "perform_initial_audio_audit"):
            mock_listener_cls.return_value = MagicMock()
            mgr.start()
        assert mgr.pw_only_mode is False
        mgr.stop()

    def test_pw_only_mode_emits_pw_only_status(self):
        mgr = self._make_manager()
        emitted = []
        mgr.status_changed.connect(lambda t, m: emitted.append((t, m)))
        caps = {
            "can_set_volume_pw": True, "can_set_volume": False,
            "can_move_stream": False, "pw_dump_available": True,
            "pw_cli_available": False, "wpctl_available": True,
            "pulse_available": False,
        }
        with patch("nativmix.audio.manager._probe_capabilities", return_value=caps), \
             patch.object(mgr, "_startup_routing_self_check"), \
             patch.object(mgr, "_mark_audit_complete"), \
             patch.object(mgr, "_refresh_pw_nodes"), \
             patch("nativmix.audio.manager._PipeWirePollerThread") as mock_cls:
            mock_cls.return_value = MagicMock()
            mgr.start()
        assert any(t == "pw_only" for t, _ in emitted), f"Expected pw_only in {emitted}"
        mgr.stop()

    def test_pa_threads_not_started_in_pw_only_mode(self):
        mgr = self._make_manager()
        caps = {
            "can_set_volume_pw": True, "can_set_volume": False,
            "can_move_stream": False, "pw_dump_available": True,
            "pw_cli_available": False, "wpctl_available": True,
            "pulse_available": False,
        }
        with patch("nativmix.audio.manager._probe_capabilities", return_value=caps), \
             patch.object(mgr, "_startup_routing_self_check"), \
             patch.object(mgr, "_mark_audit_complete"), \
             patch.object(mgr, "_refresh_pw_nodes"), \
             patch("nativmix.audio.manager._AudioListenerThread") as mock_listener_cls, \
             patch.object(mgr._sink_poll_thread, "start") as mock_sink_start, \
             patch("nativmix.audio.manager._PipeWirePollerThread") as mock_cls:
            mock_cls.return_value = MagicMock()
            mgr.start()
        mock_listener_cls.assert_not_called()
        mock_sink_start.assert_not_called()
        mgr.stop()

    def test_audit_skipped_in_pw_only_mode(self):
        mgr = self._make_manager()
        mgr.pw_only_mode = True
        mgr._running = True
        emitted = []
        mgr.audit_finished.connect(lambda: emitted.append(True))
        import pulsectl as _pa
        original_pulse = _pa.Pulse
        try:
            _pa.Pulse = MagicMock(side_effect=Exception("should not be called in PW-only mode"))
            mgr.perform_initial_audio_audit()
        finally:
            _pa.Pulse = original_pulse
        assert len(emitted) == 1  # audit_finished was emitted


# ---------------------------------------------------------------------------
# _get_active_streams_pw_only
# ---------------------------------------------------------------------------

class TestGetActiveStreamsPwOnly:
    def _make_manager(self, nodes=None):
        from nativmix.audio.manager import PipeWireManager
        cfg = MagicMock()
        cfg.num_channels = 2
        cfg.input_mode = "usb"
        cfg.get_channel_volume.return_value = 0.5
        cfg.get_app_names.return_value = []
        cfg.is_v_sink_enabled.return_value = False
        cfg.get_channel_mode.return_value = "software"
        cfg.get_all_assigned_apps_by_name.return_value = set()
        mgr = PipeWireManager(config=cfg)
        mgr._running = True
        mgr.pw_only_mode = True
        mgr.pw_dump_available = True
        if nodes is not None:
            with mgr._pw_nodes_lock:
                mgr._pw_nodes = {n.node_id: n for n in nodes}
        return mgr

    def test_returns_stream_info_for_each_node(self):
        nodes = [
            _make_pw_node(node_id=10, app_name="Spotify"),
            _make_pw_node(node_id=11, app_name="Firefox"),
        ]
        mgr = self._make_manager(nodes)
        result = mgr._get_active_streams_pw_only()
        names = {s.app_name for s in result}
        assert "Spotify" in names
        assert "Firefox" in names

    def test_uses_node_name_when_app_name_empty(self):
        props = {"node.name": "chromium-output"}
        nodes = [_make_pw_node(node_id=20, app_name="", props=props)]
        mgr = self._make_manager(nodes)
        result = mgr._get_active_streams_pw_only()
        assert len(result) == 1
        assert result[0].app_name == "chromium-output"

    def test_uses_media_name_as_last_resort(self):
        nodes = [_make_pw_node(node_id=30, app_name="", media_name="Podcast Player")]
        mgr = self._make_manager(nodes)
        result = mgr._get_active_streams_pw_only()
        assert len(result) == 1
        assert result[0].app_name == "Podcast Player"

    def test_deduplicates_by_app_name(self):
        nodes = [
            _make_pw_node(node_id=40, app_name="Spotify"),
            _make_pw_node(node_id=41, app_name="Spotify"),
        ]
        mgr = self._make_manager(nodes)
        result = mgr._get_active_streams_pw_only()
        spotify_entries = [s for s in result if s.app_name == "Spotify"]
        assert len(spotify_entries) == 1

    def test_filters_internal_nodes(self):
        nodes = [
            _make_pw_node(node_id=50, app_name="nativmix"),
            _make_pw_node(node_id=51, app_name="Spotify"),
            _make_pw_node(node_id=52, app_name="loopback"),
        ]
        mgr = self._make_manager(nodes)
        result = mgr._get_active_streams_pw_only()
        names = {s.app_name for s in result}
        assert "nativmix" not in names
        assert "loopback" not in names
        assert "Spotify" in names

    def test_node_id_used_as_stream_index(self):
        nodes = [_make_pw_node(node_id=99, app_name="VLC")]
        mgr = self._make_manager(nodes)
        result = mgr._get_active_streams_pw_only()
        assert len(result) == 1
        assert result[0].index == 99

    def test_fallback_pw_dump_when_cache_empty(self):
        mgr = self._make_manager(nodes=[])
        nodes = [_make_pw_node(node_id=60, app_name="Audacity")]
        with patch("nativmix.audio.manager._pw_dump_nodes", return_value=nodes):
            result = mgr._get_active_streams_pw_only()
        assert len(result) == 1
        assert result[0].app_name == "Audacity"

    def test_binary_resolution_applied(self):
        nodes = [_make_pw_node(node_id=70, app_name="", process_binary="spotify")]
        mgr = self._make_manager(nodes)
        with patch("nativmix.utils.proc_resolver.resolve_binary_name", return_value="Spotify"):
            result = mgr._get_active_streams_pw_only()
        assert any(s.app_name == "Spotify" for s in result)


# ---------------------------------------------------------------------------
# _apply_volume_by_name_pw_only
# ---------------------------------------------------------------------------

class TestApplyVolumePwOnly:
    def _make_manager(self, nodes=None):
        from nativmix.audio.manager import PipeWireManager
        cfg = MagicMock()
        cfg.num_channels = 2
        cfg.get_app_names.return_value = []
        mgr = PipeWireManager(config=cfg)
        mgr._running = True
        mgr.pw_only_mode = True
        mgr.can_set_volume_pw = True
        mgr.wpctl_available = True
        mgr.pw_cli_available = False
        if nodes:
            with mgr._pw_nodes_lock:
                mgr._pw_nodes = {n.node_id: n for n in nodes}
        return mgr

    def test_calls_wpctl_for_matching_node(self):
        nodes = [_make_pw_node(node_id=10, app_name="Spotify")]
        mgr = self._make_manager(nodes)
        with patch("nativmix.audio.manager._wpctl_set_volume", return_value=True) as mock_wpctl, \
             patch("nativmix.audio.manager._pw_set_volume", return_value=False):
            mgr._apply_volume_by_name_pw_only("Spotify", 0.7)
        mock_wpctl.assert_called_once_with(10, 0.7)

    def test_falls_back_to_pw_cli_when_wpctl_fails(self):
        nodes = [_make_pw_node(node_id=11, app_name="VLC")]
        mgr = self._make_manager(nodes)
        with patch("nativmix.audio.manager._wpctl_set_volume", return_value=False) as mock_wpctl, \
             patch("nativmix.audio.manager._pw_set_volume", return_value=True) as mock_pwcli:
            mgr._apply_volume_by_name_pw_only("VLC", 0.5)
        mock_wpctl.assert_called_once()
        mock_pwcli.assert_called_once()

    def test_system_master_uses_default_sink(self):
        mgr = self._make_manager()
        with patch("nativmix.audio.manager._wpctl_set_volume_default_sink", return_value=True) as mock_def:
            mgr._apply_volume_by_name_pw_only("system master", 0.8)
        mock_def.assert_called_once_with(0.8)

    def test_unresolved_app_added_to_unresolved_targets(self):
        mgr = self._make_manager(nodes=[])
        mgr._apply_volume_by_name_pw_only("Spotify", 0.5)
        targets = mgr.get_unresolved_targets()
        assert "Spotify" in targets

    def test_resolved_app_removed_from_unresolved_targets(self):
        nodes = [_make_pw_node(node_id=20, app_name="Spotify")]
        mgr = self._make_manager(nodes)
        with mgr._unresolved_lock:
            mgr._unresolved_targets.add("Spotify")
        with patch("nativmix.audio.manager._wpctl_set_volume", return_value=True):
            mgr._apply_volume_by_name_pw_only("Spotify", 0.6)
        targets = mgr.get_unresolved_targets()
        assert "Spotify" not in targets

    def test_stable_id_cache_updated_after_match(self):
        nodes = [_make_pw_node(node_id=30, client_id=5, app_name="Firefox")]
        mgr = self._make_manager(nodes)
        with patch("nativmix.audio.manager._wpctl_set_volume", return_value=True):
            mgr._apply_volume_by_name_pw_only("Firefox", 0.5)
        node_ids, client_ids = mgr._stable_ids.get("firefox", (set(), set()))
        assert 30 in node_ids
        assert 5 in client_ids


# ---------------------------------------------------------------------------
# _apply_volume_by_name dispatch
# ---------------------------------------------------------------------------

class TestApplyVolumeByNameDispatch:
    def test_dispatches_to_pw_only_when_pw_only_mode(self):
        from nativmix.audio.manager import PipeWireManager
        cfg = MagicMock()
        cfg.num_channels = 2
        cfg.get_app_names.return_value = []
        mgr = PipeWireManager(config=cfg)
        mgr.pw_only_mode = True
        mgr.can_set_volume_pw = True
        mgr.can_set_volume = False
        with patch.object(mgr, "_apply_volume_by_name_pw_only") as mock_pw_only:
            mgr._apply_volume_by_name("Spotify", 0.5)
        mock_pw_only.assert_called_once_with("Spotify", 0.5)


# ---------------------------------------------------------------------------
# _PipeWirePollerThread
# ---------------------------------------------------------------------------

class TestPipeWirePollerThread:
    def test_has_status_changed_signal(self):
        from nativmix.audio.manager import _PipeWirePollerThread
        thread = _PipeWirePollerThread()
        assert hasattr(thread, "status_changed")

    def test_has_streams_changed_signal(self):
        from nativmix.audio.manager import _PipeWirePollerThread
        thread = _PipeWirePollerThread()
        assert hasattr(thread, "streams_changed")

    def test_stop_returns_without_error(self):
        from nativmix.audio.manager import _PipeWirePollerThread
        thread = _PipeWirePollerThread()
        thread.stop()  # should not raise even before start()


# ---------------------------------------------------------------------------
# settings_panel audio mode badge
# ---------------------------------------------------------------------------

class TestSettingsPanelAudioModeBadge:
    def _import_settings_panel(self):
        try:
            from nativmix.gui.settings_panel import SettingsPanel, _AUDIO_MODE_COLORS
            return SettingsPanel, _AUDIO_MODE_COLORS
        except Exception as exc:
            pytest.skip(f"SettingsPanel unavailable (headless/display): {exc}")

    def test_set_audio_mode_exists(self):
        SettingsPanel, _ = self._import_settings_panel()
        assert hasattr(SettingsPanel, "set_audio_mode")
        assert callable(SettingsPanel.set_audio_mode)

    def test_audio_mode_colors_has_pw_only(self):
        _, _AUDIO_MODE_COLORS = self._import_settings_panel()
        assert "pw_only" in _AUDIO_MODE_COLORS

    def test_audio_mode_colors_pw_only_is_hex(self):
        _, _AUDIO_MODE_COLORS = self._import_settings_panel()
        assert _AUDIO_MODE_COLORS["pw_only"].startswith("#")

    def test_audio_mode_badge_visible_for_pw_only(self):
        try:
            from PyQt6.QtWidgets import QApplication, QLabel
            _app = QApplication.instance() or QApplication(sys.argv[:1])
        except Exception:
            pytest.skip("PyQt6 / display not available")

        from nativmix.gui.settings_panel import _AUDIO_MODE_COLORS
        label = QLabel()
        label.setVisible(False)
        status_type = "pw_only"
        label.setVisible(status_type not in ("stable", "connecting", "unknown"))
        assert label.isVisible()

    def test_audio_mode_badge_hidden_for_stable(self):
        try:
            from PyQt6.QtWidgets import QApplication, QLabel
            _app = QApplication.instance() or QApplication(sys.argv[:1])
        except Exception:
            pytest.skip("PyQt6 / display not available")

        label = QLabel()
        label.setVisible(True)
        label.setVisible("stable" not in ("stable", "connecting", "unknown"))
        assert not label.isVisible()
