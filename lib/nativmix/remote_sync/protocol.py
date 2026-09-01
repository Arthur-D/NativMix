"""Strict typed message protocol for NativMix remote synchronization.

Every message is a small immutable dataclass with an explicit ``type``
discriminator and an exact set of required fields — no unknown types, no
unknown/missing fields, no ``bool``-as-``int`` field coercion, no
non-finite JSON constants, no invalid UTF-8, no excessive nesting, no
malformed UUIDs, and no unsupported protocol/schema versions.

Wire format: canonical JSON (see ``schema.canonical_json_bytes``) framed
with a 4-byte big-endian length prefix (see :class:`FrameCodec`).

This module has no socket or threading concerns; see ``transport.py``.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from nativmix.remote_sync.schema import (
    MAX_FRAME_BYTES,
    SCHEMA_VERSION,
    SchemaError,
    canonical_json_bytes,
    parse_snapshot,
    require_uuid,
    validate_finite,
)
from nativmix.remote_sync.state import MAX_REVISION, NackReason

#: Wire protocol version. Bump only for breaking message-shape changes.
PROTOCOL_VERSION: int = 1

#: Length of the frame length-prefix, in bytes (big-endian unsigned).
FRAME_HEADER_SIZE: int = 4

#: Valid handshake roles. A valid handshake pairs exactly one of each.
ALLOWED_ROLES: frozenset[str] = frozenset({"receiver", "controller"})

#: Layer 1 allowlisted command types. This is an immutable set of constants;
#: Layer 1 does not mutate it at runtime. Later integration layers may only
#: extend supported commands by shipping a new module constant, never by
#: mutating this collection in place.
ALLOWED_COMMAND_TYPES: frozenset[str] = frozenset(
    {
        "set_channel_volume",
        "set_channel_mute",
        "switch_active_profile",
        "request_resync",
    }
)


def is_allowed_command_type(command_type: str) -> bool:
    """Return whether *command_type* is Layer 1 allowlisted.

    ``decode_message``/``_decode_command`` deliberately still decode a
    ``command`` message with an unrecognized ``command_type`` (an unknown
    command type is a business-level condition the caller should respond to
    with a NACK, not a transport-fatal/malformed-envelope condition that
    closes the connection). Callers should check this after decoding and
    construct a ``NackMessage(reason=NackReason.UNKNOWN_COMMAND_TYPE.value,
    ...)`` themselves when it returns ``False``.
    """
    return command_type in ALLOWED_COMMAND_TYPES


#: All valid message type discriminators.
MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        "hello",
        "hello_ack",
        "snapshot_request",
        "snapshot",
        "delta",
        "command",
        "ack",
        "nack",
        "hash_checkpoint",
        "ping",
        "pong",
    }
)


class ProtocolError(ValueError):
    """Base class for all message/framing validation failures."""


class UnknownMessageTypeError(ProtocolError):
    """Raised when a decoded message's ``type`` is not recognized."""


class MalformedMessageError(ProtocolError):
    """Raised for missing/extra/mistyped fields, bad UUIDs, etc."""


class VersionMismatchError(ProtocolError):
    """Raised when protocol_version/schema_version does not match expectations.

    Callers (transport layer) should treat this as "sync unavailable" for
    the connection rather than a fatal application error, and must never
    let it affect the MIDI layer.
    """


class SessionMismatchError(ProtocolError):
    """Raised when a message's transport_session_id does not match context."""


class RoleMismatchError(ProtocolError):
    """Raised when a hello/hello_ack role is invalid or fails to pair."""


class FrameError(ProtocolError):
    """Raised for malformed or oversized frame headers/payloads."""


# --------------------------------------------------------------------------
# Message dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Hello:
    protocol_version: int
    schema_version: int
    role: str
    instance_id: str
    session_token: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
            "role": self.role,
            "instance_id": self.instance_id,
            "session_token": self.session_token,
        }


