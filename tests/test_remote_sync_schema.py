"""Tests for nativmix.remote_sync.schema: canonical wire schema, normalization,
finiteness/limits validation, and the deterministic content hash.
"""

from __future__ import annotations

import copy
import math
import uuid
from dataclasses import replace

import pytest

from nativmix.remote_sync import schema


def _uuid() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------
# Channel normalization: legacy midi_bindings folding
# --------------------------------------------------------------------------


def test_normalize_channel_folds_legacy_midi_bindings_preferring_binding_cc() -> None:
    raw = {
        "index": 0,
        "midi_bindings": [{"cc": 20, "midi_channel": 3}],
        "midi_cc": 5,
        "midi_channel": 0,
    }
    channel = schema.normalize_channel(raw, channel_id=_uuid(), index_fallback=0)
    assert channel.volume_cc == 20
    assert channel.volume_channel == 3
    assert not hasattr(channel, "midi_bindings")
    # The output canonical dict never contains a midi_bindings key.
    assert "midi_bindings" not in channel.to_canonical()


def test_normalize_channel_binding_slot_present_but_invalid_cc_falls_back_to_legacy() -> None:
    raw = {
        "index": 0,
        "midi_bindings": [{"cc": None, "midi_channel": 2}],
        "midi_cc": 7,
        "midi_channel": 4,
    }
    channel = schema.normalize_channel(raw, channel_id=_uuid(), index_fallback=0)
    assert channel.volume_cc == 7
    assert channel.volume_channel == 4


def test_normalize_channel_without_binding_slot_uses_scalar_fields() -> None:
    raw = {"index": 1, "midi_cc": 9, "midi_channel": 2}
    channel = schema.normalize_channel(raw, channel_id=_uuid(), index_fallback=0)
    assert channel.volume_cc == 9
    assert channel.volume_channel == 2


def test_normalize_channel_mute_cc_independent_of_volume_cc() -> None:
    raw = {"index": 0, "midi_cc": 1, "midi_mute_cc": 99, "midi_mute_channel": 5}
    channel = schema.normalize_channel(raw, channel_id=_uuid(), index_fallback=0)
    assert channel.mute_cc == 99
    assert channel.mute_channel == 5


@pytest.mark.parametrize("bad_cc", [-1, 128, "not-a-number"])
def test_normalize_midi_cc_rejects_out_of_range_or_invalid(bad_cc: object) -> None:
    assert schema.normalize_midi_cc(bad_cc) is None


def test_normalize_midi_channel_clamped_to_0_15() -> None:
    assert schema.normalize_midi_channel(-5) == 0
    assert schema.normalize_midi_channel(99) == 15
    assert schema.normalize_midi_channel(True) == 0  # bool must not be treated as a channel number


# --------------------------------------------------------------------------
# Channel normalization: mappings / mode / labels
# --------------------------------------------------------------------------


def test_normalize_channel_mappings_deduped_case_insensitively_preserving_first_casing() -> None:
    raw = {"index": 0, "app_names": ["Firefox", "firefox", "FIREFOX", "Spotify"]}
    channel = schema.normalize_channel(raw, channel_id=_uuid(), index_fallback=0)
    assert channel.mappings == ("Firefox", "Spotify")


def test_normalize_channel_routing_paused_apps_deduped() -> None:
    raw = {"index": 0, "routing_paused_apps": ["App", "app"]}
    channel = schema.normalize_channel(raw, channel_id=_uuid(), index_fallback=0)
    assert channel.routing_paused_apps == ("App",)


def test_normalize_channel_invalid_mode_rejected() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.normalize_channel({"index": 0, "mode": "not-a-mode"}, channel_id=_uuid(), index_fallback=0)


@pytest.mark.parametrize("mode", sorted(schema.ALLOWED_CHANNEL_MODES))
def test_normalize_channel_allowed_modes_accepted(mode: str) -> None:
    channel = schema.normalize_channel({"index": 0, "mode": mode}, channel_id=_uuid(), index_fallback=0)
    assert channel.mode == mode


def test_normalize_channel_negative_index_falls_back() -> None:
    channel = schema.normalize_channel({"index": -3}, channel_id=_uuid(), index_fallback=7)
    assert channel.index == 7


def test_normalize_channel_label_must_be_string_or_none() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.normalize_channel({"index": 0, "label": 123}, channel_id=_uuid(), index_fallback=0)


