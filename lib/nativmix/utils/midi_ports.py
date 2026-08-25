"""Stable MIDI port identity helpers shared by the worker, config, and GUI."""

from __future__ import annotations

import re

_ALSA_ADDRESS_RE = re.compile(r"\s+\d+:\d+\s*$")
_DISCONNECTED_SUFFIX_RE = re.compile(r"\s+\(Disconnected\)\s*$", re.IGNORECASE)


def normalize_midi_device_name(name: str) -> str:
    """Remove UI-only and volatile RtMidi/ALSA qualifiers from a port name."""
    normalized = _DISCONNECTED_SUFFIX_RE.sub("", str(name).strip())
    normalized = _ALSA_ADDRESS_RE.sub("", normalized).strip()
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

    # Compatibility fallback for older saved names that omitted a backend qualifier.
    for name in names:
        candidate_key = midi_device_key(name)
        if target_key in candidate_key or candidate_key in target_key:
            return name
    return None
