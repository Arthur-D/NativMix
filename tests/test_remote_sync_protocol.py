"""Tests for nativmix.remote_sync.protocol: strict typed messages, canonical
JSON codec, and 4-byte length-prefixed framing.
"""

from __future__ import annotations

import json
import struct
import uuid

import pytest

from nativmix.remote_sync import protocol as p
from nativmix.remote_sync import schema
from nativmix.remote_sync.schema import MAX_FRAME_BYTES


def _uuid() -> str:
    return str(uuid.uuid4())


def _ctx(**kwargs: object) -> p.DecodeContext:
    return p.DecodeContext(**kwargs)  # type: ignore[arg-type]


def _make_valid_snapshot_dict() -> dict[str, object]:
    """Build a minimal, hash-consistent canonical snapshot dict for wire tests."""
    profile = schema.normalize_profile({"id": "p1", "name": "N", "channels": []}, channel_ids=[])
    caps = schema.ReceiverCapabilities(supports_v_sink=True, supports_midi=True, max_channels=8, features=())
    snap = schema.build_snapshot(
        epoch=_uuid(),
        revision=1,
        profiles=[profile],
        active_profile_id="p1",
        active_profile_name="N",
        channel_order=[],
        runtime_states=[],
        inventory=[],
        capabilities=caps,
    )
    return snap.to_canonical()


# --------------------------------------------------------------------------
# Round-trip encode/decode for every message type
# --------------------------------------------------------------------------


def test_hello_round_trip() -> None:
    msg = p.Hello(protocol_version=1, schema_version=1, role="controller", instance_id=_uuid(), session_token="tok")
    decoded = p.decode_message(p.encode_message(msg), _ctx())
    assert decoded == msg


def test_hello_ack_round_trip() -> None:
    msg = p.HelloAck(
        protocol_version=1,
        schema_version=1,
        role="receiver",
        instance_id=_uuid(),
        transport_session_id=_uuid(),
        accepted=True,
        reason=None,
    )
    decoded = p.decode_message(p.encode_message(msg), _ctx())
    assert decoded == msg


def test_hello_ack_with_rejection_reason_round_trip() -> None:
    msg = p.HelloAck(
        protocol_version=1,
        schema_version=1,
        role="receiver",
        instance_id=_uuid(),
        transport_session_id=_uuid(),
        accepted=False,
        reason="nope",
    )
    decoded = p.decode_message(p.encode_message(msg), _ctx())
    assert decoded == msg


def test_snapshot_request_round_trip() -> None:
    session_id = _uuid()
    msg = p.SnapshotRequest(protocol_version=1, schema_version=1, transport_session_id=session_id, request_id=_uuid())
    decoded = p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))
    assert decoded == msg


def test_snapshot_round_trip() -> None:
    session_id = _uuid()
    snapshot_dict = _make_valid_snapshot_dict()
    msg = p.SnapshotMessage(
        protocol_version=1, schema_version=1, transport_session_id=session_id, snapshot=snapshot_dict
    )
    decoded = p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))
    assert decoded == msg


def test_delta_round_trip() -> None:
    session_id = _uuid()
    msg = p.DeltaMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        receiver_epoch=_uuid(),
        base_revision=1,
        revision=2,
        resulting_hash="abc",
        changes={"x": 1},
    )
    decoded = p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))
    assert decoded == msg


def test_command_round_trip() -> None:
    session_id = _uuid()
    msg = p.CommandMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        control_session_id=_uuid(),
        command_id=_uuid(),
        receiver_epoch=_uuid(),
        expected_revision=3,
        command_type="set_channel_volume",
        payload={"channel_id": _uuid(), "volume": 0.5},
    )
    decoded = p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))
    assert decoded == msg


def test_ack_round_trip() -> None:
    session_id = _uuid()
    msg = p.AckMessage(
        protocol_version=1, schema_version=1, transport_session_id=session_id, command_id=_uuid(), revision=4
    )
    decoded = p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))
    assert decoded == msg


def test_nack_round_trip() -> None:
    session_id = _uuid()
    msg = p.NackMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        command_id=_uuid(),
        reason="stale_revision",
        current_epoch=_uuid(),
        current_revision=9,
    )
    decoded = p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))
    assert decoded == msg


def test_hash_checkpoint_round_trip() -> None:
    session_id = _uuid()
    msg = p.HashCheckpoint(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        receiver_epoch=_uuid(),
        revision=7,
        content_hash="deadbeef",
    )
    decoded = p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))
    assert decoded == msg