def test_normalize_channel_saved_fader_volume_defaults_and_parses() -> None:
    channel = schema.normalize_channel({"index": 0, "volume": "0.5"}, channel_id=_uuid(), index_fallback=0)
    assert channel.saved_fader_volume == pytest.approx(0.5)
    default_channel = schema.normalize_channel({"index": 0}, channel_id=_uuid(), index_fallback=0)
    assert default_channel.saved_fader_volume == 1.0


def test_normalize_channel_nonfinite_volume_rejected() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.normalize_channel({"index": 0, "volume": float("inf")}, channel_id=_uuid(), index_fallback=0)


def test_normalize_channel_invalid_channel_id_rejected() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.normalize_channel({"index": 0}, channel_id="not-a-uuid", index_fallback=0)


# --------------------------------------------------------------------------
# Forbidden / excluded machine-local raw keys
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "input_mode",
        "arduino_port",
        "midi_device",
        "remote_role",
        "routing_owner",
        "master_output",
        "autostart",
        "next_profile_cc",
        "midi_fader_feedback",
        "sleep_inhibitor",
        "inhibit_sleep",
        "power_management",
        "controller_transport",
        "pid",
        "node_id",
        "path",
        "address",
        "port",
        "serial",
        "secret",
        "token",
        "log",
    ],
)
def test_normalize_channel_rejects_forbidden_machine_local_keys(forbidden_key: str) -> None:
    raw = {"index": 0, forbidden_key: "anything"}
    with pytest.raises(schema.SchemaValueError, match="excluded machine-local"):
        schema.normalize_channel(raw, channel_id=_uuid(), index_fallback=0)


def test_normalize_profile_rejects_forbidden_keys() -> None:
    raw = {"id": "p1", "name": "Default", "channels": [], "remote_peer": "1.2.3.4"}
    with pytest.raises(schema.SchemaValueError, match="excluded machine-local"):
        schema.normalize_profile(raw, channel_ids=[])


def test_normalize_inventory_item_rejects_forbidden_keys() -> None:
    with pytest.raises(schema.SchemaValueError, match="excluded machine-local"):
        schema.normalize_inventory_item({"key": "sink-1", "address": "127.0.0.1"})


def test_normalize_runtime_state_rejects_forbidden_keys() -> None:
    with pytest.raises(schema.SchemaValueError, match="excluded machine-local"):
        schema.normalize_runtime_state({"channel_id": _uuid(), "pid": 1234})


# --------------------------------------------------------------------------
# Profile normalization
# --------------------------------------------------------------------------


def test_normalize_profile_builds_channels_in_order_with_supplied_ids() -> None:
    cid0, cid1 = _uuid(), _uuid()
    raw = {
        "id": "profile-1",
        "name": "Default",
        "channel_count": 2,
        "restore_fader_positions": True,
        "midi_switch_cc": 10,
        "channels": [{"index": 0}, {"index": 1}],
    }
    profile = schema.normalize_profile(raw, channel_ids=[cid0, cid1])
    assert [c.id for c in profile.channels] == [cid0, cid1]
    assert profile.restore_fader_positions is True
    assert profile.midi_switch_cc == 10


def test_normalize_profile_requires_matching_channel_ids_length() -> None:
    raw = {"id": "p1", "name": "Default", "channels": [{"index": 0}, {"index": 1}]}
    with pytest.raises(schema.SchemaValueError, match="channel_ids length"):
        schema.normalize_profile(raw, channel_ids=[_uuid()])


def test_normalize_profile_requires_non_empty_id_and_name() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.normalize_profile({"id": "", "name": "Default", "channels": []}, channel_ids=[])
    with pytest.raises(schema.SchemaValueError):
        schema.normalize_profile({"id": "p1", "name": "", "channels": []}, channel_ids=[])


def test_normalize_profile_channels_must_be_list() -> None:
    with pytest.raises(schema.SchemaValueError, match="channels must be a list"):
        schema.normalize_profile({"id": "p1", "name": "Default", "channels": "nope"}, channel_ids=[])


def test_normalize_profile_too_many_channels_rejected() -> None:
    too_many = [{"index": i} for i in range(schema.MAX_ACTIVE_CHANNELS + 1)]
    ids = [_uuid() for _ in too_many]
    with pytest.raises(schema.SchemaLimitError):
        schema.normalize_profile({"id": "p1", "name": "Default", "channels": too_many}, channel_ids=ids)


# --------------------------------------------------------------------------
# Inventory / runtime state normalization
# --------------------------------------------------------------------------


