import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import make_profile, write_profile  # noqa: E402

import nativmix.hardware.midi as midi
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


def test_ensure_midi_backend_always_prefers_rtmidi(monkeypatch):
    selected: list[str] = []
    monkeypatch.setattr(midi, "_set_rtmidi_backend", lambda: selected.append("rtmidi"))
    monkeypatch.setattr(midi, "_set_portmidi_backend", lambda: selected.append("portmidi"))

    assert midi.ensure_midi_backend() == "rtmidi"
    assert selected == ["rtmidi"]


def test_native_linux_uses_explicit_portmidi_fallback(monkeypatch, caplog):
    selected: list[str] = []

    def missing_rtmidi() -> None:
        raise ImportError("rtmidi missing")

    monkeypatch.setattr(midi, "IS_FLATPAK", False)
    monkeypatch.setattr(midi.sys, "platform", "linux")
    monkeypatch.setattr(midi, "_set_rtmidi_backend", missing_rtmidi)
    monkeypatch.setattr(midi, "_set_portmidi_backend", lambda: selected.append("portmidi"))

    with caplog.at_level(logging.WARNING, logger=midi.logger.name):
        assert midi.ensure_midi_backend() == "portmidi"

    assert selected == ["portmidi"]
    assert "USB hot-unplug is unsafe with PortMidi" in caplog.text


def test_flatpak_missing_rtmidi_never_opens_portmidi(monkeypatch):
    portmidi_calls: list[str] = []

    def missing_rtmidi() -> None:
        raise ImportError("rtmidi missing")

    monkeypatch.setattr(midi, "IS_FLATPAK", True)
    monkeypatch.setattr(midi.sys, "platform", "linux")
    monkeypatch.setattr(midi, "_set_rtmidi_backend", missing_rtmidi)
    monkeypatch.setattr(midi, "_set_portmidi_backend", lambda: portmidi_calls.append("portmidi"))

    assert midi.ensure_midi_backend() is None
    assert portmidi_calls == []


def test_flatpak_missing_rtmidi_emits_critical_status(monkeypatch):
    thread = midi.MidiThread(input_mode="midi_only")
    thread._running = True
    states: list[bool] = []
    statuses: list[tuple[str, str]] = []
    thread.connection_changed.connect(states.append)
    thread.status_changed.connect(lambda kind, message: statuses.append((kind, message)))

    monkeypatch.setattr(midi, "IS_FLATPAK", True)
    monkeypatch.setattr(midi, "ensure_midi_backend", lambda: None)
    monkeypatch.setattr(thread, "_sleep_checked", lambda _seconds: setattr(thread, "_running", False))

    thread._run_safe()

    assert states == [False]
    assert statuses == [("error_critical", "RtMidi is required for Flatpak MIDI.")]


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


class _FakeInputPort:
    def __init__(self, receive) -> None:
        self._receive = receive
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.closed = True

    def receive(self, block: bool = False):
        assert block is False
        return self._receive()


class _FakeOutputPort:
    def __init__(self, send) -> None:
        self._send = send
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.closed = True

    def send(self, message) -> None:
        self._send(message)


def test_receive_disconnect_closes_retries_and_reconnects(monkeypatch):
    thread = midi.MidiThread(device_name="Controller", input_mode="midi_only")
    thread._running = True
    first = _FakeInputPort(lambda: (_ for _ in ()).throw(OSError("device removed")))

    def stop_after_reconnect():
        thread._running = False
        return None

    second = _FakeInputPort(stop_after_reconnect)
    ports = iter((first, second))
    states: list[bool] = []
    statuses: list[tuple[str, str]] = []
    thread.connection_changed.connect(states.append)
    thread.status_changed.connect(lambda kind, message: statuses.append((kind, message)))

    monkeypatch.setattr(midi, "ensure_midi_backend", lambda: "rtmidi")
    monkeypatch.setattr(midi.mido, "get_input_names", lambda: ["Controller 20:0"])
    monkeypatch.setattr(midi.mido, "open_input", lambda _name: next(ports))
    monkeypatch.setattr(thread, "_sleep_checked", lambda _seconds: None)

    thread._run_safe()

    assert first.closed is True
    assert second.closed is True
    assert states == [True, False, True]
    assert statuses.count(("error_temporary", "MIDI Disconnected - Retrying...")) == 1