def test_ping_round_trip() -> None:
    session_id = _uuid()
    msg = p.Ping(protocol_version=1, schema_version=1, transport_session_id=session_id, ping_id=_uuid())
    decoded = p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))
    assert decoded == msg


def test_pong_round_trip() -> None:
    session_id = _uuid()
    msg = p.Pong(protocol_version=1, schema_version=1, transport_session_id=session_id, ping_id=_uuid())
    decoded = p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))
    assert decoded == msg


# --------------------------------------------------------------------------
# Unknown / missing / extra fields
# --------------------------------------------------------------------------


def test_unknown_message_type_rejected() -> None:
    payload = json.dumps({"type": "not_a_real_type"}).encode()
    with pytest.raises(p.UnknownMessageTypeError):
        p.decode_message(payload, _ctx())


def test_missing_type_field_rejected() -> None:
    payload = json.dumps({"protocol_version": 1}).encode()
    with pytest.raises(p.UnknownMessageTypeError):
        p.decode_message(payload, _ctx())


def test_missing_required_field_rejected() -> None:
    obj = {"type": "hello", "protocol_version": 1, "schema_version": 1, "role": "controller", "instance_id": _uuid()}
    # session_token missing
    with pytest.raises(p.MalformedMessageError, match="missing fields"):
        p.decode_message(json.dumps(obj).encode(), _ctx())


def test_extra_unknown_field_rejected() -> None:
    msg = p.Ping(protocol_version=1, schema_version=1, transport_session_id=_uuid(), ping_id=_uuid())
    obj = json.loads(p.encode_message(msg))
    obj["bogus_extra_field"] = 1
    with pytest.raises(p.MalformedMessageError, match="unknown fields"):
        p.decode_message(json.dumps(obj).encode(), _ctx())


# --------------------------------------------------------------------------
# bool-as-int rejection
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field_name", ["protocol_version", "schema_version"])
def test_bool_as_int_rejected_for_version_fields(field_name: str) -> None:
    msg = p.Ping(protocol_version=1, schema_version=1, transport_session_id=_uuid(), ping_id=_uuid())
    obj = json.loads(p.encode_message(msg))
    obj[field_name] = True
    with pytest.raises(p.MalformedMessageError, match="must be an integer"):
        p.decode_message(json.dumps(obj).encode(), _ctx())


def test_bool_as_int_rejected_for_revision_field() -> None:
    session_id = _uuid()
    msg = p.AckMessage(
        protocol_version=1, schema_version=1, transport_session_id=session_id, command_id=_uuid(), revision=1
    )
    obj = json.loads(p.encode_message(msg))
    obj["revision"] = False
    with pytest.raises(p.MalformedMessageError, match="must be an integer"):
        p.decode_message(json.dumps(obj).encode(), _ctx(expected_transport_session_id=session_id))


def test_bool_as_int_rejected_for_accepted_field_requires_actual_bool_not_int() -> None:
    # The inverse: accepted must be a real bool, not an int masquerading as one.
    msg = p.HelloAck(
        protocol_version=1,
        schema_version=1,
        role="receiver",
        instance_id=_uuid(),
        transport_session_id=_uuid(),
        accepted=True,
        reason=None,
    )
    obj = json.loads(p.encode_message(msg))
    obj["accepted"] = 1
    with pytest.raises(p.MalformedMessageError, match="must be a boolean"):
        p.decode_message(json.dumps(obj).encode(), _ctx())


# --------------------------------------------------------------------------
# Invalid UUID fields
# --------------------------------------------------------------------------


@pytest.mark.parametrize("uuid_field", ["instance_id"])
def test_invalid_uuid_rejected_in_hello(uuid_field: str) -> None:
    msg = p.Hello(protocol_version=1, schema_version=1, role="controller", instance_id=_uuid(), session_token="tok")
    obj = json.loads(p.encode_message(msg))
    obj[uuid_field] = "not-a-uuid"
    with pytest.raises(p.MalformedMessageError):
        p.decode_message(json.dumps(obj).encode(), _ctx())


