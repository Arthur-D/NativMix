"""Canonical, receiver-owned wire schema for NativMix remote synchronization.

This module is intentionally **Qt-free** and has no dependency on
``nativmix.utils.profile_manager`` or ``nativmix.utils.config_manager``: it
defines an independent, self-contained representation of the subset of
mixer state that is safe to transmit to a remote peer, plus canonical JSON
encoding and a deterministic content hash.

Included (machine-independent, receiver-owned) state:

* Profile list: ``id`` / ``name`` / ``channel_count`` /
  ``restore_fader_positions`` / ``midi_switch_cc``.
* Per-profile channels, in a stable channel UUID order, each with:
  ``id`` / ``index`` / ``label`` / ``is_midi`` / ``mode`` / normalized
  app-device-pseudo ``mappings`` / ``hardware_target_key`` /
  ``routing_paused_apps`` / ``inverted`` / ``v_sink`` / ``volume_cc`` +
  ``volume_channel`` / ``mute_cc`` + ``mute_channel`` /
  ``saved_fader_volume``.
* Target inventory (normalized, stable keys only).
* Runtime channel state: effective volume/mute/availability/unresolved/
  shared-target/capability state.
* Receiver capabilities.
* Epoch, revision, and a canonical SHA-256 content hash.

Explicitly EXCLUDED (never modeled anywhere in this schema, by design):

* Machine-local settings: input mode, Arduino/MIDI device selection, remote
  role/peer configuration, routing owner/backend/master-output selection,
  autostart/update-checker/UI settings, the global next/previous-profile
  MIDI CC, ``midi_fader_feedback``, sleep/power inhibitors, and controller
  transport settings.
* Anything that could identify or reach the local machine: process IDs,
  raw PipeWire/PulseAudio stream-node IDs, filesystem paths, network
  addresses/ports, hardware serial numbers, secrets/tokens, or log content.

There is intentionally **no persistence API** here: this module only builds
in-memory canonical records and (de)serializes canonical JSON. Snapshots are
not written to or read from disk.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

# --------------------------------------------------------------------------
# Version and limits
# --------------------------------------------------------------------------

#: Wire schema version. Bump only with an accompanying protocol version bump.
SCHEMA_VERSION: Final[int] = 1

#: Maximum encoded size (bytes) of a single frame or snapshot payload.
MAX_FRAME_BYTES: Final[int] = 2 * 1024 * 1024

#: Maximum nesting depth accepted/produced by canonical JSON structures.
MAX_DEPTH: Final[int] = 16

#: Maximum number of profiles in a snapshot's profile list.
MAX_PROFILES: Final[int] = 256

#: Maximum number of channels considered "active" (in the active profile).
MAX_ACTIVE_CHANNELS: Final[int] = 256

#: Maximum number of target inventory entries.
MAX_INVENTORY: Final[int] = 2048

#: Maximum length for any other bounded list (e.g. mappings, paused apps,
#: capability feature tokens).
MAX_OTHER_LIST: Final[int] = 1024

#: Maximum value representable by the wire's ``uint64`` revision counter.
#: Every wire revision integer (snapshot/delta/command/ack/nack/checkpoint)
#: must be bounded by this value; see also ``state.MAX_REVISION`` (same
#: value), which this module intentionally does not import so it stays
#: self-contained/dependency-free per the module docstring.
MAX_REVISION: Final[int] = 2**64 - 1

#: The generic list-length ceiling used by :func:`validate_finite`. This is
#: intentionally the *largest* of the field-specific bounded-list limits
#: (currently ``MAX_INVENTORY``) so that a legitimately-sized inventory list
#: (up to :data:`MAX_INVENTORY` entries) is never rejected by the generic,
#: key-agnostic recursive check. Tighter, field-specific limits (profiles,
#: active channels, mappings, paused apps, feature lists, ...) are enforced
#: independently by their own dedicated builders/parsers.
_MAX_GENERIC_LIST_LENGTH: Final[int] = MAX_INVENTORY

#: Allowed channel "mode" values.
ALLOWED_CHANNEL_MODES: Final[frozenset[str]] = frozenset({"app", "hardware", "vsink"})

#: Allowed runtime capability-state tokens.
ALLOWED_CAPABILITY_STATES: Final[frozenset[str]] = frozenset({"ok", "degraded", "unsupported"})

#: Allowed inventory "kind" tokens.
ALLOWED_INVENTORY_KINDS: Final[frozenset[str]] = frozenset({"output", "input"})

#: Raw input keys that must never appear on data fed into this schema.
#: These name machine-local or identifying concepts explicitly excluded from
#: the wire schema (see module docstring). Any of these keys found on raw
#: input mappings passed to the ``normalize_*`` helpers is rejected.
FORBIDDEN_RAW_KEYS: Final[frozenset[str]] = frozenset(
    {
        "input_mode",
        "arduino_port",
        "arduino_device",
        "midi_device",
        "midi_input_device",
        "midi_output_device",
        "remote_role",
        "remote_peer",
        "routing_owner",
        "routing_backend",
        "master_output",
        "autostart",
        "update_check",
        "ui_theme",
        "ui_settings",
        "next_profile_cc",
        "previous_profile_cc",
        "midi_fader_feedback",
        "prevent_remote_sleep",
        "allow_remote_mixer_editing",
        "sleep_inhibitor",
        "inhibit_sleep",
        "power_management",
        "controller_transport",
        "pid",
        "process_id",
        "node_id",
        "stream_node_id",
        "path",
        "file_path",
        "address",
        "host",
        "port",
        "serial",
        "serial_number",
        "secret",
        "token",
        "password",
        "log",
        "logs",
    }
)


class SchemaError(ValueError):
    """Base class for all canonical-schema validation failures."""


class SchemaLimitError(SchemaError):
    """Raised when a value exceeds a documented schema limit."""


class SchemaValueError(SchemaError):
    """Raised when a value is malformed, non-finite, or forbidden."""


# --------------------------------------------------------------------------
# Finite / depth validation of arbitrary JSON-like values
# --------------------------------------------------------------------------


def _contains_lone_surrogate(value: str) -> bool:
    """Return True if *value* contains an unpaired UTF-16 surrogate code point.

    Such code points (U+D800-U+DFFF) cannot be encoded as valid UTF-8 and
    must never be accepted anywhere in the canonical schema, whether as a
    string value or as a mapping key.
    """
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def validate_finite(value: Any, *, depth: int = 0) -> None:
    """Recursively validate that *value* is finite, JSON-safe, and bounded.

    Rejects NaN/Infinity floats, strings containing lone (unpaired)
    surrogate code points, and enforces :data:`MAX_DEPTH` and the
    bounded-list limits used across this module.

    Raises:
        SchemaLimitError: if nesting is too deep or a list is too long.
        SchemaValueError: if a float is non-finite, a string contains a lone
            surrogate, or a value's type is not JSON-representable.
    """
    if depth > MAX_DEPTH:
        raise SchemaLimitError(f"structure exceeds max depth {MAX_DEPTH}")
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return
    if isinstance(value, str):
        if _contains_lone_surrogate(value):
            raise SchemaValueError(f"string contains an unpaired surrogate code point: {value!r}")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SchemaValueError(f"non-finite float: {value!r}")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SchemaValueError(f"non-string mapping key: {key!r}")
            if _contains_lone_surrogate(key):
                raise SchemaValueError(f"mapping key contains an unpaired surrogate code point: {key!r}")
            validate_finite(item, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_GENERIC_LIST_LENGTH:
            raise SchemaLimitError(f"list exceeds max length {_MAX_GENERIC_LIST_LENGTH}")
        for item in value:
            validate_finite(item, depth=depth + 1)
        return
    raise SchemaValueError(f"value is not JSON-representable: {type(value)!r}")


def _reject_forbidden_keys(raw: Mapping[str, Any]) -> None:
    found = FORBIDDEN_RAW_KEYS & raw.keys()
    if found:
        raise SchemaValueError(f"raw input contains excluded machine-local keys: {sorted(found)}")


# --------------------------------------------------------------------------
# MIDI normalization helpers
# --------------------------------------------------------------------------


def normalize_midi_cc(value: Any) -> int | None:
    """Return a valid MIDI CC number (0-127), or ``None`` for anything else."""
    if value is None or isinstance(value, bool):
        return None
    try:
        cc = int(value)
    except (TypeError, ValueError):
        return None
    return cc if 0 <= cc <= 127 else None


def normalize_midi_channel(value: Any) -> int:
    """Return a MIDI channel clamped to the valid 0-15 range."""
    if isinstance(value, bool):
        return 0
    try:
        channel = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(15, channel))


# --------------------------------------------------------------------------
# Canonical records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelRecord:
    """A single canonical mixer channel record."""

    id: str
    index: int
    label: str | None
    is_midi: bool
    mode: str
    mappings: tuple[str, ...]
    hardware_target_key: str | None
    routing_paused_apps: tuple[str, ...]
    inverted: bool
    v_sink: bool
    volume_cc: int | None
    volume_channel: int
    mute_cc: int | None
    mute_channel: int
    saved_fader_volume: float

    def to_canonical(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "index": self.index,
            "label": self.label,
            "is_midi": self.is_midi,
            "mode": self.mode,
            "mappings": list(self.mappings),
            "hardware_target_key": self.hardware_target_key,
            "routing_paused_apps": list(self.routing_paused_apps),
            "inverted": self.inverted,
            "v_sink": self.v_sink,
            "volume_cc": self.volume_cc,
            "volume_channel": self.volume_channel,
            "mute_cc": self.mute_cc,
            "mute_channel": self.mute_channel,
            "saved_fader_volume": self.saved_fader_volume,
        }


@dataclass(frozen=True)
class ProfileRecord:
    """A single canonical profile record with its channels in stable order."""

    id: str
    name: str
    channel_count: int
    restore_fader_positions: bool
    midi_switch_cc: int | None
    channels: tuple[ChannelRecord, ...]

    def to_canonical(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "channel_count": self.channel_count,
            "restore_fader_positions": self.restore_fader_positions,
            "midi_switch_cc": self.midi_switch_cc,
            "channels": [c.to_canonical() for c in self.channels],
        }


@dataclass(frozen=True)
class TargetInventoryItem:
    """A single normalized routing-target inventory entry."""

    key: str
    label: str
    kind: str
    available: bool

    def to_canonical(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "kind": self.kind, "available": self.available}


@dataclass(frozen=True)
class RuntimeChannelState:
    """Runtime (non-persisted) state for a single channel."""

    channel_id: str
    effective_volume: float
    muted: bool
    available: bool
    unresolved: bool
    shared_target: bool
    capability_state: str

    def to_canonical(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "effective_volume": self.effective_volume,
            "muted": self.muted,
            "available": self.available,
            "unresolved": self.unresolved,
            "shared_target": self.shared_target,
            "capability_state": self.capability_state,
        }


@dataclass(frozen=True)
class ReceiverCapabilities:
    """Receiver-advertised capabilities (no machine identity)."""

    supports_v_sink: bool
    supports_midi: bool
    max_channels: int
    features: tuple[str, ...] = field(default_factory=tuple)

    def to_canonical(self) -> dict[str, Any]:
        return {
            "supports_v_sink": self.supports_v_sink,
            "supports_midi": self.supports_midi,
            "max_channels": self.max_channels,
            "features": list(self.features),
        }


@dataclass(frozen=True)
class Snapshot:
    """The full canonical, hashable receiver state snapshot."""

    schema_version: int
    epoch: str
    revision: int
    profiles: tuple[ProfileRecord, ...]
    active_profile_id: str
    active_profile_name: str
    channel_order: tuple[str, ...]
    runtime_states: tuple[RuntimeChannelState, ...]
    inventory: tuple[TargetInventoryItem, ...]
    capabilities: ReceiverCapabilities
    content_hash: str

    def to_canonical(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "epoch": self.epoch,
            "revision": self.revision,
            "profiles": [p.to_canonical() for p in self.profiles],
            "active_profile_id": self.active_profile_id,
            "active_profile_name": self.active_profile_name,
            "channel_order": list(self.channel_order),
            "runtime_states": [r.to_canonical() for r in self.runtime_states],
            "inventory": [i.to_canonical() for i in self.inventory],
            "capabilities": self.capabilities.to_canonical(),
            "content_hash": self.content_hash,
        }


# --------------------------------------------------------------------------
# Canonical JSON encoding + hashing
# --------------------------------------------------------------------------


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode *value* as compact, sorted-key, UTF-8 canonical JSON.

    Validates finiteness/depth/bounds first so malformed input never reaches
    the encoder. Uses ``allow_nan=False`` so any NaN/Infinity that slips
    through (e.g. a non-dataclass caller) still raises rather than emitting
    invalid JSON tokens. ``str.encode("utf-8")`` is also defended: any
    ``UnicodeEncodeError`` (e.g. from a lone surrogate that somehow bypassed
    :func:`validate_finite`) is wrapped as a :class:`SchemaValueError` so
    callers never see a raw ``UnicodeError`` escape this module.
    """
    validate_finite(value)
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SchemaValueError(f"canonical payload contains invalid Unicode: {exc}") from exc
    if len(encoded) > MAX_FRAME_BYTES:
        raise SchemaLimitError(f"encoded payload exceeds max frame size {MAX_FRAME_BYTES}")
    return encoded