def test_feedback_disconnect_closes_retries_and_reconnects(monkeypatch):
    thread = midi.MidiThread(device_name="Controller", input_mode="midi_only")
    thread._running = True
    thread.set_fader_feedback_enabled(True)
    thread.update_mappings({(0, 7): 0})
    thread._queue_fader_sync([(0, 0.5)])
    first_input = _FakeInputPort(lambda: None)

    def stop_after_reconnect():
        thread._running = False
        return None

    second_input = _FakeInputPort(stop_after_reconnect)
    first_output = _FakeOutputPort(lambda _message: (_ for _ in ()).throw(OSError("output removed")))
    sent = []
    second_output = _FakeOutputPort(sent.append)
    inputs = iter((first_input, second_input))
    outputs = iter((first_output, second_output))
    states: list[bool] = []
    thread.connection_changed.connect(states.append)

    monkeypatch.setattr(midi, "ensure_midi_backend", lambda: "rtmidi")
    monkeypatch.setattr(midi.mido, "get_input_names", lambda: ["Controller 20:0"])
    monkeypatch.setattr(midi.mido, "get_output_names", lambda: ["Controller 20:0"])
    monkeypatch.setattr(midi.mido, "open_input", lambda _name: next(inputs))
    monkeypatch.setattr(midi.mido, "open_output", lambda _name: next(outputs))
    monkeypatch.setattr(thread, "_sleep_checked", lambda _seconds: None)

    thread._run_safe()

    assert first_input.closed is True
    assert first_output.closed is True
    assert second_input.closed is True
    assert second_output.closed is True
    assert states == [True, False, True]
    assert [(message.control, message.value) for message in sent] == [(7, 64)]


def test_feedback_disconnect_is_requeued_for_reconnect():
    thread = midi.MidiThread(device_name="Controller", input_mode="midi_only")
    thread._running = True
    thread.set_fader_feedback_enabled(True)
    thread.update_mappings({(0, 7): 0})
    thread._queue_fader_sync([(0, 0.5)])

    class _FailingOutput:
        def send(self, _message) -> None:
            raise OSError("output removed")

    with pytest.raises(OSError, match="output removed"):
        thread._device_loop(_FakeInputPort(lambda: None), _FailingOutput(), "Controller")

    assert thread._pending_sync == [(0, 0.5)]


def test_connection_disconnect_signal_is_emitted_once():
    thread = midi.MidiThread()
    states: list[bool] = []
    thread.connection_changed.connect(states.append)

    thread._set_connection_state(True)
    thread._set_connection_state(False)
    thread._set_connection_state(False)

    assert states == [True, False]


# ---------------------------------------------------------------------------
# Startup MIDI status: refresh_layout() must NOT start MIDI thread early
# ---------------------------------------------------------------------------

try:
    import PyQt6  # noqa: F401
    _PYQT6_OK = True
except ImportError:
    _PYQT6_OK = False


@pytest.mark.skipif(not _PYQT6_OK, reason="PyQt6 not available")
def test_refresh_layout_does_not_start_midi_thread(tmp_config_path, tmp_profiles_dir, qtbot):
    """refresh_layout() must not call MidiThread.start() during MainWindow.__init__().

    Starting the MIDI thread before main.py wires up status_changed → settings panel
    causes status signals to be emitted with no listener, leaving the GUI permanently
    showing 'MIDI: Offline' on startup in midi_only mode.
    """
    from PyQt6.QtCore import pyqtSignal

    from nativmix.audio.base import AudioBackendBase
    from nativmix.gui.main_window import MainWindow
    from nativmix.hardware.midi import MidiThread
    from nativmix.utils.config_manager import ConfigManager

    # Minimal config in midi_only mode
    tmp_config_path.write_text(json.dumps({
        "version": 7,
        "hardware": {
            "port": None,
            "auto_search_device": True,
            "num_channels": 5,
            "input_mode": "midi_only",
            "midi_device": "",
            "midi_channel_count": 5,
            "baud_rate": 9600,
        },
        "settings": {
            "threshold": 0.01,
            "transparency": False,
            "compact_mode": False,
            "stay_open": False,
            "show_invert_option": False,
            "debug_logging": False,
            "midi_fader_feedback": False,
        },
    }))

    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)

    # Minimal backend stub with the required signals
    class _StubBackend(AudioBackendBase):
        mute_state_changed  = pyqtSignal(int, bool)
        channel_volume_changed = pyqtSignal(int, float)
        other_apps_changed  = pyqtSignal(list)
        audit_finished      = pyqtSignal()

        def start(self) -> None: pass
        def stop(self)  -> None: pass
        def get_real_sinks(self): return []
        def get_active_streams_debug(self): return []

    backend = _StubBackend()

    midi_thread = MidiThread(device_name="", input_mode="midi_only")
    assert not midi_thread.isRunning(), "Precondition: thread not yet started"

    # MainWindow.__init__() calls refresh_layout() — this must NOT start the thread.
    _window = MainWindow(config=config, backend=backend, midi_thread=midi_thread)

    assert not midi_thread.isRunning(), (
        "refresh_layout() must not start the MIDI thread before signal connections "
        "are made; premature start causes status_changed to fire with no listener, "
        "leaving the GUI permanently showing 'MIDI: Offline'."
    )