def test_normalize_inventory_item_defaults_and_validation() -> None:
    item = schema.normalize_inventory_item({"key": "sink-1"})
    assert item.label == "sink-1"
    assert item.kind == "output"
    assert item.available is True


def test_normalize_inventory_item_invalid_kind_rejected() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.normalize_inventory_item({"key": "sink-1", "kind": "bogus"})


def test_normalize_inventory_item_requires_non_empty_key() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.normalize_inventory_item({"key": ""})


def test_normalize_runtime_state_capability_state_validated() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.normalize_runtime_state({"channel_id": _uuid(), "capability_state": "bogus"})
    state = schema.normalize_runtime_state({"channel_id": _uuid(), "capability_state": "degraded"})
    assert state.capability_state == "degraded"


def test_normalize_runtime_state_nonfinite_effective_volume_rejected() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.normalize_runtime_state({"channel_id": _uuid(), "effective_volume": float("nan")})


def test_normalize_runtime_state_defaults() -> None:
    state = schema.normalize_runtime_state({"channel_id": _uuid()})
    assert state.effective_volume == 0.0
    assert state.muted is False
    assert state.available is True
    assert state.unresolved is False
    assert state.shared_target is False
    assert state.capability_state == "ok"


# --------------------------------------------------------------------------
# validate_finite: depth / list-length / non-finite / type checks
# --------------------------------------------------------------------------


def test_validate_finite_accepts_plain_values() -> None:
    schema.validate_finite({"a": 1, "b": "s", "c": None, "d": True, "e": [1, 2, 3], "f": 1.5})


def test_validate_finite_rejects_nan() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.validate_finite(float("nan"))


def test_validate_finite_rejects_infinity() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.validate_finite(float("inf"))
    with pytest.raises(schema.SchemaValueError):
        schema.validate_finite(float("-inf"))


def test_validate_finite_rejects_non_string_mapping_key() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.validate_finite({1: "a"})  # type: ignore[dict-item]


def test_validate_finite_depth_limit_exceeded() -> None:
    value: object = 0
    for _ in range(schema.MAX_DEPTH + 2):
        value = {"nested": value}
    with pytest.raises(schema.SchemaLimitError):
        schema.validate_finite(value)


def test_validate_finite_depth_limit_at_boundary_accepted() -> None:
    value: object = 0
    for _ in range(schema.MAX_DEPTH):
        value = {"nested": value}
    schema.validate_finite(value)


def test_validate_finite_list_length_limit() -> None:
    """The generic recursive check uses the largest bounded-list limit
    (MAX_INVENTORY) so a legitimately-sized inventory list is never rejected
    here; tighter field-specific limits (e.g. MAX_OTHER_LIST) are enforced
    by their own dedicated builders/parsers, not by this generic check.
    """
    with pytest.raises(schema.SchemaLimitError):
        schema.validate_finite(list(range(schema.MAX_INVENTORY + 1)))
    schema.validate_finite(list(range(schema.MAX_INVENTORY)))
    # A list at MAX_OTHER_LIST (or somewhat beyond it) is still accepted by
    # the generic check alone -- see MAX_INVENTORY reachability above.
    schema.validate_finite(list(range(schema.MAX_OTHER_LIST + 1)))


def test_validate_finite_rejects_lone_surrogate_in_string_value() -> None:
    with pytest.raises(schema.SchemaValueError, match="surrogate"):
        schema.validate_finite("bad \ud800 surrogate")


def test_validate_finite_rejects_lone_surrogate_in_mapping_key() -> None:
    with pytest.raises(schema.SchemaValueError, match="surrogate"):
        schema.validate_finite({"ok\ud800": "value"})


def test_validate_finite_accepts_valid_unicode_strings() -> None:
    schema.validate_finite({"emoji": "\U0001f600", "cjk": "\u4e2d\u6587", "combining": "e\u0301"})


def test_validate_finite_rejects_non_json_type() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.validate_finite(object())


# --------------------------------------------------------------------------
# Canonical JSON encoding + hashing
# --------------------------------------------------------------------------


def test_canonical_json_bytes_sorted_keys_and_compact() -> None:
    payload = {"b": 1, "a": 2}
    encoded = schema.canonical_json_bytes(payload)
    assert encoded == b'{"a":2,"b":1}'


def test_canonical_json_bytes_rejects_nan_via_allow_nan_false() -> None:
    with pytest.raises(schema.SchemaError):
        schema.canonical_json_bytes({"x": float("nan")})