def test_invalid_uuid_rejected_in_command_command_id() -> None:
    session_id = _uuid()
    msg = p.CommandMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        control_session_id=_uuid(),
        command_id=_uuid(),
        receiver_epoch=_uuid(),
        expected_revision=1,
        command_type="request_resync",
        payload={},
    )
    obj = json.loads(p.encode_message(msg))
    obj["command_id"] = "12345"
    with pytest.raises(p.MalformedMessageError):
        p.decode_message(json.dumps(obj).encode(), _ctx(expected_transport_session_id=session_id))


# --------------------------------------------------------------------------
# Version mismatch
# --------------------------------------------------------------------------


def test_wrong_protocol_version_rejected() -> None:
    msg = p.Ping(protocol_version=1, schema_version=1, transport_session_id=_uuid(), ping_id=_uuid())
    obj = json.loads(p.encode_message(msg))
    obj["protocol_version"] = 2
    with pytest.raises(p.VersionMismatchError):
        p.decode_message(json.dumps(obj).encode(), _ctx())


def test_wrong_schema_version_rejected() -> None:
    msg = p.Ping(protocol_version=1, schema_version=1, transport_session_id=_uuid(), ping_id=_uuid())
    obj = json.loads(p.encode_message(msg))
    obj["schema_version"] = 999
    with pytest.raises(p.VersionMismatchError):
        p.decode_message(json.dumps(obj).encode(), _ctx())


def test_version_mismatch_checked_against_custom_expected_versions() -> None:
    msg = p.Ping(protocol_version=2, schema_version=3, transport_session_id=_uuid(), ping_id=_uuid())
    decoded = p.decode_message(
        p.encode_message(msg), _ctx(expected_protocol_version=2, expected_schema_version=3)
    )
    assert decoded.protocol_version == 2
    assert decoded.schema_version == 3


# --------------------------------------------------------------------------
# Session mismatch
# --------------------------------------------------------------------------


def test_session_mismatch_rejected() -> None:
    msg = p.Ping(protocol_version=1, schema_version=1, transport_session_id=_uuid(), ping_id=_uuid())
    with pytest.raises(p.SessionMismatchError):
        p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=_uuid()))


def test_session_matches_when_expected_session_matches() -> None:
    session_id = _uuid()
    msg = p.Ping(protocol_version=1, schema_version=1, transport_session_id=session_id, ping_id=_uuid())
    decoded = p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))
    assert decoded.transport_session_id == session_id


def test_no_expected_session_skips_session_check() -> None:
    msg = p.Ping(protocol_version=1, schema_version=1, transport_session_id=_uuid(), ping_id=_uuid())
    decoded = p.decode_message(p.encode_message(msg), _ctx())
    assert isinstance(decoded, p.Ping)


# --------------------------------------------------------------------------
# Role validation
# --------------------------------------------------------------------------


def test_hello_invalid_role_rejected() -> None:
    msg = p.Hello(protocol_version=1, schema_version=1, role="controller", instance_id=_uuid(), session_token="t")
    obj = json.loads(p.encode_message(msg))
    obj["role"] = "not_a_role"
    with pytest.raises(p.RoleMismatchError):
        p.decode_message(json.dumps(obj).encode(), _ctx())


def test_hello_role_must_differ_from_local_role() -> None:
    msg = p.Hello(protocol_version=1, schema_version=1, role="controller", instance_id=_uuid(), session_token="t")
    with pytest.raises(p.RoleMismatchError):
        p.decode_message(p.encode_message(msg), _ctx(local_role="controller"))


def test_hello_role_differing_from_local_role_accepted() -> None:
    msg = p.Hello(protocol_version=1, schema_version=1, role="controller", instance_id=_uuid(), session_token="t")
    decoded = p.decode_message(p.encode_message(msg), _ctx(local_role="receiver"))
    assert decoded.role == "controller"


def test_hello_ack_role_must_differ_from_local_role() -> None:
    msg = p.HelloAck(
        protocol_version=1,
        schema_version=1,
        role="receiver",
        instance_id=_uuid(),
        transport_session_id=_uuid(),
        accepted=True,
    )
    with pytest.raises(p.RoleMismatchError):
        p.decode_message(p.encode_message(msg), _ctx(local_role="receiver"))


# --------------------------------------------------------------------------
# Command allowlist / payload envelope
# --------------------------------------------------------------------------


