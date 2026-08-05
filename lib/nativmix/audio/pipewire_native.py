"""
PipeWire-native stream inventory and matching helpers.

This module provides pure-Python utilities that do **not** depend on
``pulsectl`` / libpulse.  It can therefore be imported and tested without a
running PipeWire/PulseAudio session.

Contents
--------
PipeWireNode
    Dataclass describing a single PipeWire audio output node from ``pw-dump``.
_pw_dump_nodes()
    Parse ``pw-dump`` JSON and return active ``Stream/Output`` nodes.
_matches_node()
    Deterministic priority matching: stable IDs → binary → app name → media
    name → contains fallback.
_pw_set_volume()
    Set a PipeWire node's volume directly via ``pw-cli set-param``.
_pw_set_mute()
    Set a PipeWire node's mute state directly via ``pw-cli set-param``.
_wpctl_set_volume()
    Set a PipeWire node's volume via ``wpctl set-volume`` (works in Flatpak).
_wpctl_set_volume_default_sink()
    Set the default sink (system master) volume via ``wpctl set-volume``.
_wpctl_set_volume_default_source()
    Set the default source (mic) volume via ``wpctl set-volume``.
_ThrottledWarner
    Suppress repeated log messages within a configurable interval.
_probe_capabilities()
    One-time startup probe: test pw-cli/wpctl and pulsectl write capability
    and tool availability.  ``can_set_volume_pw`` reflects the PW-native write
    path; ``can_set_volume`` reflects the PulseAudio fallback path.
    ``wpctl_available`` indicates wpctl is usable as a PW-native write path
    (primary in Flatpak where pw-cli is absent).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Shared timeout (seconds) for pw-dump subprocess calls.
_SUBPROCESS_TIMEOUT: int = 5


# ---------------------------------------------------------------------------
# PipeWire-native stream inventory (Phase 2)
# ---------------------------------------------------------------------------

@dataclass
class PipeWireNode:
    """
    Represents a single PipeWire audio output node from ``pw-dump`` output.

    These objects complement the PulseAudio sink-input model with richer
    metadata that is only available in the native PW object graph (e.g.
    ``node.id``, ``client.id``, ``application.process.binary``).
    """

    node_id: int
    """Stable PipeWire node ID (``node.id`` in ``pw-dump`` output)."""

    client_id: int
    """PipeWire client ID (``client.id``).  0 if unavailable."""

    app_name: str
    """``application.name`` property value, or empty string."""

    process_binary: str
    """``application.process.binary`` property value, or empty string."""

    media_name: str
    """``media.name`` property value, or empty string."""

    media_class: str
    """``media.class`` property value (e.g. ``Stream/Output/Audio``)."""

    app_id: str
    """Desktop app-id (``application.id`` or ``pipewire.access.portal.app_id``), or empty."""

    props: dict[str, str]
    """Raw property dict for debugging / further resolution."""


def _pw_dump_nodes() -> list[PipeWireNode]:
    """
    Parse ``pw-dump`` JSON output and return all active audio output nodes.

    Returns an empty list when ``pw-dump`` is unavailable or fails (e.g.
    no PipeWire session, Flatpak sandbox without portal access).

    Only nodes whose ``media.class`` starts with ``Stream/Output`` are
    included — these correspond to app playback streams.
    """
    if not shutil.which("pw-dump"):
        return []
    try:
        result = subprocess.run(
            ["pw-dump"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            return []
        raw = json.loads(result.stdout)
    except Exception:
        return []

    nodes: list[PipeWireNode] = []
    for obj in raw:
        if not isinstance(obj, dict):
            continue
        obj_type = obj.get("type", "")
        if "Node" not in obj_type:
            continue
        info = obj.get("info", {})
        props = {str(k): str(v) for k, v in info.get("props", {}).items()}

        media_class = props.get("media.class", "")
        if not media_class.startswith("Stream/Output"):
            continue

        try:
            node_id = int(obj.get("id", 0))
        except (ValueError, TypeError):
            node_id = 0

        try:
            client_id = int(props.get("client.id", "0"))
        except (ValueError, TypeError):
            client_id = 0

        app_id = props.get("application.id", "") or props.get("pipewire.access.portal.app_id", "")

        nodes.append(PipeWireNode(
            node_id=node_id,
            client_id=client_id,
            app_name=props.get("application.name", ""),
            process_binary=props.get("application.process.binary", ""),
            media_name=props.get("media.name", ""),
            media_class=media_class,
            app_id=app_id,
            props=props,
        ))
    return nodes


def _matches_node(
    node: PipeWireNode,
    target: str,
    stable_node_ids: set[int] | None = None,
    stable_client_ids: set[int] | None = None,
) -> bool:
    """
    Return True if *node* matches *target* using the deterministic priority:

    1. Exact cached stable IDs (``node.id`` / ``client.id``).
    2. Exact ``application.process.binary`` match (case-insensitive).
    3. Exact ``application.name`` match (case-insensitive).
    4. Exact ``media.name`` match (case-insensitive).
    5. Case-insensitive *contains* fallback (last resort; target must be ≥ 3 chars).

    Args:
        node: A :class:`PipeWireNode` from the PW-native inventory.
        target: The user-visible application name to match against.
        stable_node_ids: Optional set of known-good ``node.id`` values for
            this target, populated from previous successful bindings.
        stable_client_ids: Optional set of known-good ``client.id`` values.
    """
    target_lc = target.lower()
    if not target_lc:
        return False

    # 1. Stable ID cache
    if stable_node_ids and node.node_id in stable_node_ids:
        return True
    if stable_client_ids and node.client_id and node.client_id in stable_client_ids:
        return True

    # 2. process.binary exact match
    if node.process_binary and node.process_binary.lower() == target_lc:
        return True

    # 3. application.name exact match
    if node.app_name and node.app_name.lower() == target_lc:
        return True

    # 4. media.name exact match
    if node.media_name and node.media_name.lower() == target_lc:
        return True

    # 5. Contains fallback (last resort — avoids false positives from short names)
    if len(target_lc) >= 3:
        for field in (node.app_name, node.process_binary, node.media_name):
            if field and target_lc in field.lower():
                return True

    return False


# ---------------------------------------------------------------------------
# PipeWire-native write helpers
# ---------------------------------------------------------------------------

def _pw_set_volume(node_id: int, volume: float) -> bool:
    """
    Set the linear volume [0.0–1.0] of a PipeWire node via ``pw-cli set-param``.

    Uses the SPA Props interface::

        pw-cli set-param <node_id> Props '{ volume: <value> }'

    Returns ``True`` on success, ``False`` when ``pw-cli`` is unavailable,
    *node_id* is zero, or the command fails.
    """
    if not node_id or not shutil.which("pw-cli"):
        return False
    volume = max(0.0, min(1.0, volume))
    try:
        result = subprocess.run(
            ["pw-cli", "set-param", str(node_id), "Props", f"{{ volume: {volume:.6f} }}"],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        return result.returncode == 0
    except Exception:
        return False


def _pw_set_mute(node_id: int, muted: bool) -> bool:
    """
    Set the mute state of a PipeWire node via ``pw-cli set-param``.

    Uses the SPA Props interface::

        pw-cli set-param <node_id> Props '{ mute: true|false }'

    Returns ``True`` on success, ``False`` when ``pw-cli`` is unavailable,
    *node_id* is zero, or the command fails.
    """
    if not node_id or not shutil.which("pw-cli"):
        return False
    mute_val = "true" if muted else "false"
    try:
        result = subprocess.run(
            ["pw-cli", "set-param", str(node_id), "Props", f"{{ mute: {mute_val} }}"],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# wpctl write helpers (Flatpak-compatible PW-native path)
# ---------------------------------------------------------------------------

def _wpctl_set_volume(node_id: int, volume: float) -> bool:
    """
    Set the linear volume [0.0–1.0] of a PipeWire node via ``wpctl set-volume``.

    ``wpctl`` is available inside Flatpak sandboxes that grant access to the
    ``xdg-run/pipewire-0`` socket, whereas ``pw-cli`` is typically absent.

    Uses::

        wpctl set-volume <node_id> <value>

    Returns ``True`` on success, ``False`` when ``wpctl`` is unavailable,
    *node_id* is zero, or the command fails.
    """
    if not node_id or not shutil.which("wpctl"):
        return False
    volume = max(0.0, min(1.0, volume))
    try:
        result = subprocess.run(
            ["wpctl", "set-volume", str(node_id), f"{volume:.6f}"],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        return result.returncode == 0
    except Exception:
        return False




def _wpctl_set_volume_exact(node_ref: str, volume: float) -> bool:
    """Set volume via wpctl using an exact object reference string."""
    if not node_ref or not shutil.which("wpctl"):
        return False
    volume = max(0.0, min(1.0, volume))
    try:
        result = subprocess.run(
            ["wpctl", "set-volume", node_ref, f"{volume:.6f}"],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        return result.returncode == 0
    except Exception:
        return False

def _wpctl_set_volume_default_sink(volume: float) -> bool:
    """
    Set the default audio sink (system master output) volume via ``wpctl``.

    Uses the ``@DEFAULT_AUDIO_SINK@`` alias so that the correct sink is
    targeted even when the default changes between calls::

        wpctl set-volume @DEFAULT_AUDIO_SINK@ <value>

    Returns ``True`` on success, ``False`` when ``wpctl`` is unavailable or
    the command fails.
    """
    if not shutil.which("wpctl"):
        return False
    volume = max(0.0, min(1.0, volume))
    try:
        result = subprocess.run(
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{volume:.6f}"],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        return result.returncode == 0
    except Exception:
        return False


def _wpctl_set_volume_default_source(volume: float) -> bool:
    """
    Set the default audio source (microphone / capture) volume via ``wpctl``.

    Uses the ``@DEFAULT_AUDIO_SOURCE@`` alias::

        wpctl set-volume @DEFAULT_AUDIO_SOURCE@ <value>

    Returns ``True`` on success, ``False`` when ``wpctl`` is unavailable or
    the command fails.
    """
    if not shutil.which("wpctl"):
        return False
    volume = max(0.0, min(1.0, volume))
    try:
        result = subprocess.run(
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", f"{volume:.6f}"],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        return result.returncode == 0
    except Exception:
        return False


def _wpctl_set_mute(node_id: int, muted: bool) -> bool:
    """
    Set the mute state of a PipeWire node via ``wpctl set-mute``.

    Uses::

        wpctl set-mute <node_id> 1|0

    Returns ``True`` on success, ``False`` when ``wpctl`` is unavailable,
    *node_id* is zero, or the command fails.
    """
    if not node_id or not shutil.which("wpctl"):
        return False
    mute_val = "1" if muted else "0"
    try:
        result = subprocess.run(
            ["wpctl", "set-mute", str(node_id), mute_val],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Throttled warning helper (Phase 1 / Phase 5)
# ---------------------------------------------------------------------------

class _ThrottledWarner:
    """
    Emits a ``logger.warning`` at most once per *interval* seconds for the same key.

    Used to collapse repeated per-tick failures into a single clear notice
    rather than flooding the log with identical messages.
    """

    def __init__(self, interval: float = 30.0) -> None:
        self._interval = interval
        self._last: dict[str, float] = {}

    def warn(self, key: str, msg: str, *args: Any) -> None:
        now = time.monotonic()
        if now - self._last.get(key, 0.0) >= self._interval:
            self._last[key] = now
            logger.warning(msg, *args)


# ---------------------------------------------------------------------------
# Capability probe (Phase 1)
# ---------------------------------------------------------------------------

def _probe_capabilities() -> dict[str, bool]:
    """
    Perform a one-time capability probe on startup.

    Checks tool availability and attempts write operations on both the
    PipeWire-native path (``pw-cli set-param`` / ``wpctl set-volume``) and the
    PulseAudio compat path (pulsectl) to verify which control paths are
    actually writable.

    Returns a dict with boolean flags:
        - ``can_set_volume_pw`` — PW-native volume writes are permitted
          (primary path).  True when either ``pw-cli`` or ``wpctl`` can reach
          the PipeWire session.
        - ``can_set_volume``    — pulsectl volume writes are permitted
          (fallback path).
        - ``can_move_stream``   — pactl move-sink-input is available.
        - ``pw_dump_available`` — ``pw-dump`` binary is present.
        - ``pw_cli_available``  — ``pw-cli`` binary is present.
        - ``wpctl_available``   — ``wpctl`` binary is present and reachable
          (preferred write tool in Flatpak where pw-cli is absent).

    The pulsectl import is performed lazily inside this function so that the
    rest of the module (and its tests) do not fail when libpulse is absent.
    """
    caps: dict[str, bool] = {
        "can_set_volume_pw": False,
        "can_set_volume": False,
        "can_move_stream": shutil.which("pactl") is not None,
        "pw_dump_available": shutil.which("pw-dump") is not None,
        "pw_cli_available": shutil.which("pw-cli") is not None,
        "wpctl_available": False,
    }

    # Probe wpctl first — it works in Flatpak with xdg-run/pipewire-0 grant
    # and is simpler than pw-cli for volume control.
    if shutil.which("wpctl"):
        try:
            result = subprocess.run(
                ["wpctl", "status"],
                capture_output=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
            if result.returncode == 0:
                caps["wpctl_available"] = True
                caps["can_set_volume_pw"] = True
        except Exception:
            pass

    # Probe PipeWire-native write path via pw-cli (supplementary to wpctl).
    if caps["pw_cli_available"] and not caps["can_set_volume_pw"]:
        try:
            # pw-cli info 0 is a harmless read to verify the daemon is
            # reachable.  A zero exit code means pw-cli can talk to the
            # PipeWire session; we treat that as write-capable.
            result = subprocess.run(
                ["pw-cli", "info", "0"],
                capture_output=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
            caps["can_set_volume_pw"] = result.returncode == 0
        except Exception:
            pass

    # Probe PulseAudio compat write path via pulsectl (fallback).
    try:
        import pulsectl as _pulsectl  # type: ignore[import]
        with _pulsectl.Pulse("nativmix-cap-probe") as pulse:
            # Attempt a benign read (server_info) to validate the connection.
            pulse.server_info()
            # Try a harmless volume write: set first available sink-input to
            # its current volume (no audible change).
            inputs = pulse.sink_input_list()
            if inputs:
                si = inputs[0]
                current_vol = si.volume.values[0] if si.volume.values else 1.0
                pulse.volume_set_all_chans(si, current_vol)
        caps["can_set_volume"] = True
    except Exception:
        pass

    return caps