def compute_content_hash(canonical_without_hash: Mapping[str, Any]) -> str:
    """Return the deterministic SHA-256 hex digest of a canonical payload.

    *canonical_without_hash* must not contain a ``content_hash`` key; callers
    compute the hash over everything else, then attach it.
    """
    if "content_hash" in canonical_without_hash:
        raise SchemaValueError("content_hash must be computed over a payload without content_hash")
    payload = canonical_json_bytes(canonical_without_hash)
    return hashlib.sha256(payload).hexdigest()


def snapshot_content_hash(snapshot: Snapshot) -> str:
    """Return the canonical hash for *snapshot*, ignoring its stored hash."""
    body = snapshot.to_canonical()
    body.pop("content_hash")
    return compute_content_hash(body)


# --------------------------------------------------------------------------
# Builders (normalize raw, plain-Python input into canonical records)
# --------------------------------------------------------------------------


def normalize_channel(
    raw: Mapping[str, Any],
    *,
    channel_id: str,
    index_fallback: int,
) -> ChannelRecord:
    """Build a :class:`ChannelRecord` from a raw plain mapping.

    Folds legacy ``midi_bindings`` (a list of ``{"cc": ..., "midi_channel":
    ...}`` dicts) into the canonical scalar ``volume_cc``/``volume_channel``
    fields; ``midi_bindings`` itself is never present in the output.
    """
    _reject_forbidden_keys(raw)

    raw_bindings = raw.get("midi_bindings")
    has_binding_slot = isinstance(raw_bindings, list) and bool(raw_bindings) and isinstance(raw_bindings[0], dict)
    if has_binding_slot and isinstance(raw_bindings, list):
        first = raw_bindings[0]
        binding_cc = normalize_midi_cc(first.get("cc"))
        legacy_cc = normalize_midi_cc(raw.get("midi_cc"))
        if binding_cc is not None or legacy_cc is None:
            volume_cc = binding_cc
            volume_channel = normalize_midi_channel(first.get("midi_channel", 0))
        else:
            volume_cc = legacy_cc
            volume_channel = normalize_midi_channel(raw.get("midi_channel", 0))
    else:
        volume_cc = normalize_midi_cc(raw.get("midi_cc"))
        volume_channel = normalize_midi_channel(raw.get("midi_channel", 0))

    mute_cc = normalize_midi_cc(raw.get("midi_mute_cc"))
    mute_channel = normalize_midi_channel(raw.get("midi_mute_channel", 0))

    index_raw = raw.get("index", index_fallback)
    try:
        index = int(index_raw) if not isinstance(index_raw, bool) else index_fallback
    except (TypeError, ValueError):
        index = index_fallback
    if index < 0:
        index = index_fallback

    mode = raw.get("mode", "app")
    if not isinstance(mode, str) or mode not in ALLOWED_CHANNEL_MODES:
        raise SchemaValueError(f"invalid channel mode: {mode!r}")

    mappings = _normalize_string_list(raw.get("app_names", raw.get("mappings", [])), dedup_case_insensitive=True)
    routing_paused_apps = _normalize_string_list(raw.get("routing_paused_apps", []), dedup_case_insensitive=True)

    label = raw.get("label")
    if label is not None and not isinstance(label, str):
        raise SchemaValueError("label must be a string or None")

    hardware_target_key = raw.get("hardware_target_key", raw.get("hardware_id"))
    if hardware_target_key is not None and not isinstance(hardware_target_key, str):
        raise SchemaValueError("hardware_target_key must be a string or None")

    try:
        saved_fader_volume = float(raw.get("volume", raw.get("saved_fader_volume", 1.0)))
    except (TypeError, ValueError):
        saved_fader_volume = 1.0
    if not math.isfinite(saved_fader_volume):
        raise SchemaValueError("saved_fader_volume must be finite")

    return ChannelRecord(
        id=_require_uuid(channel_id, field_name="channel id"),
        index=index,
        label=label,
        is_midi=bool(raw.get("is_midi", False)),
        mode=str(mode),
        mappings=mappings,
        hardware_target_key=hardware_target_key,
        routing_paused_apps=routing_paused_apps,
        inverted=bool(raw.get("inverted", False)),
        v_sink=bool(raw.get("v_sink", False)),
        volume_cc=volume_cc,
        volume_channel=volume_channel,
        mute_cc=mute_cc,
        mute_channel=mute_channel,
        saved_fader_volume=saved_fader_volume,
    )


