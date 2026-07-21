"""
Tests for proc_resolver.resolve_binary_name() and the Flatpak-aware
stream-info building path.

These tests do NOT require pulsectl or /proc access and are safe to run
in any CI environment.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from nativmix.utils.proc_resolver import (
    _BINARY_MAP,
    _FLATPAK_APP_MAP,
    resolve_binary_name,
)


# ---------------------------------------------------------------------------
# resolve_binary_name — no /proc required
# ---------------------------------------------------------------------------

class TestResolveBinaryName:
    def test_known_binary_returns_display_name(self):
        assert resolve_binary_name("firefox") == "Firefox"

    def test_vlc(self):
        assert resolve_binary_name("vlc") == "VLC"

    def test_spotify(self):
        assert resolve_binary_name("spotify") == "Spotify"

    def test_discord(self):
        assert resolve_binary_name("discord") == "Discord"

    def test_case_insensitive(self):
        assert resolve_binary_name("Firefox") == "Firefox"
        assert resolve_binary_name("SPOTIFY") == "Spotify"

    def test_unknown_binary_returns_none(self):
        assert resolve_binary_name("unknown-app-xyz") is None

    def test_empty_string_returns_none(self):
        assert resolve_binary_name("") is None

    def test_aur_binary_variants(self):
        # AUR/alternative package names should map to canonical display names
        assert resolve_binary_name("spotify-bin") == "Spotify"
        assert resolve_binary_name("brave-bin") == "Brave Browser"

    def test_all_mapped_binaries_roundtrip(self):
        """Every binary in _BINARY_MAP should be resolvable via resolve_binary_name."""
        for binary, expected in _BINARY_MAP.items():
            result = resolve_binary_name(binary)
            assert result == expected, f"resolve_binary_name({binary!r}) → {result!r}, expected {expected!r}"


# ---------------------------------------------------------------------------
# _build_stream_info — binary map takes priority when proc is unavailable
# ---------------------------------------------------------------------------

# Skip tests that need pulsectl if the library is absent.
try:
    import pulsectl  # noqa: F401
    _PULSECTL_OK = True
except Exception:
    _PULSECTL_OK = False


def _make_si(props: dict, index: int = 1, volume: float = 0.8, muted: bool = False):
    """Build a minimal fake pulsectl sink-input object."""
    si = MagicMock()
    si.index = index
    si.mute = int(muted)
    si.volume.values = [volume]
    si.proplist = props
    return si


@pytest.mark.skipif(not _PULSECTL_OK, reason="pulsectl / libpulse not available")
class TestBuildStreamInfoFlatpakFallback:
    """
    Verify that _build_stream_info correctly resolves app names from the
    application.process.binary property even when /proc access fails —
    the Flatpak-in-sandbox scenario.
    """

    def _build(self, props: dict):
        from nativmix.audio.manager import _AudioListenerThread
        si = _make_si(props)
        # Patch resolve_app_name to always return the fallback (simulate /proc failure)
        with patch(
            "nativmix.audio.manager.resolve_app_name",
            side_effect=lambda pid, fallback="Unknown": fallback,
        ):
            return _AudioListenerThread._build_stream_info(si)

    def test_binary_map_applied_via_process_binary(self):
        """application.process.binary in _BINARY_MAP → display name used."""
        info = self._build({
            "application.process.binary": "firefox",
            "application.name": "Firefox",  # PA also provides a good name here
            "application.process.id": "0",
        })
        assert info is not None
        assert info.app_name == "Firefox"

    def test_binary_map_takes_priority_over_generic_pa_name(self):
        """
        When application.name is generic but application.process.binary is known,
        the binary-mapped name is preferred over the raw PA name.
        """
        info = self._build({
            "application.process.binary": "vlc",
            "application.name": "ALSA plug-in [vlc]",  # ugly PA name
            "application.process.id": "0",
        })
        assert info is not None
        assert info.app_name == "VLC", (
            "Binary-map lookup should give the clean display name, not the raw PA name"
        )

    def test_unknown_binary_falls_back_to_application_name(self):
        """Unknown binary → fall back to application.name."""
        info = self._build({
            "application.process.binary": "my-custom-app",
            "application.name": "My Custom App",
            "application.process.id": "0",
        })
        assert info is not None
        assert info.app_name == "My Custom App"

    def test_no_binary_falls_back_to_application_name(self):
        """Missing binary property → fall back to application.name."""
        info = self._build({
            "application.name": "SomeApp",
            "application.process.id": "0",
        })
        assert info is not None
        assert info.app_name == "SomeApp"

    def test_mpv_binary_resolves_correctly(self):
        """mpv sets application.name to 'mpv Media Player' but binary is 'mpv'."""
        info = self._build({
            "application.process.binary": "mpv",
            "application.name": "mpv Media Player",
            "application.process.id": "0",
        })
        assert info is not None
        assert info.app_name == "mpv"
