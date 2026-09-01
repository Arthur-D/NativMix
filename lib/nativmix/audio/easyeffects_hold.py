"""Helpers for routing coexistence with Easy Effects."""

from __future__ import annotations

from typing import Literal

VolumeMode = Literal["stream", "vsink"]


def is_easyeffects_sink(sink_name: str | None) -> bool:
    """Return whether a sink is an Easy Effects playback endpoint."""
    if not sink_name:
        return False
    normalized = sink_name.strip().lower()
    if normalized == "easyeffects_source" or normalized.endswith("_source"):
        return False
    return normalized == "easyeffects_sink" or normalized.startswith("easyeffects_sink.")


def resolve_auto_route_target(
    *,
    current_sink: str | None,
    vsink_enabled: bool,
    vsink_name: str,
    default_sink: str | None,
    routing_paused: bool = False,
) -> str | None:
    """Return a safe automatic destination, or None when routing must stay untouched."""
    if routing_paused or is_easyeffects_sink(current_sink):
        return None
    if vsink_enabled:
        return None if current_sink == vsink_name else vsink_name
    if not default_sink or default_sink.startswith("NativMix_") or current_sink == default_sink:
        return None
    return default_sink


def volume_apply_mode(*, current_sink: str | None, vsink_enabled: bool, vsink_name: str) -> VolumeMode:
    """Choose stream gain unless the stream is confirmed on its owned V-Sink."""
    if vsink_enabled and current_sink == vsink_name:
        return "vsink"
    return "stream"
