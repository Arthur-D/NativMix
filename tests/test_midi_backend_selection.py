"""Backend selection tests with no native MIDI clients or hardware access."""

import logging
import sys
from types import SimpleNamespace

import pytest

import nativmix.hardware.midi as midi


@pytest.fixture
def fake_rtmidi(monkeypatch):
    probes = []
    opened = []
    state = SimpleNamespace(jack_error=None, devices=[])

    def get_devices(api=None):
        probes.append(api)
        if api == "UNIX_JACK" and state.jack_error is not None:
            raise state.jack_error
        return state.devices

    def open_port(name, **kwargs):
        opened.append((name, kwargs["api"]))
        return SimpleNamespace(name=name)

    module = SimpleNamespace(get_devices=get_devices, Input=open_port, Output=open_port)
    monkeypatch.setitem(sys.modules, "rtmidi", SimpleNamespace())
    monkeypatch.setattr(midi.mido.Backend, "load", lambda self: setattr(self, "_module", module))
    monkeypatch.setattr(midi, "_RTMIDI_BACKEND", None)
    monkeypatch.setattr(midi.sys, "platform", "linux")
    # set_backend replaces the top-level functions; restore every one afterward.
    for name, value in vars(midi.mido).copy().items():
        if name == "backend" or name.startswith(("open_", "get_")):
            monkeypatch.setattr(midi.mido, name, value)
    return state, probes, opened


def test_linux_jack_enumerates_and_opens_widi(fake_rtmidi):
    state, probes, opened = fake_rtmidi
    state.devices = [
        {"name": "WIDI Uhost:out", "is_input": True, "is_output": False},
        {"name": "WIDI Uhost:in", "is_input": False, "is_output": True},
    ]

    assert midi.ensure_midi_backend() == "rtmidi"
    assert probes == ["UNIX_JACK"]
    assert midi.mido.get_input_names() == ["WIDI Uhost:out"]
    assert midi.mido.get_output_names() == ["WIDI Uhost:in"]
    midi.mido.open_input("WIDI Uhost:out")
    midi.mido.open_output("WIDI Uhost:in")
    assert opened == [("WIDI Uhost:out", "UNIX_JACK"), ("WIDI Uhost:in", "UNIX_JACK")]


@pytest.mark.parametrize("flatpak", [False, True])
@pytest.mark.parametrize("error", [ValueError("API not compiled in"), RuntimeError("server unavailable"),
                                   OSError("connection failed"), ImportError("JACK missing")])
def test_unusable_jack_falls_back_to_rtmidi_alsa(fake_rtmidi, monkeypatch, caplog, flatpak, error):
    state, probes, opened = fake_rtmidi
    state.jack_error = error
    monkeypatch.setattr(midi, "IS_FLATPAK", flatpak)

    with caplog.at_level(logging.INFO, logger=midi.logger.name):
        assert midi.ensure_midi_backend() == "rtmidi"

    assert probes == ["UNIX_JACK"]
    assert midi.mido.backend.api == "LINUX_ALSA"
    midi.mido.open_input("USB controller")
    assert opened == [("USB controller", "LINUX_ALSA")]
    assert "falling back to RtMidi/ALSA" in caplog.text


def test_empty_jack_inventory_keeps_jack_for_later_hotplug(fake_rtmidi):
    assert midi.ensure_midi_backend() == "rtmidi"
    assert midi.mido.backend.api == "UNIX_JACK"


@pytest.mark.parametrize("jack_available", [False, True])
def test_settings_refresh_does_not_reprobe_or_switch_api(fake_rtmidi, jack_available):
    state, probes, _opened = fake_rtmidi
    if not jack_available:
        state.jack_error = RuntimeError("server unavailable")
    midi.ensure_midi_backend()
    selected = midi.mido.backend
    state.jack_error = None if not jack_available else RuntimeError("server unavailable")

    midi.ensure_midi_backend()

    assert midi.mido.backend is selected
    assert probes == ["UNIX_JACK"]


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_non_linux_keeps_default_rtmidi_api(fake_rtmidi, monkeypatch, platform):
    _state, probes, _opened = fake_rtmidi
    monkeypatch.setattr(midi.sys, "platform", platform)

    assert midi.ensure_midi_backend() == "rtmidi"
    assert midi.mido.backend.name == "mido.backends.rtmidi"
    assert midi.mido.backend.api is None
    assert probes == []