def test_canonical_json_bytes_oversized_payload_rejected() -> None:
    huge = {"data": "x" * (schema.MAX_FRAME_BYTES + 1)}
    with pytest.raises(schema.SchemaLimitError):
        schema.canonical_json_bytes(huge)


def test_compute_content_hash_rejects_payload_already_containing_hash() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.compute_content_hash({"content_hash": "deadbeef"})


def test_compute_content_hash_is_deterministic_for_same_payload() -> None:
    payload = {"a": 1, "b": [1, 2, 3]}
    assert schema.compute_content_hash(payload) == schema.compute_content_hash(dict(payload))


def test_compute_content_hash_changes_when_payload_changes() -> None:
    h1 = schema.compute_content_hash({"a": 1})
    h2 = schema.compute_content_hash({"a": 2})
    assert h1 != h2


def test_compute_content_hash_is_sha256_hex() -> None:
    digest = schema.compute_content_hash({"a": 1})
    assert len(digest) == 64
    int(digest, 16)  # raises ValueError if not valid hex


# --------------------------------------------------------------------------
# build_snapshot: assembly, hashing, and limits
# --------------------------------------------------------------------------


def _make_capabilities() -> schema.ReceiverCapabilities:
    return schema.ReceiverCapabilities(supports_v_sink=True, supports_midi=True, max_channels=8, features=("vsink",))


def _make_minimal_snapshot(
    *, epoch: str | None = None, revision: int = 1, channel_id: str | None = None
) -> schema.Snapshot:
    cid = channel_id or _uuid()
    raw_profile = {"id": "profile-1", "name": "Default", "channel_count": 1, "channels": [{"index": 0}]}
    profile = schema.normalize_profile(raw_profile, channel_ids=[cid])
    runtime = schema.normalize_runtime_state({"channel_id": cid})
    inventory = schema.normalize_inventory_item({"key": "sink-1"})
    return schema.build_snapshot(
        epoch=epoch or _uuid(),
        revision=revision,
        profiles=[profile],
        active_profile_id="profile-1",
        active_profile_name="Default",
        channel_order=[cid],
        runtime_states=[runtime],
        inventory=[inventory],
        capabilities=_make_capabilities(),
    )


def test_build_snapshot_produces_deterministic_hash_for_identical_input() -> None:
    epoch = _uuid()
    cid = _uuid()
    snap1 = _make_minimal_snapshot(epoch=epoch, revision=5, channel_id=cid)
    snap2 = _make_minimal_snapshot(epoch=epoch, revision=5, channel_id=cid)
    assert snap1.content_hash == snap2.content_hash


def test_build_snapshot_hash_matches_snapshot_content_hash_helper() -> None:
    snap = _make_minimal_snapshot()
    assert schema.snapshot_content_hash(snap) == snap.content_hash


def test_build_snapshot_hash_changes_when_revision_changes() -> None:
    epoch = _uuid()
    cid = _uuid()
    snap1 = _make_minimal_snapshot(epoch=epoch, revision=1, channel_id=cid)
    snap2 = _make_minimal_snapshot(epoch=epoch, revision=2, channel_id=cid)
    assert snap1.content_hash != snap2.content_hash


def test_build_snapshot_invalid_epoch_rejected() -> None:
    with pytest.raises(schema.SchemaValueError):
        _make_minimal_snapshot(epoch="not-a-uuid")


@pytest.mark.parametrize("revision", [-1, 2**64])
def test_build_snapshot_revision_out_of_uint64_range_rejected(revision: int) -> None:
    with pytest.raises(schema.SchemaValueError):
        _make_minimal_snapshot(revision=revision)


def test_build_snapshot_max_revision_accepted() -> None:
    snap = _make_minimal_snapshot(revision=2**64 - 1)
    assert snap.revision == 2**64 - 1


def test_build_snapshot_too_many_profiles_rejected() -> None:
    profiles = []
    for i in range(schema.MAX_PROFILES + 1):
        profiles.append(schema.normalize_profile({"id": f"p{i}", "name": "N", "channels": []}, channel_ids=[]))
    with pytest.raises(schema.SchemaLimitError):
        schema.build_snapshot(
            epoch=_uuid(),
            revision=1,
            profiles=profiles,
            active_profile_id="p0",
            active_profile_name="N",
            channel_order=[],
            runtime_states=[],
            inventory=[],
            capabilities=_make_capabilities(),
        )