def test_command_type_not_allowlisted_still_decodes_but_is_not_allowed() -> None:
    # Per review item #10: an unrecognized command_type must decode
    # successfully (so the caller can NACK it) rather than closing/raising
    # at decode time -- the payload envelope itself stays strictly validated.
    session_id = _uuid()
    msg = p.CommandMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        control_session_id=_uuid(),
        command_id=_uuid(),
        receiver_epoch=_uuid(),
        expected_revision=1,
        command_type="delete_everything",
        payload={},
    )
    decoded = p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))
    assert isinstance(decoded, p.CommandMessage)
    assert decoded.command_type == "delete_everything"
    assert p.is_allowed_command_type(decoded.command_type) is False


def test_is_allowed_command_type_true_for_allowlisted_types() -> None:
    for command_type in p.ALLOWED_COMMAND_TYPES:
        assert p.is_allowed_command_type(command_type) is True


def test_is_allowed_command_type_false_for_unknown_type() -> None:
    assert p.is_allowed_command_type("not_a_real_command") is False


def test_command_payload_must_be_object() -> None:
    session_id = _uuid()
    msg = p.CommandMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        control_session_id=_uuid(),
        command_id=_uuid(),
        receiver_epoch=_uuid(),
        expected_revision=1,
        command_type="request_resync",
        payload={},
    )
    obj = json.loads(p.encode_message(msg))
    obj["payload"] = [1, 2, 3]
    with pytest.raises(p.MalformedMessageError, match="must be an object"):
        p.decode_message(json.dumps(obj).encode(), _ctx(expected_transport_session_id=session_id))


def test_allowed_command_types_is_frozenset_and_immutable_reference() -> None:
    assert isinstance(p.ALLOWED_COMMAND_TYPES, frozenset)
    with pytest.raises(AttributeError):
        p.ALLOWED_COMMAND_TYPES.add("new_type")  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# NACK reason validation
# --------------------------------------------------------------------------


def test_nack_invalid_reason_rejected() -> None:
    session_id = _uuid()
    msg = p.NackMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        command_id=_uuid(),
        reason="stale_revision",
        current_epoch=_uuid(),
        current_revision=1,
    )
    obj = json.loads(p.encode_message(msg))
    obj["reason"] = "not_a_real_reason"
    with pytest.raises(p.MalformedMessageError, match="not recognized"):
        p.decode_message(json.dumps(obj).encode(), _ctx(expected_transport_session_id=session_id))


# --------------------------------------------------------------------------
# UTF-8 / non-finite / excessive nesting / non-dict top-level
# --------------------------------------------------------------------------


def test_invalid_utf8_rejected() -> None:
    with pytest.raises(p.MalformedMessageError):
        p.decode_message(b"\xff\xfe\x00\x01", _ctx())


@pytest.mark.parametrize("token", [b"NaN", b"Infinity", b"-Infinity"])
def test_nonfinite_json_constants_rejected(token: bytes) -> None:
    session_id = _uuid().encode()
    payload = (
        b'{"type":"ping","protocol_version":1,"schema_version":1,'
        b'"transport_session_id":"' + session_id + b'","ping_id":' + token + b"}"
    )
    with pytest.raises(p.MalformedMessageError):
        p.decode_message(payload, _ctx())


def test_non_dict_top_level_rejected() -> None:
    with pytest.raises(p.MalformedMessageError):
        p.decode_message(b"[1, 2, 3]", _ctx())


def test_invalid_json_rejected() -> None:
    with pytest.raises(p.MalformedMessageError):
        p.decode_message(b"{not valid json", _ctx())


def test_excessive_nesting_in_snapshot_payload_rejected() -> None:
    from nativmix.remote_sync.schema import MAX_DEPTH

    # Build the nested JSON text directly so the oversized structure only
    # reaches protocol validation at decode time (encode-time validation in
    # schema.canonical_json_bytes would otherwise reject it first).
    nested_json = "0"
    for _ in range(MAX_DEPTH + 2):
        nested_json = '{"n":' + nested_json + "}"
    session_id = _uuid()
    payload = (
        '{"type":"snapshot","protocol_version":1,"schema_version":1,'
        f'"transport_session_id":"{session_id}","snapshot":{{"deep":{nested_json}}}}}'
    ).encode()
    with pytest.raises(p.MalformedMessageError):
        p.decode_message(payload, _ctx(expected_transport_session_id=session_id))


def test_oversized_message_payload_rejected() -> None:
    huge = b"x" * (MAX_FRAME_BYTES + 100)
    with pytest.raises(p.FrameError):
        p.decode_message(huge, _ctx())


