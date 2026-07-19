import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import make_profile, write_profile  # noqa: E402

import nativmix.hardware.midi as midi
import nativmix.utils.distro as distro
from nativmix.hardware.midi import _FADER_FEEDBACK_TOLERANCE, _inbound_fader_suppressed


class _FakePortMidiFunction:
    def __init__(self) -> None:
        self.restype = None
        self.argtypes = None


class _FakePortMidiLibrary:
    def __getattr__(self, name: str):
        func = _FakePortMidiFunction()
        setattr(self, name, func)
        return func


@pytest.fixture
def reset_portmidi_cache(monkeypatch):
    monkeypatch.setattr(midi._PORTMIDI, "handle", None)
    monkeypatch.setattr(midi._PORTMIDI, "candidate", None)
    monkeypatch.setattr(midi._PORTMIDI, "failure_reported", False)


@pytest.fixture
def isolated_mido_portmidi_init_module():
    original_module = sys.modules.pop("mido.backends.portmidi_init", None)
    try:
        yield
    finally:
        sys.modules.pop("mido.backends.portmidi_init", None)
        if original_module is not None:
            sys.modules["mido.backends.portmidi_init"] = original_module


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


def test_load_portmidi_library_prefers_find_library_result(monkeypatch, reset_portmidi_cache):
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


def test_load_portmidi_library_tries_versioned_soname_when_find_library_returns_none(
    monkeypatch,
    reset_portmidi_cache,
):
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

    assert attempts == ["libportmidi.so.0"]


def test_load_portmidi_library_deduplicates_candidates(monkeypatch, reset_portmidi_cache):
    attempts: list[str] = []

    monkeypatch.setattr(midi.ctypes.util, "find_library", lambda name: "libportmidi.so.0")

    def fake_cdll(candidate: str):
        attempts.append(candidate)
        return object()

    monkeypatch.setattr(midi.ctypes, "CDLL", fake_cdll)

    midi._load_portmidi_library()

    assert attempts == ["libportmidi.so.0"]


def test_load_portmidi_library_reuses_cached_handle(monkeypatch, reset_portmidi_cache):
    attempts: list[str] = []
    handle = object()

    monkeypatch.setattr(midi.ctypes.util, "find_library", lambda name: "libportmidi.so.0")

    def fake_cdll(candidate: str):
        attempts.append(candidate)
        return handle

    monkeypatch.setattr(midi.ctypes, "CDLL", fake_cdll)

    assert midi._load_portmidi_library() is handle
    assert midi._load_portmidi_library() is handle
    assert attempts == ["libportmidi.so.0"]


def test_prime_mido_portmidi_init_module_reuses_cached_handle(
    monkeypatch,
    reset_portmidi_cache,
    isolated_mido_portmidi_init_module,
):
    attempts: list[str] = []
    handle = _FakePortMidiLibrary()

    monkeypatch.setattr(midi.ctypes.util, "find_library", lambda name: "libportmidi.so.0")

    def fake_cdll(candidate: str):
        attempts.append(candidate)
        if candidate == "libportmidi.so.0":
            return handle
        raise OSError(candidate)

    monkeypatch.setattr(midi.ctypes, "CDLL", fake_cdll)
    midi._prime_mido_portmidi_init_module()

    assert attempts == ["libportmidi.so.0"]
    assert sys.modules["mido.backends.portmidi_init"].lib is handle
    assert sys.modules["mido.backends.portmidi_init"].dll_name == "libportmidi.so.0"


def test_ensure_midi_backend_prefers_portmidi_on_fedora(monkeypatch, reset_portmidi_cache):
    monkeypatch.setattr(distro, "is_fedora", lambda: True)

    portmidi_set_calls: list[str] = []
    handle = _FakePortMidiLibrary()

    monkeypatch.setattr(midi.ctypes.util, "find_library", lambda name: "libportmidi.so.0")
    monkeypatch.setattr(midi.ctypes, "CDLL", lambda candidate: handle)
    monkeypatch.setattr(midi.mido, "set_backend", lambda name: portmidi_set_calls.append(name))

    assert midi.ensure_midi_backend() == "portmidi"
    assert portmidi_set_calls == ["mido.backends.portmidi"]


def test_load_portmidi_warns_once_on_failure(monkeypatch, caplog, reset_portmidi_cache):
    monkeypatch.setattr(midi.ctypes.util, "find_library", lambda name: "libportmidi.so.0")

    def fake_cdll(candidate: str):
        raise OSError(f"missing:{candidate}")

    monkeypatch.setattr(midi.ctypes, "CDLL", fake_cdll)

    with caplog.at_level(logging.DEBUG, logger=midi.logger.name):
        for _ in range(2):
            with pytest.raises(ImportError):
                midi._load_portmidi_library()

    warning_records = [record for record in caplog.records if record.levelno == logging.WARNING]
    debug_messages = [record.getMessage() for record in caplog.records if record.levelno == logging.DEBUG]
    expected_so0_msg = "PortMidi candidate load failed: libportmidi.so.0 (missing:libportmidi.so.0)"
    expected_so_msg = "PortMidi candidate load failed: libportmidi.so (missing:libportmidi.so)"

    assert len(warning_records) == 1
    assert "Unable to load PortMidi library; attempted=" in warning_records[0].getMessage()
    assert "libportmidi.so.0" in warning_records[0].getMessage()
    assert "libportmidi.so" in warning_records[0].getMessage()
    assert "last_error=missing:libportmidi.so" in warning_records[0].getMessage()
    assert debug_messages.count(expected_so0_msg) == 2
    assert debug_messages.count(expected_so_msg) == 2
