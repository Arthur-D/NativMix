"""Stable MIDI port identity helpers shared by the worker, config, and GUI."""

from __future__ import annotations

import re

_ALSA_ADDRESS_RE = re.compile(r"\s+\d+:\d+\s*$")
_DISCONNECTED_SUFFIX_RE = re.compile(r"\s+\(Disconnected\)\s*$", re.IGNORECASE)
_JACK_DIRECTION_RE = re.compile(r":(?:in|out)$", re.IGNORECASE)
_JACK_BRIDGE_DIRECTION_RE = re.compile(r"\s+\((?:capture|playback)\)$", re.IGNORECASE)


def normalize_midi_device_name(name: str) -> str:
    """Remove UI-only and backend-specific ALSA/JACK qualifiers from a port name."""
    normalized = _DISCONNECTED_SUFFIX_RE.sub("", str(name).strip())
    normalized = _ALSA_ADDRESS_RE.sub("", normalized).strip()
    # PipeWire Bluetooth MIDI uses <device>:out / <device>:in. Its ALSA
    # bridge wraps the existing ALSA identity with a client and direction.
    if normalized.startswith("Midi-Bridge:"):
        normalized = _JACK_BRIDGE_DIRECTION_RE.sub("", normalized[len("Midi-Bridge:"):])
    normalized = _JACK_DIRECTION_RE.sub("", normalized)
    if ":" in normalized:
        client_name, port_name = normalized.split(":", 1)
        port_name = port_name.strip()
        if port_name and port_name.casefold().startswith(client_name.strip().casefold()):
            normalized = port_name
    return " ".join(normalized.split())


def midi_device_key(name: str) -> str:
    """Return a comparison key that is stable across MIDI backends and reconnects."""
    return normalize_midi_device_name(name).casefold()


def match_midi_port(names: list[str], configured_name: str) -> str | None:
    """Resolve a stable configured name to the current backend's raw port name."""
    target_key = midi_device_key(configured_name)
    if not target_key:
        return None

    for name in names:
        if midi_device_key(name) == target_key:
            return name

    # JACK's ALSA bridge can separate the client and port with a colon where
    # RtMidi/ALSA repeats the client in the port name (e.g. Scarlett USB:MIDI 1).
    # Keep the full client identity, and prefer this over a partial-name match.
    for name in names:
        if midi_device_key(name).replace(":", " ") == target_key.replace(":", " "):
            return name

    # Compatibility fallback for older saved names that omitted a backend qualifier.
    for name in names:
        candidate_key = midi_device_key(name)
        if target_key in candidate_key or candidate_key in target_key:
            return name
    return None
