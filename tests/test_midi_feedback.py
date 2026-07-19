import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import make_profile, write_profile  # noqa: E402

import nativmix.hardware.midi as midi
from nativmix.hardware.midi import _FADER_FEEDBACK_TOLERANCE, _inbound_fader_suppressed


def test_inbound_fader_suppressed_no_takeover():
    assert _inbound_fader_suppressed(None, 64) is False


def test_inbound_fader_suppressed_within_tolerance():
    takeover = 0.5
    cc_value = int(round(takeover * 127))
    assert _inbound_fader_suppressed(takeover, cc_value) is True


def test_inbound_fader_suppressed_outside_tolerance():
    takeover = 0.5
    cc_value = int(round((takeover + _FADER_FEEDBACK_TOLERANCE + 0.01) * 127))
    assert _inbound_fader_suppressed(takeover, cc_value) is False


def test_get_midi_fader_feedback_targets(tmp_config_path, tmp_profiles_dir):
    from nativmix.utils.config_manager import ConfigManager

    channels = make_profile(channel_count=2)["channels"]
    channels[1]["is_midi"] = True
    channels[1]["midi_cc"] = 7
    channels[1]["volume"] = 0.25
    profile = make_profile(channel_count=2, channels=channels)
    write_profile(tmp_profiles_dir, profile)

    tmp_config_path.write_text(
        json.dumps(
            {
                "version": 7,
                "active_profile": profile["id"],
                "hardware": {
                    "port": None,
                    "auto_search_device": True,
                    "num_channels": 2,
                    "input_mode": "hybrid",
                    "midi_device": "",
                    "midi_channel_count": 1,
                    "baud_rate": 9600,
                },
                "settings": {
                    "threshold": 0.01,
                    "transparency": True,
                    "compact_mode": False,
                    "stay_open": False,
                    "show_invert_option": False,
                    "debug_logging": False,
                    "midi_fader_feedback": False,
                },
            }
        )
    )

    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    config.apply_profile(profile)
    assert config.get_midi_fader_feedback_targets() == [(1, pytest.approx(0.25))]


def test_load_portmidi_library_prefers_find_library_result(monkeypatch):
    attempts: list[str] = []

    def fake_find_library(name: str):
        assert name == "portmidi"
        return "libportmidi-discovered.so"

    monkeypatch.setattr(midi.ctypes.util, "find_library", fake_find_library)

    def fake_cdll(candidate: str):
        attempts.append(candidate)
        if candidate == "libportmidi-discovered.so":
            return object()
        raise OSError(candidate)

    monkeypatch.setattr(midi.ctypes, "CDLL", fake_cdll)

    midi._load_portmidi_library()

    assert attempts == ["libportmidi-discovered.so"]


def test_load_portmidi_library_falls_back_to_sonames(monkeypatch):
    attempts: list[str] = []

    def fake_find_library(name: str):
        assert name == "portmidi"
        return None

    monkeypatch.setattr(midi.ctypes.util, "find_library", fake_find_library)

    def fake_cdll(candidate: str):
        attempts.append(candidate)
        if candidate == "libportmidi.so.0":
            return object()
        raise OSError(candidate)

    monkeypatch.setattr(midi.ctypes, "CDLL", fake_cdll)

    midi._load_portmidi_library()

    assert attempts == ["libportmidi.so", "libportmidi.so.0"]