def normalize_profile(
    raw: Mapping[str, Any],
    *,
    channel_ids: Sequence[str],
) -> ProfileRecord:
    """Build a :class:`ProfileRecord` from a raw plain mapping.

    ``channel_ids`` supplies the stable UUID for each channel in
    ``raw["channels"]`` positional order (the caller/integration layer owns
    UUID assignment/persistence; this module only consumes them).
    """
    _reject_forbidden_keys(raw)

    raw_channels = raw.get("channels", [])
    if not isinstance(raw_channels, list):
        raise SchemaValueError("channels must be a list")
    if len(raw_channels) > MAX_ACTIVE_CHANNELS:
        raise SchemaLimitError(f"profile has more than {MAX_ACTIVE_CHANNELS} channels")
    if len(channel_ids) != len(raw_channels):
        raise SchemaValueError("channel_ids length must match channels length")

    channels = tuple(
        normalize_channel(ch, channel_id=cid, index_fallback=i)
        for i, (ch, cid) in enumerate(zip(raw_channels, channel_ids, strict=True))
    )

    profile_id = raw.get("id")
    if not isinstance(profile_id, str) or not profile_id:
        raise SchemaValueError("profile id must be a non-empty string")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise SchemaValueError("profile name must be a non-empty string")

    try:
        channel_count = int(raw.get("channel_count", len(channels)))
    except (TypeError, ValueError):
        channel_count = len(channels)
    if channel_count != len(channels) or channel_count > MAX_ACTIVE_CHANNELS:
        raise SchemaValueError("channel_count must exactly match the bounded channels list")

    midi_switch_cc = normalize_midi_cc(raw.get("midi_switch_cc"))

    return ProfileRecord(
        id=profile_id,
        name=name,
        channel_count=channel_count,
        restore_fader_positions=bool(raw.get("restore_fader_positions", False)),
        midi_switch_cc=midi_switch_cc,
        channels=channels,
    )