def test_build_snapshot_too_many_inventory_entries_rejected() -> None:
    inventory = [schema.normalize_inventory_item({"key": f"sink-{i}"}) for i in range(schema.MAX_INVENTORY + 1)]
    with pytest.raises(schema.SchemaLimitError):
        schema.build_snapshot(
            epoch=_uuid(),
            revision=1,
            profiles=[],
            active_profile_id="",
            active_profile_name="",
            channel_order=[],
            runtime_states=[],
            inventory=inventory,
            capabilities=_make_capabilities(),
        )


def test_build_snapshot_too_many_active_channels_rejected() -> None:
    ids = [_uuid() for _ in range(schema.MAX_ACTIVE_CHANNELS + 1)]
    with pytest.raises(schema.SchemaLimitError):
        schema.build_snapshot(
            epoch=_uuid(),
            revision=1,
            profiles=[],
            active_profile_id="",
            active_profile_name="",
            channel_order=ids,
            runtime_states=[],
            inventory=[],
            capabilities=_make_capabilities(),
        )


def test_build_snapshot_to_canonical_round_trips_through_json() -> None:
    import json

    snap = _make_minimal_snapshot()
    encoded = schema.canonical_json_bytes(snap.to_canonical())
    decoded = json.loads(encoded)
    assert decoded["content_hash"] == snap.content_hash
    assert decoded["schema_version"] == schema.SCHEMA_VERSION


def test_iter_all_channel_ids_yields_in_order() -> None:
    cid0, cid1 = _uuid(), _uuid()
    p1 = schema.normalize_profile(
        {"id": "p1", "name": "N", "channels": [{"index": 0}, {"index": 1}]}, channel_ids=[cid0, cid1]
    )
    cid2 = _uuid()
    p2 = schema.normalize_profile({"id": "p2", "name": "N2", "channels": [{"index": 0}]}, channel_ids=[cid2])
    assert list(schema.iter_all_channel_ids([p1, p2])) == [cid0, cid1, cid2]


def test_require_uuid_rejects_malformed_string() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.require_uuid("not-a-uuid", field_name="test")


def test_require_uuid_accepts_and_normalizes() -> None:
    raw = "01234567-89ab-cdef-0123-456789abcdef"
    assert schema.require_uuid(raw, field_name="test") == raw


def test_math_isfinite_sanity_check_used_by_validate_finite() -> None:
    # Guard against accidental regressions to the finiteness predicate itself.
    assert math.isfinite(1.0)
    assert not math.isfinite(float("nan"))


# --------------------------------------------------------------------------
# parse_snapshot: strict inbound parsing, cross-references, hash verification
# --------------------------------------------------------------------------


def _make_rich_snapshot() -> schema.Snapshot:
    cid = _uuid()
    channel_raw = {
        "id": cid,
        "index": 0,
        "label": "Chan 1",
        "is_midi": True,
        "mode": "app",
        "app_names": ["foo"],
        "midi_cc": 10,
        "midi_channel": 2,
        "midi_mute_cc": 11,
        "midi_mute_channel": 3,
        "volume": 0.5,
    }
    profile_raw = {
        "id": "profile-1",
        "name": "Default",
        "channel_count": 1,
        "restore_fader_positions": True,
        "channels": [channel_raw],
    }
    profile = schema.normalize_profile(profile_raw, channel_ids=[cid])
    runtime = schema.normalize_runtime_state(
        {
            "channel_id": cid,
            "effective_volume": 0.5,
            "muted": False,
            "available": True,
            "unresolved": False,
            "shared_target": False,
            "capability_state": "ok",
        }
    )
    inventory = schema.normalize_inventory_item({"key": "sink-1", "label": "Sink 1", "kind": "output"})
    return schema.build_snapshot(
        epoch=_uuid(),
        revision=3,
        profiles=[profile],
        active_profile_id="profile-1",
        active_profile_name="Default",
        channel_order=[cid],
        runtime_states=[runtime],
        inventory=[inventory],
        capabilities=_make_capabilities(),
    )


def test_parse_snapshot_round_trips_a_valid_snapshot() -> None:
    snap = _make_rich_snapshot()
    raw = snap.to_canonical()
    parsed = schema.parse_snapshot(raw)
    assert parsed == snap


