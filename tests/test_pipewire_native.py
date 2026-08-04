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