def normalize_inventory_item(raw: Mapping[str, Any]) -> TargetInventoryItem:
    """Build a normalized :class:`TargetInventoryItem` from a raw mapping."""
    _reject_forbidden_keys(raw)
    key = raw.get("key")
    if not isinstance(key, str) or not key:
        raise SchemaValueError("inventory key must be a non-empty string")
    label = raw.get("label", key)
    if not isinstance(label, str):
        raise SchemaValueError("inventory label must be a string")
    kind = raw.get("kind", "output")
    if not isinstance(kind, str) or kind not in ALLOWED_INVENTORY_KINDS:
        raise SchemaValueError(f"invalid inventory kind: {kind!r}")
    return TargetInventoryItem(key=key, label=label, kind=str(kind), available=bool(raw.get("available", True)))


def normalize_runtime_state(raw: Mapping[str, Any]) -> RuntimeChannelState:
    """Build a normalized :class:`RuntimeChannelState` from a raw mapping."""
    _reject_forbidden_keys(raw)
    channel_id = raw.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id:
        raise SchemaValueError("channel_id must be a non-empty string")
    try:
        effective_volume = float(raw.get("effective_volume", 0.0))
    except (TypeError, ValueError):
        raise SchemaValueError("effective_volume must be a number") from None
    if not math.isfinite(effective_volume):
        raise SchemaValueError("effective_volume must be finite")
    capability_state = raw.get("capability_state", "ok")
    if not isinstance(capability_state, str) or capability_state not in ALLOWED_CAPABILITY_STATES:
        raise SchemaValueError(f"invalid capability_state: {capability_state!r}")
    return RuntimeChannelState(
        channel_id=_require_uuid(channel_id, field_name="channel_id"),
        effective_volume=effective_volume,
        muted=bool(raw.get("muted", False)),
        available=bool(raw.get("available", True)),
        unresolved=bool(raw.get("unresolved", False)),
        shared_target=bool(raw.get("shared_target", False)),
        capability_state=str(capability_state),
    )