def test_parse_snapshot_rejects_noncanonical_uuid_hash_and_duplicate_ids() -> None:
    snap = _make_rich_snapshot()
    uppercase = copy.deepcopy(snap.to_canonical())
    uppercase["epoch"] = str(uppercase["epoch"]).upper()
    uppercase["content_hash"] = schema.compute_content_hash(
        {key: value for key, value in uppercase.items() if key != "content_hash"}
    )
    with pytest.raises(schema.SchemaValueError, match="canonical representation"):
        schema.parse_snapshot(uppercase)

    duplicate = copy.deepcopy(snap.to_canonical())
    duplicate["profiles"][0]["channels"].append(copy.deepcopy(duplicate["profiles"][0]["channels"][0]))
    duplicate["profiles"][0]["channel_count"] = 2
    duplicate["content_hash"] = schema.compute_content_hash(
        {key: value for key, value in duplicate.items() if key != "content_hash"}
    )
    with pytest.raises(schema.SchemaValueError, match="duplicate channel identities"):
        schema.parse_snapshot(duplicate)


def test_profile_channel_count_must_match_and_builder_cannot_emit_invalid_snapshot() -> None:
    snap = _make_rich_snapshot()
    profile = snap.profiles[0]
    raw = {
        "id": profile.id,
        "name": profile.name,
        "channel_count": 2,
        "channels": [{}],
    }
    with pytest.raises(schema.SchemaValueError, match="exactly match"):
        schema.normalize_profile(raw, channel_ids=[profile.channels[0].id])

    invalid_profile = replace(profile, channel_count=2)
    with pytest.raises(schema.SchemaValueError):
        schema.build_snapshot(
            epoch=snap.epoch,
            revision=snap.revision,
            profiles=[invalid_profile],
            active_profile_id=snap.active_profile_id,
            active_profile_name=snap.active_profile_name,
            channel_order=snap.channel_order,
            runtime_states=snap.runtime_states,
            inventory=snap.inventory,
            capabilities=snap.capabilities,
        )


def test_parse_snapshot_round_trips_minimal_snapshot() -> None:
    snap = _make_minimal_snapshot()
    parsed = schema.parse_snapshot(snap.to_canonical())
    assert parsed == snap


def test_parse_snapshot_rejects_non_mapping() -> None:
    with pytest.raises(schema.SchemaValueError):
        schema.parse_snapshot("not-a-mapping")  # type: ignore[arg-type]


