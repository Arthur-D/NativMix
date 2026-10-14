import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from nativmix.hardware.midi_throttle import CcVolumeThrottler


def test_throttler_emits_trailing_value():
    t = CcVolumeThrottler(0.02)
    assert t.note((0, 7), 1, 0.1, 0.0) == [(1, 0.1)]
    assert t.note((0, 7), 1, 0.5, 0.005) is None
    assert t.note((0, 7), 1, 0.9, 0.010) is None
    assert t.flush_due(0.025) == [(1, 0.9)]
    assert t.flush_due(0.030) == []


def test_throttler_emits_immediately_outside_window():
    t = CcVolumeThrottler(0.02)
    assert t.note((0, 7), 1, 0.1, 0.0) == [(1, 0.1)]
    assert t.note((0, 7), 1, 0.2, 0.025) == [(1, 0.2)]


def test_throttler_tracks_bindings_independently():
    t = CcVolumeThrottler(0.02)
    assert t.note((0, 7), 1, 0.1, 0.0) == [(1, 0.1)]
    assert t.note((1, 8), 2, 0.3, 0.0) == [(2, 0.3)]
    # Second binding's burst is still pending while the first is not due again.
    assert t.note((0, 7), 1, 0.4, 0.005) is None
    assert t.note((1, 8), 2, 0.6, 0.005) is None
    assert t.flush_due(0.02) == [(1, 0.4), (2, 0.6)]


def test_flush_due_without_pending_returns_empty():
    t = CcVolumeThrottler(0.02)
    assert t.flush_due(1.0) == []


def test_midi_trailing_volume_keeps_local_origin_and_clears_on_remap(monkeypatch):
    from nativmix.hardware import midi

    thread = midi.MidiThread(input_mode="midi_only")
    thread._active_generation = 7
    thread.update_mappings({(0, 7): 0})
    clock = [1.0]
    monkeypatch.setattr(midi.time, "monotonic", lambda: clock[0])
    requested = []
    thread.local_volume_requested.connect(requested.append)
    thread._handle_cc(0, 7, 32)
    clock[0] = 1.005
    thread._handle_cc(0, 7, 64)
    assert len(requested) == 1
    clock[0] = 1.03
    thread._flush_due_cc_volumes()
    assert len(requested) == 2
    channel, volume, origin = requested[-1]
    assert channel == 0
    assert volume == 64 / 127
    assert origin.generation == 7
    assert origin.raw_cc_value == 64
    assert origin.requested_volume == volume
    clock[0] = 1.035
    thread._handle_cc(0, 7, 100)
    thread.update_mappings({(0, 7): 1})
    clock[0] = 1.06
    thread._flush_due_cc_volumes()
    assert len(requested) == 2