def build_snapshot(
    *,
    epoch: str,
    revision: int,
    profiles: Sequence[ProfileRecord],
    active_profile_id: str,
    active_profile_name: str,
    channel_order: Sequence[str],
    runtime_states: Sequence[RuntimeChannelState],
    inventory: Sequence[TargetInventoryItem],
    capabilities: ReceiverCapabilities,
) -> Snapshot:
    """Assemble and hash a canonical :class:`Snapshot` from normalized parts.

    Raises:
        SchemaLimitError: if any bounded collection exceeds its limit.
        SchemaValueError: if revision is negative or not representable.
    """
    if revision < 0 or revision > MAX_REVISION:
        raise SchemaValueError("revision must fit in an unsigned 64-bit integer")
    if len(profiles) > MAX_PROFILES:
        raise SchemaLimitError(f"more than {MAX_PROFILES} profiles")
    if len(channel_order) > MAX_ACTIVE_CHANNELS:
        raise SchemaLimitError(f"more than {MAX_ACTIVE_CHANNELS} active channels")
    if len(runtime_states) > MAX_ACTIVE_CHANNELS:
        raise SchemaLimitError(f"more than {MAX_ACTIVE_CHANNELS} runtime states")
    if len(inventory) > MAX_INVENTORY:
        raise SchemaLimitError(f"more than {MAX_INVENTORY} inventory entries")

    epoch = _require_uuid(epoch, field_name="epoch")

    provisional = Snapshot(
        schema_version=SCHEMA_VERSION,
        epoch=epoch,
        revision=revision,
        profiles=tuple(profiles),
        active_profile_id=active_profile_id,
        active_profile_name=active_profile_name,
        channel_order=tuple(channel_order),
        runtime_states=tuple(runtime_states),
        inventory=tuple(inventory),
        capabilities=capabilities,
        content_hash="",
    )
    content_hash = snapshot_content_hash(provisional)
    final = Snapshot(
        schema_version=provisional.schema_version,
        epoch=provisional.epoch,
        revision=provisional.revision,
        profiles=provisional.profiles,
        active_profile_id=provisional.active_profile_id,
        active_profile_name=provisional.active_profile_name,
        channel_order=provisional.channel_order,
        runtime_states=provisional.runtime_states,
        inventory=provisional.inventory,
        capabilities=provisional.capabilities,
        content_hash=content_hash,
    )
    # The builder and strict inbound parser share one contract: anything we
    # publish must also be accepted exactly as encoded by a peer.
    return parse_snapshot(final.to_canonical())


# --------------------------------------------------------------------------
# Strict inbound snapshot parsing (wire bytes -> verified Snapshot)
# --------------------------------------------------------------------------
#
# ``parse_snapshot`` is the receiving-side counterpart to ``build_snapshot``.
# It is intentionally much stricter than the ``normalize_*`` builders above
# (which accept loose, legacy-shaped local data): every record at every
# level must have *exactly* the documented fields, every numeric field must
# be a real ``int``/``float`` (never ``bool``), every UUID must be
# well-formed (and the normalized form is what is retained), every bounded
# list must respect its documented limit, ``channel_order`` and every
# ``runtime_states[].channel_id`` must reference a channel id that actually
# exists in ``profiles[].channels``, and the declared ``content_hash`` must
# verify against a freshly recomputed canonical hash of the payload.

_SNAPSHOT_TOP_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "epoch",
        "revision",
        "profiles",
        "active_profile_id",
        "active_profile_name",
        "channel_order",
        "runtime_states",
        "inventory",
        "capabilities",
        "content_hash",
    }
)
_PROFILE_FIELDS: Final[frozenset[str]] = frozenset(
    {"id", "name", "channel_count", "restore_fader_positions", "midi_switch_cc", "channels"}
)
_CHANNEL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "index",
        "label",
        "is_midi",
        "mode",
        "mappings",
        "hardware_target_key",
        "routing_paused_apps",
        "inverted",
        "v_sink",
        "volume_cc",
        "volume_channel",
        "mute_cc",
        "mute_channel",
        "saved_fader_volume",
    }
)
_INVENTORY_ITEM_FIELDS: Final[frozenset[str]] = frozenset({"key", "label", "kind", "available"})
_RUNTIME_STATE_FIELDS: Final[frozenset[str]] = frozenset(
    {"channel_id", "effective_volume", "muted", "available", "unresolved", "shared_target", "capability_state"}
)
_CAPABILITIES_FIELDS: Final[frozenset[str]] = frozenset(
    {"supports_v_sink", "supports_midi", "max_channels", "features"}
)


def _expect_exact_fields(raw: Mapping[str, Any], required: frozenset[str], type_name: str) -> None:
    keys = set(raw.keys())
    missing = required - keys
    extra = keys - required
    if missing:
        raise SchemaValueError(f"{type_name} missing fields: {sorted(missing)}")
    if extra:
        raise SchemaValueError(f"{type_name} has unknown fields: {sorted(extra)}")


