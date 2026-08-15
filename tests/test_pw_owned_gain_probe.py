"""
Tests for the owned gain node probe and fallback logic.

Covers:
- PipeWireManager.owned_gain_supported defaults to True.
- PipeWireManager.loopback_backend_supported defaults to False.
- _probe_owned_gain() sets owned_gain_supported=True when probe node resolves.
- _probe_owned_gain() sets owned_gain_supported=False when probe node times out.
- _probe_owned_gain() sets owned_gain_supported=False when create-node fails.
- _probe_owned_gain() sets owned_gain_supported=False when pw-cli unavailable.
- _probe_owned_gain() emits capability_changed("owned_gain_supported", ...) signal.
- _probe_loopback_backend() sets loopback_backend_supported=True when probe resolves.
- _probe_loopback_backend() sets loopback_backend_supported=False when create-node fails.
- _probe_loopback_backend() sets loopback_backend_supported=False when pw-cli unavailable.
- _probe_loopback_backend() emits capability_changed("loopback_backend_supported", ...) signal.
- _ensure_pw_owned_gain_path() returns degraded reason when owned_gain_supported=False.
- start() in pw_only+nativmix mode calls _probe_owned_gain().
- start() calls _probe_loopback_backend() when _probe_owned_gain() yields False.
- ChannelWidget.set_gain_control_supported(False) disables slider and shows badge.
- ChannelWidget.set_gain_control_supported(True) hides badge.
- MainWindow._on_capability_changed propagates to all channel widgets.
- _rebuild_channels applies current gain_control_supported state to new widgets.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))


@pytest.fixture(autouse=True)
def _keep_qapplication_alive(qapp):
    """Keep the pytest-qt QApplication alive for all Qt object tests."""

# ---------------------------------------------------------------------------
# pulsectl mock
# ---------------------------------------------------------------------------

def _make_mock_pulsectl():
    mock_pa = MagicMock()
    mock_pulse_instance = MagicMock()
    mock_pulse_instance.__enter__ = MagicMock(return_value=mock_pulse_instance)
    mock_pulse_instance.__exit__ = MagicMock(return_value=False)
    mock_pulse_instance.sink_input_list.return_value = []
    mock_pulse_instance.sink_list.return_value = []
    mock_pulse_instance.source_list.return_value = []
    mock_pulse_instance.server_info.return_value = MagicMock(
        default_sink_name="alsa_output.default"
    )
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

def _make_config(num_channels: int = 2) -> MagicMock:
    cfg = MagicMock()
    cfg.num_channels = num_channels
    cfg.input_mode = "usb"
    cfg.get_channel_volume.return_value = 0.5
    cfg.get_app_names.return_value = []
    cfg.is_v_sink_enabled.return_value = False
    cfg.get_channel_mode.return_value = "software"
    cfg.routing_owner = "nativmix"
    cfg.get_channel_label.return_value = ""
    return cfg


def _make_pw_node(node_id: int, node_name: str = "", **kwargs):
    from nativmix.audio.pipewire_native import PipeWireNode
    return PipeWireNode(
        node_id=node_id,
        client_id=0,
        app_name=kwargs.get("app_name", ""),
        process_binary=kwargs.get("process_binary", ""),
        media_name=kwargs.get("media_name", ""),
        media_class=kwargs.get("media_class", "Stream/Output/Audio"),
        app_id="",
        node_name=node_name,
        props={},
    )


def _make_manager():
    from nativmix.audio.manager import PipeWireManager
    return PipeWireManager(config=_make_config())


# ---------------------------------------------------------------------------
# Default capability flags
# ---------------------------------------------------------------------------

class TestCapabilityFlagDefaults:
    def test_owned_gain_supported_defaults_true(self):
        mgr = _make_manager()
        assert mgr.owned_gain_supported is True

    def test_loopback_backend_supported_defaults_false(self):
        mgr = _make_manager()
        assert mgr.loopback_backend_supported is False

    def test_gain_control_supported_defaults_true(self):
        mgr = _make_manager()
        assert mgr.gain_control_supported is True

    def test_capability_changed_signal_exists(self):
        mgr = _make_manager()
        assert hasattr(mgr, "capability_changed")


# ---------------------------------------------------------------------------
# _probe_owned_gain
# ---------------------------------------------------------------------------

class TestProbeOwnedGain:
    """Tests for PipeWireManager._probe_owned_gain()."""

    def _make_probe_node(self):
        return _make_pw_node(node_id=99, node_name="nativmix-probe-owned-gain")

    def test_resolves_sets_supported_true(self):
        """When pw-dump returns the probe node, owned_gain_supported becomes True."""
        mgr = _make_manager()
        mgr.pw_cli_available = True
        probe_node = self._make_probe_node()
        with patch.object(mgr, "_run_pw_command", return_value=(True, "", "")) as mock_cmd, \
             patch.object(mgr, "_pw_dump_nodes_with_raw", return_value=([probe_node], [])):
            mgr._probe_owned_gain()
        assert mgr.owned_gain_supported is True
        # destroy call issued with the node id
        destroy_calls = [c for c in mock_cmd.call_args_list if "destroy" in c[0][0]]
        assert destroy_calls

    def test_timeout_sets_supported_false(self):
        """When pw-dump never returns the probe node, owned_gain_supported becomes False."""
        mgr = _make_manager()
        mgr.pw_cli_available = True
        # pw-dump always returns an empty list → times out
        with patch.object(mgr, "_run_pw_command", return_value=(True, "", "")), \
             patch.object(mgr, "_pw_dump_nodes_with_raw", return_value=([], [])), \
             patch("nativmix.audio.manager.time.monotonic", side_effect=[0.0, 3.0, 3.0]):
            mgr._probe_owned_gain()
        assert mgr.owned_gain_supported is False

    def test_create_node_failure_sets_supported_false(self):
        """When pw-cli create-node fails, owned_gain_supported becomes False."""
        mgr = _make_manager()
        mgr.pw_cli_available = True
        with patch.object(mgr, "_run_pw_command", return_value=(False, "", "some error")):
            mgr._probe_owned_gain()
        assert mgr.owned_gain_supported is False

    def test_pw_cli_unavailable_sets_supported_false(self):
        """When pw_cli_available is False, probe skips and marks unsupported."""
        mgr = _make_manager()
        mgr.pw_cli_available = False
        mgr._probe_owned_gain()
        assert mgr.owned_gain_supported is False

    def test_emits_capability_changed_true(self):
        """capability_changed("owned_gain_supported", True) emitted on success."""
        mgr = _make_manager()
        mgr.pw_cli_available = True
        emitted: list[tuple] = []
        mgr.capability_changed.connect(lambda n, v: emitted.append((n, v)))
        probe_node = self._make_probe_node()
        with patch.object(mgr, "_run_pw_command", return_value=(True, "", "")), \
             patch.object(mgr, "_pw_dump_nodes_with_raw", return_value=([probe_node], [])):
            mgr._probe_owned_gain()
        assert ("owned_gain_supported", True) in emitted

    def test_emits_capability_changed_false_on_timeout(self):
        """capability_changed("owned_gain_supported", False) emitted on timeout."""
        mgr = _make_manager()
        mgr.pw_cli_available = True
        emitted: list[tuple] = []
        mgr.capability_changed.connect(lambda n, v: emitted.append((n, v)))
        with patch.object(mgr, "_run_pw_command", return_value=(True, "", "")), \
             patch.object(mgr, "_pw_dump_nodes_with_raw", return_value=([], [])), \
             patch("nativmix.audio.manager.time.monotonic", side_effect=[0.0, 3.0, 3.0]):
            mgr._probe_owned_gain()
        assert ("owned_gain_supported", False) in emitted

    def test_emits_capability_changed_false_when_cli_missing(self):
        """capability_changed("owned_gain_supported", False) emitted when cli missing."""
        mgr = _make_manager()
        mgr.pw_cli_available = False
        emitted: list[tuple] = []
        mgr.capability_changed.connect(lambda n, v: emitted.append((n, v)))
        mgr._probe_owned_gain()
        assert ("owned_gain_supported", False) in emitted


# ---------------------------------------------------------------------------
# _probe_loopback_backend
# ---------------------------------------------------------------------------

class TestProbeLoopbackBackend:
    """Tests for PipeWireManager._probe_loopback_backend()."""

    def _make_loopback_node(self):
        return _make_pw_node(node_id=42, node_name="nativmix-probe-loopback")

    def test_resolves_sets_supported_true(self):
        """When pw-dump returns the loopback probe node, loopback_backend_supported becomes True."""
        mgr = _make_manager()
        mgr.pw_cli_available = True
        probe_node = self._make_loopback_node()
        with patch.object(mgr, "_run_pw_command", return_value=(True, "", "")), \
             patch.object(mgr, "_pw_dump_nodes_with_raw", return_value=([probe_node], [])):
            mgr._probe_loopback_backend()
        assert mgr.loopback_backend_supported is True

    def test_create_node_failure_sets_supported_false(self):
        """When pw-cli create-node loopback fails, loopback_backend_supported becomes False."""
        mgr = _make_manager()
        mgr.pw_cli_available = True
        with patch.object(mgr, "_run_pw_command", return_value=(False, "", "error")):
            mgr._probe_loopback_backend()
        assert mgr.loopback_backend_supported is False

    def test_timeout_sets_supported_false(self):
        """When the loopback probe times out, loopback_backend_supported becomes False."""
        mgr = _make_manager()
        mgr.pw_cli_available = True
        with patch.object(mgr, "_run_pw_command", return_value=(True, "", "")), \
             patch.object(mgr, "_pw_dump_nodes_with_raw", return_value=([], [])), \
             patch("nativmix.audio.manager.time.monotonic", side_effect=[0.0, 3.0, 3.0]):
            mgr._probe_loopback_backend()
        assert mgr.loopback_backend_supported is False

    def test_pw_cli_unavailable_sets_supported_false(self):
        mgr = _make_manager()
        mgr.pw_cli_available = False
        mgr._probe_loopback_backend()
        assert mgr.loopback_backend_supported is False

    def test_emits_capability_changed_true(self):
        mgr = _make_manager()
        mgr.pw_cli_available = True
        emitted: list[tuple] = []
        mgr.capability_changed.connect(lambda n, v: emitted.append((n, v)))
        probe_node = self._make_loopback_node()
        with patch.object(mgr, "_run_pw_command", return_value=(True, "", "")), \
             patch.object(mgr, "_pw_dump_nodes_with_raw", return_value=([probe_node], [])):
            mgr._probe_loopback_backend()
        assert ("loopback_backend_supported", True) in emitted

    def test_emits_capability_changed_false(self):
        mgr = _make_manager()
        mgr.pw_cli_available = True
        emitted: list[tuple] = []
        mgr.capability_changed.connect(lambda n, v: emitted.append((n, v)))
        with patch.object(mgr, "_run_pw_command", return_value=(False, "", "err")):
            mgr._probe_loopback_backend()
        assert ("loopback_backend_supported", False) in emitted


# ---------------------------------------------------------------------------
# _ensure_pw_owned_gain_path gates on owned_gain_supported
# ---------------------------------------------------------------------------

class TestEnsurePwOwnedGainPathGated:
    def test_returns_degraded_when_owned_gain_unsupported(self):
        """_ensure_pw_owned_gain_path returns degraded reason when probe failed."""
        mgr = _make_manager()
        mgr.pw_only_mode = True
        mgr.routing_owner = "nativmix"
        mgr.owned_gain_supported = False
        route = mgr._ensure_pw_owned_gain_path("Spotify")
        assert route.degraded_reason == "PW owned gain unsupported in this runtime"
        assert not route.active
        assert not route.writable

    def test_proceeds_when_owned_gain_supported(self):
        """_ensure_pw_owned_gain_path proceeds past the gate when supported=True."""
        mgr = _make_manager()
        mgr.pw_only_mode = True
        mgr.routing_owner = "nativmix"
        mgr.owned_gain_supported = True
        mgr.pw_cli_available = False  # will hit a different guard further in
        # Should not return the "unsupported in this runtime" degraded reason
        route = mgr._ensure_pw_owned_gain_path("Spotify")
        assert route.degraded_reason != "PW owned gain unsupported in this runtime"

    def test_inactive_when_not_pw_only(self):
        """_ensure_pw_owned_gain_path returns inactive reason when not in pw_only mode."""
        mgr = _make_manager()
        mgr.pw_only_mode = False
        mgr.owned_gain_supported = False
        route = mgr._ensure_pw_owned_gain_path("Spotify")
        assert route.degraded_reason == "inactive"


# ---------------------------------------------------------------------------
# start() integration: probe called in the right conditions
# ---------------------------------------------------------------------------

class TestStartCallsProbes:
    _CAPS_PW_ONLY = {
        "can_set_volume_pw": True,
        "can_set_volume": False,
        "can_move_stream": False,
        "pw_dump_available": True,
        "pw_cli_available": True,
        "wpctl_available": True,
        "pulse_available": False,
        "force_pw_only": False,
    }
    _CAPS_NORMAL = {
        "can_set_volume_pw": True,
        "can_set_volume": True,
        "can_move_stream": True,
        "pw_dump_available": True,
        "pw_cli_available": True,
        "wpctl_available": True,
        "pulse_available": True,
        "force_pw_only": False,
    }

    def test_probe_owned_gain_called_in_pw_only_mode(self):
        mgr = _make_manager()
        with patch("nativmix.audio.manager._probe_capabilities", return_value=self._CAPS_PW_ONLY), \
             patch.object(mgr, "_startup_routing_self_check"), \
             patch.object(mgr, "_probe_owned_gain") as mock_probe, \
             patch.object(mgr, "_probe_loopback_backend"), \
             patch.object(mgr, "_mark_audit_complete"), \
             patch.object(mgr, "_refresh_owned_gain_paths"), \
             patch.object(mgr, "_refresh_pw_nodes"), \
             patch("nativmix.audio.manager._PipeWirePollerThread") as mock_cls:
            mock_cls.return_value = MagicMock()
            mgr.start()
        mock_probe.assert_called_once()
        mgr.stop()

    def test_probe_loopback_called_when_owned_gain_unsupported(self):
        """_probe_loopback_backend is called only when _probe_owned_gain sets unsupported."""
        mgr = _make_manager()
        def _fake_probe_owned_gain():
            mgr.owned_gain_supported = False
        with patch("nativmix.audio.manager._probe_capabilities", return_value=self._CAPS_PW_ONLY), \
             patch.object(mgr, "_startup_routing_self_check"), \
             patch.object(mgr, "_probe_owned_gain", side_effect=_fake_probe_owned_gain), \
             patch.object(mgr, "_apply_routing_owner_runtime_override", return_value=False), \
             patch.object(mgr, "_probe_loopback_backend") as mock_lb, \
             patch.object(mgr, "_mark_audit_complete"), \
             patch.object(mgr, "_refresh_owned_gain_paths"), \
             patch.object(mgr, "_refresh_pw_nodes"), \
             patch("nativmix.audio.manager._PipeWirePollerThread") as mock_cls:
            mock_cls.return_value = MagicMock()
            mgr.start()
        mock_lb.assert_called_once()
        mgr.stop()

    def test_probe_loopback_not_called_when_owned_gain_supported(self):
        """_probe_loopback_backend is NOT called when owned gain probe succeeds."""
        mgr = _make_manager()
        def _fake_probe_owned_gain():
            mgr.owned_gain_supported = True
        with patch("nativmix.audio.manager._probe_capabilities", return_value=self._CAPS_PW_ONLY), \
             patch.object(mgr, "_startup_routing_self_check"), \
             patch.object(mgr, "_probe_owned_gain", side_effect=_fake_probe_owned_gain), \
             patch.object(mgr, "_probe_loopback_backend") as mock_lb, \
             patch.object(mgr, "_mark_audit_complete"), \
             patch.object(mgr, "_refresh_owned_gain_paths"), \
             patch.object(mgr, "_refresh_pw_nodes"), \
             patch("nativmix.audio.manager._PipeWirePollerThread") as mock_cls:
            mock_cls.return_value = MagicMock()
            mgr.start()
        mock_lb.assert_not_called()
        mgr.stop()

    def test_probe_not_called_in_normal_mode(self):
        """Owned gain probe is NOT called outside PW-only mode."""
        mgr = _make_manager()
        with patch("nativmix.audio.manager._probe_capabilities", return_value=self._CAPS_NORMAL), \
             patch.object(mgr, "_startup_routing_self_check"), \
             patch.object(mgr, "_probe_owned_gain") as mock_probe, \
             patch.object(mgr, "_probe_loopback_backend"), \
             patch("nativmix.audio.manager._AudioListenerThread") as mock_listener_cls, \
             patch("nativmix.audio.manager.SinkPollThread") as mock_sink:
            mock_listener_cls.return_value = MagicMock()
            mock_sink.return_value = MagicMock()
            mgr._sink_poll_thread = MagicMock()
            mgr.start()
        mock_probe.assert_not_called()
        mgr.stop()


# ---------------------------------------------------------------------------
# GUI: ChannelWidget.set_owned_gain_supported
# ---------------------------------------------------------------------------

class TestChannelWidgetOwnedGainBadge:
    """Tests for ChannelWidget.set_gain_control_supported()."""

    def _make_channel_widget(self):
        try:
            from PyQt6.QtWidgets import QApplication
            _app = QApplication.instance() or QApplication(sys.argv[:1])
        except Exception:
            pytest.skip("PyQt6 / display not available")
        from nativmix.gui.main_window import ChannelWidget
        cfg = _make_config()
        cfg.get_effective_inversion.return_value = False
        cfg.show_invert_option = False
        backend = MagicMock()
        backend.gain_control_supported = True
        return ChannelWidget(0, cfg, backend)

    def test_badge_hidden_by_default(self):
        w = self._make_channel_widget()
        assert not w._gain_unsupported_badge.isVisible()

    def test_set_unsupported_shows_badge(self):
        w = self._make_channel_widget()
        w.set_gain_control_supported(False)
        assert not w._gain_unsupported_badge.isHidden()

    def test_set_unsupported_disables_slider(self):
        w = self._make_channel_widget()
        w.set_gain_control_supported(False)
        assert not w._slider.isEnabled()

    def test_set_supported_hides_badge(self):
        w = self._make_channel_widget()
        w.set_gain_control_supported(False)
        w.set_gain_control_supported(True)
        assert not w._gain_unsupported_badge.isVisible()

    def test_slider_tooltip_set_when_unsupported(self):
        w = self._make_channel_widget()
        w.set_gain_control_supported(False)
        assert "unavailable" in w._slider.toolTip().lower()

    def test_slider_tooltip_cleared_when_supported(self):
        w = self._make_channel_widget()
        w.set_gain_control_supported(False)
        w.set_gain_control_supported(True)
        assert w._slider.toolTip() == ""

    def test_unmuting_does_not_enable_slider_when_gain_is_unsupported(self):
        w = self._make_channel_widget()
        w.set_gain_control_supported(False)
        w.set_mute_state(True)
        w.set_mute_state(False)
        assert not w._slider.isEnabled()


# ---------------------------------------------------------------------------
# GUI: MainWindow._on_capability_changed dispatch
# ---------------------------------------------------------------------------

class TestMainWindowCapabilityChanged:
    def _try_import_mainwindow(self):
        try:
            from PyQt6.QtWidgets import QApplication
            QApplication.instance() or QApplication(sys.argv[:1])
            from nativmix.gui.main_window import MainWindow
            return MainWindow
        except Exception as exc:
            pytest.skip(f"MainWindow unavailable (headless/display): {exc}")

    def _make_win(self):
        MainWindow = self._try_import_mainwindow()
        cfg = _make_config()
        cfg.get_effective_inversion.return_value = False
        cfg.show_invert_option = False
        cfg.stay_open = False
        cfg.compact_mode = False
        cfg.input_mode = "usb"
        cfg.hardware_port = ""
        cfg.all_channels.return_value = []

        backend = MagicMock()
        backend.gain_control_supported = True
        # Give the backend real Qt signals so MainWindow can connect to them.
        real_mgr = _make_manager()
        backend.other_apps_changed = real_mgr.other_apps_changed
        backend.unresolved_targets_changed = real_mgr.unresolved_targets_changed
        backend.status_changed = real_mgr.status_changed
        backend.capability_changed = real_mgr.capability_changed
        return MainWindow(config=cfg, backend=backend)

    def test_on_capability_changed_propagates_to_channels(self):
        """Effective gain capability changes propagate to all channels."""
        win = self._make_win()
        ch_mock_a = MagicMock()
        ch_mock_b = MagicMock()
        win._channels = [ch_mock_a, ch_mock_b]

        win._on_capability_changed("gain_control_supported", False)

        ch_mock_a.set_gain_control_supported.assert_called_once_with(False)
        ch_mock_b.set_gain_control_supported.assert_called_once_with(False)

    def test_on_capability_changed_unknown_cap_ignored(self):
        """_on_capability_changed with unknown cap_name does not error."""
        win = self._make_win()
        ch_mock = MagicMock()
        win._channels = [ch_mock]

        # Should not raise
        win._on_capability_changed("some_unknown_capability", False)
        ch_mock.set_gain_control_supported.assert_not_called()

    def test_rebuild_uses_effective_gain_capability(self):
        win = self._make_win()
        win._backend.owned_gain_supported = False
        win._backend.gain_control_supported = True
        win._config.all_channels.return_value = [{"index": 0, "is_midi": False}]

        win._rebuild_channels()

        assert len(win._channels) == 1
        assert win._channels[0]._slider.isEnabled()
        assert not win._channels[0]._gain_unsupported_badge.isVisible()