@dataclass(frozen=True)
class HelloAck:
    protocol_version: int
    schema_version: int
    role: str
    instance_id: str
    transport_session_id: str
    accepted: bool
    reason: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "hello_ack",
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
            "role": self.role,
            "instance_id": self.instance_id,
            "transport_session_id": self.transport_session_id,
            "accepted": self.accepted,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SnapshotRequest:
    protocol_version: int
    schema_version: int
    transport_session_id: str
    request_id: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "snapshot_request",
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
            "transport_session_id": self.transport_session_id,
            "request_id": self.request_id,
        }


@dataclass(frozen=True)
class SnapshotMessage:
    protocol_version: int
    schema_version: int
    transport_session_id: str
    snapshot: Mapping[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
            "transport_session_id": self.transport_session_id,
            "snapshot": dict(self.snapshot),
        }


@dataclass(frozen=True)
class DeltaMessage:
    protocol_version: int
    schema_version: int
    transport_session_id: str
    receiver_epoch: str
    base_revision: int
    revision: int
    resulting_hash: str
    changes: Mapping[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "delta",
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
            "transport_session_id": self.transport_session_id,
            "receiver_epoch": self.receiver_epoch,
            "base_revision": self.base_revision,
            "revision": self.revision,
            "resulting_hash": self.resulting_hash,
            "changes": dict(self.changes),
        }


@dataclass(frozen=True)
class CommandMessage:
    protocol_version: int
    schema_version: int
    transport_session_id: str
    control_session_id: str
    command_id: str
    receiver_epoch: str
    expected_revision: int
    command_type: str
    payload: Mapping[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "command",
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
            "transport_session_id": self.transport_session_id,
            "control_session_id": self.control_session_id,
            "command_id": self.command_id,
            "receiver_epoch": self.receiver_epoch,
            "expected_revision": self.expected_revision,
            "command_type": self.command_type,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class AckMessage:
    protocol_version: int
    schema_version: int
    transport_session_id: str
    command_id: str
    revision: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "ack",
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
            "transport_session_id": self.transport_session_id,
            "command_id": self.command_id,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class NackMessage:
    protocol_version: int
    schema_version: int
    transport_session_id: str
    command_id: str
    reason: str
    current_epoch: str
    current_revision: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "nack",
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
            "transport_session_id": self.transport_session_id,
            "command_id": self.command_id,
            "reason": self.reason,
            "current_epoch": self.current_epoch,
            "current_revision": self.current_revision,
        }


@dataclass(frozen=True)
class HashCheckpoint:
    protocol_version: int
    schema_version: int
    transport_session_id: str
    receiver_epoch: str
    revision: int
    content_hash: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "hash_checkpoint",
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
            "transport_session_id": self.transport_session_id,
            "receiver_epoch": self.receiver_epoch,
            "revision": self.revision,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class Ping:
    protocol_version: int
    schema_version: int
    transport_session_id: str
    ping_id: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "ping",
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
            "transport_session_id": self.transport_session_id,
            "ping_id": self.ping_id,
        }


@dataclass(frozen=True)
class Pong:
    protocol_version: int
    schema_version: int
    transport_session_id: str
    ping_id: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "pong",
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
            "transport_session_id": self.transport_session_id,
            "ping_id": self.ping_id,
        }


Message = (
    Hello
    | HelloAck
    | SnapshotRequest
    | SnapshotMessage
    | DeltaMessage
    | CommandMessage
    | AckMessage
    | NackMessage
    | HashCheckpoint
    | Ping
    | Pong
)


# --------------------------------------------------------------------------
# Decode context (what the receiving side currently expects)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DecodeContext:
    """What a decoding endpoint currently expects/knows.

    ``expected_transport_session_id`` and ``local_role`` may be ``None``
    before a handshake has established them (i.e. while decoding a
    ``hello``/``hello_ack`` itself).
    """

    expected_protocol_version: int = PROTOCOL_VERSION
    expected_schema_version: int = SCHEMA_VERSION
    expected_transport_session_id: str | None = None
    local_role: str | None = None


