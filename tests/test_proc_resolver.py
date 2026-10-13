"""Tests for process-name resolution helpers."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from nativmix.utils import proc_resolver as pr
from nativmix.utils.proc_resolver import (
    GENERIC_PA_NAMES,
    _extract_flag,
    _match_user_data_dir,
    _resolve_via_path_hints,
    is_generic_pa_name,
    resolve_app_name,
)


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