def _expect_mapping(raw: Any, type_name: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise SchemaValueError(f"{type_name} must be an object, got {type(raw)!r}")
    return raw


def _expect_str(raw: Mapping[str, Any], key: str, type_name: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise SchemaValueError(f"{type_name}.{key} must be a string, got {type(value)!r}")
    return value


def _expect_optional_str(raw: Mapping[str, Any], key: str, type_name: str) -> str | None:
    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        raise SchemaValueError(f"{type_name}.{key} must be a string or null, got {type(value)!r}")
    return value


def _expect_bool(raw: Mapping[str, Any], key: str, type_name: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise SchemaValueError(f"{type_name}.{key} must be a boolean, got {type(value)!r}")
    return value


def _expect_int(
    raw: Mapping[str, Any], key: str, type_name: str, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    value = raw.get(key)
    # bool is a subclass of int; reject bool-as-int for every numeric field.
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaValueError(f"{type_name}.{key} must be an integer, got {type(value)!r}")
    if minimum is not None and value < minimum:
        raise SchemaValueError(f"{type_name}.{key} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise SchemaValueError(f"{type_name}.{key} must be <= {maximum}, got {value}")
    return value


def _expect_optional_midi_cc(raw: Mapping[str, Any], key: str, type_name: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaValueError(f"{type_name}.{key} must be an integer or null, got {type(value)!r}")
    if not (0 <= value <= 127):
        raise SchemaValueError(f"{type_name}.{key} must be between 0 and 127, got {value}")
    return value


def _expect_midi_channel(raw: Mapping[str, Any], key: str, type_name: str) -> int:
    return _expect_int(raw, key, type_name, minimum=0, maximum=15)


def _expect_finite_number(raw: Mapping[str, Any], key: str, type_name: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaValueError(f"{type_name}.{key} must be a number, got {type(value)!r}")
    result = float(value)
    if not math.isfinite(result):
        raise SchemaValueError(f"{type_name}.{key} must be finite")
    return result


def _expect_string_list(raw: Mapping[str, Any], key: str, type_name: str, *, max_length: int) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise SchemaValueError(f"{type_name}.{key} must be a list, got {type(value)!r}")
    if len(value) > max_length:
        raise SchemaLimitError(f"{type_name}.{key} exceeds max length {max_length}")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise SchemaValueError(f"{type_name}.{key} entries must be strings, got {item!r}")
        result.append(item)
    return tuple(result)


def _parse_channel_strict(raw: Any, *, type_name: str) -> ChannelRecord:
    channel = _expect_mapping(raw, type_name)
    _expect_exact_fields(channel, _CHANNEL_FIELDS, type_name)
    channel_id = require_uuid(_expect_str(channel, "id", type_name), field_name=f"{type_name}.id")
    index = _expect_int(channel, "index", type_name, minimum=0)
    label = _expect_optional_str(channel, "label", type_name)
    is_midi = _expect_bool(channel, "is_midi", type_name)
    mode = _expect_str(channel, "mode", type_name)
    if mode not in ALLOWED_CHANNEL_MODES:
        raise SchemaValueError(f"{type_name}.mode invalid: {mode!r}")
    mappings = _expect_string_list(channel, "mappings", type_name, max_length=MAX_OTHER_LIST)
    hardware_target_key = _expect_optional_str(channel, "hardware_target_key", type_name)
    routing_paused_apps = _expect_string_list(channel, "routing_paused_apps", type_name, max_length=MAX_OTHER_LIST)
    inverted = _expect_bool(channel, "inverted", type_name)
    v_sink = _expect_bool(channel, "v_sink", type_name)
    volume_cc = _expect_optional_midi_cc(channel, "volume_cc", type_name)
    volume_channel = _expect_midi_channel(channel, "volume_channel", type_name)
    mute_cc = _expect_optional_midi_cc(channel, "mute_cc", type_name)
    mute_channel = _expect_midi_channel(channel, "mute_channel", type_name)
    saved_fader_volume = _expect_finite_number(channel, "saved_fader_volume", type_name)
    return ChannelRecord(
        id=channel_id,
        index=index,
        label=label,
        is_midi=is_midi,
        mode=mode,
        mappings=mappings,
        hardware_target_key=hardware_target_key,
        routing_paused_apps=routing_paused_apps,
        inverted=inverted,
        v_sink=v_sink,
        volume_cc=volume_cc,
        volume_channel=volume_channel,
        mute_cc=mute_cc,
        mute_channel=mute_channel,
        saved_fader_volume=saved_fader_volume,
    )


def _parse_profile_strict(raw: Any, *, type_name: str) -> ProfileRecord:
    profile = _expect_mapping(raw, type_name)
    _expect_exact_fields(profile, _PROFILE_FIELDS, type_name)
    profile_id = _expect_str(profile, "id", type_name)
    if not profile_id:
        raise SchemaValueError(f"{type_name}.id must be non-empty")
    name = _expect_str(profile, "name", type_name)
    if not name:
        raise SchemaValueError(f"{type_name}.name must be non-empty")
    channel_count = _expect_int(profile, "channel_count", type_name, minimum=0, maximum=MAX_ACTIVE_CHANNELS)
    restore_fader_positions = _expect_bool(profile, "restore_fader_positions", type_name)
    midi_switch_cc = _expect_optional_midi_cc(profile, "midi_switch_cc", type_name)
    raw_channels = profile.get("channels")
    if not isinstance(raw_channels, list):
        raise SchemaValueError(f"{type_name}.channels must be a list")
    if len(raw_channels) > MAX_ACTIVE_CHANNELS:
        raise SchemaLimitError(f"{type_name}.channels exceeds max length {MAX_ACTIVE_CHANNELS}")
    channels = tuple(
        _parse_channel_strict(ch, type_name=f"{type_name}.channels[{i}]") for i, ch in enumerate(raw_channels)
    )
    if channel_count != len(channels):
        raise SchemaValueError(f"{type_name}.channel_count must exactly match channels length")
    return ProfileRecord(
        id=profile_id,
        name=name,
        channel_count=channel_count,
        restore_fader_positions=restore_fader_positions,
        midi_switch_cc=midi_switch_cc,
        channels=channels,
    )


def _parse_inventory_item_strict(raw: Any, *, type_name: str) -> TargetInventoryItem:
    item = _expect_mapping(raw, type_name)
    _expect_exact_fields(item, _INVENTORY_ITEM_FIELDS, type_name)
    key = _expect_str(item, "key", type_name)
    if not key:
        raise SchemaValueError(f"{type_name}.key must be non-empty")
    label = _expect_str(item, "label", type_name)
    kind = _expect_str(item, "kind", type_name)
    if kind not in ALLOWED_INVENTORY_KINDS:
        raise SchemaValueError(f"{type_name}.kind invalid: {kind!r}")
    available = _expect_bool(item, "available", type_name)
    return TargetInventoryItem(key=key, label=label, kind=kind, available=available)


def _parse_runtime_state_strict(raw: Any, *, type_name: str) -> RuntimeChannelState:
    rs = _expect_mapping(raw, type_name)
    _expect_exact_fields(rs, _RUNTIME_STATE_FIELDS, type_name)
    channel_id = require_uuid(_expect_str(rs, "channel_id", type_name), field_name=f"{type_name}.channel_id")
    effective_volume = _expect_finite_number(rs, "effective_volume", type_name)
    muted = _expect_bool(rs, "muted", type_name)
    available = _expect_bool(rs, "available", type_name)
    unresolved = _expect_bool(rs, "unresolved", type_name)
    shared_target = _expect_bool(rs, "shared_target", type_name)
    capability_state = _expect_str(rs, "capability_state", type_name)
    if capability_state not in ALLOWED_CAPABILITY_STATES:
        raise SchemaValueError(f"{type_name}.capability_state invalid: {capability_state!r}")
    return RuntimeChannelState(
        channel_id=channel_id,
        effective_volume=effective_volume,
        muted=muted,
        available=available,
        unresolved=unresolved,
        shared_target=shared_target,
        capability_state=capability_state,
    )


def _parse_capabilities_strict(raw: Any, *, type_name: str) -> ReceiverCapabilities:
    caps = _expect_mapping(raw, type_name)
    _expect_exact_fields(caps, _CAPABILITIES_FIELDS, type_name)
    supports_v_sink = _expect_bool(caps, "supports_v_sink", type_name)
    supports_midi = _expect_bool(caps, "supports_midi", type_name)
    max_channels = _expect_int(caps, "max_channels", type_name, minimum=0)
    features = _expect_string_list(caps, "features", type_name, max_length=MAX_OTHER_LIST)
    return ReceiverCapabilities(
        supports_v_sink=supports_v_sink, supports_midi=supports_midi, max_channels=max_channels, features=features
    )


def parse_snapshot(raw: Mapping[str, Any]) -> Snapshot:
    """Strictly parse, cross-validate, and hash-verify an inbound snapshot.

    Raises:
        SchemaLimitError: if any bounded collection (including nested ones
            checked by :func:`validate_finite`) exceeds its documented limit.
        SchemaValueError: for any missing/unknown/mistyped/malformed field,
            an unrecognized enum-like token, a ``channel_order`` or runtime
            state referencing a channel id absent from every profile, a
            duplicate ``channel_order`` entry, or a ``content_hash`` that
            does not verify against a freshly recomputed canonical hash.
    """
    snapshot_obj = _expect_mapping(raw, "snapshot")
    validate_finite(snapshot_obj)
    _expect_exact_fields(snapshot_obj, _SNAPSHOT_TOP_FIELDS, "snapshot")

    # Hash verification happens over the untouched raw payload (the same
    # shape ``snapshot_content_hash``/``build_snapshot`` hash on the sending
    # side), before any field is otherwise interpreted.
    content_hash = _expect_str(snapshot_obj, "content_hash", "snapshot")
    body_without_hash = {key: value for key, value in snapshot_obj.items() if key != "content_hash"}
    expected_hash = compute_content_hash(body_without_hash)
    if expected_hash != content_hash:
        raise SchemaValueError("snapshot content_hash does not verify against its canonical payload")

    schema_version = _expect_int(snapshot_obj, "schema_version", "snapshot", minimum=1)
    if schema_version != SCHEMA_VERSION:
        raise SchemaValueError(f"snapshot.schema_version unsupported: {schema_version} != {SCHEMA_VERSION}")
    epoch = require_uuid(_expect_str(snapshot_obj, "epoch", "snapshot"), field_name="snapshot.epoch")
    revision = _expect_int(snapshot_obj, "revision", "snapshot", minimum=0, maximum=MAX_REVISION)

    raw_profiles = snapshot_obj.get("profiles")
    if not isinstance(raw_profiles, list):
        raise SchemaValueError("snapshot.profiles must be a list")
    if len(raw_profiles) > MAX_PROFILES:
        raise SchemaLimitError(f"snapshot.profiles exceeds max length {MAX_PROFILES}")
    profiles = tuple(
        _parse_profile_strict(p, type_name=f"snapshot.profiles[{i}]") for i, p in enumerate(raw_profiles)
    )
    channel_ids_in_order = list(iter_all_channel_ids(profiles))
    all_channel_ids = set(channel_ids_in_order)
    if len(channel_ids_in_order) != len(all_channel_ids):
        raise SchemaValueError("snapshot contains duplicate channel identities")

    active_profile_id = _expect_str(snapshot_obj, "active_profile_id", "snapshot")
    if not active_profile_id:
        raise SchemaValueError("snapshot.active_profile_id must be non-empty")
    if profiles and active_profile_id not in {p.id for p in profiles}:
        raise SchemaValueError(f"snapshot.active_profile_id {active_profile_id!r} not present in profiles")
    active_profile_name = _expect_str(snapshot_obj, "active_profile_name", "snapshot")

    raw_channel_order = snapshot_obj.get("channel_order")
    if not isinstance(raw_channel_order, list):
        raise SchemaValueError("snapshot.channel_order must be a list")
    if len(raw_channel_order) > MAX_ACTIVE_CHANNELS:
        raise SchemaLimitError(f"snapshot.channel_order exceeds max length {MAX_ACTIVE_CHANNELS}")
    channel_order: list[str] = []
    seen_channel_order: set[str] = set()
    for i, entry in enumerate(raw_channel_order):
        if not isinstance(entry, str):
            raise SchemaValueError(f"snapshot.channel_order[{i}] must be a string, got {type(entry)!r}")
        normalized = require_uuid(entry, field_name=f"snapshot.channel_order[{i}]")
        if normalized in seen_channel_order:
            raise SchemaValueError(f"snapshot.channel_order contains duplicate id: {normalized!r}")
        if normalized not in all_channel_ids:
            raise SchemaValueError(f"snapshot.channel_order references unknown channel id: {normalized!r}")
        seen_channel_order.add(normalized)
        channel_order.append(normalized)

    raw_runtime_states = snapshot_obj.get("runtime_states")
    if not isinstance(raw_runtime_states, list):
        raise SchemaValueError("snapshot.runtime_states must be a list")
    if len(raw_runtime_states) > MAX_ACTIVE_CHANNELS:
        raise SchemaLimitError(f"snapshot.runtime_states exceeds max length {MAX_ACTIVE_CHANNELS}")
    runtime_states: list[RuntimeChannelState] = []
    for i, rs in enumerate(raw_runtime_states):
        parsed_rs = _parse_runtime_state_strict(rs, type_name=f"snapshot.runtime_states[{i}]")
        if parsed_rs.channel_id not in all_channel_ids:
            raise SchemaValueError(
                f"snapshot.runtime_states[{i}].channel_id references unknown channel id: "
                f"{parsed_rs.channel_id!r}"
            )
        runtime_states.append(parsed_rs)

    raw_inventory = snapshot_obj.get("inventory")
    if not isinstance(raw_inventory, list):
        raise SchemaValueError("snapshot.inventory must be a list")
    if len(raw_inventory) > MAX_INVENTORY:
        raise SchemaLimitError(f"snapshot.inventory exceeds max length {MAX_INVENTORY}")
    inventory = tuple(
        _parse_inventory_item_strict(item, type_name=f"snapshot.inventory[{i}]")
        for i, item in enumerate(raw_inventory)
    )

    capabilities = _parse_capabilities_strict(snapshot_obj.get("capabilities"), type_name="snapshot.capabilities")

    parsed = Snapshot(
        schema_version=schema_version,
        epoch=epoch,
        revision=revision,
        profiles=profiles,
        active_profile_id=active_profile_id,
        active_profile_name=active_profile_name,
        channel_order=tuple(channel_order),
        runtime_states=tuple(runtime_states),
        inventory=inventory,
        capabilities=capabilities,
        content_hash=content_hash,
    )
    if snapshot_content_hash(parsed) != content_hash:
        raise SchemaValueError("snapshot UUIDs and content must use their canonical representation")
    return parsed


# --------------------------------------------------------------------------
# Small internal helpers
# --------------------------------------------------------------------------


def _normalize_string_list(value: Any, *, dedup_case_insensitive: bool) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    if len(value) > MAX_OTHER_LIST:
        raise SchemaLimitError(f"list exceeds max length {MAX_OTHER_LIST}")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise SchemaValueError(f"expected string list entry, got {item!r}")
        key = item.lower() if dedup_case_insensitive else item
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(result)


def require_uuid(value: str, *, field_name: str = "value") -> str:
    """Validate that *value* is a well-formed UUID string and return it normalized.

    Raises:
        SchemaValueError: if *value* is not a valid UUID string.
    """
    import uuid as _uuid

    if not isinstance(value, str):
        raise SchemaValueError(f"{field_name} must be a UUID string, got {type(value)!r}")
    try:
        parsed = _uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise SchemaValueError(f"{field_name} must be a valid UUID string: {value!r}") from exc
    return str(parsed)


# Backwards-compatible private alias used internally in this module.
_require_uuid = require_uuid


def iter_all_channel_ids(profiles: Iterable[ProfileRecord]) -> Iterable[str]:
    """Yield every channel id across all profiles, in profile/channel order."""
    for profile in profiles:
        for channel in profile.channels:
            yield channel.id
