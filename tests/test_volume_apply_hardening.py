"""
Tests for hardened sink-input matching and volume-apply robustness.

Covers:
- _is_internal_stream correctly rejects loopback/monitor streams.
- _matches_app_name priority order: application.name → media.name → resolved.
- Multiple sink-inputs with the same application.name all receive the volume change.
- A loopback stream is never chosen for a normal app target.
- One failing volume_set_all_chans does not abort applying to other valid matches.
- Per-ID warning diagnostics are emitted on failure.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

try:
    import pulsectl  # noqa: F401
    _PULSECTL_OK = True
except Exception:
    _PULSECTL_OK = False

_SKIP_NO_PULSECTL = pytest.mark.skipif(
    not _PULSECTL_OK, reason="pulsectl / libpulse not available"
)


def _make_si(index: int, props: dict | None = None) -> MagicMock:
    """Return a minimal fake pulsectl sink_input object."""
    si = MagicMock()
    si.index = index
    si.proplist = props if props is not None else {}
    return si


# ---------------------------------------------------------------------------
# _is_internal_stream — stream exclusion filter
# ---------------------------------------------------------------------------

@_SKIP_NO_PULSECTL
class TestIsInternalStream:
    """Verify loopback/monitor/NativMix streams are excluded."""

    def _check(self, props: dict) -> bool:
        from nativmix.audio.manager import _is_internal_stream
        return _is_internal_stream(props)

    def test_loopback_application_name_excluded(self):
        assert self._check({"application.name": "loopback-2799-13 output"}) is True

    def test_loopback_media_name_excluded(self):
        assert self._check({"media.name": "loopback stream"}) is True

    def test_monitor_media_class_excluded(self):
        assert self._check({"media.class": "Audio/Monitor", "application.name": "SomeApp"}) is True

    def test_nativmix_stream_excluded(self):
        assert self._check({"application.name": "NativMix_CH_2"}) is True

    def test_spotify_not_excluded(self):
        assert self._check({"application.name": "Spotify"}) is False

    def test_firefox_not_excluded(self):
        assert self._check({"application.name": "Firefox"}) is False

    def test_empty_props_not_excluded(self):
        assert self._check({}) is False


# ---------------------------------------------------------------------------
# _matches_app_name — priority-based sink-input matching
# ---------------------------------------------------------------------------

@_SKIP_NO_PULSECTL
class TestMatchesAppName:
    """Verify matching priority: application.name > media.name > resolved."""

    def _match(self, props: dict, resolved: str, target: str) -> bool:
        from nativmix.audio.manager import _matches_app_name
        return _matches_app_name(props, resolved, target)

    def test_exact_application_name_match(self):
        props = {"application.name": "Spotify", "media.name": "other"}
        assert self._match(props, "other", "Spotify") is True

    def test_application_name_case_insensitive(self):
        props = {"application.name": "SPOTIFY"}
        assert self._match(props, "other", "spotify") is True

    def test_media_name_match_when_no_application_name(self):
        props = {"media.name": "Spotify"}
        assert self._match(props, "other", "Spotify") is True

    def test_media_name_case_insensitive(self):
        props = {"media.name": "SPOTIFY"}
        assert self._match(props, "other", "spotify") is True

    def test_resolved_fallback_match(self):
        props = {"application.name": "chromium", "media.name": "WebRTC Audio"}
        assert self._match(props, "Discord", "Discord") is True

    def test_no_match(self):
        props = {"application.name": "Firefox", "media.name": "YouTube video"}
        assert self._match(props, "Firefox", "Spotify") is False

    def test_application_name_takes_priority_over_media_name(self):
        """If application.name matches, return True without checking media.name."""
        props = {"application.name": "Spotify", "media.name": "Firefox"}
        assert self._match(props, "Firefox", "Spotify") is True

    def test_application_name_does_not_match_but_media_name_does(self):
        props = {"application.name": "some_binary", "media.name": "Spotify"}
        assert self._match(props, "other", "Spotify") is True

    def test_empty_application_name_falls_through(self):
        props = {"application.name": "", "media.name": "Spotify"}
        assert self._match(props, "other", "Spotify") is True

    def test_loopback_name_does_not_match_spotify(self):
        props = {"application.name": "loopback-2799-13 output", "media.name": ""}
        assert self._match(props, "loopback-2799-13 output", "Spotify") is False


# ---------------------------------------------------------------------------
# _apply_volume_by_name — robustness tests (require pulsectl mock)
# ---------------------------------------------------------------------------

@_SKIP_NO_PULSECTL
class TestApplyVolumeByName:
    """
    Test _apply_volume_by_name through PipeWireManager with a mocked Pulse
    connection.  These tests verify per-sink-input error isolation and
    diagnostics without a real PipeWire/PulseAudio session.
    """

    def _make_manager(self, tmp_path: Path):
        """Return a PipeWireManager with a minimal mocked config."""
        import threading
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
        return mgr

    def _make_pulse_mock(self, sink_inputs):
        """Return a mock pulsectl.Pulse context manager with given sink_inputs."""
        pulse = MagicMock()
        pulse.sink_input_list.return_value = sink_inputs
        return pulse

    def test_multiple_firefox_tabs_all_receive_volume(self, tmp_path):
        """All Firefox sink-inputs must receive the volume update."""
        mgr = self._make_manager(tmp_path)

        _ff_props = {"application.name": "Firefox", "application.process.id": "0"}
        si1 = _make_si(101, _ff_props.copy())
        si2 = _make_si(102, _ff_props.copy())
        si3 = _make_si(103, _ff_props.copy())

        pulse = self._make_pulse_mock([si1, si2, si3])

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Firefox"):
            mgr._apply_volume_by_name("Firefox", 0.7, pulse=pulse)

        assert pulse.volume_set_all_chans.call_count == 3
        pulse.volume_set_all_chans.assert_any_call(si1, 0.7)
        pulse.volume_set_all_chans.assert_any_call(si2, 0.7)
        pulse.volume_set_all_chans.assert_any_call(si3, 0.7)

    def test_loopback_stream_not_matched_for_spotify(self, tmp_path):
        """Loopback streams are rejected by _is_internal_stream before matching.

        Real PipeWire loopback streams (as seen via ``pactl list sink-inputs``)
        report the loopback identifier in ``media.name`` and have no
        ``application.name``.  _is_internal_stream checks both properties so
        the stream must be filtered before it can reach the matching logic.
        """
        mgr = self._make_manager(tmp_path)

        # Mirrors a real loopback stream: media.name contains 'loopback',
        # application.name is absent.
        loopback = _make_si(1, {"media.name": "loopback-2799-13 output", "application.process.id": "0"})
        spotify = _make_si(2, {"application.name": "Spotify", "application.process.id": "0"})

        pulse = self._make_pulse_mock([loopback, spotify])

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Spotify"):
            mgr._apply_volume_by_name("Spotify", 0.9, pulse=pulse)

        # Only spotify should receive the volume change, not loopback
        assert pulse.volume_set_all_chans.call_count == 1
        pulse.volume_set_all_chans.assert_called_once_with(spotify, 0.9)

    def test_one_failing_sink_input_does_not_abort_others(self, tmp_path):
        """If volume_set_all_chans raises for one SI, the rest still get updated."""
        import pulsectl

        mgr = self._make_manager(tmp_path)

        _ff_props = {"application.name": "Firefox", "application.process.id": "0"}
        si1 = _make_si(101, _ff_props.copy())
        si2 = _make_si(102, _ff_props.copy())
        si3 = _make_si(103, _ff_props.copy())

        pulse = self._make_pulse_mock([si1, si2, si3])

        # Make si2 fail with a PulseError
        def _side_effect(si, vol):
            if si.index == 102:
                raise pulsectl.PulseError("set-volume", 5)
        pulse.volume_set_all_chans.side_effect = _side_effect

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Firefox"):
            mgr._apply_volume_by_name("Firefox", 0.6, pulse=pulse)

        # All three should have been attempted
        assert pulse.volume_set_all_chans.call_count == 3

    def test_failure_warning_logged_per_id(self, tmp_path, caplog):
        """A WARNING is logged for each sink-input that fails."""
        import logging
        import pulsectl

        mgr = self._make_manager(tmp_path)

        si1 = _make_si(200, {"application.name": "Spotify", "application.process.id": "0"})

        pulse = self._make_pulse_mock([si1])
        pulse.volume_set_all_chans.side_effect = pulsectl.PulseError("set-volume", 5)

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Spotify"):
            with caplog.at_level(logging.WARNING, logger="nativmix.audio.manager"):
                mgr._apply_volume_by_name("Spotify", 0.8, pulse=pulse)

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("200" in msg for msg in warning_messages), (
            f"Expected warning mentioning sink-input #200, got: {warning_messages}"
        )

    def test_debug_log_emitted_for_matched_ids(self, tmp_path, caplog):
        """A DEBUG message listing matched sink-input IDs is emitted."""
        import logging

        mgr = self._make_manager(tmp_path)

        si1 = _make_si(301, {"application.name": "Spotify", "application.process.id": "0"})
        pulse = self._make_pulse_mock([si1])

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Spotify"):
            with caplog.at_level(logging.DEBUG, logger="nativmix.audio.manager"):
                mgr._apply_volume_by_name("Spotify", 0.5, pulse=pulse)

        debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("301" in msg for msg in debug_messages), (
            f"Expected debug message mentioning sink-input #301, got: {debug_messages}"
        )