# --------------------------------------------------------------------------
# Framing: encode_frame / FrameDecoder
# --------------------------------------------------------------------------


def test_encode_frame_prefixes_four_byte_big_endian_length() -> None:
    payload = b"hello"
    frame = p.encode_frame(payload)
    assert frame[:4] == struct.pack(">I", len(payload))
    assert frame[4:] == payload


def test_encode_frame_rejects_oversized_payload() -> None:
    with pytest.raises(p.FrameError):
        p.encode_frame(b"x" * (MAX_FRAME_BYTES + 1))


def test_frame_decoder_single_frame() -> None:
    decoder = p.FrameDecoder()
    frame = p.encode_frame(b"abc")
    frames = decoder.feed(frame)
    assert frames == [b"abc"]
    assert decoder.pending_bytes() == 0


def test_frame_decoder_incremental_feed_across_header_boundary() -> None:
    decoder = p.FrameDecoder()
    frame = p.encode_frame(b"abcdef")
    assert decoder.feed(frame[:2]) == []
    assert decoder.feed(frame[2:5]) == []
    frames = decoder.feed(frame[5:])
    assert frames == [b"abcdef"]


def test_frame_decoder_multiple_frames_in_one_chunk() -> None:
    decoder = p.FrameDecoder()
    combined = p.encode_frame(b"one") + p.encode_frame(b"two") + p.encode_frame(b"three")
    frames = decoder.feed(combined)
    assert frames == [b"one", b"two", b"three"]


def test_frame_decoder_rejects_oversized_length_header() -> None:
    decoder = p.FrameDecoder()
    bad_header = struct.pack(">I", MAX_FRAME_BYTES + 1)
    with pytest.raises(p.FrameError):
        decoder.feed(bad_header)


def test_frame_decoder_partial_payload_does_not_yield_frame_yet() -> None:
    decoder = p.FrameDecoder()
    frame = p.encode_frame(b"0123456789")
    frames = decoder.feed(frame[:6])  # header + partial payload
    assert frames == []
    assert decoder.pending_bytes() == 6


def test_frame_decoder_end_to_end_reassembly() -> None:
    decoder = p.FrameDecoder()
    frame = p.encode_frame(b"truncated-test-payload")
    collected: list[bytes] = []
    for i in range(len(frame)):
        collected.extend(decoder.feed(frame[i : i + 1]))
    assert collected == [b"truncated-test-payload"]


# --------------------------------------------------------------------------
# MAX_REVISION bound enforced on every wire revision integer (review item #3)
# --------------------------------------------------------------------------


def test_delta_base_revision_above_max_revision_rejected() -> None:
    session_id = _uuid()
    msg = p.DeltaMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        receiver_epoch=_uuid(),
        base_revision=1,
        revision=2,
        resulting_hash="abc",
        changes={},
    )
    obj = json.loads(p.encode_message(msg))
    obj["base_revision"] = p.MAX_REVISION + 1
    with pytest.raises(p.MalformedMessageError):
        p.decode_message(json.dumps(obj).encode(), _ctx(expected_transport_session_id=session_id))


def test_delta_revision_above_max_revision_rejected() -> None:
    session_id = _uuid()
    msg = p.DeltaMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        receiver_epoch=_uuid(),
        base_revision=1,
        revision=2,
        resulting_hash="abc",
        changes={},
    )
    obj = json.loads(p.encode_message(msg))
    obj["revision"] = p.MAX_REVISION + 1
    with pytest.raises(p.MalformedMessageError):
        p.decode_message(json.dumps(obj).encode(), _ctx(expected_transport_session_id=session_id))


def test_delta_revision_at_max_revision_accepted() -> None:
    session_id = _uuid()
    msg = p.DeltaMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        receiver_epoch=_uuid(),
        base_revision=p.MAX_REVISION - 1,
        revision=p.MAX_REVISION,
        resulting_hash="abc",
        changes={},
    )
    decoded = p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))
    assert decoded == msg


def test_command_expected_revision_above_max_revision_rejected() -> None:
    session_id = _uuid()
    msg = p.CommandMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        control_session_id=_uuid(),
        command_id=_uuid(),
        receiver_epoch=_uuid(),
        expected_revision=1,
        command_type="request_resync",
        payload={},
    )
    obj = json.loads(p.encode_message(msg))
    obj["expected_revision"] = p.MAX_REVISION + 1
    with pytest.raises(p.MalformedMessageError):
        p.decode_message(json.dumps(obj).encode(), _ctx(expected_transport_session_id=session_id))


