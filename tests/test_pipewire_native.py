"""
Tests for the PipeWire-native backend features (Phase 1–5).

Covers:
- PipeWireNode dataclass fields.
- _pw_dump_nodes parses pw-dump JSON output correctly.
- _matches_node priority order: stable IDs → binary → app name → media name → contains.
- _ThrottledWarner suppresses repeated messages within the interval.
- _probe_capabilities returns correct flags.
- PipeWireManager.can_set_volume / can_move_stream flags are exposed.
- set_volume / set_mute / _apply_volume_by_name skip writes when can_set_volume is False.
- PW-native inventory is included in get_active_streams_debug output.
- Stable ID cache is populated after a successful PW-native match.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

# ---------------------------------------------------------------------------
# pulsectl availability guard (for tests that need the real pulsectl import)
# ---------------------------------------------------------------------------
try:
    import pulsectl  # noqa: F401
    _PULSECTL_OK = True
except Exception:
    _PULSECTL_OK = False

_SKIP_NO_PULSECTL = pytest.mark.skipif(
    not _PULSECTL_OK, reason="pulsectl / libpulse not available"
)

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
    node_name: str = "",
    props: dict | None = None,
):
    from nativmix.audio.pipewire_native import PipeWireNode
    _props = props or {}
    _node_name = node_name or _props.get("node.name", "")
    return PipeWireNode(
        node_id=node_id,
        client_id=client_id,
        app_name=app_name,
        process_binary=process_binary,
        media_name=media_name,
        media_class=media_class,
        app_id=app_id,
        node_name=_node_name,
        props=_props,
    )


# ---------------------------------------------------------------------------
# PipeWireNode dataclass
# ---------------------------------------------------------------------------

class TestPipeWireNode:
    def test_fields_populated(self):
        from nativmix.audio.pipewire_native import PipeWireNode
        n = PipeWireNode(
            node_id=42,
            client_id=10,
            app_name="Spotify",
            process_binary="spotify",
            media_name="",
            media_class="Stream/Output/Audio",
            app_id="com.spotify.Client",
            props={"foo": "bar"},
        )
        assert n.node_id == 42
        assert n.client_id == 10
        assert n.app_name == "Spotify"
        assert n.process_binary == "spotify"
        assert n.app_id == "com.spotify.Client"
        assert n.props == {"foo": "bar"}

    def test_default_fields(self):
        node = _make_pw_node()
        assert node.node_id == 1
        assert node.client_id == 0
        assert node.app_name == ""
        assert node.media_class == "Stream/Output/Audio"


# ---------------------------------------------------------------------------
# _matches_node — priority order
# ---------------------------------------------------------------------------

class TestMatchesNode:
    """Verify _matches_node priority: stable IDs → binary → app name → media name → contains."""

    def _match(self, node, target, node_ids=None, client_ids=None):
        from nativmix.audio.pipewire_native import _matches_node
        return _matches_node(node, target,
                             stable_node_ids=node_ids,
                             stable_client_ids=client_ids)

    def test_stable_node_id_match(self):
        node = _make_pw_node(node_id=99, app_name="Other")
        assert self._match(node, "Spotify", node_ids={99}) is True

    def test_stable_client_id_match(self):
        node = _make_pw_node(node_id=1, client_id=55, app_name="Other")
        assert self._match(node, "Spotify", client_ids={55}) is True

    def test_stable_node_id_not_in_cache_falls_through(self):
        node = _make_pw_node(node_id=99, app_name="Spotify")
        # Node ID 100 ≠ 99 → falls through to app_name check → matches
        assert self._match(node, "Spotify", node_ids={100}) is True

    def test_process_binary_exact_match(self):
        node = _make_pw_node(process_binary="spotify", app_name="")
        assert self._match(node, "spotify") is True

    def test_process_binary_case_insensitive(self):
        node = _make_pw_node(process_binary="SPOTIFY")
        assert self._match(node, "spotify") is True

    def test_app_name_exact_match(self):
        node = _make_pw_node(app_name="Spotify")
        assert self._match(node, "Spotify") is True

    def test_app_name_case_insensitive(self):
        node = _make_pw_node(app_name="SPOTIFY")
        assert self._match(node, "spotify") is True

    def test_media_name_exact_match(self):
        node = _make_pw_node(media_name="Spotify")
        assert self._match(node, "Spotify") is True

    def test_media_name_case_insensitive(self):
        node = _make_pw_node(media_name="SPOTIFY")
        assert self._match(node, "spotify") is True

    def test_contains_fallback_app_name(self):
        node = _make_pw_node(app_name="Spotify Premium")
        assert self._match(node, "Spotify") is True

    def test_contains_fallback_binary(self):
        node = _make_pw_node(process_binary="spotify_helper")
        assert self._match(node, "spotify") is True

    def test_no_match(self):
        node = _make_pw_node(app_name="Firefox", process_binary="firefox", media_name="YouTube")
        assert self._match(node, "Spotify") is False

    def test_empty_target_never_matches(self):
        node = _make_pw_node(app_name="Spotify")
        assert self._match(node, "") is False

    def test_short_target_skips_contains(self):
        """Targets shorter than 3 chars must not use the contains fallback."""
        node = _make_pw_node(app_name="xz player")
        # "xz" is 2 chars — contains fallback is disabled
        assert self._match(node, "xz") is False

    def test_process_binary_priority_over_app_name(self):
        """process.binary is checked before application.name."""
        node = _make_pw_node(process_binary="spotify", app_name="Firefox")
        # binary matches "spotify" → True even though app_name is "Firefox"
        assert self._match(node, "spotify") is True

    def test_stable_ids_take_priority_over_fields(self):
        """Stable ID match returns True even when fields don't match."""
        node = _make_pw_node(node_id=7, app_name="VLC", process_binary="vlc")
        # node_id 7 is in the cache for "Spotify" — trust the cache
        assert self._match(node, "Spotify", node_ids={7}) is True


# ---------------------------------------------------------------------------
# _pw_dump_nodes — JSON parsing
# ---------------------------------------------------------------------------

