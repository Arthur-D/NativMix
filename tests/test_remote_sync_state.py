"""Tests for nativmix.remote_sync.state: epoch/revision clock, the publication
contract, sender-side gap/hash/epoch handling, the idempotency cache, and the
pending-command tracker.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from nativmix.remote_sync import schema, state


def _uuid() -> str:
    return str(uuid.uuid4())


def _make_capabilities() -> schema.ReceiverCapabilities:
    return schema.ReceiverCapabilities(supports_v_sink=True, supports_midi=True, max_channels=8, features=())


def _make_snapshot(*, epoch: str, revision: int) -> schema.Snapshot:
    """Build a minimal, hash-consistent Snapshot for a given epoch/revision."""
    profile = schema.normalize_profile({"id": "p1", "name": "N", "channels": []}, channel_ids=[])
    return schema.build_snapshot(
        epoch=epoch,
        revision=revision,
        profiles=[profile],
        active_profile_id="p1",
        active_profile_name="N",
        channel_order=[],
        runtime_states=[],
        inventory=[],
        capabilities=_make_capabilities(),
    )


# --------------------------------------------------------------------------
# RevisionClock: publication contract
# --------------------------------------------------------------------------


def test_revision_clock_starts_at_zero() -> None:
    clock = state.RevisionClock(epoch=_uuid())
    assert clock.revision == 0


def test_revision_clock_first_publish_advances_to_one() -> None:
    clock = state.RevisionClock(epoch=_uuid())
    assert clock.advance() == 1
    assert clock.revision == 1


def test_revision_clock_every_publish_advances_by_exactly_one() -> None:
    clock = state.RevisionClock(epoch=_uuid())
    seen = [clock.advance() for _ in range(5)]
    assert seen == [1, 2, 3, 4, 5]


def test_revision_clock_overflow_rejected() -> None:
    clock = state.RevisionClock(epoch=_uuid(), revision=state.MAX_REVISION)
    with pytest.raises(state.RevisionOverflowError):
        clock.advance()


def test_revision_clock_at_max_minus_one_can_advance_once_more() -> None:
    clock = state.RevisionClock(epoch=_uuid(), revision=state.MAX_REVISION - 1)
    assert clock.advance() == state.MAX_REVISION


# --------------------------------------------------------------------------
# SubscriberState: apply_snapshot
# --------------------------------------------------------------------------


def test_subscriber_state_defaults_to_needs_snapshot() -> None:
    sub = state.SubscriberState()
    assert sub.needs_snapshot is True


def test_subscriber_apply_snapshot_resets_state_and_clears_needs_snapshot() -> None:
    sub = state.SubscriberState()
    epoch = _uuid()
    snap = _make_snapshot(epoch=epoch, revision=1)
    applied = sub.apply_snapshot(snap)
    assert applied is True
    assert sub.epoch == epoch
    assert sub.revision == 1
    assert sub.content_hash == snap.content_hash
    assert sub.needs_snapshot is False
    assert sub.needs_snapshot_reason == state.NeedsSnapshotReason.NONE


def test_subscriber_apply_snapshot_rejects_out_of_range_revision() -> None:
    sub = state.SubscriberState()
    caps = _make_capabilities()
    negative = schema.Snapshot(
        schema_version=schema.SCHEMA_VERSION,
        epoch=_uuid(),
        revision=-1,
        profiles=(),
        active_profile_id="",
        active_profile_name="",
        channel_order=(),
        runtime_states=(),
        inventory=(),
        capabilities=caps,
        content_hash="irrelevant-because-range-checked-first",
    )
    with pytest.raises(state.RevisionOverflowError):
        sub.apply_snapshot(negative)
    too_large = dataclasses.replace(negative, revision=state.MAX_REVISION + 1)
    with pytest.raises(state.RevisionOverflowError):
        sub.apply_snapshot(too_large)


def test_subscriber_apply_snapshot_rejects_tampered_content_hash_and_leaves_state_unmodified() -> None:
    sub = state.SubscriberState()
    epoch = _uuid()
    good = _make_snapshot(epoch=epoch, revision=1)
    sub.apply_snapshot(good)
    tampered = dataclasses.replace(good, content_hash="0" * 64)
    applied = sub.apply_snapshot(tampered)
    assert applied is False
    assert sub.needs_snapshot is True
    assert sub.needs_snapshot_reason == state.NeedsSnapshotReason.HASH_MISMATCH
    # Prior verified state must be untouched by the rejected snapshot.
    assert sub.epoch == epoch
    assert sub.revision == 1
    assert sub.content_hash == good.content_hash


def test_subscriber_apply_snapshot_rejects_content_that_does_not_match_declared_hash() -> None:
    sub = state.SubscriberState()
    epoch = _uuid()
    good = _make_snapshot(epoch=epoch, revision=1)
    # Mutate a field without recomputing the hash: content_hash now
    # describes different content than what is actually present.
    mismatched = dataclasses.replace(good, revision=2)
    applied = sub.apply_snapshot(mismatched)
    assert applied is False
    assert sub.needs_snapshot_reason == state.NeedsSnapshotReason.HASH_MISMATCH


def test_subscriber_rejects_stale_snapshot_without_replacing_valid_state() -> None:
    sub = state.SubscriberState()
    epoch = _uuid()
    current = _make_snapshot(epoch=epoch, revision=3)
    assert sub.apply_snapshot(current)

    assert not sub.apply_snapshot(_make_snapshot(epoch=epoch, revision=2))
    assert sub.revision == 3
    assert sub.content_hash == current.content_hash
    assert sub.needs_snapshot_reason == state.NeedsSnapshotReason.GAP


def test_subscriber_needs_newer_snapshot_after_publication_rejection() -> None:
    sub = state.SubscriberState()
    epoch = _uuid()
    current = _make_snapshot(epoch=epoch, revision=3)
    assert sub.apply_snapshot(current)
    sub._flag(state.NeedsSnapshotReason.HASH_MISMATCH)

    assert not sub.apply_snapshot(current)
    assert sub.needs_snapshot
    assert sub.apply_snapshot(_make_snapshot(epoch=epoch, revision=4))
    assert not sub.needs_snapshot


# --------------------------------------------------------------------------
# SubscriberState: apply_delta contiguity / epoch / hash / overflow
# --------------------------------------------------------------------------


def test_subscriber_requires_snapshot_before_first_delta() -> None:
    sub = state.SubscriberState()
    applied = sub.apply_delta(epoch=_uuid(), base_revision=0, resulting_revision=1, resulting_hash="h")
    assert applied is False
    assert sub.needs_snapshot is True


def test_subscriber_apply_delta_contiguous_succeeds() -> None:
    sub = state.SubscriberState()
    epoch = _uuid()
    sub.apply_snapshot(_make_snapshot(epoch=epoch, revision=1))
    applied = sub.apply_delta(epoch=epoch, base_revision=1, resulting_revision=2, resulting_hash="h2")
    assert applied is True
    assert sub.revision == 2
    assert sub.content_hash == "h2"
    assert sub.needs_snapshot is False


def test_subscriber_apply_delta_gap_triggers_needs_snapshot() -> None:
    sub = state.SubscriberState()
    epoch = _uuid()
    snap = _make_snapshot(epoch=epoch, revision=1)
    sub.apply_snapshot(snap)
    applied = sub.apply_delta(epoch=epoch, base_revision=5, resulting_revision=6, resulting_hash="h2")
    assert applied is False
    assert sub.needs_snapshot is True
    assert sub.needs_snapshot_reason == state.NeedsSnapshotReason.GAP
    # State must remain unchanged after a rejected delta.
    assert sub.revision == 1
    assert sub.content_hash == snap.content_hash


def test_subscriber_apply_delta_epoch_change_triggers_needs_snapshot() -> None:
    sub = state.SubscriberState()
    sub.apply_snapshot(_make_snapshot(epoch=_uuid(), revision=1))
    applied = sub.apply_delta(epoch=_uuid(), base_revision=1, resulting_revision=2, resulting_hash="h2")
    assert applied is False
    assert sub.needs_snapshot_reason == state.NeedsSnapshotReason.EPOCH_CHANGE


def test_subscriber_apply_delta_hash_mismatch_triggers_needs_snapshot() -> None:
    sub = state.SubscriberState()
    epoch = _uuid()
    sub.apply_snapshot(_make_snapshot(epoch=epoch, revision=1))
    applied = sub.apply_delta(
        epoch=epoch,
        base_revision=1,
        resulting_revision=2,
        resulting_hash="claimed-hash",
        verify_hash=lambda: "actually-computed-hash",
    )
    assert applied is False
    assert sub.needs_snapshot_reason == state.NeedsSnapshotReason.HASH_MISMATCH


def test_subscriber_apply_delta_hash_verified_success() -> None:
    sub = state.SubscriberState()
    epoch = _uuid()
    sub.apply_snapshot(_make_snapshot(epoch=epoch, revision=1))
    applied = sub.apply_delta(
        epoch=epoch, base_revision=1, resulting_revision=2, resulting_hash="h2", verify_hash=lambda: "h2"
    )
    assert applied is True
    assert sub.content_hash == "h2"


def test_subscriber_apply_delta_resulting_revision_must_advance_by_exactly_one() -> None:
    # A non-advancing (repeated) resulting_revision is a contiguity/gap
    # issue, not a counter-overflow issue.
    sub = state.SubscriberState()
    epoch = _uuid()
    sub.apply_snapshot(_make_snapshot(epoch=epoch, revision=5))
    applied = sub.apply_delta(epoch=epoch, base_revision=5, resulting_revision=5, resulting_hash="h2")
    assert applied is False
    assert sub.needs_snapshot_reason == state.NeedsSnapshotReason.GAP


def test_subscriber_apply_delta_resulting_revision_skip_ahead_is_a_gap_not_overflow() -> None:
    # base=1 -> resulting=3 advances (3 > 1) and is within uint64 range, but
    # it is not contiguous (skips revision 2), so it must be a GAP.
    sub = state.SubscriberState()
    epoch = _uuid()
    sub.apply_snapshot(_make_snapshot(epoch=epoch, revision=1))
    applied = sub.apply_delta(epoch=epoch, base_revision=1, resulting_revision=3, resulting_hash="h2")
    assert applied is False
    assert sub.needs_snapshot_reason == state.NeedsSnapshotReason.GAP
    assert sub.revision == 1


def test_subscriber_apply_delta_resulting_revision_backward_is_a_gap() -> None:
    sub = state.SubscriberState()
    epoch = _uuid()
    sub.apply_snapshot(_make_snapshot(epoch=epoch, revision=5))
    applied = sub.apply_delta(epoch=epoch, base_revision=5, resulting_revision=4, resulting_hash="h2")
    assert applied is False
    assert sub.needs_snapshot_reason == state.NeedsSnapshotReason.GAP


def test_subscriber_apply_delta_true_overflow_rejected() -> None:
    # base_revision at MAX_REVISION with a contiguous (+1) resulting_revision
    # is the only way to hit the overflow branch specifically.
    sub = state.SubscriberState()
    epoch = _uuid()
    sub.apply_snapshot(_make_snapshot(epoch=epoch, revision=state.MAX_REVISION))
    applied = sub.apply_delta(
        epoch=epoch,
        base_revision=state.MAX_REVISION,
        resulting_revision=state.MAX_REVISION + 1,
        resulting_hash="h2",
    )
    assert applied is False
    assert sub.needs_snapshot_reason == state.NeedsSnapshotReason.OVERFLOW


def test_subscriber_apply_delta_resulting_revision_overflow_rejected() -> None:
    sub = state.SubscriberState()
    epoch = _uuid()
    sub.apply_snapshot(_make_snapshot(epoch=epoch, revision=state.MAX_REVISION - 1))
    applied = sub.apply_delta(
        epoch=epoch,
        base_revision=state.MAX_REVISION - 1,
        resulting_revision=state.MAX_REVISION + 5,
        resulting_hash="h2",
    )
    assert applied is False
    assert sub.needs_snapshot_reason == state.NeedsSnapshotReason.OVERFLOW


def test_subscriber_once_needs_snapshot_further_deltas_rejected_until_new_snapshot() -> None:
    sub = state.SubscriberState()
    epoch = _uuid()
    sub.apply_snapshot(_make_snapshot(epoch=epoch, revision=1))
    sub.apply_delta(epoch=epoch, base_revision=99, resulting_revision=100, resulting_hash="h2")
    assert sub.needs_snapshot is True
    # A subsequent, otherwise-valid delta must still be rejected.
    applied = sub.apply_delta(epoch=epoch, base_revision=1, resulting_revision=2, resulting_hash="h3")
    assert applied is False
    # A fresh snapshot recovers the subscriber.
    sub.apply_snapshot(_make_snapshot(epoch=epoch, revision=100))
    assert sub.needs_snapshot is False


# --------------------------------------------------------------------------
# evaluate_command_epoch_revision: exact stale epoch/revision NACK
# --------------------------------------------------------------------------


def test_evaluate_command_epoch_revision_ok_when_current() -> None:
    epoch = _uuid()
    result = state.evaluate_command_epoch_revision(
        command_epoch=epoch, command_expected_revision=5, current_epoch=epoch, current_revision=5
    )
    assert result is None


def test_evaluate_command_epoch_revision_stale_epoch() -> None:
    result = state.evaluate_command_epoch_revision(
        command_epoch=_uuid(), command_expected_revision=5, current_epoch=_uuid(), current_revision=5
    )
    assert result == state.NackReason.STALE_EPOCH


def test_evaluate_command_epoch_revision_stale_revision() -> None:
    epoch = _uuid()
    result = state.evaluate_command_epoch_revision(
        command_epoch=epoch, command_expected_revision=4, current_epoch=epoch, current_revision=5
    )
    assert result == state.NackReason.STALE_REVISION


def test_evaluate_command_epoch_revision_epoch_checked_before_revision() -> None:
    # Both are wrong: the exact reason returned must be epoch, not revision.
    result = state.evaluate_command_epoch_revision(
        command_epoch=_uuid(), command_expected_revision=1, current_epoch=_uuid(), current_revision=99
    )
    assert result == state.NackReason.STALE_EPOCH


# --------------------------------------------------------------------------
# CommandResultCache: idempotency, bounded eviction
# --------------------------------------------------------------------------


def test_command_result_cache_duplicate_command_id_returns_cached_result() -> None:
    cache = state.CommandResultCache()
    command_id = _uuid()
    result = state.CachedCommandResult(accepted=True, revision=3)
    cache.put(command_id, result)
    assert cache.get(command_id) is result


def test_command_result_cache_unknown_command_id_returns_none() -> None:
    cache = state.CommandResultCache()
    assert cache.get(_uuid()) is None


def test_command_result_cache_bounded_eviction_drops_oldest() -> None:
    cache = state.CommandResultCache(max_size=3)
    ids = [_uuid() for _ in range(4)]
    for i, command_id in enumerate(ids):
        cache.put(command_id, state.CachedCommandResult(accepted=True, revision=i))
    assert len(cache) == 3
    assert cache.get(ids[0]) is None  # oldest evicted
    assert cache.get(ids[-1]) is not None


def test_command_result_cache_default_max_size_matches_documented_limit() -> None:
    cache = state.CommandResultCache()
    for i in range(state.MAX_IDEMPOTENCY_CACHE):
        cache.put(_uuid(), state.CachedCommandResult(accepted=True, revision=i))
    assert len(cache) == state.MAX_IDEMPOTENCY_CACHE
    cache.put(_uuid(), state.CachedCommandResult(accepted=True, revision=99999))
    assert len(cache) == state.MAX_IDEMPOTENCY_CACHE


def test_command_result_cache_rejects_non_positive_max_size() -> None:
    with pytest.raises(state.StateError):
        state.CommandResultCache(max_size=0)


def test_command_result_cache_put_existing_id_updates_and_refreshes_recency() -> None:
    cache = state.CommandResultCache(max_size=2)
    id_a, id_b = _uuid(), _uuid()
    cache.put(id_a, state.CachedCommandResult(accepted=True, revision=1))
    cache.put(id_b, state.CachedCommandResult(accepted=True, revision=2))
    # Refresh id_a's recency so it is not the next eviction victim.
    cache.put(id_a, state.CachedCommandResult(accepted=False, revision=1, reason=state.NackReason.STALE_REVISION))
    id_c = _uuid()
    cache.put(id_c, state.CachedCommandResult(accepted=True, revision=3))
    assert cache.get(id_b) is None  # id_b was least-recently-used, evicted
    assert cache.get(id_a) is not None
    assert cache.get(id_a).accepted is False  # type: ignore[union-attr]


# --------------------------------------------------------------------------
# PendingCommandTracker: bounded pending set + expiry/retry representation
# --------------------------------------------------------------------------


def test_pending_command_tracker_register_and_ack() -> None:
    tracker = state.PendingCommandTracker()
    command_id = _uuid()
    tracker.register(command_id, now=0.0)
    assert tracker.is_pending(command_id) is True
    tracker.acknowledge(command_id)
    assert tracker.is_pending(command_id) is False


def test_pending_command_tracker_register_same_id_twice_is_idempotent() -> None:
    tracker = state.PendingCommandTracker()
    command_id = _uuid()
    tracker.register(command_id, now=0.0)
    tracker.register(command_id, now=1.0)
    assert len(tracker) == 1


def test_pending_command_tracker_overflow_raises() -> None:
    tracker = state.PendingCommandTracker(max_size=2)
    tracker.register(_uuid(), now=0.0)
    tracker.register(_uuid(), now=0.0)
    with pytest.raises(state.PendingCommandOverflowError):
        tracker.register(_uuid(), now=0.0)


def test_pending_command_tracker_default_max_size_matches_documented_limit() -> None:
    tracker = state.PendingCommandTracker()
    for _ in range(state.MAX_PENDING_COMMANDS):
        tracker.register(_uuid(), now=0.0)
    assert len(tracker) == state.MAX_PENDING_COMMANDS
    with pytest.raises(state.PendingCommandOverflowError):
        tracker.register(_uuid(), now=0.0)


def test_pending_command_tracker_rejects_non_positive_max_size() -> None:
    with pytest.raises(state.StateError):
        state.PendingCommandTracker(max_size=0)


def test_pending_command_tracker_expire_stale_after_deadline() -> None:
    tracker = state.PendingCommandTracker(deadline_seconds=5.0)
    command_id = _uuid()
    tracker.register(command_id, now=0.0)
    assert tracker.expire_stale(now=4.9) == []
    expired = tracker.expire_stale(now=5.0)
    assert len(expired) == 1
    assert expired[0].command_id == command_id
    assert expired[0].retries == 0


def test_pending_command_tracker_expired_entry_re_registered_with_incremented_retry() -> None:
    tracker = state.PendingCommandTracker(deadline_seconds=5.0)
    command_id = _uuid()
    tracker.register(command_id, now=0.0)
    tracker.expire_stale(now=5.0)
    # Still tracked as pending (for the caller to resend), but now at a
    # refreshed send time with retries incremented.
    assert tracker.is_pending(command_id) is True
    expired_again = tracker.expire_stale(now=9.9)
    assert expired_again == []
    expired_again = tracker.expire_stale(now=10.0)
    assert len(expired_again) == 1
    assert expired_again[0].retries == 1


def test_pending_command_tracker_apply_deadline_default_is_five_seconds() -> None:
    assert state.COMMAND_APPLY_DEADLINE_SECONDS == 5.0
    tracker = state.PendingCommandTracker()
    command_id = _uuid()
    tracker.register(command_id, now=100.0)
    assert tracker.expire_stale(now=104.9) == []
    assert len(tracker.expire_stale(now=105.0)) == 1