def test_ack_revision_above_max_revision_rejected() -> None:
    session_id = _uuid()
    msg = p.AckMessage(
        protocol_version=1, schema_version=1, transport_session_id=session_id, command_id=_uuid(), revision=1
    )
    obj = json.loads(p.encode_message(msg))
    obj["revision"] = p.MAX_REVISION + 1
    with pytest.raises(p.MalformedMessageError):
        p.decode_message(json.dumps(obj).encode(), _ctx(expected_transport_session_id=session_id))


def test_nack_current_revision_above_max_revision_rejected() -> None:
    session_id = _uuid()
    msg = p.NackMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        command_id=_uuid(),
        reason="stale_revision",
        current_epoch=_uuid(),
        current_revision=1,
    )
    obj = json.loads(p.encode_message(msg))
    obj["current_revision"] = p.MAX_REVISION + 1
    with pytest.raises(p.MalformedMessageError):
        p.decode_message(json.dumps(obj).encode(), _ctx(expected_transport_session_id=session_id))


def test_hash_checkpoint_revision_above_max_revision_rejected() -> None:
    session_id = _uuid()
    msg = p.HashCheckpoint(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        receiver_epoch=_uuid(),
        revision=1,
        content_hash="deadbeef",
    )
    obj = json.loads(p.encode_message(msg))
    obj["revision"] = p.MAX_REVISION + 1
    with pytest.raises(p.MalformedMessageError):
        p.decode_message(json.dumps(obj).encode(), _ctx(expected_transport_session_id=session_id))


def test_revision_bearing_bool_as_int_rejected_for_every_message_type() -> None:
    session_id = _uuid()
    delta = p.DeltaMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        receiver_epoch=_uuid(),
        base_revision=1,
        revision=2,
        resulting_hash="abc",
        changes={},
    )
    obj = json.loads(p.encode_message(delta))
    obj["revision"] = True
    with pytest.raises(p.MalformedMessageError):
        p.decode_message(json.dumps(obj).encode(), _ctx(expected_transport_session_id=session_id))


# --------------------------------------------------------------------------
# schema.parse_snapshot wiring through decode_message (review item #5)
# --------------------------------------------------------------------------


def test_decode_snapshot_rejects_malformed_nested_snapshot_missing_fields() -> None:
    session_id = _uuid()
    msg = p.SnapshotMessage(
        protocol_version=1,
        schema_version=1,
        transport_session_id=session_id,
        snapshot={"not": "a valid canonical snapshot"},
    )
    with pytest.raises(p.MalformedMessageError, match="strict validation"):
        p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))


def test_decode_snapshot_rejects_tampered_content_hash() -> None:
    session_id = _uuid()
    snapshot_dict = _make_valid_snapshot_dict()
    snapshot_dict["revision"] = snapshot_dict["revision"] + 1  # type: ignore[operator]
    msg = p.SnapshotMessage(
        protocol_version=1, schema_version=1, transport_session_id=session_id, snapshot=snapshot_dict
    )
    with pytest.raises(p.MalformedMessageError, match="strict validation"):
        p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))


def test_decode_snapshot_rejects_unknown_channel_order_reference() -> None:
    session_id = _uuid()
    snapshot_dict = _make_valid_snapshot_dict()
    snapshot_dict["channel_order"] = [_uuid()]
    msg = p.SnapshotMessage(
        protocol_version=1, schema_version=1, transport_session_id=session_id, snapshot=snapshot_dict
    )
    with pytest.raises(p.MalformedMessageError, match="strict validation"):
        p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))


def test_decode_snapshot_rejects_wrong_schema_version_inside_nested_snapshot() -> None:
    session_id = _uuid()
    snapshot_dict = _make_valid_snapshot_dict()
    snapshot_dict["schema_version"] = schema.SCHEMA_VERSION + 1
    snapshot_dict["content_hash"] = schema.compute_content_hash(
        {k: v for k, v in snapshot_dict.items() if k != "content_hash"}
    )
    msg = p.SnapshotMessage(
        protocol_version=1, schema_version=1, transport_session_id=session_id, snapshot=snapshot_dict
    )
    with pytest.raises(p.MalformedMessageError, match="strict validation"):
        p.decode_message(p.encode_message(msg), _ctx(expected_transport_session_id=session_id))