class TestPwDumpNodes:
    """Verify _pw_dump_nodes parses pw-dump output correctly."""

    def test_returns_empty_when_no_pw_dump(self):
        from nativmix.audio.pipewire_native import _pw_dump_nodes
        with patch("shutil.which", return_value=None):
            assert _pw_dump_nodes() == []

    def test_parses_output_node(self):
        from nativmix.audio.pipewire_native import _pw_dump_nodes
        fake_json = json.dumps([
            {
                "id": 42,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "props": {
                        "media.class": "Stream/Output/Audio",
                        "application.name": "Spotify",
                        "application.process.binary": "spotify",
                        "media.name": "Spotify stream",
                        "client.id": "10",
                        "object.serial": "42",
                    }
                }
            }
        ])
        with patch("shutil.which", return_value="/usr/bin/pw-dump"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=fake_json)
            nodes = _pw_dump_nodes()

        assert len(nodes) == 1
        n = nodes[0]
        assert n.node_id == 42
        assert n.client_id == 10
        assert n.app_name == "Spotify"
        assert n.process_binary == "spotify"
        assert n.media_name == "Spotify stream"

    def test_filters_non_output_nodes(self):
        from nativmix.audio.pipewire_native import _pw_dump_nodes
        fake_json = json.dumps([
            {
                "id": 1,
                "type": "PipeWire:Interface:Node",
                "info": {"props": {"media.class": "Stream/Input/Audio"}}
            },
            {
                "id": 2,
                "type": "PipeWire:Interface:Node",
                "info": {"props": {"media.class": "Stream/Output/Audio",
                                   "application.name": "Player"}}
            }
        ])
        with patch("shutil.which", return_value="/usr/bin/pw-dump"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=fake_json)
            nodes = _pw_dump_nodes()

        assert len(nodes) == 1
        assert nodes[0].app_name == "Player"

    def test_non_zero_returncode_returns_empty(self):
        from nativmix.audio.pipewire_native import _pw_dump_nodes
        with patch("shutil.which", return_value="/usr/bin/pw-dump"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _pw_dump_nodes() == []

    def test_invalid_json_returns_empty(self):
        from nativmix.audio.pipewire_native import _pw_dump_nodes
        with patch("shutil.which", return_value="/usr/bin/pw-dump"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not-json")
            assert _pw_dump_nodes() == []

    def test_skips_non_node_objects(self):
        from nativmix.audio.pipewire_native import _pw_dump_nodes
        fake_json = json.dumps([
            {"id": 1, "type": "PipeWire:Interface:Client", "info": {"props": {}}},
            {
                "id": 2,
                "type": "PipeWire:Interface:Node",
                "info": {"props": {"media.class": "Stream/Output/Audio",
                                   "application.name": "VLC"}}
            }
        ])
        with patch("shutil.which", return_value="/usr/bin/pw-dump"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=fake_json)
            nodes = _pw_dump_nodes()

        assert len(nodes) == 1
        assert nodes[0].app_name == "VLC"

    def test_portal_app_id_populated(self):
        from nativmix.audio.pipewire_native import _pw_dump_nodes
        fake_json = json.dumps([
            {
                "id": 5,
                "type": "PipeWire:Interface:Node",
                "info": {"props": {
                    "media.class": "Stream/Output/Audio",
                    "pipewire.access.portal.app_id": "org.spotify.Spotify",
                }}
            }
        ])
        with patch("shutil.which", return_value="/usr/bin/pw-dump"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=fake_json)
            nodes = _pw_dump_nodes()

        assert nodes[0].app_id == "org.spotify.Spotify"


# ---------------------------------------------------------------------------
# _pw_set_volume / _pw_set_mute
# ---------------------------------------------------------------------------

class TestPwSetVolume:
    """Verify _pw_set_volume delegates to pw-cli correctly."""

    def test_returns_true_on_success(self):
        from nativmix.audio.pipewire_native import _pw_set_volume
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        with patch("shutil.which", return_value="/usr/bin/pw-cli"), \
             patch("subprocess.run", mock_run):
            assert _pw_set_volume(42, 0.5) is True
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "pw-cli"
        assert cmd[2] == "42"
        assert "volume" in cmd[4]

    def test_returns_false_when_pw_cli_missing(self):
        from nativmix.audio.pipewire_native import _pw_set_volume
        with patch("shutil.which", return_value=None):
            assert _pw_set_volume(42, 0.5) is False

    def test_returns_false_when_node_id_zero(self):
        from nativmix.audio.pipewire_native import _pw_set_volume
        with patch("shutil.which", return_value="/usr/bin/pw-cli"):
            assert _pw_set_volume(0, 0.5) is False

    def test_returns_false_on_nonzero_returncode(self):
        from nativmix.audio.pipewire_native import _pw_set_volume
        mock_run = MagicMock(return_value=MagicMock(returncode=1))
        with patch("shutil.which", return_value="/usr/bin/pw-cli"), \
             patch("subprocess.run", mock_run):
            assert _pw_set_volume(42, 0.5) is False

    def test_clamps_volume_above_one(self):
        from nativmix.audio.pipewire_native import _pw_set_volume
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        with patch("shutil.which", return_value="/usr/bin/pw-cli"), \
             patch("subprocess.run", mock_run):
            _pw_set_volume(1, 1.5)
        cmd = mock_run.call_args[0][0]
        assert "1.000000" in cmd[4]

    def test_clamps_volume_below_zero(self):
        from nativmix.audio.pipewire_native import _pw_set_volume
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        with patch("shutil.which", return_value="/usr/bin/pw-cli"), \
             patch("subprocess.run", mock_run):
            _pw_set_volume(1, -0.5)
        cmd = mock_run.call_args[0][0]
        assert "0.000000" in cmd[4]

    def test_returns_false_on_subprocess_exception(self):
        from nativmix.audio.pipewire_native import _pw_set_volume
        with patch("shutil.which", return_value="/usr/bin/pw-cli"), \
             patch("subprocess.run", side_effect=OSError("broken")):
            assert _pw_set_volume(1, 0.5) is False


class TestPwSetMute:
    """Verify _pw_set_mute delegates to pw-cli correctly."""

    def test_returns_true_on_success_mute(self):
        from nativmix.audio.pipewire_native import _pw_set_mute
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        with patch("shutil.which", return_value="/usr/bin/pw-cli"), \
             patch("subprocess.run", mock_run):
            assert _pw_set_mute(42, True) is True
        cmd = mock_run.call_args[0][0]
        assert "true" in cmd[4]

    def test_returns_true_on_success_unmute(self):
        from nativmix.audio.pipewire_native import _pw_set_mute
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        with patch("shutil.which", return_value="/usr/bin/pw-cli"), \
             patch("subprocess.run", mock_run):
            assert _pw_set_mute(42, False) is True
        cmd = mock_run.call_args[0][0]
        assert "false" in cmd[4]

    def test_returns_false_when_pw_cli_missing(self):
        from nativmix.audio.pipewire_native import _pw_set_mute
        with patch("shutil.which", return_value=None):
            assert _pw_set_mute(42, True) is False

    def test_returns_false_when_node_id_zero(self):
        from nativmix.audio.pipewire_native import _pw_set_mute
        with patch("shutil.which", return_value="/usr/bin/pw-cli"):
            assert _pw_set_mute(0, True) is False

    def test_returns_false_on_nonzero_returncode(self):
        from nativmix.audio.pipewire_native import _pw_set_mute
        mock_run = MagicMock(return_value=MagicMock(returncode=1))
        with patch("shutil.which", return_value="/usr/bin/pw-cli"), \
             patch("subprocess.run", mock_run):
            assert _pw_set_mute(42, True) is False

    def test_returns_false_on_subprocess_exception(self):
        from nativmix.audio.pipewire_native import _pw_set_mute
        with patch("shutil.which", return_value="/usr/bin/pw-cli"), \
             patch("subprocess.run", side_effect=OSError("broken")):
            assert _pw_set_mute(1, True) is False


# ---------------------------------------------------------------------------
# _probe_capabilities
# ---------------------------------------------------------------------------

class TestProbeCapabilities:
    """Verify capability probe flags."""

    def test_can_set_volume_true_when_pulse_succeeds(self):
        from nativmix.audio.pipewire_native import _probe_capabilities

        mock_si = MagicMock()
        mock_si.volume.values = [1.0]
        mock_pulse = MagicMock()
        mock_pulse.__enter__ = MagicMock(return_value=mock_pulse)
        mock_pulse.__exit__ = MagicMock(return_value=False)
        mock_pulse.server_info.return_value = MagicMock()
        mock_pulse.sink_input_list.return_value = [mock_si]

        mock_pulsectl = MagicMock()
        mock_pulsectl.Pulse.return_value = mock_pulse

        with patch.dict("sys.modules", {"pulsectl": mock_pulsectl}), \
             patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"):
            caps = _probe_capabilities()

        assert caps["can_set_volume"] is True

    def test_can_set_volume_false_when_pulse_raises(self):
        from nativmix.audio.pipewire_native import _probe_capabilities

        mock_pulsectl = MagicMock()
        mock_pulsectl.Pulse.side_effect = Exception("connection refused")

        with patch.dict("sys.modules", {"pulsectl": mock_pulsectl}), \
             patch("shutil.which", return_value=None):
            caps = _probe_capabilities()

        assert caps["can_set_volume"] is False

    def test_can_move_stream_false_when_pactl_missing(self):
        from nativmix.audio.pipewire_native import _probe_capabilities
        with patch("shutil.which", return_value=None):
            caps = _probe_capabilities()
        assert caps["can_move_stream"] is False

    def test_can_move_stream_true_when_pactl_present(self):
        from nativmix.audio.pipewire_native import _probe_capabilities

        def _which(tool):
            return f"/usr/bin/{tool}" if tool == "pactl" else None

        with patch("shutil.which", side_effect=_which):
            caps = _probe_capabilities()

        assert caps["can_move_stream"] is True

    def test_pw_dump_available_flag(self):
        from nativmix.audio.pipewire_native import _probe_capabilities
        mock_pulse = MagicMock()
        mock_pulse.__enter__ = MagicMock(return_value=mock_pulse)
        mock_pulse.__exit__ = MagicMock(return_value=False)
        mock_pulse.server_info.return_value = MagicMock()
        mock_pulse.sink_input_list.return_value = []
        mock_pulsectl = MagicMock()
        mock_pulsectl.Pulse.return_value = mock_pulse

        def _which(tool):
            return f"/usr/bin/{tool}" if tool == "pw-dump" else None

        with patch.dict("sys.modules", {"pulsectl": mock_pulsectl}), \
             patch("shutil.which", side_effect=_which):
            caps = _probe_capabilities()

        assert caps["pw_dump_available"] is True
        assert caps["pw_cli_available"] is False

    def test_all_tools_present(self):
        from nativmix.audio.pipewire_native import _probe_capabilities
        mock_pulse = MagicMock()
        mock_pulse.__enter__ = MagicMock(return_value=mock_pulse)
        mock_pulse.__exit__ = MagicMock(return_value=False)
        mock_pulse.server_info.return_value = MagicMock()
        mock_pulse.sink_input_list.return_value = []
        mock_pulsectl = MagicMock()
        mock_pulsectl.Pulse.return_value = mock_pulse

        mock_run = MagicMock()
        mock_run.return_value = MagicMock(returncode=0)

        with patch.dict("sys.modules", {"pulsectl": mock_pulsectl}), \
             patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"), \
             patch("subprocess.run", mock_run):
            caps = _probe_capabilities()

        assert all(caps.values())

    def test_can_set_volume_pw_true_when_pw_cli_succeeds(self):
        from nativmix.audio.pipewire_native import _probe_capabilities

        mock_run = MagicMock(return_value=MagicMock(returncode=0))

        def _which(tool):
            return f"/usr/bin/{tool}" if tool == "pw-cli" else None

        with patch("shutil.which", side_effect=_which), \
             patch("subprocess.run", mock_run):
            caps = _probe_capabilities()

        assert caps["can_set_volume_pw"] is True

    def test_can_set_volume_pw_false_when_pw_cli_missing(self):
        from nativmix.audio.pipewire_native import _probe_capabilities
        with patch("shutil.which", return_value=None):
            caps = _probe_capabilities()
        assert caps["can_set_volume_pw"] is False

    def test_can_set_volume_pw_false_when_pw_cli_returns_nonzero(self):
        from nativmix.audio.pipewire_native import _probe_capabilities

        mock_run = MagicMock(return_value=MagicMock(returncode=1))

        def _which(tool):
            return f"/usr/bin/{tool}" if tool == "pw-cli" else None

        with patch("shutil.which", side_effect=_which), \
             patch("subprocess.run", mock_run):
            caps = _probe_capabilities()

        assert caps["can_set_volume_pw"] is False


# ---------------------------------------------------------------------------
# _ThrottledWarner
# ---------------------------------------------------------------------------

class TestThrottledWarner:
    def test_first_call_logs(self, caplog):
        import logging
        from nativmix.audio.pipewire_native import _ThrottledWarner
        w = _ThrottledWarner(interval=60.0)
        with caplog.at_level(logging.WARNING, logger="nativmix.audio.pipewire_native"):
            w.warn("k", "hello %s", "world")
        assert any("hello world" in r.message for r in caplog.records)

    def test_second_call_within_interval_suppressed(self, caplog):
        import logging
        from nativmix.audio.pipewire_native import _ThrottledWarner
        w = _ThrottledWarner(interval=60.0)
        with caplog.at_level(logging.WARNING, logger="nativmix.audio.pipewire_native"):
            w.warn("k", "msg1")
            caplog.clear()
            w.warn("k", "msg2")
        assert not any("msg2" in r.message for r in caplog.records)

    def test_call_after_interval_logs_again(self, caplog):
        import logging
        from nativmix.audio.pipewire_native import _ThrottledWarner
        w = _ThrottledWarner(interval=0.01)
        with caplog.at_level(logging.WARNING, logger="nativmix.audio.pipewire_native"):
            w.warn("k", "first")
        time.sleep(0.02)
        with caplog.at_level(logging.WARNING, logger="nativmix.audio.pipewire_native"):
            w.warn("k", "second")
        assert any("second" in r.message for r in caplog.records)

    def test_different_keys_logged_independently(self, caplog):
        import logging
        from nativmix.audio.pipewire_native import _ThrottledWarner
        w = _ThrottledWarner(interval=60.0)
        with caplog.at_level(logging.WARNING, logger="nativmix.audio.pipewire_native"):
            w.warn("key_a", "msg_a")
            w.warn("key_b", "msg_b")
        messages = [r.message for r in caplog.records]
        assert any("msg_a" in m for m in messages)
        assert any("msg_b" in m for m in messages)


# ---------------------------------------------------------------------------
# PipeWireManager capability flags + feature gate (require pulsectl)
# ---------------------------------------------------------------------------

@_SKIP_NO_PULSECTL
class TestPipeWireManagerCapabilityFlags:
    """Verify capability flags are exposed and gate write operations."""

    def _make_manager(self, tmp_path: Path):
        from nativmix.audio.manager import PipeWireManager
        from nativmix.utils.config_manager import ConfigManager
        cfg = ConfigManager(
            config_path=tmp_path / "config.json",
            profiles_dir=tmp_path / "profiles",
        )
        mgr = PipeWireManager.__new__(PipeWireManager)
        mgr._config = cfg
        mgr._state_lock = threading.RLock()
        mgr._poti_volumes = {}
        mgr._channel_muted = {}
        mgr._vsink_creating = set()
        mgr._pw_nodes = {}
        mgr._pw_nodes_lock = threading.Lock()
        mgr._stable_ids = {}
        mgr.can_set_volume_pw = False
        mgr.can_set_volume = True
        mgr.can_move_stream = True
        mgr.pw_dump_available = False
        mgr.pw_cli_available = False
        return mgr

    def test_default_can_set_volume_is_true(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        assert mgr.can_set_volume is True

    def test_set_volume_skipped_when_both_caps_false(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        mgr.can_set_volume_pw = False
        mgr.can_set_volume = False
        with patch("pulsectl.Pulse") as mock_pulse_cls:
            mgr.set_volume(1, 0.5)
        mock_pulse_cls.assert_not_called()

    def test_set_mute_skipped_when_both_caps_false(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        mgr.can_set_volume_pw = False
        mgr.can_set_volume = False
        with patch("pulsectl.Pulse") as mock_pulse_cls:
            mgr.set_mute(1, True)
        mock_pulse_cls.assert_not_called()

    def test_apply_volume_by_name_skipped_when_both_caps_false(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        mgr.can_set_volume_pw = False
        mgr.can_set_volume = False
        with patch("pulsectl.Pulse") as mock_pulse_cls:
            mgr._apply_volume_by_name("Spotify", 0.5)
        mock_pulse_cls.assert_not_called()

    def test_apply_volume_by_name_with_pw_nodes_snapshot(self, tmp_path):
        """PW-native nodes in the inventory are used for matching (Phase 3)."""
        from nativmix.audio.pipewire_native import PipeWireNode
        mgr = self._make_manager(tmp_path)
        mgr.can_set_volume = True
        mgr.pw_dump_available = True

        pw_node = PipeWireNode(
            node_id=101,
            client_id=0,
            app_name="Spotify",
            process_binary="spotify",
            media_name="",
            media_class="Stream/Output/Audio",
            app_id="",
            props={"object.serial": "101"},
        )
        with mgr._pw_nodes_lock:
            mgr._pw_nodes = {101: pw_node}

        si = MagicMock()
        si.index = 55
        si.proplist = {
            "application.name": "Spotify",
            "application.process.id": "0",
            "object.serial": "101",
        }
        pulse = MagicMock()
        pulse.sink_input_list.return_value = [si]

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Spotify"):
            mgr._apply_volume_by_name("Spotify", 0.7, pulse=pulse)

        pulse.volume_set_all_chans.assert_called_once_with(si, 0.7)

    def test_stable_id_cache_populated_after_match(self, tmp_path):
        """After a successful PW-native match, node_id is cached in _stable_ids."""
        from nativmix.audio.pipewire_native import PipeWireNode
        mgr = self._make_manager(tmp_path)
        mgr.can_set_volume = True
        mgr.pw_dump_available = True

        pw_node = PipeWireNode(
            node_id=200,
            client_id=0,
            app_name="Firefox",
            process_binary="firefox",
            media_name="",
            media_class="Stream/Output/Audio",
            app_id="",
            props={"object.serial": "200"},
        )
        with mgr._pw_nodes_lock:
            mgr._pw_nodes = {200: pw_node}

        si = MagicMock()
        si.index = 77
        si.proplist = {
            "application.name": "Firefox",
            "application.process.id": "0",
            "object.serial": "200",
        }
        pulse = MagicMock()
        pulse.sink_input_list.return_value = [si]

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Firefox"):
            mgr._apply_volume_by_name("Firefox", 0.8, pulse=pulse)

        node_ids, _ = mgr._stable_ids.get("firefox", (set(), set()))
        assert 200 in node_ids

    def test_get_active_streams_debug_includes_pw_nodes(self, tmp_path):
        """get_active_streams_debug must include pw_nodes and capabilities keys."""
        from nativmix.audio.pipewire_native import PipeWireNode
        mgr = self._make_manager(tmp_path)
        mgr._active_streams = {}
        mgr._last_other_apps = []
        mgr.can_set_volume = True
        mgr.can_move_stream = True
        mgr.pw_dump_available = True
        mgr.pw_cli_available = False

        pw_node = PipeWireNode(
            node_id=1,
            client_id=0,
            app_name="VLC",
            process_binary="vlc",
            media_name="",
            media_class="Stream/Output/Audio",
            app_id="",
            props={},
        )
        with mgr._pw_nodes_lock:
            mgr._pw_nodes = {1: pw_node}

        with patch.object(mgr, "get_active_streams", return_value=[]), \
             patch.object(mgr, "_get_all_assigned_apps", return_value=set()):
            result = mgr.get_active_streams_debug()

        assert "pw_nodes" in result
        assert "capabilities" in result
        assert result["capabilities"]["pw_dump_available"] is True
        assert result["capabilities"]["pw_cli_available"] is False
        assert len(result["pw_nodes"]) == 1
        assert result["pw_nodes"][0]["app_name"] == "VLC"


# ---------------------------------------------------------------------------
# wpctl helpers
# ---------------------------------------------------------------------------

class TestWpctlHelpers:
    """Verify wpctl volume/mute helpers behave correctly."""

    def test_wpctl_set_volume_returns_false_when_wpctl_missing(self):
        from nativmix.audio.pipewire_native import _wpctl_set_volume
        with patch("shutil.which", return_value=None):
            assert _wpctl_set_volume(42, 0.5) is False

    def test_wpctl_set_volume_returns_false_for_zero_node_id(self):
        from nativmix.audio.pipewire_native import _wpctl_set_volume
        with patch("shutil.which", return_value="/usr/bin/wpctl"):
            assert _wpctl_set_volume(0, 0.5) is False

    def test_wpctl_set_volume_returns_true_on_success(self):
        from nativmix.audio.pipewire_native import _wpctl_set_volume
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        with patch("shutil.which", return_value="/usr/bin/wpctl"), \
             patch("subprocess.run", mock_run):
            assert _wpctl_set_volume(42, 0.8) is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[:3] == ["wpctl", "set-volume", "42"]

    def test_wpctl_set_volume_returns_false_on_nonzero_rc(self):
        from nativmix.audio.pipewire_native import _wpctl_set_volume
        with patch("shutil.which", return_value="/usr/bin/wpctl"), \
             patch("subprocess.run", return_value=MagicMock(returncode=1)):
            assert _wpctl_set_volume(42, 0.5) is False

    def test_wpctl_set_volume_clamps_above_one(self):
        from nativmix.audio.pipewire_native import _wpctl_set_volume
        captured = {}
        def mock_run(args, **kw):
            captured["vol"] = args[3]
            return MagicMock(returncode=0)
        with patch("shutil.which", return_value="/usr/bin/wpctl"), \
             patch("subprocess.run", mock_run):
            _wpctl_set_volume(1, 1.5)
        assert float(captured["vol"]) <= 1.0

    def test_wpctl_set_volume_default_sink_returns_false_when_missing(self):
        from nativmix.audio.pipewire_native import _wpctl_set_volume_default_sink
        with patch("shutil.which", return_value=None):
            assert _wpctl_set_volume_default_sink(0.5) is False

    def test_wpctl_set_volume_default_sink_uses_alias(self):
        from nativmix.audio.pipewire_native import _wpctl_set_volume_default_sink
        captured = {}
        def mock_run(args, **kw):
            captured["args"] = args
            return MagicMock(returncode=0)
        with patch("shutil.which", return_value="/usr/bin/wpctl"), \
             patch("subprocess.run", mock_run):
            _wpctl_set_volume_default_sink(0.6)
        assert captured["args"][2] == "@DEFAULT_AUDIO_SINK@"

    def test_wpctl_set_volume_default_source_uses_alias(self):
        from nativmix.audio.pipewire_native import _wpctl_set_volume_default_source
        captured = {}
        def mock_run(args, **kw):
            captured["args"] = args
            return MagicMock(returncode=0)
        with patch("shutil.which", return_value="/usr/bin/wpctl"), \
             patch("subprocess.run", mock_run):
            _wpctl_set_volume_default_source(0.4)
        assert captured["args"][2] == "@DEFAULT_AUDIO_SOURCE@"

    def test_wpctl_set_mute_returns_false_when_missing(self):
        from nativmix.audio.pipewire_native import _wpctl_set_mute
        with patch("shutil.which", return_value=None):
            assert _wpctl_set_mute(1, True) is False

    def test_wpctl_set_mute_passes_correct_value(self):
        from nativmix.audio.pipewire_native import _wpctl_set_mute
        captured = {}
        def mock_run(args, **kw):
            captured["args"] = args
            return MagicMock(returncode=0)
        with patch("shutil.which", return_value="/usr/bin/wpctl"), \
             patch("subprocess.run", mock_run):
            _wpctl_set_mute(5, True)
        assert captured["args"] == ["wpctl", "set-mute", "5", "1"]


# ---------------------------------------------------------------------------
# _probe_capabilities — wpctl path
# ---------------------------------------------------------------------------

class TestProbeCapabilitiesWpctl:
    """Verify wpctl probe sets can_set_volume_pw and wpctl_available."""

    def test_wpctl_available_and_can_set_volume_pw_when_wpctl_succeeds(self):
        from nativmix.audio.pipewire_native import _probe_capabilities
        mock_run = MagicMock(return_value=MagicMock(returncode=0))

        def _which(tool):
            return f"/usr/bin/{tool}" if tool == "wpctl" else None

        with patch("shutil.which", side_effect=_which), \
             patch("subprocess.run", mock_run):
            caps = _probe_capabilities()

        assert caps["wpctl_available"] is True
        assert caps["can_set_volume_pw"] is True

    def test_wpctl_not_available_when_missing(self):
        from nativmix.audio.pipewire_native import _probe_capabilities
        with patch("shutil.which", return_value=None):
            caps = _probe_capabilities()
        assert caps["wpctl_available"] is False

    def test_wpctl_preferred_over_pw_cli_for_can_set_volume_pw(self):
        """When wpctl succeeds, pw-cli probe is skipped (already True)."""
        from nativmix.audio.pipewire_native import _probe_capabilities
        mock_run = MagicMock(return_value=MagicMock(returncode=0))

        def _which(tool):
            # Both available
            return f"/usr/bin/{tool}" if tool in ("wpctl", "pw-cli") else None

        with patch("shutil.which", side_effect=_which), \
             patch("subprocess.run", mock_run):
            caps = _probe_capabilities()

        assert caps["can_set_volume_pw"] is True
        assert caps["wpctl_available"] is True


# ---------------------------------------------------------------------------
# _apply_volume_by_name — PW-native backend selection
# ---------------------------------------------------------------------------

@_SKIP_NO_PULSECTL
class TestApplyVolumeByNamePWBackend:
    """
    Verify _apply_volume_by_name routes writes through PW-native path
    (wpctl) when can_set_volume_pw is True and node data is available,
    and uses PA compat as fallback.
    """

    def _make_manager(self, tmp_path: Path):
        from nativmix.audio.manager import PipeWireManager
        from nativmix.utils.config_manager import ConfigManager
        cfg = ConfigManager(
            config_path=tmp_path / "config.json",
            profiles_dir=tmp_path / "profiles",
        )
        mgr = PipeWireManager.__new__(PipeWireManager)
        mgr._config = cfg
        mgr._state_lock = threading.Lock()
        mgr._poti_volumes = {}
        mgr._channel_muted = {}
        mgr._vsink_creating = set()
        mgr._pw_nodes = {}
        mgr._pw_nodes_lock = threading.Lock()
        mgr._stable_ids = {}
        mgr.can_set_volume_pw = True
        mgr.can_set_volume = True
        mgr.wpctl_available = True
        mgr.pw_cli_available = False
        return mgr

    def _make_si(self, index, props=None):
        si = MagicMock()
        si.index = index
        si.proplist = props or {}
        return si

    def test_system_master_uses_wpctl_when_pw_path_active(self, tmp_path):
        """system master write goes through wpctl when can_set_volume_pw=True."""
        mgr = self._make_manager(tmp_path)
        pulse = MagicMock()

        with patch("nativmix.audio.manager._wpctl_set_volume_default_sink", return_value=True) as mock_wpctl:
            mgr._apply_volume_by_name("System Master", 0.7, pulse=pulse)

        mock_wpctl.assert_called_once_with(0.7)
        # PA sink write should NOT be called since wpctl succeeded
        pulse.volume_set_all_chans.assert_not_called()

    def test_system_master_falls_back_to_pa_when_wpctl_fails(self, tmp_path):
        """When wpctl fails, system master falls back to PA."""
        mgr = self._make_manager(tmp_path)
        pulse = MagicMock()
        mock_sink = MagicMock()
        pulse.server_info.return_value = MagicMock(default_sink_name="default_sink")
        pulse.get_sink_by_name.return_value = mock_sink

        with patch("nativmix.audio.manager._wpctl_set_volume_default_sink", return_value=False):
            mgr._apply_volume_by_name("System Master", 0.6, pulse=pulse)

        pulse.volume_set_all_chans.assert_called_once_with(mock_sink, 0.6)

    def test_app_stream_uses_wpctl_when_pw_node_available(self, tmp_path):
        """App stream write uses wpctl when PW node is known."""
        from nativmix.audio.pipewire_native import PipeWireNode
        mgr = self._make_manager(tmp_path)

        pw_node = PipeWireNode(
            node_id=99, client_id=0, app_name="Spotify", process_binary="spotify",
            media_name="", media_class="Stream/Output/Audio", app_id="",
            props={"object.serial": "99"},
        )
        with mgr._pw_nodes_lock:
            mgr._pw_nodes = {99: pw_node}

        si = self._make_si(10, {
            "application.name": "Spotify",
            "application.process.id": "0",
            "object.serial": "99",
        })
        pulse = MagicMock()
        pulse.sink_input_list.return_value = [si]

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Spotify"), \
             patch("nativmix.audio.manager._wpctl_set_volume", return_value=True) as mock_wpctl:
            mgr._apply_volume_by_name("Spotify", 0.8, pulse=pulse)

        mock_wpctl.assert_called_once_with(99, 0.8)
        # PA write not needed since wpctl succeeded
        pulse.volume_set_all_chans.assert_not_called()

    def test_app_stream_falls_back_to_pa_when_pw_write_fails(self, tmp_path):
        """When both wpctl and pw-cli fail, PA compat is used for the stream."""
        from nativmix.audio.pipewire_native import PipeWireNode
        mgr = self._make_manager(tmp_path)

        pw_node = PipeWireNode(
            node_id=55, client_id=0, app_name="Firefox", process_binary="firefox",
            media_name="", media_class="Stream/Output/Audio", app_id="",
            props={"object.serial": "55"},
        )
        with mgr._pw_nodes_lock:
            mgr._pw_nodes = {55: pw_node}

        si = self._make_si(20, {
            "application.name": "Firefox",
            "application.process.id": "0",
            "object.serial": "55",
        })
        pulse = MagicMock()
        pulse.sink_input_list.return_value = [si]

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Firefox"), \
             patch("nativmix.audio.manager._wpctl_set_volume", return_value=False), \
             patch("nativmix.audio.manager._pw_set_volume", return_value=False):
            mgr._apply_volume_by_name("Firefox", 0.5, pulse=pulse)

        pulse.volume_set_all_chans.assert_called_once_with(si, 0.5)

    def test_vsink_volume_prefers_wpctl_owned_sink(self, tmp_path):
        """V-Sink volume prefers wpctl on the owned sink before PA fallback."""
        mgr = self._make_manager(tmp_path)
        pulse = MagicMock()
        sink = MagicMock()
        sink.index = 321
        pulse.get_sink_by_name.return_value = sink

        with patch("nativmix.audio.manager._wpctl_set_volume_exact", return_value=True) as mock_wpctl:
            mgr._set_v_sink_volume(2, 0.4, pulse=pulse)

        mock_wpctl.assert_called_once_with("321", 0.4)
        pulse.volume_set_all_chans.assert_not_called()

    def test_vsink_volume_falls_back_to_pa_when_wpctl_owned_sink_fails(self, tmp_path):
        """V-Sink volume falls back to PA when wpctl on the owned sink fails."""
        mgr = self._make_manager(tmp_path)
        pulse = MagicMock()
        sink = MagicMock()
        sink.index = 654
        pulse.get_sink_by_name.return_value = sink

        with patch("nativmix.audio.manager._wpctl_set_volume_exact", return_value=False):
            mgr._set_v_sink_volume(1, 0.55, pulse=pulse)

        pulse.volume_set_all_chans.assert_called_once_with(sink, 0.55)

    def test_flatpak_unresolved_target_skips_pa_fallback(self, tmp_path):
        """Flatpak unresolved targets do not retry PA sink-input writes."""
        from nativmix.audio.pipewire_native import PipeWireNode
        mgr = self._make_manager(tmp_path)
        mgr._unresolved_targets = {"Spotify"}

        pw_node = PipeWireNode(
            node_id=77, client_id=0, app_name="Spotify", process_binary="spotify",
            media_name="", media_class="Stream/Output/Audio", app_id="",
            props={"object.serial": "77"},
        )
        with mgr._pw_nodes_lock:
            mgr._pw_nodes = {77: pw_node}

        si = self._make_si(12, {
            "application.name": "Spotify",
            "application.process.id": "0",
            "object.serial": "77",
        })
        pulse = MagicMock()
        pulse.sink_input_list.return_value = [si]

        with patch("nativmix.audio.manager.IS_FLATPAK", True), \
             patch("nativmix.audio.manager.resolve_app_name", return_value="Spotify"), \
             patch("nativmix.audio.manager._wpctl_set_volume", return_value=False), \
             patch("nativmix.audio.manager._pw_set_volume", return_value=False):
            mgr._apply_volume_by_name("Spotify", 0.8, pulse=pulse)

        pulse.volume_set_all_chans.assert_not_called()

    def test_seamless_move_skips_pactl_under_flatpak(self, tmp_path):
        """Flatpak hard guard disables pactl moves in seamless routing."""
        mgr = self._make_manager(tmp_path)
        pulse = MagicMock()

        with patch("nativmix.audio.manager.IS_FLATPAK", True), \
             patch("nativmix.audio.manager.subprocess.run") as mock_run:
            mgr._seamless_move(pulse, 9, 99, volume=1.0)

        mock_run.assert_not_called()
        pulse.sink_input_mute.assert_not_called()

    def test_sink_input_failure_is_throttled(self, tmp_path, caplog):
        """Repeated PA sink-input failures emit throttled (not per-call) warnings."""
        import logging, pulsectl
        mgr = self._make_manager(tmp_path)
        mgr.can_set_volume_pw = False  # Force PA path

        si = self._make_si(77, {"application.name": "Spotify", "application.process.id": "0"})
        pulse = MagicMock()
        pulse.sink_input_list.return_value = [si]
        pulse.volume_set_all_chans.side_effect = pulsectl.PulseError("set-volume", 1)

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Spotify"), \
             patch("nativmix.audio.manager._throttled_warner") as mock_warner:
            mgr._apply_volume_by_name("Spotify", 0.5, pulse=pulse)
            mgr._apply_volume_by_name("Spotify", 0.6, pulse=pulse)
            mgr._apply_volume_by_name("Spotify", 0.7, pulse=pulse)

        # _throttled_warner.warn should be called, not direct logger.warning
        assert mock_warner.warn.called


# ---------------------------------------------------------------------------
# Flatpak mode: no code path invokes pactl move-sink-input
# ---------------------------------------------------------------------------

@_SKIP_NO_PULSECTL
class TestFlatpakNoPactlMoveSinkInput:
    """
    Regression suite: when IS_FLATPAK is True no code path in the manager
    should invoke ``subprocess.run`` with the ``move-sink-input`` command.

    Each test exercises a different entry point that previously contained a
    direct ``pactl move-sink-input`` call.  All must complete without touching
    ``subprocess.run``.
    """

    # ------------------------------------------------------------------
    # Shared fixtures
    # ------------------------------------------------------------------

    @staticmethod
    def _make_manager(tmp_path: Path):
        from nativmix.audio.manager import PipeWireManager
        from nativmix.utils.config_manager import ConfigManager
        d = tmp_path / "profiles"
        d.mkdir()
        config = ConfigManager(profiles_dir=str(d))
        mgr = PipeWireManager(config=config)
        mgr.can_set_volume_pw = True
        mgr.can_set_volume = True
        mgr.can_move_stream = True
        return mgr

    @staticmethod
    def _make_si(index: int, props: dict):
        si = MagicMock()
        si.index = index
        si.sink = 0
        si.proplist = props
        vol = MagicMock()
        vol.values = [1.0]
        si.volume = vol
        return si

    # ------------------------------------------------------------------
    # move_stream_to_vsink (the new central function)
    # ------------------------------------------------------------------

    def test_move_stream_to_vsink_no_subprocess_in_flatpak(self, tmp_path):
        """move_stream_to_vsink must not call subprocess.run under Flatpak."""
        from nativmix.audio import manager as mgr_mod

        pulse = MagicMock()
        with patch("nativmix.audio.manager.IS_FLATPAK", True), \
             patch("nativmix.audio.manager.subprocess.run") as mock_run:
            result = mgr_mod.move_stream_to_vsink(42, "NativMix_CH_0", pulse)

        mock_run.assert_not_called()
        assert result is False

    # ------------------------------------------------------------------
    # _seamless_move (delegates to move_stream_to_vsink)
    # ------------------------------------------------------------------

    def test_seamless_move_no_subprocess_in_flatpak(self, tmp_path):
        """_seamless_move must not call subprocess.run under Flatpak."""
        mgr = self._make_manager(tmp_path)
        pulse = MagicMock()
        pulse.sink_input_list.return_value = []

        with patch("nativmix.audio.manager.IS_FLATPAK", True), \
             patch("nativmix.audio.manager.subprocess.run") as mock_run:
            mgr._seamless_move(pulse, 7, 99, volume=1.0)

        mock_run.assert_not_called()
        pulse.sink_input_mute.assert_not_called()

    # ------------------------------------------------------------------
    # _apply_auto_reconnect path (stream event routing)
    # ------------------------------------------------------------------

    def test_apply_auto_reconnect_no_subprocess_in_flatpak(self, tmp_path):
        """Stream auto-reconnect must not call subprocess.run under Flatpak."""
        from nativmix.audio.manager import StreamInfo

        mgr = self._make_manager(tmp_path)
        mgr._config.set_app_names(0, ["spotify"])
        mgr._config.set_v_sink_enabled(0, True)

        info = StreamInfo(
            index=5,
            app_name="spotify",
            volume=1.0,
            muted=False,
            pid=1234,
            proplist={},
        )

        pulse = MagicMock()
        pulse.get_sink_by_name.return_value = MagicMock(name="NativMix_CH_0", index=10)
        pulse.sink_input_info.return_value = self._make_si(5, {})

        with patch("nativmix.audio.manager.IS_FLATPAK", True), \
             patch("nativmix.audio.manager.subprocess.run") as mock_run, \
             patch("nativmix.audio.manager.resolve_app_name", return_value="spotify"):
            mgr._apply_auto_reconnect(pulse, info)

        mock_run.assert_not_called()

    # ------------------------------------------------------------------
    # on_mapping_changed "added apps" path
    # ------------------------------------------------------------------

    def test_on_mapping_changed_added_no_subprocess_in_flatpak(self, tmp_path):
        """on_mapping_changed must not call subprocess.run for newly-added apps under Flatpak."""
        import pulsectl

        mgr = self._make_manager(tmp_path)
        mgr._config.set_v_sink_enabled(0, True)

        si = self._make_si(3, {"application.name": "firefox", "application.process.id": "999"})
        target_sink = MagicMock()
        target_sink.index = 20

        pulse_ctx = MagicMock()
        pulse_ctx.__enter__ = MagicMock(return_value=pulse_ctx)
        pulse_ctx.__exit__ = MagicMock(return_value=False)
        pulse_ctx.sink_input_list.return_value = [si]
        pulse_ctx.get_sink_by_name.return_value = target_sink

        with patch("nativmix.audio.manager.IS_FLATPAK", True), \
             patch("nativmix.audio.manager.subprocess.run") as mock_run, \
             patch("nativmix.audio.manager.resolve_app_name", return_value="firefox"), \
             patch("nativmix.audio.manager.pulsectl.Pulse", return_value=pulse_ctx):
            mgr.on_mapping_changed(0, ["firefox"])

        mock_run.assert_not_called()

    # ------------------------------------------------------------------
    # Startup self-check passes cleanly after centralisation
    # ------------------------------------------------------------------

    def test_startup_routing_self_check_no_legacy_callsites(self, tmp_path, caplog):
        """After centralisation the self-check must find no legacy call-sites."""
        import logging

        mgr = self._make_manager(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nativmix.audio.manager"):
            mgr._startup_routing_self_check()

        legacy_warnings = [
            r for r in caplog.records
            if "legacy direct" in r.message and r.levelno >= logging.WARNING
        ]
        assert legacy_warnings == [], (
            "Unexpected legacy pactl move-sink-input call-sites detected: "
            + str([r.message for r in legacy_warnings])
        )


# ---------------------------------------------------------------------------
# _normalize_name — PR-39
# ---------------------------------------------------------------------------

class TestNormalizeName:
    """Verify _normalize_name strips launcher suffixes and normalizes case."""

    def _norm(self, s):
        from nativmix.audio.pipewire_native import _normalize_name
        return _normalize_name(s)

    def test_lowercase(self):
        assert self._norm("Spotify") == "spotify"

    def test_strips_wayland_suffix(self):
        assert self._norm("spotify-wayland") == "spotify"

    def test_strips_x11_suffix(self):
        assert self._norm("chromium-x11") == "chromium"

    def test_strips_bin_suffix(self):
        assert self._norm("firefox-bin") == "firefox"

    def test_strips_desktop_suffix(self):
        assert self._norm("vlc.desktop") == "vlc"

    def test_strips_trailing_whitespace(self):
        assert self._norm("  spotify  ") == "spotify"

    def test_no_false_strip_mid_word(self):
        # "-wayland" only stripped from suffix, not mid-word
        assert self._norm("wayland-browser") == "wayland-browser"

    def test_empty_string(self):
        assert self._norm("") == ""

    def test_case_insensitive_suffix(self):
        # suffix matching is after lowercasing, so WAYLAND → wayland → stripped
        assert self._norm("Spotify-WAYLAND".lower()) == "spotify"


# ---------------------------------------------------------------------------
# _matches_node — node_name field and normalization (PR-39)
# ---------------------------------------------------------------------------

class TestMatchesNodeNodeName:
    """Verify _matches_node handles node_name field and normalization."""

    def _match(self, node, target, node_ids=None, client_ids=None):
        from nativmix.audio.pipewire_native import _matches_node
        return _matches_node(node, target,
                             stable_node_ids=node_ids,
                             stable_client_ids=client_ids)

    def test_node_name_exact_match(self):
        node = _make_pw_node(node_name="spotify-output")
        assert self._match(node, "spotify-output") is True

    def test_node_name_case_insensitive(self):
        node = _make_pw_node(node_name="SPOTIFY-OUTPUT")
        assert self._match(node, "spotify-output") is True

    def test_node_name_normalized_wayland(self):
        # node.name = "spotify-wayland" should match target "spotify"
        node = _make_pw_node(node_name="spotify-wayland", app_name="")
        assert self._match(node, "spotify") is True

    def test_app_name_normalized_wayland(self):
        node = _make_pw_node(app_name="Spotify-wayland")
        assert self._match(node, "spotify") is True

    def test_binary_normalized_bin(self):
        node = _make_pw_node(process_binary="firefox-bin")
        assert self._match(node, "firefox") is True

    def test_media_name_normalized_desktop(self):
        node = _make_pw_node(media_name="vlc.desktop")
        assert self._match(node, "vlc") is True

    def test_node_name_contains_fallback(self):
        node = _make_pw_node(node_name="org.spotify.client-output")
        assert self._match(node, "spotify") is True

    def test_node_name_does_not_match_unrelated(self):
        node = _make_pw_node(node_name="firefox-output", app_name="Firefox")
        assert self._match(node, "spotify") is False


# ---------------------------------------------------------------------------
# _pw_dump_nodes — node_name field populated (PR-39)
# ---------------------------------------------------------------------------

class TestPwDumpNodesNodeName:
    def test_node_name_extracted_from_props(self):
        from nativmix.audio.pipewire_native import _pw_dump_nodes
        import json
        fake_json = json.dumps([{
            "id": 5,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Stream/Output/Audio",
                    "application.name": "Spotify",
                    "node.name": "spotify-output-node",
                    "application.process.binary": "spotify",
                    "media.name": "",
                    "client.id": "10",
                }
            }
        }])
        with patch("shutil.which", return_value="/usr/bin/pw-dump"), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=fake_json)):
            nodes = _pw_dump_nodes()
        assert len(nodes) == 1
        assert nodes[0].node_name == "spotify-output-node"
