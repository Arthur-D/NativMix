"""
PipeWire-native stream inventory and matching helpers.

This module provides pure-Python utilities that do **not** depend on
``pulsectl`` / libpulse.  It can therefore be imported and tested without a
running PipeWire/PulseAudio session.

Contents
--------
PipeWireNode
    Dataclass describing a single PipeWire audio output node from ``pw-dump``.
_normalize_name()
    Canonical lowercase form of a name with launcher suffixes stripped.
_pw_dump_nodes()
    Parse ``pw-dump`` JSON and return active ``Stream/Output`` nodes (or any
    other ``media.class`` prefixes requested by the caller).
VirtualProcessingSink / discover_virtual_processing_sinks()
    Discover existing virtual sink/source nodes (``easyeffects_sink`` /
    ``easyeffects_source`` and NativMix equivalents) usable as a processing
    backend.
_pw_move_node_to_target()
    Route a stream node to a target sink via the PipeWire-native
    ``pw-metadata target.object`` path (no PulseAudio required).
_matches_node()
    Deterministic priority matching: stable IDs → binary → app name → node
    name → media name → normalized contains fallback.
_pw_set_volume() / _pw_set_volume_traced()
    Set a PipeWire node's volume directly via ``pw-cli set-param``.  The
    ``_traced`` variant also returns the command, rc, stdout and stderr for
    INFO-level write logging at the call site.
_pw_set_mute()
    Set a PipeWire node's mute state directly via ``pw-cli set-param``.
_wpctl_set_volume() / _wpctl_set_volume_traced()
    Set a PipeWire node's volume via ``wpctl set-volume`` (works in Flatpak).
    The ``_traced`` variant also returns the command, rc, stdout and stderr.
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
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Shared timeout (seconds) for pw-dump subprocess calls.
_SUBPROCESS_TIMEOUT: int = 5

# When set to "1", force PW-only mode regardless of PulseAudio socket
# availability.  Useful for testing PW-only codepaths without a Flatpak sandbox
# and to opt-in explicitly on systems where PulseAudio is present but unwanted.
NATIVMIX_FORCE_PW_ONLY: bool = os.environ.get("NATIVMIX_FORCE_PW_ONLY", "0") == "1"

# Suffixes stripped during name normalization.  Order matters: longest /
# most specific suffixes should appear first so they are removed before the
# shorter, more general ones.
_NAME_STRIP_SUFFIXES: tuple[str, ...] = (
    "-wayland",
    "-x11",
    "-bin",
    ".desktop",
)

# ---------------------------------------------------------------------------
# Name normalization helper (PR-39)
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    """
    Return a canonical lowercase form of *name* suitable for fuzzy matching.

    Transformations applied in order:

    1. Strip leading/trailing whitespace.
    2. Lowercase the entire string.
    3. Remove common launcher suffixes: ``-wayland``, ``-x11``, ``-bin``,
       ``.desktop``.

    The result is used for all field comparisons inside :func:`_matches_node`
    so that, for example, ``"Spotify-wayland"`` and ``"spotify"`` resolve to the
    same canonical token ``"spotify"``.
    """
    s = name.strip().lower()
    for suffix in _NAME_STRIP_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


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

    node_name: str = ""
    """``node.name`` property value, or empty string."""

    props: dict[str, str] = None  # type: ignore[assignment]
    """Raw property dict for debugging / further resolution."""

    permissions: list[str] = None  # type: ignore[assignment]
    """PipeWire object permissions list (e.g. ``['r', 'w', 'x']``).  Empty list if unavailable."""

    def __post_init__(self) -> None:
        if self.props is None:
            object.__setattr__(self, "props", {})
        if self.permissions is None:
            object.__setattr__(self, "permissions", [])


def _pw_dump_nodes(media_class_prefixes: tuple[str, ...] = ("Stream/Output",)) -> list[PipeWireNode]:
    """
    Parse ``pw-dump`` JSON output and return matching audio nodes.

    Returns an empty list when ``pw-dump`` is unavailable or fails (e.g.
    no PipeWire session, Flatpak sandbox without portal access).

    By default only nodes whose ``media.class`` starts with ``Stream/Output``
    are included — these correspond to app playback streams.  Pass a different
    *media_class_prefixes* tuple (e.g. ``("Audio/Sink", "Audio/Source")``) to
    enumerate device-like nodes such as virtual processing sinks.
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
        if not media_class.startswith(media_class_prefixes):
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

        raw_permissions = obj.get("permissions", [])
        perms: list[str] = [str(p) for p in raw_permissions] if isinstance(raw_permissions, list) else []

        nodes.append(PipeWireNode(
            node_id=node_id,
            client_id=client_id,
            app_name=props.get("application.name", ""),
            process_binary=props.get("application.process.binary", ""),
            media_name=props.get("media.name", ""),
            media_class=media_class,
            app_id=app_id,
            node_name=props.get("node.name", ""),
            props=props,
            permissions=perms,
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
    2. Normalized exact ``application.process.binary`` match.
    3. Normalized exact ``application.name`` match.
    4. Normalized exact ``node.name`` match.
    5. Normalized exact ``media.name`` match.
    6. Normalized *contains* fallback across all fields (last resort;
       normalized target must be ≥ 3 chars).

    All field comparisons use :func:`_normalize_name` so that launcher
    suffixes such as ``-wayland``, ``-x11``, ``.desktop`` are stripped
    before comparison.

    Args:
        node: A :class:`PipeWireNode` from the PW-native inventory.
        target: The user-visible application name to match against.
        stable_node_ids: Optional set of known-good ``node.id`` values for
            this target, populated from previous successful bindings.
        stable_client_ids: Optional set of known-good ``client.id`` values.
    """
    target_norm = _normalize_name(target)
    if not target_norm:
        return False

    # 1. Stable ID cache — most reliable; trust even when fields have changed.
    if stable_node_ids and node.node_id in stable_node_ids:
        return True
    if stable_client_ids and node.client_id and node.client_id in stable_client_ids:
        return True

    # 2–5. Normalized exact field matches (priority: binary → app → node → media)
    for raw_field in (node.process_binary, node.app_name, node.node_name, node.media_name):
        if raw_field and _normalize_name(raw_field) == target_norm:
            return True

    # 6. Normalized contains fallback (last resort — avoids false positives from short names)
    if len(target_norm) >= 3:
        for raw_field in (node.app_name, node.process_binary, node.node_name, node.media_name):
            if raw_field and target_norm in _normalize_name(raw_field):
                return True

    return False


# ---------------------------------------------------------------------------
# PipeWire-native write helpers
# ---------------------------------------------------------------------------

def _pw_set_volume_traced(node_id: int, volume: float) -> tuple[bool, list[str], int | None, str, str]:
    """
    Same as :func:`_pw_set_volume` but returns the full trace details
    (command, return code, stdout, stderr) needed for INFO-level write
    logging at the call site.
    """
    if not node_id or not shutil.which("pw-cli"):
        return False, [], None, "", ""
    volume = max(0.0, min(1.0, volume))
    cmd = ["pw-cli", "set-param", str(node_id), "Props", f"{{ volume: {volume:.6f} }}"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        stdout = result.stdout.decode(errors="replace").strip()
        stderr = result.stderr.decode(errors="replace").strip()
        return result.returncode == 0, cmd, result.returncode, stdout, stderr
    except Exception as exc:
        return False, cmd, None, "", str(exc)


def _pw_set_volume(node_id: int, volume: float) -> bool:
    """
    Set the linear volume [0.0–1.0] of a PipeWire node via ``pw-cli set-param``.

    Uses the SPA Props interface::

        pw-cli set-param <node_id> Props '{ volume: <value> }'

    Returns ``True`` on success, ``False`` when ``pw-cli`` is unavailable,
    *node_id* is zero, or the command fails.
    """
    ok, _cmd, _rc, _out, _err = _pw_set_volume_traced(node_id, volume)
    return ok


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

def _wpctl_set_volume_traced(node_id: int, volume: float) -> tuple[bool, list[str], int | None, str, str]:
    """
    Same as :func:`_wpctl_set_volume` but returns the full trace details
    (command, return code, stdout, stderr) needed for INFO-level write
    logging at the call site.
    """
    if not node_id or not shutil.which("wpctl"):
        return False, [], None, "", ""
    volume = max(0.0, min(1.0, volume))
    cmd = ["wpctl", "set-volume", str(node_id), f"{volume:.6f}"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        stdout = result.stdout.decode(errors="replace").strip()
        stderr = result.stderr.decode(errors="replace").strip()
        return result.returncode == 0, cmd, result.returncode, stdout, stderr
    except Exception as exc:
        return False, cmd, None, "", str(exc)


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
    ok, _cmd, _rc, _out, _err = _wpctl_set_volume_traced(node_id, volume)
    return ok




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
# Easy Effects detection
# ---------------------------------------------------------------------------

#: Known sink/node name patterns created by Easy Effects.
_EE_SINK_NAMES: tuple[str, ...] = (
    "easyeffects",
    "easy-effects",
    "easyeffects_sink",
    "easyeffects_source",
)

#: Known process/application name patterns for Easy Effects.
_EE_APP_NAMES: tuple[str, ...] = (
    "easyeffects",
    "com.github.wwmm.easyeffects",
)


#: Media classes that can act as a virtual processing sink/source endpoint.
_VIRTUAL_SINK_MEDIA_CLASSES: tuple[str, ...] = ("Audio/Sink", "Audio/Source", "Audio/Duplex")

#: Exact node.name values of Easy Effects virtual endpoints.
_EE_VIRTUAL_NODE_NAMES: tuple[str, ...] = ("easyeffects_sink", "easyeffects_source")

#: Prefixes of NativMix-owned virtual endpoints (fallback backend).
_NATIVMIX_VIRTUAL_PREFIXES: tuple[str, ...] = ("nativmix_", "nativmix-")


@dataclass
class VirtualProcessingSink:
    """
    A virtual sink/source node that can be used as a processing backend.

    These are device-like PipeWire nodes (``Audio/Sink`` / ``Audio/Source`` /
    ``Audio/Duplex``) created by an effects host such as Easy Effects, or by
    NativMix itself.  Application streams can be routed into such a node and
    the gain of the resulting path is controlled on the backend node instead
    of on the (usually read-only) application stream nodes.
    """

    node_id: int
    """PipeWire ``node.id`` of the virtual endpoint."""

    node_name: str
    """``node.name`` of the virtual endpoint (e.g. ``easyeffects_sink``)."""

    media_class: str
    """``media.class`` of the node (e.g. ``Audio/Sink``)."""

    backend: str
    """Backend owning this node: ``"easyeffects"`` or ``"nativmix"``."""

    direction: str
    """``"sink"`` for playback endpoints, ``"source"`` for capture endpoints."""

    description: str = ""
    """``node.description`` of the node, or empty string."""

    permissions: list[str] = None  # type: ignore[assignment]
    """PipeWire object permissions (e.g. ``['r', 'w', 'x']``).  Empty if unknown."""

    def __post_init__(self) -> None:
        if self.permissions is None:
            object.__setattr__(self, "permissions", [])

    @property
    def writable(self) -> bool:
        """True when the node has no permission info or explicitly grants ``w``."""
        return (not self.permissions) or ("w" in self.permissions)


def _classify_virtual_node(node: PipeWireNode) -> str | None:
    """
    Return the backend name owning *node*, or ``None`` if it is not a known
    virtual processing endpoint.

    ``easyeffects`` wins over ``nativmix`` so Easy Effects nodes are always
    preferred when both are present.
    """
    name = (node.node_name or "").strip().lower()
    if not name:
        return None
    if name in _EE_VIRTUAL_NODE_NAMES:
        return "easyeffects"
    if name.startswith(_NATIVMIX_VIRTUAL_PREFIXES):
        return "nativmix"
    return None


def discover_virtual_processing_sinks() -> list[VirtualProcessingSink]:
    """
    Discover existing virtual sink/source nodes in the live ``pw-dump`` graph.

    Recognised endpoints:

    * Easy Effects: ``easyeffects_sink`` / ``easyeffects_source``.
    * NativMix equivalents: any node whose ``node.name`` starts with
      ``nativmix_`` or ``nativmix-``.

    Easy Effects endpoints are returned first so callers can simply prefer the
    head of the list as the routing backend.  Returns an empty list when no
    virtual processing endpoint exists (or ``pw-dump`` is unavailable).
    """
    try:
        nodes = _pw_dump_nodes(media_class_prefixes=_VIRTUAL_SINK_MEDIA_CLASSES)
    except Exception as exc:
        logger.debug("discover_virtual_processing_sinks: pw-dump failed: %s", exc)
        return []

    found: list[VirtualProcessingSink] = []
    for node in nodes:
        backend = _classify_virtual_node(node)
        if backend is None:
            continue
        direction = "source" if node.media_class.startswith("Audio/Source") else "sink"
        found.append(VirtualProcessingSink(
            node_id=node.node_id,
            node_name=node.node_name,
            media_class=node.media_class,
            backend=backend,
            direction=direction,
            description=node.props.get("node.description", ""),
            permissions=list(node.permissions),
        ))

    # Easy Effects first, then NativMix; stable ordering by node_id within a backend.
    found.sort(key=lambda s: (0 if s.backend == "easyeffects" else 1, s.node_id))
    return found


def _pw_move_node_to_target(node_id: int, target_node_name: str) -> bool:
    """
    Route a stream node to *target_node_name* using the PipeWire-native path.

    Sets the ``target.object`` metadata key on the node via ``pw-metadata``,
    which is the PipeWire-native equivalent of ``pactl move-sink-input`` and
    works without a PulseAudio socket::

        pw-metadata <node_id> target.object "<sink node.name>"

    Returns ``True`` on success, ``False`` when ``pw-metadata`` is unavailable,
    arguments are missing, or the command fails.
    """
    if not node_id or not target_node_name or not shutil.which("pw-metadata"):
        return False
    try:
        result = subprocess.run(
            [
                "pw-metadata", str(node_id), "target.object",
                target_node_name, "Spa:String",
            ],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        return result.returncode == 0
    except Exception as exc:
        logger.debug(
            "_pw_move_node_to_target(%d, %r) failed: %s", node_id, target_node_name, exc,
        )
        return False


def detect_easyeffects() -> tuple[bool, str]:
    """
    Heuristic detection of a running Easy Effects instance.

    Evidence sources (checked in order):
    1. Process name via ``/proc``.
    2. Easy Effects virtual endpoints (``easyeffects_sink`` / ``_source``).
    3. PipeWire stream node names from ``pw-dump``.

    Returns a ``(detected, evidence)`` tuple where *detected* is ``True`` when
    any evidence is found and *evidence* is a short human-readable string
    explaining what was found (for logging).
    """
    # 1. Process scan via /proc (Linux only; safe to fail on other platforms).
    try:
        import glob as _glob
        for cmdline_path in _glob.iglob("/proc/*/cmdline"):
            try:
                with open(cmdline_path, "rb") as fh:
                    cmdline = fh.read().replace(b"\x00", b" ").decode("utf-8", errors="replace").lower()
                for pat in _EE_APP_NAMES:
                    if pat in cmdline:
                        return True, f"process cmdline contains {pat!r}"
            except OSError:
                pass
    except Exception:
        pass

    # 2. Virtual endpoint scan (easyeffects_sink / easyeffects_source).
    try:
        for vsink in discover_virtual_processing_sinks():
            if vsink.backend == "easyeffects":
                return True, f"virtual node {vsink.node_name!r} (node_id={vsink.node_id})"
    except Exception:
        pass

    # 3. PipeWire stream node scan.
    try:
        nodes = _pw_dump_nodes()
        for node in nodes:
            combined = " ".join(filter(None, [
                node.app_name or "",
                node.node_name or "",
                node.media_name or "",
            ])).lower()
            for pat in _EE_SINK_NAMES + _EE_APP_NAMES:
                if pat in combined:
                    return True, f"pw node matches {pat!r} (node_id={node.node_id})"
    except Exception:
        pass

    return False, "no evidence found"


# ---------------------------------------------------------------------------
# Capability probe (Phase 1)
# ---------------------------------------------------------------------------

def _detect_pulse_available() -> bool:
    """
    Return True if a PulseAudio-compatible socket is reachable.

    Performs a non-destructive ``server_info()`` call via pulsectl.  Returns
    False when pulsectl is not installed, libpulse is absent, or the
    PulseAudio / pipewire-pulse socket is blocked (e.g. ``--nosocket=pulseaudio``
    in Flatpak).

    This is used by :class:`PipeWireManager` to determine whether to enter
    PW-only mode on startup.
    """
    try:
        import pulsectl as _pulsectl  # type: ignore[import]
        with _pulsectl.Pulse("nativmix-pulse-probe") as _p:
            _p.server_info()
        return True
    except Exception:
        return False


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
        - ``pulse_available``   — PulseAudio socket is reachable (pulsectl
          server_info succeeds).  False when the PA socket is blocked (e.g.
          ``--nosocket=pulseaudio`` in Flatpak) or when ``NATIVMIX_FORCE_PW_ONLY``
          is set.  PulseAudio is treated as an optional fallback path; PW-only
          mode is preferred in Flatpak regardless of PA availability.
        - ``force_pw_only``    — ``NATIVMIX_FORCE_PW_ONLY=1`` was set in the
          environment; the caller should activate PW-only mode unconditionally.

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
        "pulse_available": False,
        "force_pw_only": NATIVMIX_FORCE_PW_ONLY,
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

    # Probe PulseAudio compat write path via pulsectl (optional fallback).
    # Skipped when NATIVMIX_FORCE_PW_ONLY is set so the forced PW-only path is
    # never accidentally overridden by a reachable PA socket.
    if not NATIVMIX_FORCE_PW_ONLY:
        try:
            import pulsectl as _pulsectl  # type: ignore[import]
            with _pulsectl.Pulse("nativmix-cap-probe") as pulse:
                # Attempt a benign read (server_info) to validate the connection.
                pulse.server_info()
                caps["pulse_available"] = True
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
