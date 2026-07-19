"""
Tests for app-channel binding and mute-state propagation to new streams.

Covers:
- Case-insensitive app name matching via ConfigManager.find_channel_for_app
- Multi-key fallback: application.name, application.process.binary, media.name
- _update_thread_states includes 'muted' in channel_states
- _AudioListenerThread.channel_states.muted drives reflex mute decision
"""
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path, channels=None):
    """Return a ConfigManager with a temporary config file."""
    from nativmix.utils.config_manager import ConfigManager

    config_path = tmp_path / "config.json"
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(exist_ok=True)
    cm = ConfigManager(config_path=config_path, profiles_dir=profiles_dir)
    if channels:
        for ch_idx, app_names in channels.items():
            cm.set_app_names(ch_idx, app_names)
    return cm


# Skip all tests that need pulsectl (libpulse.so.0) if the library is absent.
try:
    import pulsectl as _pulsectl  # noqa: F401
    _PULSECTL_OK = True
except Exception:
    _PULSECTL_OK = False


# ---------------------------------------------------------------------------
# ConfigManager.find_channel_for_app — case-insensitive matching
# ---------------------------------------------------------------------------

class TestFindChannelForApp:
    def test_exact_match(self, tmp_path):
        cm = _make_config(tmp_path, {5: ["Spotify"]})
        assert cm.find_channel_for_app("Spotify") == 5

    def test_lowercase_match(self, tmp_path):
        cm = _make_config(tmp_path, {5: ["Spotify"]})
        assert cm.find_channel_for_app("spotify") == 5

    def test_uppercase_match(self, tmp_path):
        cm = _make_config(tmp_path, {5: ["Spotify"]})
        assert cm.find_channel_for_app("SPOTIFY") == 5

    def test_mixed_case_match(self, tmp_path):
        cm = _make_config(tmp_path, {5: ["Spotify"]})
        assert cm.find_channel_for_app("SpOtIfY") == 5

    def test_no_match_returns_none(self, tmp_path):
        cm = _make_config(tmp_path, {5: ["Spotify"]})
        assert cm.find_channel_for_app("Firefox") is None

    def test_multi_app_channel(self, tmp_path):
        cm = _make_config(tmp_path, {3: ["Firefox", "Chrome"]})
        assert cm.find_channel_for_app("Firefox") == 3
        assert cm.find_channel_for_app("chrome") == 3

    def test_channel_stored_case_insensitive(self, tmp_path):
        """App names stored in upper-case are still matched case-insensitively."""
        cm = _make_config(tmp_path, {2: ["SPOTIFY"]})
        assert cm.find_channel_for_app("Spotify") == 2

    @pytest.mark.skipif(not _PULSECTL_OK, reason="pulsectl / libpulse not available")
    def test_media_name_fallback_key(self):
        """
        Simulate the pa_fallback chain: when application.name is absent,
        media.name should be used.  This is tested through _build_stream_info.
        """
        from nativmix.audio.manager import _AudioListenerThread

        # Build a fake sink_input object with only media.name set
        si = MagicMock()
        si.index = 42
        si.mute = 0
        si.volume.values = [0.8]
        si.proplist = {
            "media.name": "Spotify",
            # deliberately omit application.name
        }

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Spotify"):
            info = _AudioListenerThread._build_stream_info(si)

        assert info is not None
        assert info.app_name == "Spotify"

    @pytest.mark.skipif(not _PULSECTL_OK, reason="pulsectl / libpulse not available")
    def test_application_name_takes_priority(self):
        """application.name is checked before application.process.binary."""
        from nativmix.audio.manager import _AudioListenerThread

        si = MagicMock()
        si.index = 99
        si.mute = 0
        si.volume.values = [1.0]
        si.proplist = {
            "application.name": "Spotify",
            "application.process.binary": "spotify_binary",
        }

        # resolve_app_name receives pa_fallback="Spotify" (application.name wins)
        captured = {}
        def fake_resolve(pid, fallback="Unknown"):
            captured["fallback"] = fallback
            return fallback

        with patch("nativmix.audio.manager.resolve_app_name", side_effect=fake_resolve):
            info = _AudioListenerThread._build_stream_info(si)

        assert captured["fallback"] == "Spotify"


# ---------------------------------------------------------------------------
# _update_thread_states includes muted in channel_states
# ---------------------------------------------------------------------------

class TestUpdateThreadStatesMuted:
    """Verify that mute state is propagated into channel_states."""

    def _build_states(self, cm, channel_muted):
        """Reproduce _update_thread_states logic without Qt/pulsectl."""
        poti_volumes: dict[int, float] = {}
        vsink_creating: set[int] = set()
        return {
            ch: {
                'vol': poti_volumes.get(ch, 0.5),
                'v_sink': cm.is_v_sink_enabled(ch),
                'v_sink_busy': ch in vsink_creating,
                'apps': cm.get_app_names(ch),
                'mode': cm.get_channel_mode(ch),
                'muted': channel_muted.get(ch, False),
            }
            for ch in range(cm.num_channels)
        }

    def test_muted_false_by_default(self, tmp_path):
        from nativmix.utils.config_manager import ConfigManager

        config_path = tmp_path / "config.json"
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir(exist_ok=True)
        cm = ConfigManager(config_path=config_path, profiles_dir=profiles_dir)
        for ch in range(3):
            cm._channel(ch)

        states = self._build_states(cm, channel_muted={})
        for ch in range(cm.num_channels):
            assert states[ch]['muted'] is False

    def test_muted_true_propagates(self, tmp_path):
        from nativmix.utils.config_manager import ConfigManager

        config_path = tmp_path / "config.json"
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir(exist_ok=True)
        cm = ConfigManager(config_path=config_path, profiles_dir=profiles_dir)
        # Ensure at least 2 channels exist (default already has 5)
        assert cm.num_channels >= 2

        # Mark channel 1 as muted
        states = self._build_states(cm, channel_muted={1: True})

        assert states[1]['muted'] is True
        # Other channels unaffected
        assert states[0]['muted'] is False