def test_parse_snapshot_rejects_missing_top_level_field() -> None:
    raw = dict(_make_minimal_snapshot().to_canonical())
    del raw["revision"]
    with pytest.raises(schema.SchemaValueError, match="missing fields"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_unknown_top_level_field() -> None:
    raw = dict(_make_minimal_snapshot().to_canonical())
    raw["unexpected_extra"] = 1
    with pytest.raises(schema.SchemaValueError, match="unknown fields"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_content_hash_mismatch() -> None:
    raw = dict(_make_minimal_snapshot().to_canonical())
    raw["revision"] = raw["revision"] + 1
    with pytest.raises(schema.SchemaValueError, match="content_hash"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_non_string_content_hash() -> None:
    raw = dict(_make_minimal_snapshot().to_canonical())
    raw["content_hash"] = 12345
    with pytest.raises(schema.SchemaValueError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_wrong_schema_version() -> None:
    snap = _make_minimal_snapshot()
    raw = dict(snap.to_canonical())
    raw["schema_version"] = schema.SCHEMA_VERSION + 1
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError, match="schema_version"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_bool_as_int_schema_version() -> None:
    snap = _make_minimal_snapshot()
    raw = dict(snap.to_canonical())
    raw["schema_version"] = True
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_invalid_epoch_uuid() -> None:
    snap = _make_minimal_snapshot()
    raw = dict(snap.to_canonical())
    raw["epoch"] = "not-a-uuid"
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_bool_as_int_revision() -> None:
    snap = _make_minimal_snapshot()
    raw = dict(snap.to_canonical())
    raw["revision"] = True
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_revision_above_max_revision() -> None:
    snap = _make_minimal_snapshot()
    raw = dict(snap.to_canonical())
    raw["revision"] = schema.MAX_REVISION + 1
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_accepts_revision_at_max_revision() -> None:
    snap = _make_minimal_snapshot(revision=schema.MAX_REVISION)
    parsed = schema.parse_snapshot(snap.to_canonical())
    assert parsed.revision == schema.MAX_REVISION


def test_parse_snapshot_rejects_profiles_not_a_list() -> None:
    snap = _make_minimal_snapshot()
    raw = dict(snap.to_canonical())
    raw["profiles"] = {}
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_too_many_profiles() -> None:
    template_profile = schema.normalize_profile({"id": "p0", "name": "N", "channels": []}, channel_ids=[])
    profiles = [template_profile.to_canonical() | {"id": f"p{i}"} for i in range(schema.MAX_PROFILES + 1)]
    raw = dict(_make_minimal_snapshot().to_canonical())
    raw["profiles"] = profiles
    raw["active_profile_id"] = "p0"
    raw["channel_order"] = []
    raw["runtime_states"] = []
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaLimitError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_profile_missing_field() -> None:
    snap = _make_minimal_snapshot()
    raw = dict(snap.to_canonical())
    profile0 = dict(raw["profiles"][0])
    del profile0["channel_count"]
    raw["profiles"] = [profile0]
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError, match="missing fields"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_profile_unknown_field() -> None:
    snap = _make_minimal_snapshot()
    raw = dict(snap.to_canonical())
    profile0 = dict(raw["profiles"][0])
    profile0["bogus"] = 1
    raw["profiles"] = [profile0]
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError, match="unknown fields"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_profile_not_a_mapping() -> None:
    snap = _make_minimal_snapshot()
    raw = dict(snap.to_canonical())
    raw["profiles"] = ["not-a-mapping"]
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_active_profile_id_not_in_profiles() -> None:
    snap = _make_minimal_snapshot()
    raw = dict(snap.to_canonical())
    raw["active_profile_id"] = "no-such-profile"
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError, match="active_profile_id"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_channel_missing_field() -> None:
    snap = _make_rich_snapshot()
    raw = dict(snap.to_canonical())
    profile0 = dict(raw["profiles"][0])
    channel0 = dict(profile0["channels"][0])
    del channel0["mode"]
    profile0["channels"] = [channel0]
    raw["profiles"] = [profile0]
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError, match="missing fields"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_channel_bool_as_int_index() -> None:
    snap = _make_rich_snapshot()
    raw = dict(snap.to_canonical())
    profile0 = dict(raw["profiles"][0])
    channel0 = dict(profile0["channels"][0])
    channel0["index"] = True
    profile0["channels"] = [channel0]
    raw["profiles"] = [profile0]
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_channel_invalid_mode() -> None:
    snap = _make_rich_snapshot()
    raw = dict(snap.to_canonical())
    profile0 = dict(raw["profiles"][0])
    channel0 = dict(profile0["channels"][0])
    channel0["mode"] = "bogus-mode"
    profile0["channels"] = [channel0]
    raw["profiles"] = [profile0]
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError, match="mode"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_channel_id_invalid_uuid() -> None:
    snap = _make_rich_snapshot()
    raw = dict(snap.to_canonical())
    profile0 = dict(raw["profiles"][0])
    channel0 = dict(profile0["channels"][0])
    channel0["id"] = "not-a-uuid"
    profile0["channels"] = [channel0]
    raw["profiles"] = [profile0]
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_channel_nonfinite_saved_fader_volume() -> None:
    snap = _make_rich_snapshot()
    raw = dict(snap.to_canonical())
    profile0 = dict(raw["profiles"][0])
    channel0 = dict(profile0["channels"][0])
    channel0["saved_fader_volume"] = "nan"
    profile0["channels"] = [channel0]
    raw["profiles"] = [profile0]
    # Deliberately do not fix up content_hash: the field-level type check
    # (must be a number) must fail before hash verification is reached, and
    # either way this must be rejected.
    with pytest.raises(schema.SchemaValueError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_too_many_channels_in_profile() -> None:
    channel_template = schema.normalize_channel({"index": 0}, channel_id=_uuid(), index_fallback=0).to_canonical()
    channels = [channel_template | {"id": _uuid()} for _ in range(schema.MAX_ACTIVE_CHANNELS + 1)]
    raw = dict(_make_minimal_snapshot().to_canonical())
    profile0 = dict(raw["profiles"][0])
    profile0["channels"] = channels
    # channel_count itself has its own <= MAX_ACTIVE_CHANNELS bound; keep it
    # valid so this test exercises the *channels list length* limit
    # specifically, not the channel_count field bound.
    profile0["channel_count"] = schema.MAX_ACTIVE_CHANNELS
    raw["profiles"] = [profile0]
    raw["channel_order"] = []
    raw["runtime_states"] = []
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaLimitError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_inventory_item_invalid_kind() -> None:
    snap = _make_rich_snapshot()
    raw = dict(snap.to_canonical())
    item0 = dict(raw["inventory"][0])
    item0["kind"] = "bogus-kind"
    raw["inventory"] = [item0]
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError, match="kind"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_inventory_not_a_list() -> None:
    raw = dict(_make_minimal_snapshot().to_canonical())
    raw["inventory"] = "not-a-list"
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_too_many_inventory_entries() -> None:
    inv_item = schema.normalize_inventory_item({"key": "sink"}).to_canonical()
    inventory = [inv_item | {"key": f"sink-{i}"} for i in range(schema.MAX_INVENTORY + 1)]
    raw = dict(_make_minimal_snapshot().to_canonical())
    raw["inventory"] = inventory
    # A payload with more than MAX_INVENTORY entries in any list already
    # exceeds the generic finiteness list-length ceiling (which equals
    # MAX_INVENTORY), so even computing a fresh content_hash for it raises
    # SchemaLimitError -- confirming the limit is enforced no matter which
    # stage first encounters the oversized list.
    with pytest.raises(schema.SchemaLimitError):
        schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaLimitError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_accepts_inventory_at_2048_even_though_generic_list_cap_is_1024() -> None:
    # Regression test for item #6: inventory's documented limit (2048) must
    # be independently reachable even though the generic finiteness list
    # check and other fields cap out at MAX_OTHER_LIST (1024).
    inv_item = schema.normalize_inventory_item({"key": "sink"}).to_canonical()
    inventory = [inv_item | {"key": f"sink-{i}"} for i in range(schema.MAX_INVENTORY)]
    assert schema.MAX_INVENTORY > schema.MAX_OTHER_LIST
    raw = dict(_make_minimal_snapshot().to_canonical())
    raw["inventory"] = inventory
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    parsed = schema.parse_snapshot(raw)
    assert len(parsed.inventory) == schema.MAX_INVENTORY


def test_parse_snapshot_rejects_runtime_state_invalid_capability_state() -> None:
    snap = _make_rich_snapshot()
    raw = dict(snap.to_canonical())
    rs0 = dict(raw["runtime_states"][0])
    rs0["capability_state"] = "bogus"
    raw["runtime_states"] = [rs0]
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError, match="capability_state"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_runtime_state_unknown_channel_id() -> None:
    snap = _make_rich_snapshot()
    raw = dict(snap.to_canonical())
    rs0 = dict(raw["runtime_states"][0])
    rs0["channel_id"] = _uuid()
    raw["runtime_states"] = [rs0]
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError, match="unknown channel id"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_channel_order_unknown_channel_id() -> None:
    snap = _make_rich_snapshot()
    raw = dict(snap.to_canonical())
    raw["channel_order"] = [_uuid()]
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError, match="unknown channel id"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_channel_order_duplicate_entries() -> None:
    snap = _make_rich_snapshot()
    raw = dict(snap.to_canonical())
    cid = raw["channel_order"][0]
    raw["channel_order"] = [cid, cid]
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError, match="duplicate"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_channel_order_invalid_uuid_entry() -> None:
    raw = dict(_make_minimal_snapshot().to_canonical())
    raw["channel_order"] = ["not-a-uuid"]
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_capabilities_missing_field() -> None:
    raw = dict(_make_minimal_snapshot().to_canonical())
    caps = dict(raw["capabilities"])
    del caps["max_channels"]
    raw["capabilities"] = caps
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError, match="missing fields"):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_capabilities_not_a_mapping() -> None:
    raw = dict(_make_minimal_snapshot().to_canonical())
    raw["capabilities"] = "not-a-mapping"
    raw["content_hash"] = schema.compute_content_hash({k: v for k, v in raw.items() if k != "content_hash"})
    with pytest.raises(schema.SchemaValueError):
        schema.parse_snapshot(raw)


def test_parse_snapshot_rejects_deeply_nested_extra_structure_via_validate_finite() -> None:
    raw = dict(_make_minimal_snapshot().to_canonical())
    # validate_finite is invoked up-front, so an excessively deep structure
    # anywhere in the payload must be rejected before field-level parsing.
    nested: dict[str, object] = {"x": 0}
    for _ in range(schema.MAX_DEPTH + 2):
        nested = {"x": nested}
    raw["capabilities"] = dict(raw["capabilities"]) | {"__deep__": nested}
    with pytest.raises(schema.SchemaError):
        schema.parse_snapshot(raw)
