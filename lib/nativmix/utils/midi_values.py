"""Canonical conversion between normalized volume and 7-bit MIDI CC values."""

from __future__ import annotations

import math


def clamp_midi_cc(value: int) -> int:
    """Clamp an integer to the MIDI 7-bit value range."""
    return max(0, min(127, int(value)))


def midi_cc_to_volume(value: int) -> float:
    """Convert a 7-bit MIDI CC value to a normalized volume."""
    return clamp_midi_cc(value) / 127.0


def volume_to_midi_cc(volume: float) -> int:
    """Quantize normalized volume to MIDI CC using deterministic half-up rounding."""
    normalized = max(0.0, min(1.0, float(volume)))
    return clamp_midi_cc(math.floor(normalized * 127.0 + 0.5))


def is_same_origin_midi_acknowledgement(requested_volume: float, canonical_volume: float) -> bool:
    """Compare validated same-origin acknowledgements within one CC, not general volume changes."""
    return abs(volume_to_midi_cc(canonical_volume) - volume_to_midi_cc(requested_volume)) <= 1
