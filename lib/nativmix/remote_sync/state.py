"""Revision/epoch state machine for NativMix remote synchronization.

This module is deliberately independent of any transport or GUI code. It
models:

* The receiver-owned **epoch** (a random UUID chosen once per receiver
  process/session) and **revision** clock (a ``uint64`` counter starting at
  0).
* The **publication contract**: every publication (snapshot or delta)
  advances the revision by exactly one, including the very first one. A
  freshly constructed :class:`RevisionClock` is at revision 0, meaning
  "nothing has been published yet"; the first ``advance()`` call moves it to
  revision 1, which becomes the revision of the first published snapshot.
* The **sender-side (subscriber) state machine**, which only applies
  contiguous, current-epoch revisions and flips to ``needs_snapshot`` on a
  gap, an epoch change, or a resulting-hash mismatch.
* A bounded **idempotency/result cache** (max 2048 entries) so duplicate
  command IDs return the cached ACK/NACK without re-applying the command.
* A bounded **pending-command tracker** (max 128 entries) with a 5 second
  apply deadline, used to represent commands awaiting acknowledgement and to
  detect dropped ACKs that should be retried.

No network I/O, JSON, or dataclass wire-format concerns live here; see
``protocol.py`` for message shapes and ``transport.py`` for the socket layer.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from nativmix.remote_sync.schema import Snapshot, snapshot_content_hash

#: Maximum number of cached command results (idempotency cache).
MAX_IDEMPOTENCY_CACHE: int = 2048

#: Maximum number of concurrently tracked pending (unacknowledged) commands.
MAX_PENDING_COMMANDS: int = 128

#: Seconds a sent command may remain unacknowledged before it is considered
#: for a dropped-ACK retry.
COMMAND_APPLY_DEADLINE_SECONDS: float = 5.0

#: Maximum value representable by the uint64 revision counter.
MAX_REVISION: int = 2**64 - 1


class StateError(ValueError):
    """Base class for state-machine errors in this module."""


class RevisionOverflowError(StateError):
    """Raised when advancing the revision clock would exceed ``uint64``."""


class PendingCommandOverflowError(StateError):
    """Raised when the pending-command tracker is already at capacity."""


class NeedsSnapshotReason(str, Enum):
    """Why a subscriber has fallen out of delta-sync and needs a full snapshot."""

    NONE = "none"
    GAP = "gap"
    EPOCH_CHANGE = "epoch_change"
    HASH_MISMATCH = "hash_mismatch"
    OVERFLOW = "overflow"


class NackReason(str, Enum):
    """Exact reasons a receiver rejects a command with a NACK."""

    STALE_EPOCH = "stale_epoch"
    STALE_REVISION = "stale_revision"
    UNKNOWN_COMMAND_TYPE = "unknown_command_type"
    INVALID_PAYLOAD = "invalid_payload"
    PERMISSION_DISABLED = "permission_disabled"
    NO_ACTIVE_SESSION = "no_active_session"
    SESSION_MISMATCH = "session_mismatch"
    GENERATION_MISMATCH = "generation_mismatch"
    ROLE_MISMATCH = "role_mismatch"
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    DESTRUCTIVE_RATE_LIMITED = "destructive_rate_limited"
    APPLY_FAILED = "apply_failed"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    WRONG_THREAD = "wrong_thread"


def new_epoch(uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4) -> str:
    """Return a freshly generated receiver epoch string."""
    return str(uuid_factory())


@dataclass
class RevisionClock:
    """A monotonic ``uint64`` revision counter bound to a single epoch."""

    epoch: str
    revision: int = 0

    def advance(self) -> int:
        """Advance the clock by one and return the new revision.

        Every publication (the first snapshot included) calls this exactly
        once, so the first published revision is always 1.
        """
        if self.revision >= MAX_REVISION:
            raise RevisionOverflowError("revision counter would overflow uint64")
        self.revision += 1
        return self.revision


# --------------------------------------------------------------------------
# Sender-side (subscriber) state tracking
# --------------------------------------------------------------------------


@dataclass
class SubscriberState:
    """Tracks what a sync subscriber (sender of state, receiver of sync data)
    currently believes about the remote receiver's epoch/revision/hash.

    Despite the name mirroring "subscriber", this is the side that *consumes*
    snapshots/deltas emitted by a :class:`RevisionClock` owner; it does not
    itself own the canonical epoch/revision.
    """

    epoch: str | None = None
    revision: int = 0
    content_hash: str | None = None
    needs_snapshot: bool = True
    needs_snapshot_reason: NeedsSnapshotReason = NeedsSnapshotReason.NONE

    def apply_snapshot(self, snapshot: Snapshot) -> bool:
        """Accept a full :class:`~nativmix.remote_sync.schema.Snapshot`.

        This is the only accepted input shape: a snapshot's declared
        ``content_hash`` is re-verified against a freshly recomputed
        canonical hash of its own content before anything is applied. If the
        hash does not verify, local state is left completely unmodified and
        this flags ``HASH_MISMATCH`` (the caller should treat sync as
        unavailable/needing a fresh snapshot rather than trusting a
        corrupted one).

        Returns:
            True if the snapshot was verified and applied, False if the
            hash did not verify (``needs_snapshot``/``needs_snapshot_reason``
            explain why).
        """
        if snapshot.revision < 0 or snapshot.revision > MAX_REVISION:
            raise RevisionOverflowError("snapshot revision out of uint64 range")
        if snapshot_content_hash(snapshot) != snapshot.content_hash:
            self._flag(NeedsSnapshotReason.HASH_MISMATCH)
            return False
        self.epoch = snapshot.epoch
        self.revision = snapshot.revision
        self.content_hash = snapshot.content_hash
        self.needs_snapshot = False
        self.needs_snapshot_reason = NeedsSnapshotReason.NONE
        return True

    def apply_delta(
        self,
        *,
        epoch: str,
        base_revision: int,
        resulting_revision: int,
        resulting_hash: str,
        verify_hash: Callable[[], str] | None = None,
    ) -> bool:
        """Attempt to apply a delta on top of current state.

        Returns True if applied, False if the subscriber now needs a full
        snapshot (``needs_snapshot`` / ``needs_snapshot_reason`` explain why).
        A delta is only applied when:

        * The subscriber is not already flagged ``needs_snapshot``.
        * ``epoch`` matches the currently tracked epoch.
        * ``base_revision`` equals the subscriber's current revision
          (contiguity — no gaps).
        * ``resulting_revision`` is exactly ``base_revision + 1`` (strict
          contiguity — a delta that repeats, goes backward, or skips ahead
          is rejected as a gap, not silently accepted).
        * ``resulting_revision`` does not overflow ``uint64``.
        * If *verify_hash* is given, calling it after applying produces a
          value equal to ``resulting_hash``.
        """
        if self.needs_snapshot or self.epoch is None:
            self._flag(NeedsSnapshotReason.EPOCH_CHANGE if self.epoch is None else self.needs_snapshot_reason)
            return False
        if epoch != self.epoch:
            self._flag(NeedsSnapshotReason.EPOCH_CHANGE)
            return False
        if base_revision != self.revision:
            self._flag(NeedsSnapshotReason.GAP)
            return False
        if resulting_revision > MAX_REVISION:
            self._flag(NeedsSnapshotReason.OVERFLOW)
            return False
        if resulting_revision != base_revision + 1:
            self._flag(NeedsSnapshotReason.GAP)
            return False
        if verify_hash is not None and verify_hash() != resulting_hash:
            self._flag(NeedsSnapshotReason.HASH_MISMATCH)
            return False
        self.revision = resulting_revision
        self.content_hash = resulting_hash
        return True

    def _flag(self, reason: NeedsSnapshotReason) -> None:
        self.needs_snapshot = True
        self.needs_snapshot_reason = reason


# --------------------------------------------------------------------------
# Receiver-side command evaluation (exact stale epoch/revision NACK)
# --------------------------------------------------------------------------


def evaluate_command_epoch_revision(
    *,
    command_epoch: str,
    command_expected_revision: int,
    current_epoch: str,
    current_revision: int,
) -> NackReason | None:
    """Return the exact NACK reason for a command against current state.

    Returns ``None`` when the command's epoch and expected revision are
    exactly current (i.e. it may proceed to type/payload validation).
    """
    if command_epoch != current_epoch:
        return NackReason.STALE_EPOCH
    if command_expected_revision != current_revision:
        return NackReason.STALE_REVISION
    return None


# --------------------------------------------------------------------------
# Idempotency / result cache
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CachedCommandResult:
    """A cached ACK/NACK outcome for a previously processed command."""

    accepted: bool
    revision: int
    reason: NackReason | None = None


class CommandResultCache:
    """Bounded FIFO cache mapping command UUID -> cached result.

    Duplicate command IDs must return the cached result without reapplying
    the command. Capacity defaults to :data:`MAX_IDEMPOTENCY_CACHE`; the
    oldest entry is evicted once capacity is exceeded.
    """

    def __init__(self, max_size: int = MAX_IDEMPOTENCY_CACHE) -> None:
        if max_size <= 0:
            raise StateError("max_size must be positive")
        self._max_size = max_size
        self._cache: OrderedDict[str, CachedCommandResult] = OrderedDict()

    def __len__(self) -> int:
        return len(self._cache)

    def get(self, command_id: str) -> CachedCommandResult | None:
        return self._cache.get(command_id)

    def put(self, command_id: str, result: CachedCommandResult) -> None:
        if command_id in self._cache:
            self._cache.move_to_end(command_id)
            self._cache[command_id] = result
            return
        self._cache[command_id] = result
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)


# --------------------------------------------------------------------------
# Pending command tracker (dropped-ACK retry support)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingCommand:
    """Bookkeeping for a command awaiting acknowledgement."""

    command_id: str
    sent_at: float
    retries: int = 0


class PendingCommandTracker:
    """Bounded tracker (max 128) of in-flight commands awaiting ACK/NACK.

    Provides the retry/apply-deadline representation described by the
    5 second :data:`COMMAND_APPLY_DEADLINE_SECONDS` contract: a command not
    acknowledged within the deadline is reported by :meth:`expire_stale` so
    the caller can decide to retry (resend) or give up.
    """

    def __init__(
        self,
        max_size: int = MAX_PENDING_COMMANDS,
        deadline_seconds: float = COMMAND_APPLY_DEADLINE_SECONDS,
    ) -> None:
        if max_size <= 0:
            raise StateError("max_size must be positive")
        self._max_size = max_size
        self._deadline = deadline_seconds
        self._pending: dict[str, PendingCommand] = {}

    def __len__(self) -> int:
        return len(self._pending)

    def register(self, command_id: str, now: float) -> None:
        """Register a newly sent command as pending.

        Raises:
            PendingCommandOverflowError: if the tracker is already full.
        """
        if command_id in self._pending:
            return
        if len(self._pending) >= self._max_size:
            raise PendingCommandOverflowError("pending command tracker is at capacity")
        self._pending[command_id] = PendingCommand(command_id=command_id, sent_at=now)

    def acknowledge(self, command_id: str) -> None:
        """Remove *command_id* from pending tracking once ACK/NACK arrives."""
        self._pending.pop(command_id, None)

    def is_pending(self, command_id: str) -> bool:
        return command_id in self._pending

    def expire_stale(self, now: float) -> list[PendingCommand]:
        """Return pending commands whose apply deadline has elapsed.

        Expired entries are re-registered internally with an incremented
        retry counter and a refreshed ``sent_at`` so callers can resend them
        (dropped-ACK retry) without needing to re-check capacity.
        """
        expired: list[PendingCommand] = []
        for command_id, pending in list(self._pending.items()):
            if now - pending.sent_at >= self._deadline:
                expired.append(pending)
                self._pending[command_id] = PendingCommand(
                    command_id=command_id, sent_at=now, retries=pending.retries + 1
                )
        return expired