# --------------------------------------------------------------------------
# Field-level helpers
# --------------------------------------------------------------------------


def _require_keys(obj: Mapping[str, Any], required: frozenset[str], type_name: str) -> None:
    obj_keys = set(obj.keys())
    missing = required - obj_keys
    extra = obj_keys - required - {"type"}
    if missing:
        raise MalformedMessageError(f"{type_name} message missing fields: {sorted(missing)}")
    if extra:
        raise MalformedMessageError(f"{type_name} message has unknown fields: {sorted(extra)}")


def _require_str(obj: Mapping[str, Any], key: str, type_name: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise MalformedMessageError(f"{type_name}.{key} must be a string, got {type(value)!r}")
    return value


def _require_optional_str(obj: Mapping[str, Any], key: str, type_name: str) -> str | None:
    value = obj.get(key)
    if value is not None and not isinstance(value, str):
        raise MalformedMessageError(f"{type_name}.{key} must be a string or null, got {type(value)!r}")
    return value


def _require_bool(obj: Mapping[str, Any], key: str, type_name: str) -> bool:
    value = obj.get(key)
    if not isinstance(value, bool):
        raise MalformedMessageError(f"{type_name}.{key} must be a boolean, got {type(value)!r}")
    return value


def _require_int(
    obj: Mapping[str, Any], key: str, type_name: str, *, minimum: int = 0, maximum: int | None = None
) -> int:
    value = obj.get(key)
    # bool is a subclass of int in Python; JSON booleans must never satisfy
    # an integer field ("bool-as-int" is explicitly rejected).
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedMessageError(f"{type_name}.{key} must be an integer, got {type(value)!r}")
    if value < minimum:
        raise MalformedMessageError(f"{type_name}.{key} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise MalformedMessageError(f"{type_name}.{key} must be <= {maximum}, got {value}")
    return value


def _require_uuid_field(obj: Mapping[str, Any], key: str, type_name: str) -> str:
    value = _require_str(obj, key, type_name)
    try:
        return require_uuid(value, field_name=f"{type_name}.{key}")
    except SchemaError as exc:
        raise MalformedMessageError(str(exc)) from exc


def _require_mapping(obj: Mapping[str, Any], key: str, type_name: str) -> Mapping[str, Any]:
    value = obj.get(key)
    if not isinstance(value, Mapping):
        raise MalformedMessageError(f"{type_name}.{key} must be an object, got {type(value)!r}")
    return value


def _check_versions(obj: Mapping[str, Any], type_name: str, ctx: DecodeContext) -> tuple[int, int]:
    protocol_version = _require_int(obj, "protocol_version", type_name, minimum=1)
    schema_version = _require_int(obj, "schema_version", type_name, minimum=1)
    if protocol_version != ctx.expected_protocol_version or schema_version != ctx.expected_schema_version:
        raise VersionMismatchError(
            f"{type_name} version mismatch: got protocol={protocol_version} schema={schema_version}, "
            f"expected protocol={ctx.expected_protocol_version} schema={ctx.expected_schema_version}"
        )
    return protocol_version, schema_version


def _check_session(obj: Mapping[str, Any], type_name: str, ctx: DecodeContext) -> str:
    session_id = _require_uuid_field(obj, "transport_session_id", type_name)
    if ctx.expected_transport_session_id is not None and session_id != ctx.expected_transport_session_id:
        raise SessionMismatchError(
            f"{type_name}.transport_session_id {session_id!r} does not match expected "
            f"session {ctx.expected_transport_session_id!r}"
        )
    return session_id


# --------------------------------------------------------------------------
# Per-type decoders
# --------------------------------------------------------------------------

_HELLO_FIELDS = frozenset({"protocol_version", "schema_version", "role", "instance_id", "session_token"})
_HELLO_ACK_FIELDS = frozenset(
    {"protocol_version", "schema_version", "role", "instance_id", "transport_session_id", "accepted", "reason"}
)
_SNAPSHOT_REQUEST_FIELDS = frozenset({"protocol_version", "schema_version", "transport_session_id", "request_id"})
_SNAPSHOT_FIELDS = frozenset({"protocol_version", "schema_version", "transport_session_id", "snapshot"})
_DELTA_FIELDS = frozenset(
    {
        "protocol_version",
        "schema_version",
        "transport_session_id",
        "receiver_epoch",
        "base_revision",
        "revision",
        "resulting_hash",
        "changes",
    }
)
_COMMAND_FIELDS = frozenset(
    {
        "protocol_version",
        "schema_version",
        "transport_session_id",
        "control_session_id",
        "command_id",
        "receiver_epoch",
        "expected_revision",
        "command_type",
        "payload",
    }
)
_ACK_FIELDS = frozenset({"protocol_version", "schema_version", "transport_session_id", "command_id", "revision"})
_NACK_FIELDS = frozenset(
    {
        "protocol_version",
        "schema_version",
        "transport_session_id",
        "command_id",
        "reason",
        "current_epoch",
        "current_revision",
    }
)
_HASH_CHECKPOINT_FIELDS = frozenset(
    {"protocol_version", "schema_version", "transport_session_id", "receiver_epoch", "revision", "content_hash"}
)
_PING_FIELDS = frozenset({"protocol_version", "schema_version", "transport_session_id", "ping_id"})
_PONG_FIELDS = frozenset({"protocol_version", "schema_version", "transport_session_id", "ping_id"})

_VALID_NACK_REASONS = frozenset(reason.value for reason in NackReason)


def _decode_hello(obj: Mapping[str, Any], ctx: DecodeContext) -> Hello:
    _require_keys(obj, _HELLO_FIELDS, "hello")
    _check_versions(obj, "hello", ctx)
    role = _require_str(obj, "role", "hello")
    if role not in ALLOWED_ROLES:
        raise RoleMismatchError(f"hello.role must be one of {sorted(ALLOWED_ROLES)}, got {role!r}")
    if ctx.local_role is not None and role == ctx.local_role:
        raise RoleMismatchError(f"hello.role {role!r} must differ from local role {ctx.local_role!r}")
    instance_id = _require_uuid_field(obj, "instance_id", "hello")
    session_token = _require_str(obj, "session_token", "hello")
    if not session_token:
        raise MalformedMessageError("hello.session_token must not be empty")
    return Hello(
        protocol_version=obj["protocol_version"],
        schema_version=obj["schema_version"],
        role=role,
        instance_id=instance_id,
        session_token=session_token,
    )


def _decode_hello_ack(obj: Mapping[str, Any], ctx: DecodeContext) -> HelloAck:
    _require_keys(obj, _HELLO_ACK_FIELDS, "hello_ack")
    _check_versions(obj, "hello_ack", ctx)
    role = _require_str(obj, "role", "hello_ack")
    if role not in ALLOWED_ROLES:
        raise RoleMismatchError(f"hello_ack.role must be one of {sorted(ALLOWED_ROLES)}, got {role!r}")
    if ctx.local_role is not None and role == ctx.local_role:
        raise RoleMismatchError(f"hello_ack.role {role!r} must differ from local role {ctx.local_role!r}")
    instance_id = _require_uuid_field(obj, "instance_id", "hello_ack")
    transport_session_id = _require_uuid_field(obj, "transport_session_id", "hello_ack")
    accepted = _require_bool(obj, "accepted", "hello_ack")
    reason = _require_optional_str(obj, "reason", "hello_ack")
    return HelloAck(
        protocol_version=obj["protocol_version"],
        schema_version=obj["schema_version"],
        role=role,
        instance_id=instance_id,
        transport_session_id=transport_session_id,
        accepted=accepted,
        reason=reason,
    )


def _decode_snapshot_request(obj: Mapping[str, Any], ctx: DecodeContext) -> SnapshotRequest:
    _require_keys(obj, _SNAPSHOT_REQUEST_FIELDS, "snapshot_request")
    _check_versions(obj, "snapshot_request", ctx)
    session_id = _check_session(obj, "snapshot_request", ctx)
    request_id = _require_uuid_field(obj, "request_id", "snapshot_request")
    return SnapshotRequest(
        protocol_version=obj["protocol_version"],
        schema_version=obj["schema_version"],
        transport_session_id=session_id,
        request_id=request_id,
    )


def _decode_snapshot(obj: Mapping[str, Any], ctx: DecodeContext) -> SnapshotMessage:
    _require_keys(obj, _SNAPSHOT_FIELDS, "snapshot")
    _check_versions(obj, "snapshot", ctx)
    session_id = _check_session(obj, "snapshot", ctx)
    snapshot = _require_mapping(obj, "snapshot", "snapshot")
    # Strict inbound parsing (exact fields at every level, versions, UUIDs,
    # list limits, bool-as-int rejection, channel-id cross-references, and
    # content_hash verification) is delegated to schema.parse_snapshot; the
    # wire message itself still carries the raw dict for shape continuity.
    try:
        parse_snapshot(snapshot)
    except SchemaError as exc:
        raise MalformedMessageError(f"snapshot.snapshot failed strict validation: {exc}") from exc
    return SnapshotMessage(
        protocol_version=obj["protocol_version"],
        schema_version=obj["schema_version"],
        transport_session_id=session_id,
        snapshot=snapshot,
    )


def _decode_delta(obj: Mapping[str, Any], ctx: DecodeContext) -> DeltaMessage:
    _require_keys(obj, _DELTA_FIELDS, "delta")
    _check_versions(obj, "delta", ctx)
    session_id = _check_session(obj, "delta", ctx)
    receiver_epoch = _require_uuid_field(obj, "receiver_epoch", "delta")
    base_revision = _require_int(obj, "base_revision", "delta", minimum=0, maximum=MAX_REVISION)
    revision = _require_int(obj, "revision", "delta", minimum=0, maximum=MAX_REVISION)
    resulting_hash = _require_str(obj, "resulting_hash", "delta")
    changes = _require_mapping(obj, "changes", "delta")
    validate_finite(changes)
    return DeltaMessage(
        protocol_version=obj["protocol_version"],
        schema_version=obj["schema_version"],
        transport_session_id=session_id,
        receiver_epoch=receiver_epoch,
        base_revision=base_revision,
        revision=revision,
        resulting_hash=resulting_hash,
        changes=changes,
    )


def _decode_command(obj: Mapping[str, Any], ctx: DecodeContext) -> CommandMessage:
    _require_keys(obj, _COMMAND_FIELDS, "command")
    _check_versions(obj, "command", ctx)
    session_id = _check_session(obj, "command", ctx)
    control_session_id = _require_uuid_field(obj, "control_session_id", "command")
    command_id = _require_uuid_field(obj, "command_id", "command")
    receiver_epoch = _require_uuid_field(obj, "receiver_epoch", "command")
    expected_revision = _require_int(obj, "expected_revision", "command", minimum=0, maximum=MAX_REVISION)
    command_type = _require_str(obj, "command_type", "command")
    # An unrecognized command_type is a business-level condition the caller
    # should NACK (see is_allowed_command_type), not a decode/framing
    # failure -- the envelope itself (payload) is still validated strictly.
    payload = _require_mapping(obj, "payload", "command")
    validate_finite(payload)
    return CommandMessage(
        protocol_version=obj["protocol_version"],
        schema_version=obj["schema_version"],
        transport_session_id=session_id,
        control_session_id=control_session_id,
        command_id=command_id,
        receiver_epoch=receiver_epoch,
        expected_revision=expected_revision,
        command_type=command_type,
        payload=payload,
    )


def _decode_ack(obj: Mapping[str, Any], ctx: DecodeContext) -> AckMessage:
    _require_keys(obj, _ACK_FIELDS, "ack")
    _check_versions(obj, "ack", ctx)
    session_id = _check_session(obj, "ack", ctx)
    command_id = _require_uuid_field(obj, "command_id", "ack")
    revision = _require_int(obj, "revision", "ack", minimum=0, maximum=MAX_REVISION)
    return AckMessage(
        protocol_version=obj["protocol_version"],
        schema_version=obj["schema_version"],
        transport_session_id=session_id,
        command_id=command_id,
        revision=revision,
    )


def _decode_nack(obj: Mapping[str, Any], ctx: DecodeContext) -> NackMessage:
    _require_keys(obj, _NACK_FIELDS, "nack")
    _check_versions(obj, "nack", ctx)
    session_id = _check_session(obj, "nack", ctx)
    command_id = _require_uuid_field(obj, "command_id", "nack")
    reason = _require_str(obj, "reason", "nack")
    if reason not in _VALID_NACK_REASONS:
        raise MalformedMessageError(f"nack.reason not recognized: {reason!r}")
    current_epoch = _require_uuid_field(obj, "current_epoch", "nack")
    current_revision = _require_int(obj, "current_revision", "nack", minimum=0, maximum=MAX_REVISION)
    return NackMessage(
        protocol_version=obj["protocol_version"],
        schema_version=obj["schema_version"],
        transport_session_id=session_id,
        command_id=command_id,
        reason=reason,
        current_epoch=current_epoch,
        current_revision=current_revision,
    )


def _decode_hash_checkpoint(obj: Mapping[str, Any], ctx: DecodeContext) -> HashCheckpoint:
    _require_keys(obj, _HASH_CHECKPOINT_FIELDS, "hash_checkpoint")
    _check_versions(obj, "hash_checkpoint", ctx)
    session_id = _check_session(obj, "hash_checkpoint", ctx)
    receiver_epoch = _require_uuid_field(obj, "receiver_epoch", "hash_checkpoint")
    revision = _require_int(obj, "revision", "hash_checkpoint", minimum=0, maximum=MAX_REVISION)
    content_hash = _require_str(obj, "content_hash", "hash_checkpoint")
    return HashCheckpoint(
        protocol_version=obj["protocol_version"],
        schema_version=obj["schema_version"],
        transport_session_id=session_id,
        receiver_epoch=receiver_epoch,
        revision=revision,
        content_hash=content_hash,
    )


def _decode_ping(obj: Mapping[str, Any], ctx: DecodeContext) -> Ping:
    _require_keys(obj, _PING_FIELDS, "ping")
    _check_versions(obj, "ping", ctx)
    session_id = _check_session(obj, "ping", ctx)
    ping_id = _require_uuid_field(obj, "ping_id", "ping")
    return Ping(
        protocol_version=obj["protocol_version"],
        schema_version=obj["schema_version"],
        transport_session_id=session_id,
        ping_id=ping_id,
    )


def _decode_pong(obj: Mapping[str, Any], ctx: DecodeContext) -> Pong:
    _require_keys(obj, _PONG_FIELDS, "pong")
    _check_versions(obj, "pong", ctx)
    session_id = _check_session(obj, "pong", ctx)
    ping_id = _require_uuid_field(obj, "ping_id", "pong")
    return Pong(
        protocol_version=obj["protocol_version"],
        schema_version=obj["schema_version"],
        transport_session_id=session_id,
        ping_id=ping_id,
    )


_DECODERS: dict[str, Any] = {
    "hello": _decode_hello,
    "hello_ack": _decode_hello_ack,
    "snapshot_request": _decode_snapshot_request,
    "snapshot": _decode_snapshot,
    "delta": _decode_delta,
    "command": _decode_command,
    "ack": _decode_ack,
    "nack": _decode_nack,
    "hash_checkpoint": _decode_hash_checkpoint,
    "ping": _decode_ping,
    "pong": _decode_pong,
}


# --------------------------------------------------------------------------
# Top-level encode/decode
# --------------------------------------------------------------------------


def encode_message(message: Message) -> bytes:
    """Encode any message dataclass to canonical JSON bytes (unframed)."""
    return canonical_json_bytes(message.to_wire())


def _reject_nonfinite_constant(token: str) -> float:
    raise MalformedMessageError(f"non-finite JSON constant encountered: {token}")


def decode_message(data: bytes, ctx: DecodeContext | None = None) -> Message:
    """Decode and strictly validate a single canonical-JSON message payload.

    Raises:
        MalformedMessageError: invalid UTF-8, invalid JSON, wrong/extra/
            missing fields, invalid UUIDs, non-object payload, bool-as-int,
            excessive nesting/list length, unrecognized nack reason, etc.
        UnknownMessageTypeError: unrecognized ``type`` discriminator.
        VersionMismatchError: protocol_version/schema_version not supported.
        SessionMismatchError: transport_session_id does not match context.
        RoleMismatchError: hello/hello_ack role invalid or fails to pair.
    """
    if ctx is None:
        ctx = DecodeContext()
    if len(data) > MAX_FRAME_BYTES:
        raise FrameError(f"message payload exceeds max frame size {MAX_FRAME_BYTES}")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise MalformedMessageError(f"payload is not valid UTF-8: {exc}") from exc
    try:
        obj = json.loads(text, parse_constant=_reject_nonfinite_constant)
    except json.JSONDecodeError as exc:
        raise MalformedMessageError(f"payload is not valid JSON: {exc}") from exc
    except MalformedMessageError:
        raise
    if not isinstance(obj, dict):
        raise MalformedMessageError(f"top-level message must be a JSON object, got {type(obj)!r}")
    try:
        validate_finite(obj)
    except SchemaError as exc:
        raise MalformedMessageError(str(exc)) from exc
    message_type = obj.get("type")
    if not isinstance(message_type, str) or message_type not in MESSAGE_TYPES:
        raise UnknownMessageTypeError(f"unknown or missing message type: {message_type!r}")
    decoder = _DECODERS[message_type]
    result: Message = decoder(obj, ctx)
    return result


# --------------------------------------------------------------------------
# Length-prefixed framing (4-byte big-endian)
# --------------------------------------------------------------------------

_LENGTH_STRUCT = struct.Struct(">I")


def encode_frame(payload: bytes) -> bytes:
    """Prefix *payload* with a 4-byte big-endian length header.

    Raises:
        FrameError: if *payload* exceeds :data:`MAX_FRAME_BYTES`.
    """
    if len(payload) > MAX_FRAME_BYTES:
        raise FrameError(f"frame payload exceeds max frame size {MAX_FRAME_BYTES}")
    return _LENGTH_STRUCT.pack(len(payload)) + payload


class FrameDecoder:
    """Incremental length-prefixed frame decoder for streaming sockets.

    Feed arbitrary chunks of received bytes via :meth:`feed`; complete frame
    payloads (still framed length-prefix stripped) are returned as they
    become available. Oversized or corrupt length headers raise
    :class:`FrameError` immediately rather than buffering unbounded data.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        frames: list[bytes] = []
        while True:
            if len(self._buffer) < FRAME_HEADER_SIZE:
                break
            (length,) = _LENGTH_STRUCT.unpack_from(self._buffer, 0)
            if length > MAX_FRAME_BYTES:
                raise FrameError(f"frame length {length} exceeds max frame size {MAX_FRAME_BYTES}")
            total = FRAME_HEADER_SIZE + length
            if len(self._buffer) < total:
                # Bound the buffer itself so a slow/partial oversized send
                # cannot accumulate unbounded memory before the header is
                # even checked again.
                if len(self._buffer) > MAX_FRAME_BYTES + FRAME_HEADER_SIZE:
                    raise FrameError("pending buffer exceeds max frame size while awaiting completion")
                break
            frame_payload = bytes(self._buffer[FRAME_HEADER_SIZE:total])
            del self._buffer[:total]
            frames.append(frame_payload)
        return frames

    def pending_bytes(self) -> int:
        return len(self._buffer)
