"""
Tests for proc_resolver.resolve_binary_name() and the Flatpak-aware
stream-info building path.

These tests do NOT require pulsectl or /proc access and are safe to run
in any CI environment.
"""
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from nativmix.utils import proc_resolver as pr
from nativmix.utils.proc_resolver import (
    GENERIC_PA_NAMES,
    _BINARY_MAP,
    _FLATPAK_APP_MAP,
    _extract_flag,
    _match_user_data_dir,
    _resolve_via_path_hints,
    is_generic_pa_name,
    resolve_app_name,
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

def test_is_generic_pa_name_webrtc_spacing() -> None:
    assert is_generic_pa_name("WEBRTC VoiceEngine")
    assert is_generic_pa_name("WEBRTC Voice Engine")
    assert is_generic_pa_name("chromium")
    assert not is_generic_pa_name("Discord")
    assert "webrtc voiceengine" in GENERIC_PA_NAMES


def test_match_user_data_dir_spotify() -> None:
    assert _match_user_data_dir("/home/user/.config/spotify") == "Spotify"


def test_match_user_data_dir_rekord() -> None:
    assert _match_user_data_dir(r"C:\Users\x\AppData\Roaming\RE-KORD") == "RE-KORD"
    assert _match_user_data_dir("/home/user/.config/rekord") == "RE-KORD"


def test_match_user_data_dir_canary_before_discord() -> None:
    assert _match_user_data_dir("/home/user/.config/discordcanary") == "Discord Canary"
    assert _match_user_data_dir("/home/user/.config/discord") == "Discord"


def test_match_user_data_dir_unknown_returns_none() -> None:
    assert _match_user_data_dir("/tmp/random-app-data") is None


def test_extract_flag_app_id() -> None:
    args = ["electron", "--app-id=com.spotify.client", "--some-flag"]
    pattern = re.compile(r"--app-id=([^\s]+)")
    assert _extract_flag(args, pattern) == "com.spotify.client"


def test_extract_flag_rekord_app_id() -> None:
    args = ["RE-KORD.exe", "--app-id=com.rekord.app"]
    pattern = re.compile(r"--app-id=([^\s]+)")
    assert _extract_flag(args, pattern) == "com.rekord.app"


def test_path_has_config_fragment_boundaries() -> None:
    assert pr._path_has_config_fragment("/home/u/.config/discord/Cookies", "discord")
    assert not pr._path_has_config_fragment("/home/u/.config/discordcanary/x", "discord")
    assert pr._path_has_config_fragment("/home/u/.config/discordcanary/x", "discordcanary")


def test_path_hints_discord_via_open_fd(monkeypatch) -> None:
    """WEBRTC-style helpers keep Discord identity via ~/.config/discord FDs."""
    provider = MagicMock()
    provider.check_fd_for_path.return_value = False
    provider.check_fd_for_config_fragment.side_effect = lambda _pid, frag: frag == "discord"
    monkeypatch.setattr(pr, "_proc_provider", provider)

    name = _resolve_via_path_hints(
        4242,
        ["/usr/lib/chromium/chrome", "--type=utility"],
    )
    assert name == "Discord"


def test_path_hints_vesktop_via_cmdline(monkeypatch) -> None:
    provider = MagicMock()
    provider.check_fd_for_path.return_value = False
    provider.list_fd_targets.return_value = []
    monkeypatch.setattr(pr, "_proc_provider", provider)

    name = _resolve_via_path_hints(
        1,
        ["/opt/vesktop/vesktop", "--type=renderer"],
    )
    assert name == "Vesktop"


def test_path_hints_prefers_canary_over_discord(monkeypatch) -> None:
    provider = MagicMock()
    provider.check_fd_for_path.return_value = False
    provider.list_fd_targets.return_value = []
    monkeypatch.setattr(pr, "_proc_provider", provider)

    name = _resolve_via_path_hints(
        1,
        ["electron", "--user-data-dir=/home/x/.config/discordcanary"],
    )
    assert name == "Discord Canary"


def test_resolve_app_name_webrtc_fallback_via_fd(monkeypatch) -> None:
    """Parent walk + FD hint turns WEBRTC VoiceEngine fallback into Discord."""
    provider = MagicMock()
    provider.get_cmdline.return_value = [b"/usr/lib/chromium/chrome", b"--type=utility"]
    provider.get_environ.return_value = {"HOME": "/home/user"}
    provider.get_ppid.return_value = None
    provider.get_flatpak_app_id.return_value = None
    provider.check_fd_for_path.return_value = False
    provider.check_fd_for_config_fragment.side_effect = lambda _pid, frag: frag == "discord"
    monkeypatch.setattr(pr, "_proc_provider", provider)
    resolve_app_name.cache_clear()

    assert resolve_app_name(9999, fallback="WEBRTC VoiceEngine") == "Discord"