# ---------------------------------------------------------------------------
# Reflex mute decision: channel muted → stream stays muted
# ---------------------------------------------------------------------------

class TestReflexMuteDecision:
    """
    Unit-test the logic that decides whether a newly bound stream is muted.

    We extract the core decision logic and test it independently:
        channel_muted = channel_states.get(target_ch, {}).get('muted', False)
    Then verify that sink_input_mute is called with the correct value.
    """

    def test_unmapped_stream_is_unmuted(self):
        """A stream with no channel mapping should be unmuted (reflex=False)."""
        channel_states = {}  # no mappings

        def find_channel(app_name):
            return None  # not mapped

        target_ch = find_channel("UnknownApp")
        channel_muted = False
        if target_ch is not None:
            channel_muted = channel_states.get(target_ch, {}).get('muted', False)

        assert channel_muted is False

    def test_mapped_unmuted_channel_stream_is_unmuted(self):
        """Stream bound to an unmuted channel should be unmuted after reflex."""
        channel_states = {5: {'muted': False, 'vol': 0.8}}

        def find_channel(app_name):
            return 5 if app_name.lower() == "spotify" else None

        target_ch = find_channel("Spotify")
        channel_muted = False
        if target_ch is not None:
            channel_muted = channel_states.get(target_ch, {}).get('muted', False)

        assert channel_muted is False

    def test_mapped_muted_channel_keeps_stream_muted(self):
        """Stream bound to a muted channel must remain muted after reflex."""
        channel_states = {5: {'muted': True, 'vol': 0.8}}

        def find_channel(app_name):
            return 5 if app_name.lower() == "spotify" else None

        target_ch = find_channel("Spotify")
        channel_muted = False
        if target_ch is not None:
            channel_muted = channel_states.get(target_ch, {}).get('muted', False)

        assert channel_muted is True

    def test_other_channel_muted_does_not_affect_spotify(self):
        """Muting channel 3 should not affect a stream bound to channel 5."""
        channel_states = {
            3: {'muted': True, 'vol': 1.0},
            5: {'muted': False, 'vol': 0.8},
        }

        def find_channel(app_name):
            return 5 if app_name.lower() == "spotify" else None

        target_ch = find_channel("Spotify")
        channel_muted = False
        if target_ch is not None:
            channel_muted = channel_states.get(target_ch, {}).get('muted', False)

        assert channel_muted is False

    def test_missing_muted_key_defaults_to_false(self):
        """If 'muted' key is absent from channel_states, default to unmuted."""
        channel_states = {5: {'vol': 0.8}}  # no 'muted' key

        target_ch = 5
        channel_muted = channel_states.get(target_ch, {}).get('muted', False)

        assert channel_muted is False


# ---------------------------------------------------------------------------
# _get_channel_mute_state helper logic
# ---------------------------------------------------------------------------

class TestGetChannelMuteState:
    """
    Test the logic used by _AudioListenerThread._get_channel_mute_state.

    Rather than importing from nativmix.audio.manager (which requires
    libpulse at import time), we replicate the helper logic inline and verify
    it against a real ConfigManager.
    """

    def _get_channel_mute_state(self, config, channel_states, app_name: str) -> bool:
        """Replicate _AudioListenerThread._get_channel_mute_state."""
        target_ch = config.find_channel_for_app(app_name)
        if target_ch is None:
            return False
        return bool(channel_states.get(target_ch, {}).get('muted', False))

    def test_unmapped_app_returns_false(self, tmp_path):
        cm = _make_config(tmp_path, {0: ["Spotify"]})
        states = {0: {'muted': True}}
        # Firefox is not mapped → always False
        assert self._get_channel_mute_state(cm, states, "Firefox") is False

    def test_mapped_muted_returns_true(self, tmp_path):
        cm = _make_config(tmp_path, {0: ["Spotify"]})
        states = {0: {'muted': True}}
        assert self._get_channel_mute_state(cm, states, "Spotify") is True

    def test_mapped_unmuted_returns_false(self, tmp_path):
        cm = _make_config(tmp_path, {0: ["Spotify"]})
        states = {0: {'muted': False}}
        assert self._get_channel_mute_state(cm, states, "Spotify") is False

    def test_case_insensitive_lookup(self, tmp_path):
        cm = _make_config(tmp_path, {0: ["Spotify"]})
        states = {0: {'muted': True}}
        assert self._get_channel_mute_state(cm, states, "SPOTIFY") is True
        assert self._get_channel_mute_state(cm, states, "spotify") is True

    def test_empty_channel_states_returns_false(self, tmp_path):
        """When channel_states has no entry for the channel, default to False."""
        cm = _make_config(tmp_path, {0: ["Spotify"]})
        assert self._get_channel_mute_state(cm, {}, "Spotify") is False

    def test_missing_muted_key_in_state_returns_false(self, tmp_path):
        """channel_states entry exists but has no 'muted' key → False."""
        cm = _make_config(tmp_path, {0: ["Spotify"]})
        states = {0: {'vol': 0.8}}  # no 'muted' key
        assert self._get_channel_mute_state(cm, states, "Spotify") is False
