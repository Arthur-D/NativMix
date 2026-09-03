"""Receiver-owned, Qt-main-thread authority for remote mixer control.

Layer 1 deliberately has no knowledge of Qt or application managers.  This
module is the integration boundary: it validates an already decoded command,
applies it through manager APIs, and publishes canonical Layer 1 state.
"""

from __future__ import annotations

import copy
import logging
import math
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Protocol, TypeVar, cast

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal, pyqtSlot

from nativmix.remote_sync.protocol import (
    PROTOCOL_VERSION,
    AckMessage,
    CommandMessage,
    DeltaMessage,
    NackMessage,
    SnapshotMessage,
    SnapshotRequest,
)
from nativmix.remote_sync.schema import (
    ALLOWED_CHANNEL_MODES,
    FORBIDDEN_RAW_KEYS,
    MAX_ACTIVE_CHANNELS,
    MAX_OTHER_LIST,
    SCHEMA_VERSION,
    ReceiverCapabilities,
    RuntimeChannelState,
    SchemaError,
    Snapshot,
    TargetInventoryItem,
    apply_volume_delta,
    build_snapshot,
    normalize_inventory_item,
    normalize_profile,
    normalize_runtime_state,
    require_uuid,
    validate_finite,
)
from nativmix.remote_sync.state import (
    COMMAND_APPLY_DEADLINE_SECONDS,
    MAX_IDEMPOTENCY_CACHE,
    CachedCommandResult,
    CommandResultCache,
    NackReason,
    RevisionClock,
    evaluate_command_epoch_revision,
    new_epoch,
)
from nativmix.utils.config_manager import SPECIAL_APPS
from nativmix.utils.midi_values import is_same_origin_midi_acknowledgement, volume_to_midi_cc
from nativmix.utils.profile_manager import default_channels

logger = logging.getLogger(__name__)

MAX_CONFIGURATION_COMMANDS_PER_WINDOW = 20
MAX_DESTRUCTIVE_COMMANDS_PER_WINDOW = 5
MAX_VOLUME_COMMANDS_PER_CHANNEL_WINDOW = 300
MAX_SNAPSHOT_REQUESTS_PER_WINDOW = 20
MAX_SNAPSHOT_REQUEST_CACHE = 32
COMMAND_RATE_WINDOW_SECONDS = 10.0
VOLUME_PUBLICATION_HZ = 30
MAX_NAME_LENGTH = 128
MAX_LABEL_LENGTH = 256
MAX_TARGET_KEY_LENGTH = 512
MAX_REMOTE_ORIGIN_CHANNELS = 64
MAX_REMOTE_FEEDBACK_ORIGINS = 256


class AuthorityStatus(str, Enum):
    """Stable UI-facing status tokens."""

    CONNECTED = "Connected"
    SYNCING = "Syncing"
    RECONNECTING = "Reconnecting"
    CONFLICT = "Conflict"
    VERSION_INCOMPATIBLE = "Version incompatible"
    PERMISSION_DISABLED = "Permission disabled"


class AuthorityErrorCode(str, Enum):
    """Allowlisted reasons exposed by :class:`CommandResult`."""

    PERMISSION_DISABLED = "permission_disabled"
    NO_ACTIVE_SESSION = "no_active_session"
    SESSION_MISMATCH = "session_mismatch"
    GENERATION_MISMATCH = "generation_mismatch"
    ROLE_MISMATCH = "role_mismatch"
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    STALE_EPOCH = "stale_epoch"
    STALE_REVISION = "stale_revision"
    UNKNOWN_COMMAND_TYPE = "unknown_command_type"
    INVALID_PAYLOAD = "invalid_payload"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    DESTRUCTIVE_RATE_LIMITED = "destructive_rate_limited"
    APPLY_FAILED = "apply_failed"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    WRONG_THREAD = "wrong_thread"


ALLOWED_AUTHORITY_ERROR_CODES = frozenset(code.value for code in AuthorityErrorCode)

RECEIVER_COMMAND_TYPES = frozenset(
    {
        "create_profile",
        "duplicate_profile",
        "rename_profile",
        "select_profile",
        "switch_active_profile",
        "delete_profile",
        "set_profile_restore_positions",
        "set_profile_restore_fader_positions",
        "set_profile_midi_cc",
        "set_profile_midi_switch_cc",
        "add_midi_channel",
        "delete_midi_channels",
        "reorder_channels",
        "set_channel_label",
        "set_channel_inverted",
        "set_channel_mode",
        "set_channel_mappings",
        "set_channel_hardware_target",
        "set_channel_v_sink",
        "set_channel_routing_paused",
        "set_channel_volume_midi_binding",
        "set_channel_mute_midi_binding",
        "set_channel_volume",
        "set_channel_mute",
        "request_resync",
    }
)

# Compatibility name for early Layer 2 callers.  Layer 1's
# protocol.ALLOWED_COMMAND_TYPES remains intentionally untouched.
ALLOWED_AUTHORITY_COMMAND_TYPES = RECEIVER_COMMAND_TYPES

DESTRUCTIVE_COMMAND_TYPES = frozenset({"delete_profile", "delete_midi_channels"})
VOLUME_COMMAND_TYPES = frozenset({"set_channel_volume"})

REQUIRED_LAYER1_NACK_REASONS = frozenset(
    code.value
    for code in AuthorityErrorCode
    if code.value not in {reason.value for reason in NackReason}
)


@dataclass(frozen=True)
class ControlSessionMetadata:
    """The one controller session currently authorized to mutate the receiver."""

    transport_session_id: str
    control_session_id: str
    generation: int
    role: str = "receive"
    permission_enabled: bool = True
    protocol_version: int = PROTOCOL_VERSION
    schema_version: int = SCHEMA_VERSION
    selected_peer_id: str | None = None
    connected_peer_id: str | None = None

    def __post_init__(self) -> None:
        require_uuid(self.transport_session_id, field_name="transport_session_id")
        require_uuid(self.control_session_id, field_name="control_session_id")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")
        if self.role != "receive":
            raise ValueError("receiver authority session role must be 'receive'")


@dataclass(frozen=True)
class ValidatedCommandEnvelope:
    """Transport metadata attached to a strictly decoded command."""

    command: CommandMessage
    generation: int
    role: str = "receive"
    selected_peer_id: str | None = None
    connected_peer_id: str | None = None
    transport_session_id: str | None = None
    received_at: float | None = None
    deadline_seconds: float = COMMAND_APPLY_DEADLINE_SECONDS

    def __post_init__(self) -> None:
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")
        if not 0 < self.deadline_seconds <= COMMAND_APPLY_DEADLINE_SECONDS:
            raise ValueError(f"deadline_seconds must be in (0, {COMMAND_APPLY_DEADLINE_SECONDS}]")
        if self.received_at is not None and not math.isfinite(self.received_at):
            raise ValueError("received_at must be finite")


@dataclass(frozen=True)
class QueueReceipt:
    command_id: str
    deadline_at: float


@dataclass(frozen=True)
class CommandContext:
    command: CommandMessage
    session: ControlSessionMetadata
    generation: int
    role: str
    received_at: float


@dataclass(frozen=True)
class StatePublication:
    """One contiguous receiver publication and its canonical resulting state."""

    kind: str
    base_revision: int
    revision: int
    snapshot: Snapshot
    delta: DeltaMessage | None = None

    def to_protocol_message(self, transport_session_id: str) -> SnapshotMessage | DeltaMessage:
        """Convert this publication to its actual Layer 1 protocol message."""
        if self.delta is not None:
            return self.delta
        return SnapshotMessage(
            protocol_version=PROTOCOL_VERSION,
            schema_version=SCHEMA_VERSION,
            transport_session_id=transport_session_id,
            snapshot=self.snapshot.to_canonical(),
        )


@dataclass(frozen=True)
class CommandResult:
    """Authority result. ``response`` is always a Layer 1 ACK or NACK."""

    accepted: bool
    response: AckMessage | NackMessage
    error_code: AuthorityErrorCode | None = None
    publication: StatePublication | None = None

    @property
    def revision(self) -> int:
        return int(self.response.revision if isinstance(self.response, AckMessage) else self.response.current_revision)

    def to_protocol_message(self) -> AckMessage | NackMessage:
        """Return the actual Layer 1 response for ``request_remote_sync_send``."""
        return self.response


class CapabilitiesProvider(Protocol):
    def __call__(self) -> ReceiverCapabilities | Mapping[str, Any]: ...


class InventoryProvider(Protocol):
    def __call__(self) -> Sequence[TargetInventoryItem | Mapping[str, Any]]: ...


class RuntimeStateProvider(Protocol):
    def __call__(self) -> Sequence[RuntimeChannelState | Mapping[str, Any]]: ...


class ReceiverBackendAdapter(Protocol):
    def set_channel_volume(self, channel_index: int, volume: float) -> None: ...

    def apply_poti_volumes(self, volumes: list[float], *, force: bool = False) -> None: ...

    def is_channel_muted(self, channel_index: int) -> bool: ...

    def toggle_mute(self, channel_index: int) -> None: ...


class ProtocolMessageSender(Protocol):
    def __call__(self, message: Any, generation: int, transport_session_id: str) -> None: ...


ProfileSelector = Callable[[str], bool]
@dataclass
class _PreparedCommand:
    apply: Callable[[], None]
    rollback: Callable[[], None]
    changes: dict[str, Any]
    destructive: bool = False
    volume: bool = False


class _PayloadError(ValueError):
    def __init__(self, code: AuthorityErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class _SlidingWindow:
    def __init__(self, limit: int, window: float) -> None:
        if limit <= 0 or window <= 0:
            raise ValueError("rate limits must be positive")
        self._limit = limit
        self._window = window
        self._events: deque[float] = deque()

    def allow(self, now: float) -> bool:
        cutoff = now - self._window
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()
        if len(self._events) >= self._limit:
            return False
        self._events.append(now)
        return True


T = TypeVar("T")


class ReceiverMixerAuthority(QObject):
    """Authoritative receiver-side command processor and state publisher.

    All manager access is restricted to this object's Qt thread.  Worker
    threads must call :meth:`queue_validated`; completion is delivered through
    :attr:`command_completed`.
    """

    status_changed = pyqtSignal(str)
    publication_ready = pyqtSignal(object)
    command_completed = pyqtSignal(object)
    _queued_envelope = pyqtSignal(object)

    def __init__(
        self,
        config_manager: Any,
        profile_manager: Any,
        backend: ReceiverBackendAdapter | None = None,
        *,
        capabilities_provider: CapabilitiesProvider | ReceiverCapabilities | Mapping[str, Any] | None = None,
        inventory_provider: InventoryProvider | Sequence[TargetInventoryItem | Mapping[str, Any]] | None = None,
        runtime_state_provider: RuntimeStateProvider | Sequence[RuntimeChannelState | Mapping[str, Any]] | None = None,
        active_session: ControlSessionMetadata | None = None,
        epoch: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        command_rate_limit: int = MAX_CONFIGURATION_COMMANDS_PER_WINDOW,
        destructive_rate_limit: int = MAX_DESTRUCTIVE_COMMANDS_PER_WINDOW,
        runtime_mute_setter: Callable[[int, bool], None] | None = None,
        protocol_message_sender: ProtocolMessageSender | None = None,
        profile_selector: ProfileSelector | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config_manager
        self._profiles = profile_manager
        self._backend = backend
        self._capabilities_provider = capabilities_provider
        self._inventory_provider = inventory_provider
        self._runtime_state_provider = runtime_state_provider
        self._active_session = active_session
        self._clock_source = clock
        self.revision_clock = RevisionClock(epoch=require_uuid(epoch or new_epoch(), field_name="epoch"))
        self._runtime_mute_setter = runtime_mute_setter
        self._protocol_message_sender = protocol_message_sender
        self._profile_selector = profile_selector
        self._command_rate = _SlidingWindow(command_rate_limit, COMMAND_RATE_WINDOW_SECONDS)
        self._destructive_rate = _SlidingWindow(destructive_rate_limit, COMMAND_RATE_WINDOW_SECONDS)
        self._volume_rates: dict[str, _SlidingWindow] = {}
        self._snapshot_rate = _SlidingWindow(MAX_SNAPSHOT_REQUESTS_PER_WINDOW, COMMAND_RATE_WINDOW_SECONDS)
        self._snapshot_request_cache: OrderedDict[tuple[int, str, str], StatePublication] = OrderedDict()
        self._semantic_cache = CommandResultCache()
        self._result_cache: OrderedDict[str, CommandResult] = OrderedDict()
        self._pending_volume_changes: dict[str, dict[str, Any]] = {}
        self._pending_volume_origins: dict[str, dict[str, Any]] = {}
        self._pending_volume_responses: list[tuple[CommandMessage, int, str]] = []
        self._last_volume_publication_at: float | None = None
        self._status: AuthorityStatus | None = None
        self._last_observed_hash: str | None = None
        self._last_published_snapshot: Snapshot | None = None
        self._applying_command = False
        self._transport_generation = -1
        self._transport_session_id: str | None = None
        self._last_snapshot_request_id: str | None = None
        self._last_info_log_at: dict[str, float] = {}
        self._remote_volume_origins: OrderedDict[int, Any] = OrderedDict()
        self._feedback_remote_origins: OrderedDict[tuple[int, int], Any] = OrderedDict()

        self._volume_timer = QTimer(self)
        self._volume_timer.setSingleShot(True)
        self._volume_timer.timeout.connect(self.flush_volume_publication)
        self._queued_envelope.connect(self._apply_queued, Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]

    @property
    def epoch(self) -> str:
        return str(self.revision_clock.epoch)

    @property
    def revision(self) -> int:
        return int(self.revision_clock.revision)

    @property
    def status(self) -> AuthorityStatus | None:
        return self._status

    @property
    def active_session(self) -> ControlSessionMetadata | None:
        return self._active_session

    def set_active_session(self, session: ControlSessionMetadata | None) -> None:
        self._require_main_thread()
        self._active_session = session
        if session is not None:
            self._transport_generation = max(self._transport_generation, session.generation)
            self._transport_session_id = session.transport_session_id
        self._remote_volume_origins.clear()
        self._feedback_remote_origins.clear()

    @pyqtSlot(object)
    def begin_transport_session(self, raw_session: Any) -> None:
        """Track the current receiver control lifecycle before messages arrive."""
        self._require_main_thread()
        role = getattr(raw_session, "role", "")
        role_value = str(getattr(role, "value", role))
        if role_value != "receive":
            return
        generation = int(raw_session.generation)
        if generation < self._transport_generation:
            logger.debug("Ignoring stale receiver control lifecycle generation %d", generation)
            return
        transport_session_id = getattr(raw_session, "transport_session_id", None)
        available = bool(getattr(raw_session, "available", False)) and bool(transport_session_id)
        self._transport_generation = generation
        self._transport_session_id = str(transport_session_id) if available else None
        self._last_snapshot_request_id = None
        self._remote_volume_origins.clear()
        self._feedback_remote_origins.clear()
        if not available:
            if self._active_session is not None and generation >= self._active_session.generation:
                self._active_session = None
            return
        permission_enabled = self._control_permitted()
        self._active_session = ControlSessionMetadata(
            transport_session_id=str(transport_session_id),
            control_session_id=str(uuid.UUID(int=0)),
            generation=generation,
            permission_enabled=permission_enabled,
            selected_peer_id=getattr(raw_session, "selected_peer_id", None),
            connected_peer_id=getattr(raw_session, "connected_peer_id", None),
        )
        self.set_status(
            AuthorityStatus.SYNCING if permission_enabled else AuthorityStatus.PERMISSION_DISABLED
        )
        self._log_info_rate_limited(
            f"session:{generation}:{transport_session_id}",
            "Receiver mixer control session ready: generation=%d permission=%s",
            generation,
            "enabled" if permission_enabled else "disabled",
        )

    def set_protocol_message_sender(self, sender: ProtocolMessageSender | None) -> None:
        """Wire ``MidiThread.request_remote_sync_send`` after composition."""
        self._require_main_thread()
        self._protocol_message_sender = sender

    def set_profile_selector(self, selector: ProfileSelector | None) -> None:
        """Use the composition root's complete profile-switch transaction."""
        self._require_main_thread()
        self._profile_selector = selector

    def prime_observed_state(self) -> None:
        """Record the initial canonical state without advancing the revision."""
        self._require_main_thread()
        snapshot = self.current_snapshot()
        self._last_observed_hash = snapshot.content_hash
        self._last_published_snapshot = snapshot

    def connect_local_sources(self) -> None:
        """Capture completed desktop-local mutations through canonical publications."""
        self._require_main_thread()

        def settings_changed() -> None:
            self.reconcile_permission()
            schedule("settings")

        def schedule(event: str) -> None:
            QTimer.singleShot(0, lambda: self.capture_local_mutation(event))

        self._config.settings_changed.connect(settings_changed)
        self._config.mapping_changed.connect(lambda *_: schedule("mapping"))
        self._config.v_sink_changed.connect(lambda *_: schedule("v_sink"))
        self._config.routing_pause_changed.connect(lambda *_: schedule("routing_pause"))
        self._profiles.profile_changed.connect(lambda *_: schedule("profile_selection"))
        self._profiles.profile_list_changed.connect(lambda: schedule("profile_structure"))
        self._profiles.profile_content_changed.connect(
            lambda *_: schedule("profile_content")
        )

        channel_volume_changed = getattr(self._backend, "channel_volume_changed", None)
        if channel_volume_changed is not None:
            channel_volume_changed.connect(
                lambda channel, volume: QTimer.singleShot(
                    0,
                    lambda: self.capture_runtime_volume(channel, volume),
                )
            )
        mute_state_changed = getattr(self._backend, "mute_state_changed", None)
        if mute_state_changed is not None:
            mute_state_changed.connect(self.capture_runtime_mute)

        refresh_inventory = getattr(self._inventory_provider, "refresh", None)
        if not callable(refresh_inventory):
            return

        def refresh_remote_target_inventory(*_args: Any) -> None:
            if refresh_inventory():
                self.capture_local_mutation("target_inventory")

        for signal_name in (
            "target_inventory_changed",
            "other_apps_changed",
            "unresolved_targets_changed",
            "capability_changed",
            "routing_owner_status_changed",
        ):
            backend_signal = getattr(self._backend, signal_name, None)
            if backend_signal is not None:
                backend_signal.connect(refresh_remote_target_inventory)

    @pyqtSlot(object)
    def note_remote_controller_origin(self, origin: Any) -> None:
        """Associate the next canonical backend observation with its physical source."""
        self._require_main_thread()
        session = self._active_session
        transport_session_id = getattr(origin, "transport_session_id", None)
        if transport_session_id is not None:
            if (
                session is None
                or int(getattr(origin, "generation", -1)) != session.generation
                or str(transport_session_id) != session.transport_session_id
            ):
                logger.debug("Ignoring stale remote controller origin")
                return
        channel_index = int(origin.channel_index)
        affected_indexes = [channel_index]
        shared_channels = getattr(self._backend, "get_effective_shared_target_channels", None)
        if (
            self._config.midi_fader_feedback
            or self._config.remote_midi_role == "receive"
        ) and callable(shared_channels):
            affected_indexes = list(shared_channels(channel_index))
        for index in affected_indexes:
            if transport_session_id is not None:
                self._remote_volume_origins[index] = origin
                self._remote_volume_origins.move_to_end(index)
            sequence = int(getattr(origin, "rtp_sequence", getattr(origin, "local_sequence", -1)))
            feedback_key = (index, sequence)
            self._feedback_remote_origins[feedback_key] = origin
            self._feedback_remote_origins.move_to_end(feedback_key)
        while len(self._remote_volume_origins) > MAX_REMOTE_ORIGIN_CHANNELS:
            self._remote_volume_origins.popitem(last=False)
        while len(self._feedback_remote_origins) > MAX_REMOTE_FEEDBACK_ORIGINS:
            self._feedback_remote_origins.popitem(last=False)

    def controller_feedback_directive(
        self,
        channel_index: int,
        volume: float | None = None,
    ) -> tuple[frozenset[tuple[int, int]], tuple[tuple[int, int, int], ...]]:
        """Suppress a source acknowledgement and preload equivalent bindings.

        Configured bindings belong to the process's single selected MIDI endpoint;
        remote sessions likewise expose one selected peer endpoint at a time.
        """
        channel_origins = [
            origin
            for (origin_channel, _sequence), origin in reversed(self._feedback_remote_origins.items())
            if origin_channel == channel_index
        ]
        if not channel_origins:
            logger.debug(
                "Receiver motor feedback emitted: channel=%d source=external_or_local reason=no_remote_origin",
                channel_index,
            )
            return frozenset(), ()
        matching_origins = channel_origins
        if volume is not None:
            matching_origins = [
                origin
                for origin in channel_origins
                if is_same_origin_midi_acknowledgement(float(origin.requested_volume), float(volume))
            ]
        if not matching_origins:
            logger.debug(
                "Receiver motor feedback emitted: channel=%d source=external_or_local "
                "reason=value_not_in_remote_origin_history",
                channel_index,
            )
            return frozenset(), ()
        origin = matching_origins[0]
        matching_ids = {id(origin) for origin in matching_origins}
        for key, recorded_origin in list(self._feedback_remote_origins.items()):
            if key[0] == channel_index and id(recorded_origin) in matching_ids:
                self._feedback_remote_origins.pop(key, None)
        logger.debug(
            "Receiver motor feedback suppressed: channel=%d source=controller "
            "effective_group=%s reason=equivalent_origin_confirmation",
            channel_index,
            sorted(
                {
                    origin_channel
                    for origin_channel, _sequence in self._feedback_remote_origins
                    if self._feedback_remote_origins[(origin_channel, _sequence)] in matching_origins
                }
            ),
        )
        shared_channels = getattr(self._backend, "get_effective_shared_target_channels", None)
        component = (
            list(shared_channels(channel_index))
            if callable(shared_channels)
            else [channel_index]
        )
        bindings = {
            (self._config.get_midi_channel(index), int(cc))
            for index in component
            if (cc := self._config.get_midi_cc(index)) is not None
        }
        source = (int(origin.midi_channel), int(origin.control))
        raw_value = getattr(origin, "raw_cc_value", None)
        if raw_value is None:
            raw_value = volume_to_midi_cc(float(origin.requested_volume))
        preloads = tuple(
            (midi_channel, cc, int(raw_value))
            for midi_channel, cc in sorted(bindings - {source})
        )
        return frozenset(bindings), preloads

    def clear_controller_origins(self) -> None:
        """Drop controller lineage when its endpoint or transport generation changes."""
        self._require_main_thread()
        self._remote_volume_origins.clear()
        self._feedback_remote_origins.clear()

    def set_status(self, status: AuthorityStatus) -> None:
        self._require_main_thread()
        if status != self._status:
            self._status = status
            self.status_changed.emit(status.value)

    def reconcile_permission(self) -> None:
        """Publish live command-permission changes while keeping state viewable."""
        self._require_main_thread()
        session = self._active_session
        permitted = self._control_permitted()
        if session is None:
            if getattr(self._config, "remote_midi_role", "off") == "receive" and not permitted:
                self.set_status(AuthorityStatus.PERMISSION_DISABLED)
            return
        if session.permission_enabled == permitted:
            return

        if not permitted:
            self.flush_volume_publication()
        self._active_session = replace(session, permission_enabled=permitted)
        self._log_info_rate_limited(
            f"permission:{permitted}",
            "Receiver mixer permission propagated live: generation=%d state=%s",
            session.generation,
            "enabled" if permitted else "disabled",
            interval=1.0,
        )
        self.set_status(AuthorityStatus.SYNCING if permitted else AuthorityStatus.PERMISSION_DISABLED)
        self.publish_snapshot()

    def _control_permitted(self) -> bool:
        return bool(getattr(self._config, "allow_remote_mixer_editing", False)) and (
            getattr(self._config, "remote_midi_role", "off") == "receive"
        )

    def _log_info_rate_limited(
        self,
        key: str,
        message: str,
        *args: Any,
        interval: float = 5.0,
    ) -> None:
        now = self._clock_source()
        last = self._last_info_log_at.get(key)
        if last is not None and now - last < interval:
            return
        self._last_info_log_at[key] = now
        logger.info(message, *args)

    def queue_validated(self, envelope: ValidatedCommandEnvelope) -> QueueReceipt:
        """Queue an envelope from any thread for main-thread application."""
        received_at = self._clock_source() if envelope.received_at is None else envelope.received_at
        receipt = QueueReceipt(
            command_id=envelope.command.command_id,
            deadline_at=received_at + envelope.deadline_seconds,
        )
        self._queued_envelope.emit((envelope, received_at))
        return receipt

    @pyqtSlot(object)
    def queue_control_envelope(self, envelope: Any) -> QueueReceipt | None:
        """Adapt ``hardware.remote_midi.SyncControlEnvelope`` without importing hardware.

        The MIDI bridge may deliver other Layer 1 messages through the same
        signal.  Those are left to the snapshot/transport coordinator.
        """
        message = getattr(envelope, "message", None)
        if isinstance(message, SnapshotRequest):
            self._handle_snapshot_request(envelope, message)
            return None
        if not isinstance(message, CommandMessage):
            logger.debug("Ignoring non-command remote sync envelope: %s", type(message).__name__)
            return None
        role_value = getattr(getattr(envelope, "role", None), "value", getattr(envelope, "role", None))
        adapted = ValidatedCommandEnvelope(
            command=message,
            generation=envelope.generation,
            role=str(role_value),
            selected_peer_id=getattr(envelope, "selected_peer_id", None),
            connected_peer_id=getattr(envelope, "connected_peer_id", None),
            transport_session_id=getattr(envelope, "transport_session_id", None),
            received_at=getattr(envelope, "received_at", None),
        )
        return self.queue_validated(adapted)

    def _handle_snapshot_request(self, envelope: Any, request: SnapshotRequest) -> None:
        self._require_main_thread()
        role_value = getattr(getattr(envelope, "role", None), "value", getattr(envelope, "role", None))
        selected_peer_id = getattr(envelope, "selected_peer_id", None)
        connected_peer_id = getattr(envelope, "connected_peer_id", None)
        transport_session_id = getattr(envelope, "transport_session_id", None)
        generation = int(envelope.generation)
        if request.protocol_version != PROTOCOL_VERSION or request.schema_version != SCHEMA_VERSION:
            self.set_status(AuthorityStatus.VERSION_INCOMPATIBLE)
            return
        if (
            str(role_value) != "receive"
            or not selected_peer_id
            or selected_peer_id != connected_peer_id
            or transport_session_id != request.transport_session_id
        ):
            self.set_status(AuthorityStatus.CONFLICT)
            return
        if generation < self._transport_generation or (
            generation == self._transport_generation
            and self._transport_session_id is not None
            and transport_session_id != self._transport_session_id
        ):
            logger.debug(
                "Ignoring stale receiver snapshot request: generation=%d session=%s",
                generation,
                transport_session_id,
            )
            return
        self._transport_generation = generation
        self._transport_session_id = str(transport_session_id)
        previous = self._active_session
        control_session_id = (
            previous.control_session_id
            if previous is not None
            and previous.generation == generation
            and previous.transport_session_id == transport_session_id
            else str(uuid.UUID(int=0))
        )
        permitted = self._control_permitted()
        self._active_session = ControlSessionMetadata(
            transport_session_id=request.transport_session_id,
            control_session_id=control_session_id,
            generation=generation,
            permission_enabled=permitted,
            selected_peer_id=selected_peer_id,
            connected_peer_id=connected_peer_id,
        )
        self._last_snapshot_request_id = request.request_id
        self._log_info_rate_limited(
            f"snapshot-request:{generation}:{transport_session_id}",
            "Receiver snapshot requested: generation=%d permission=%s",
            generation,
            "enabled" if permitted else "disabled",
        )
        cache_key = (generation, request.transport_session_id, request.request_id)
        cached = self._snapshot_request_cache.get(cache_key)
        if cached is not None and cached.revision == self.revision:
            self._snapshot_request_cache.move_to_end(cache_key)
            self._resend_snapshot(cached)
            return
        self._snapshot_request_cache.pop(cache_key, None)
        if not self._snapshot_rate.allow(self._clock_source()):
            self.set_status(AuthorityStatus.CONFLICT)
            return
        self.set_status(AuthorityStatus.SYNCING if permitted else AuthorityStatus.PERMISSION_DISABLED)
        publication = self.publish_snapshot()
        self._log_info_rate_limited(
            f"snapshot-serve:{generation}:{transport_session_id}",
            "Receiver canonical snapshot served: generation=%d revision=%d",
            generation,
            publication.revision,
        )
        self._snapshot_request_cache[cache_key] = publication
        while len(self._snapshot_request_cache) > MAX_SNAPSHOT_REQUEST_CACHE:
            self._snapshot_request_cache.popitem(last=False)

    def _resend_snapshot(self, publication: StatePublication) -> None:
        session = self._active_session
        if self._protocol_message_sender is not None and session is not None:
            self._protocol_message_sender(
                publication.to_protocol_message(session.transport_session_id),
                session.generation,
                session.transport_session_id,
            )

    def _send_permission_disabled(self, request_id: str) -> None:
        session = self._active_session
        if self._protocol_message_sender is None or session is None:
            return
        message = NackMessage(
            protocol_version=PROTOCOL_VERSION,
            schema_version=SCHEMA_VERSION,
            transport_session_id=session.transport_session_id,
            command_id=request_id,
            reason=AuthorityErrorCode.PERMISSION_DISABLED.value,
            current_epoch=self.epoch,
            current_revision=self.revision,
        )
        try:
            self._protocol_message_sender(
                message,
                session.generation,
                session.transport_session_id,
            )
        except Exception:
            logger.exception("Could not queue remote permission status")

    @pyqtSlot(object)
    def _apply_queued(self, queued: tuple[ValidatedCommandEnvelope, float]) -> None:
        envelope, received_at = queued
        if self._clock_source() >= received_at + envelope.deadline_seconds:
            result = self._nack(envelope.command, AuthorityErrorCode.DEADLINE_EXCEEDED, cache=True)
        else:
            result = self.process_command(
                envelope.command,
                generation=envelope.generation,
                role=envelope.role,
                selected_peer_id=envelope.selected_peer_id,
                connected_peer_id=envelope.connected_peer_id,
                envelope_transport_session_id=envelope.transport_session_id,
                received_at=received_at,
            )
        if result.accepted and result.publication is None and envelope.command.command_type in VOLUME_COMMAND_TYPES:
            self._pending_volume_responses.append(
                (envelope.command, envelope.generation, envelope.command.transport_session_id)
            )
        else:
            self.command_completed.emit(result)
            self._send_response(result, envelope.generation, envelope.command.transport_session_id)

    def process_command(
        self,
        command: CommandMessage,
        *,
        generation: int | None = None,
        role: str = "receive",
        selected_peer_id: str | None = None,
        connected_peer_id: str | None = None,
        envelope_transport_session_id: str | None = None,
        received_at: float | None = None,
    ) -> CommandResult:
        """Validate and synchronously apply one decoded command."""
        if QThread.currentThread() is not self.thread():
            return self._nack(command, AuthorityErrorCode.WRONG_THREAD, cache=False)

        session = self._active_session
        if session is None:
            return self._nack(command, AuthorityErrorCode.NO_ACTIVE_SESSION)
        if (
            not session.permission_enabled
            or not bool(getattr(self._config, "allow_remote_mixer_editing", False))
            or getattr(self._config, "remote_midi_role", "off") != "receive"
        ):
            self.set_status(AuthorityStatus.PERMISSION_DISABLED)
            return self._nack(command, AuthorityErrorCode.PERMISSION_DISABLED)
        if command.protocol_version != PROTOCOL_VERSION or session.protocol_version != PROTOCOL_VERSION:
            self.set_status(AuthorityStatus.VERSION_INCOMPATIBLE)
            return self._nack(command, AuthorityErrorCode.PROTOCOL_INCOMPATIBLE)
        if command.schema_version != SCHEMA_VERSION or session.schema_version != SCHEMA_VERSION:
            self.set_status(AuthorityStatus.VERSION_INCOMPATIBLE)
            return self._nack(command, AuthorityErrorCode.SCHEMA_INCOMPATIBLE)
        if role != "receive" or session.role != "receive":
            return self._nack(command, AuthorityErrorCode.ROLE_MISMATCH)
        if (
            command.transport_session_id != session.transport_session_id
            or (
                envelope_transport_session_id is not None
                and envelope_transport_session_id != command.transport_session_id
            )
        ):
            return self._nack(command, AuthorityErrorCode.SESSION_MISMATCH)
        if selected_peer_id is not None and connected_peer_id is not None and selected_peer_id != connected_peer_id:
            return self._nack(command, AuthorityErrorCode.SESSION_MISMATCH)
        if session.selected_peer_id is not None and selected_peer_id != session.selected_peer_id:
            return self._nack(command, AuthorityErrorCode.SESSION_MISMATCH)
        if session.connected_peer_id is not None and connected_peer_id != session.connected_peer_id:
            return self._nack(command, AuthorityErrorCode.SESSION_MISMATCH)
        if session.control_session_id == str(uuid.UUID(int=0)):
            session = ControlSessionMetadata(
                transport_session_id=session.transport_session_id,
                control_session_id=command.control_session_id,
                generation=session.generation,
                role=session.role,
                permission_enabled=session.permission_enabled,
                protocol_version=session.protocol_version,
                schema_version=session.schema_version,
                selected_peer_id=session.selected_peer_id,
                connected_peer_id=session.connected_peer_id,
            )
            self._active_session = session
        elif command.control_session_id != session.control_session_id:
            return self._nack(command, AuthorityErrorCode.SESSION_MISMATCH)

        cached = self._result_cache.get(command.command_id)
        if cached is not None:
            return self._cached_result_for_transport(cached, command.transport_session_id)

        actual_generation = session.generation if generation is None else generation
        if actual_generation != session.generation:
            return self._nack(command, AuthorityErrorCode.GENERATION_MISMATCH)

        stale = evaluate_command_epoch_revision(
            command_epoch=command.receiver_epoch,
            command_expected_revision=command.expected_revision,
            current_epoch=self.epoch,
            current_revision=self.revision,
        )
        if stale is NackReason.STALE_EPOCH:
            self.set_status(AuthorityStatus.CONFLICT)
            return self._nack(command, AuthorityErrorCode.STALE_EPOCH)
        if stale is NackReason.STALE_REVISION:
            self.set_status(AuthorityStatus.CONFLICT)
            return self._nack(command, AuthorityErrorCode.STALE_REVISION)
        if command.command_type not in RECEIVER_COMMAND_TYPES:
            return self._nack(command, AuthorityErrorCode.UNKNOWN_COMMAND_TYPE)

        now = self._clock_source()
        try:
            prepared = self._prepare_command(command)
        except _PayloadError as exc:
            logger.debug("Remote command %s rejected: %s", command.command_type, exc)
            return self._nack(command, exc.code)
        except (SchemaError, TypeError, ValueError) as exc:
            logger.debug("Remote command %s has invalid payload: %s", command.command_type, exc)
            return self._nack(command, AuthorityErrorCode.INVALID_PAYLOAD)

        if prepared.volume:
            channel_id = str(command.payload.get("channel_id", ""))
            volume_rate = self._volume_rates.setdefault(
                channel_id,
                _SlidingWindow(MAX_VOLUME_COMMANDS_PER_CHANNEL_WINDOW, COMMAND_RATE_WINDOW_SECONDS),
            )
            if not volume_rate.allow(now):
                return self._nack(command, AuthorityErrorCode.RATE_LIMITED)
        elif not self._command_rate.allow(now):
            return self._nack(command, AuthorityErrorCode.RATE_LIMITED)
        if prepared.destructive and not self._destructive_rate.allow(now):
            return self._nack(command, AuthorityErrorCode.DESTRUCTIVE_RATE_LIMITED)

        if not prepared.volume:
            try:
                self.flush_volume_publication()
            except Exception:
                logger.exception("Could not flush pending volume state before %s", command.command_type)
                return self._nack(command, AuthorityErrorCode.APPLY_FAILED)
        publication: StatePublication | None
        self._applying_command = True
        try:
            if prepared.volume and self._last_published_snapshot is None:
                self._last_published_snapshot = self.current_snapshot()
            prepared.apply()
            if command.command_type == "request_resync":
                publication = self.publish_snapshot()
            elif prepared.volume:
                publication = self.publish_local_mutation(prepared.changes, volume=True)
            else:
                publication = self.publish_snapshot()
        except Exception as exc:
            logger.warning(
                "Remote command %s failed; rolling back (%s: %s)",
                command.command_type,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            try:
                prepared.rollback()
            except Exception:
                logger.exception("Rollback failed for remote command %s", command.command_type)
            return self._nack(command, AuthorityErrorCode.APPLY_FAILED)
        finally:
            self._applying_command = False

        result = CommandResult(
            accepted=True,
            response=AckMessage(
                protocol_version=PROTOCOL_VERSION,
                schema_version=SCHEMA_VERSION,
                transport_session_id=command.transport_session_id,
                command_id=command.command_id,
                revision=self.revision,
            ),
            publication=publication,
        )
        self._cache_result(command.command_id, result)
        self.set_status(AuthorityStatus.CONNECTED)
        return result

    def current_snapshot(self) -> Snapshot:
        """Build, without publishing, the canonical state at current revision."""
        self._require_main_thread()
        return self._build_snapshot(self.revision)

    def publish_snapshot(self) -> StatePublication:
        """Publish a full snapshot, advancing the Layer 1 clock once."""
        self._require_main_thread()
        self.flush_volume_publication()
        base = self.revision
        snapshot = self._build_snapshot(base + 1)
        revision = self.revision_clock.advance()
        publication = StatePublication("snapshot", base, revision, snapshot)
        self._emit_publication(publication)
        return publication

    def publish_local_mutation(
        self,
        changes: Mapping[str, Any],
        *,
        volume: bool = False,
    ) -> StatePublication | None:
        """Publish a mutation made locally or by a successfully applied command.

        Volume-only changes are merged and emitted at no more than 30Hz.
        Structural changes always flush a pending volume delta first.
        """
        self._require_main_thread()
        self._validate_publication_changes(changes)
        if volume:
            for key, value in changes.items():
                self._pending_volume_changes[str(key)] = copy.deepcopy(value)
            now = self._clock_source()
            interval = 1.0 / VOLUME_PUBLICATION_HZ
            if self._last_volume_publication_at is None or now - self._last_volume_publication_at >= interval:
                return self.flush_volume_publication()
            remaining_ms = max(1, math.ceil((interval - (now - self._last_volume_publication_at)) * 1000))
            if not self._volume_timer.isActive():
                self._volume_timer.start(remaining_ms)
            return None

        self.flush_volume_publication()
        return self._publish_delta(dict(changes))

    def capture_local_mutation(self, event: str, *, volume: bool = False) -> StatePublication | None:
        """Publish a completed desktop-local mutation exactly once."""
        self._require_main_thread()
        if self._applying_command:
            return None
        if volume:
            raise ValueError("volume events require capture_runtime_volume")
        current_hash = self.current_snapshot().content_hash
        if self._last_observed_hash is None:
            return self.publish_local_mutation({"local_event": event})
        if current_hash == self._last_observed_hash:
            return None
        return self.publish_snapshot()

    def capture_runtime_volume(self, channel_index: int, volume: float) -> StatePublication | None:
        self._require_main_thread()
        if self._applying_command:
            return None
        affected_indexes = [channel_index]
        shared_channels = getattr(self._backend, "get_effective_shared_target_channels", None)
        if (
            self._config.midi_fader_feedback
            or self._config.remote_midi_role == "receive"
        ) and callable(shared_channels):
            affected_indexes = list(shared_channels(channel_index))
        changes = {
            self._profiles.get_channel_id(index): {
                "volume": self._finite_float(self._config.get_channel_volume(index), "volume")
            }
            for index in affected_indexes
        }
        origin = self._remote_volume_origins.get(channel_index)
        if all(self._pending_volume_changes.get(channel_id) == change for channel_id, change in changes.items()):
            return None
        if not self._pending_volume_changes and self.current_snapshot().content_hash == self._last_observed_hash:
            for index in affected_indexes:
                self._remote_volume_origins.pop(index, None)
            return None
        origin_wire = origin.to_wire() if origin is not None else None
        for index in affected_indexes:
            channel_id = self._profiles.get_channel_id(index)
            if origin_wire is not None:
                self._pending_volume_origins[channel_id] = copy.deepcopy(origin_wire)
            self._remote_volume_origins.pop(index, None)
        return self.publish_local_mutation(changes, volume=True)

    def capture_runtime_mute(self, channel_index: int, muted: bool) -> StatePublication | None:
        self._require_main_thread()
        if self._applying_command:
            return None
        self.flush_volume_publication()
        if self.current_snapshot().content_hash == self._last_observed_hash:
            return None
        self._strict_bool(muted, "muted")
        return self.publish_snapshot()

    @pyqtSlot()
    def flush_volume_publication(self) -> StatePublication | None:
        self._require_main_thread()
        if not self._pending_volume_changes:
            return None
        self._volume_timer.stop()
        pending = copy.deepcopy(self._pending_volume_changes)
        origins = copy.deepcopy(self._pending_volume_origins)
        changes = {"volumes": pending}
        if origins:
            changes["origins"] = origins
        self._pending_volume_changes.clear()
        self._pending_volume_origins.clear()
        try:
            publication = self._publish_delta(changes)
        except Exception:
            self._pending_volume_changes.update(pending)
            self._pending_volume_origins.update(origins)
            raise
        self._last_volume_publication_at = self._clock_source()
        self._complete_pending_volume_responses(publication)
        return publication

    def _complete_pending_volume_responses(self, publication: StatePublication) -> None:
        pending = self._pending_volume_responses
        self._pending_volume_responses = []
        for command, generation, transport_session_id in pending:
            result = CommandResult(
                accepted=True,
                response=AckMessage(
                    protocol_version=PROTOCOL_VERSION,
                    schema_version=SCHEMA_VERSION,
                    transport_session_id=transport_session_id,
                    command_id=command.command_id,
                    revision=publication.revision,
                ),
                publication=publication,
            )
            self._cache_result(command.command_id, result)
            self.command_completed.emit(result)
            self._send_response(result, generation, transport_session_id)

    def _publish_delta(self, changes: dict[str, Any]) -> StatePublication:
        base = self.revision
        snapshot = self._build_snapshot(base + 1)
        revision = self.revision_clock.advance()
        previous = self._last_published_snapshot
        requested_volumes = changes.get("volumes")
        if previous is None or not isinstance(requested_volumes, Mapping):
            publication = StatePublication("snapshot", base, revision, snapshot)
            self._emit_publication(publication)
            return publication

        previous_volumes = {state.channel_id: state.effective_volume for state in previous.runtime_states}
        canonical_volumes = {
            state.channel_id: state.effective_volume
            for state in snapshot.runtime_states
            if previous_volumes.get(state.channel_id) != state.effective_volume
        }
        try:
            candidate = apply_volume_delta(
                previous,
                epoch=snapshot.epoch,
                revision=revision,
                resulting_hash=snapshot.content_hash,
                volumes=canonical_volumes,
            )
        except SchemaError:
            publication = StatePublication("snapshot", base, revision, snapshot)
            self._emit_publication(publication)
            return publication
        if candidate != snapshot:
            publication = StatePublication("snapshot", base, revision, snapshot)
            self._emit_publication(publication)
            return publication

        session_id = self._active_session.transport_session_id if self._active_session else str(uuid.UUID(int=0))
        delta = DeltaMessage(
            protocol_version=PROTOCOL_VERSION,
            schema_version=SCHEMA_VERSION,
            transport_session_id=session_id,
            receiver_epoch=self.epoch,
            base_revision=base,
            revision=revision,
            resulting_hash=snapshot.content_hash,
            changes={
                "volumes": {
                    channel_id: {"volume": volume}
                    for channel_id, volume in canonical_volumes.items()
                },
                **(
                    {
                        "origins": {
                            channel_id: origin
                            for channel_id, origin in changes.get("origins", {}).items()
                            if channel_id in canonical_volumes
                        }
                    }
                    if isinstance(changes.get("origins"), Mapping)
                    else {}
                ),
            },
        )
        publication = StatePublication("delta", base, revision, snapshot, delta)
        self._emit_publication(publication)
        return publication

    def _emit_publication(self, publication: StatePublication) -> None:
        self._last_observed_hash = publication.snapshot.content_hash
        self._last_published_snapshot = publication.snapshot
        self.publication_ready.emit(publication)
        session = self._active_session
        if (
            self._protocol_message_sender is not None
            and session is not None
        ):
            volumes = publication.delta.changes.get("volumes", {}) if publication.delta is not None else {}
            if volumes:
                logger.debug(
                    "Receiver canonical volume acknowledgement queued: generation=%d session=%s revision=%d "
                    "controls=%s",
                    session.generation,
                    session.transport_session_id,
                    publication.revision,
                    sorted(volumes),
                )
            try:
                self._protocol_message_sender(
                    publication.to_protocol_message(session.transport_session_id),
                    session.generation,
                    session.transport_session_id,
                )
            except Exception:
                logger.exception("Could not queue remote state publication")

    def _send_response(self, result: CommandResult, generation: int, transport_session_id: str) -> None:
        if self._protocol_message_sender is not None:
            try:
                self._protocol_message_sender(result.to_protocol_message(), generation, transport_session_id)
            except Exception:
                logger.exception("Could not queue remote command response")

    def _build_snapshot(self, revision: int) -> Snapshot:
        refresh_inventory = getattr(self._inventory_provider, "refresh", None)
        if callable(refresh_inventory):
            refresh_inventory()
        profile_records = []
        active_id = str(self._profiles.active_profile_id or self._config.active_profile_id)
        active_name = ""
        profiles = self._profiles.list_profiles()
        for summary in profiles:
            profile = self._profiles.load(str(summary["id"]))
            if profile["id"] == active_id:
                active_name = str(profile["name"])
                runtime_channels = self._config.all_channels()
                profile_ids = [str(channel.get("channel_id", "")) for channel in profile.get("channels", [])]
                runtime_ids = [str(channel.get("channel_id", "")) for channel in runtime_channels]
                if (
                    len(runtime_channels) == int(profile.get("channel_count", len(runtime_channels)))
                    and runtime_ids == profile_ids
                ):
                    saved_volumes = {
                        str(channel.get("channel_id")): float(channel.get("volume", 1.0))
                        for channel in profile.get("channels", [])
                    }
                    canonical_channels = copy.deepcopy(runtime_channels)
                    for channel in canonical_channels:
                        channel_id = str(channel.get("channel_id"))
                        channel["volume"] = saved_volumes.get(channel_id, 1.0)
                    profile = {**profile, "channels": canonical_channels}
            hardware_key = getattr(self._inventory_provider, "key_for_hardware_value", None)
            if callable(hardware_key):
                profile = copy.deepcopy(profile)
                for channel in profile.get("channels", []):
                    raw_target = channel.get("hardware_id")
                    channel["hardware_id"] = hardware_key(raw_target) if raw_target else None
            channel_ids = [str(channel.get("channel_id", "")) for channel in profile.get("channels", [])]
            profile_records.append(normalize_profile(profile, channel_ids=channel_ids))
        if not profile_records:
            raise RuntimeError("receiver has no profiles")
        if not active_id:
            active_id = profile_records[0].id
            active_name = profile_records[0].name
        if not active_name:
            active_name = next((profile.name for profile in profile_records if profile.id == active_id), "")

        order = self._profiles.get_channel_order_ids(active_id)
        inventory = self._inventory()
        capabilities = self._capabilities()
        runtime_states = self._runtime_states(profile_records, active_id)
        return build_snapshot(
            epoch=self.epoch,
            revision=revision,
            profiles=profile_records,
            active_profile_id=active_id,
            active_profile_name=active_name,
            channel_order=order,
            runtime_states=runtime_states,
            inventory=inventory,
            capabilities=capabilities,
        )

    def _capabilities(self) -> ReceiverCapabilities:
        raw = self._provider_value(self._capabilities_provider)
        if raw is None:
            result = ReceiverCapabilities(
                supports_v_sink=bool(self._backend is not None),
                supports_midi=True,
                max_channels=MAX_ACTIVE_CHANNELS,
                features=tuple(sorted(RECEIVER_COMMAND_TYPES)),
            )
        elif isinstance(raw, ReceiverCapabilities):
            result = raw
        elif isinstance(raw, Mapping):
            allowed = {"supports_v_sink", "supports_midi", "max_channels", "features"}
            if set(raw) != allowed:
                raise ValueError("capabilities provider returned non-canonical fields")
            features = raw["features"]
            if not isinstance(features, (list, tuple)) or not all(isinstance(item, str) for item in features):
                raise ValueError("capability features must be strings")
            result = ReceiverCapabilities(
                supports_v_sink=self._strict_bool(raw["supports_v_sink"], "supports_v_sink"),
                supports_midi=self._strict_bool(raw["supports_midi"], "supports_midi"),
                max_channels=self._strict_int(raw["max_channels"], "max_channels", minimum=0),
                features=tuple(features),
            )
        else:
            raise TypeError("invalid capabilities provider result")
        features = set(result.features)
        features.add("remote_permissions")
        if self._control_permitted():
            features.add("remote_editing")
        else:
            features.discard("remote_editing")
        result = replace(result, features=tuple(sorted(features)))
        if result.max_channels > MAX_ACTIVE_CHANNELS or len(result.features) > MAX_OTHER_LIST:
            raise ValueError("capabilities exceed schema limits")
        return result

    def _inventory(self) -> list[TargetInventoryItem]:
        raw = self._provider_value(self._inventory_provider)
        if raw is None:
            return []
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise TypeError("inventory provider must return a sequence")
        result: list[TargetInventoryItem] = []
        keys: set[str] = set()
        for item in raw:
            normalized = item if isinstance(item, TargetInventoryItem) else normalize_inventory_item(item)
            self._validate_target_key(normalized.key)
            if normalized.key in keys:
                raise ValueError("inventory keys must be unique")
            keys.add(normalized.key)
            result.append(normalized)
        return sorted(result, key=lambda item: item.key)

    def _runtime_states(
        self,
        profiles: Sequence[Any],
        active_profile_id: str,
    ) -> list[RuntimeChannelState]:
        raw = self._provider_value(self._runtime_state_provider)
        if raw is not None:
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
                raise TypeError("runtime state provider must return a sequence")
            return [
                item if isinstance(item, RuntimeChannelState) else normalize_runtime_state(item)
                for item in raw
            ]
        active = next((profile for profile in profiles if profile.id == active_profile_id), None)
        if active is None:
            return []
        inventory = self._inventory()
        available_names = {
            item.label.casefold()
            for item in inventory
            if item.available and item.key.startswith(("app:", "pseudo:"))
        }
        available_keys = {item.key for item in inventory if item.available}
        target_counts: dict[str, int] = {}
        for channel in active.channels:
            for mapping in channel.mappings:
                folded = mapping.casefold()
                target_counts[folded] = target_counts.get(folded, 0) + 1
            if channel.hardware_target_key:
                target_counts[channel.hardware_target_key] = target_counts.get(channel.hardware_target_key, 0) + 1
        capabilities = self._capabilities()
        result = []
        for channel in active.channels:
            muted = False
            if self._backend is not None:
                try:
                    muted = bool(self._backend.is_channel_muted(channel.index))
                except (AttributeError, NotImplementedError):
                    muted = False
            mapping_availability = [mapping.casefold() in available_names for mapping in channel.mappings]
            hardware_available = (
                channel.hardware_target_key in available_keys if channel.hardware_target_key else None
            )
            unresolved = any(not available for available in mapping_availability) or hardware_available is False
            available = any(mapping_availability) or hardware_available is True
            shared_target = any(target_counts.get(mapping.casefold(), 0) > 1 for mapping in channel.mappings)
            if channel.hardware_target_key and target_counts.get(channel.hardware_target_key, 0) > 1:
                shared_target = True
            capability_state = "unsupported" if channel.v_sink and not capabilities.supports_v_sink else "ok"
            if capability_state == "ok" and unresolved:
                capability_state = "degraded"
            result.append(
                RuntimeChannelState(
                    channel_id=channel.id,
                    effective_volume=float(self._config.get_channel_volume(channel.index)),
                    muted=muted,
                    available=available,
                    unresolved=unresolved,
                    shared_target=shared_target,
                    capability_state=capability_state,
                )
            )
        return result

    @staticmethod
    def _provider_value(provider: T | Callable[[], T] | None) -> T | None:
        return provider() if callable(provider) else provider

    def _prepare_command(self, command: CommandMessage) -> _PreparedCommand:
        payload = command.payload
        command_type = command.command_type
        if command_type == "request_resync":
            self._exact(payload, set())
            return _PreparedCommand(lambda: None, lambda: None, {"resync_requested": True})
        if command_type == "create_profile":
            self._exact(payload, {"name", "channel_count"})
            name = self._name(payload["name"])
            count = self._strict_int(
                payload["channel_count"],
                "channel_count",
                minimum=0,
                maximum=self._capabilities().max_channels,
            )
            created: list[str] = []

            def apply() -> None:
                created.append(self._profiles.create(name, channel_count=count))

            def rollback() -> None:
                if created:
                    self._profiles.delete(created[0])

            return _PreparedCommand(apply, rollback, {"profiles": "created"})
        if command_type == "duplicate_profile":
            self._exact(payload, {"profile_id", "name"})
            source = self._load_profile(payload["profile_id"])
            name = self._name(payload["name"])
            duplicate_created: list[str] = []

            def apply() -> None:
                profile_id = self._profiles.create(
                    name,
                    channel_count=int(source["channel_count"]),
                    channels=source["channels"],
                    channel_order=source.get("channel_order"),
                )
                duplicate_created.append(profile_id)
                duplicate = self._profiles.load(profile_id)
                duplicate["restore_fader_positions"] = bool(source.get("restore_fader_positions", False))
                duplicate["midi_switch_cc"] = None
                self._profiles.save_profile(duplicate)

            def rollback() -> None:
                if duplicate_created:
                    self._profiles.delete(duplicate_created[0])

            return _PreparedCommand(apply, rollback, {"profiles": "duplicated"})
        if command_type == "rename_profile":
            self._exact(payload, {"profile_id", "name"})
            profile = self._load_profile(payload["profile_id"])
            name = self._name(payload["name"])
            old_name = str(profile["name"])
            return _PreparedCommand(
                lambda: self._profiles.rename(profile["id"], name),
                lambda: self._profiles.rename(profile["id"], old_name),
                {"profile": {"id": profile["id"], "name": name}},
            )
        if command_type in {"select_profile", "switch_active_profile"}:
            self._exact(payload, {"profile_id"})
            target = self._load_profile(payload["profile_id"])
            old_id = str(self._profiles.active_profile_id or self._config.active_profile_id)
            old = self._load_profile(old_id) if old_id else None
            return _PreparedCommand(
                lambda: self._select_profile(target),
                lambda: self._select_profile(old) if old is not None else None,
                {"active_profile_id": target["id"]},
            )
        if command_type == "delete_profile":
            self._exact(payload, {"profile_id"})
            target = self._load_profile(payload["profile_id"])
            summaries = self._profiles.list_profiles()
            if len(summaries) <= 1:
                raise _PayloadError(AuthorityErrorCode.CONFLICT, "cannot delete last profile")
            was_active = target["id"] == (self._profiles.active_profile_id or self._config.active_profile_id)
            fallback = None
            if was_active:
                fallback_id = next(str(item["id"]) for item in summaries if item["id"] != target["id"])
                fallback = self._load_profile(fallback_id)

            def apply() -> None:
                if fallback is not None:
                    self._select_profile(fallback)
                self._profiles.delete(target["id"])

            def rollback() -> None:
                self._profiles.save_profile(target, allow_resize=True)
                if was_active:
                    self._select_profile(target)

            return _PreparedCommand(apply, rollback, {"profiles": "deleted"}, destructive=True)
        if command_type in {
            "set_profile_restore_positions",
            "set_profile_restore_fader_positions",
            "set_profile_midi_cc",
            "set_profile_midi_switch_cc",
        }:
            return self._prepare_profile_setting(command_type, payload)
        if command_type == "add_midi_channel":
            return self._prepare_add_channel(payload)
        if command_type == "delete_midi_channels":
            return self._prepare_delete_channels(payload)
        if command_type == "reorder_channels":
            return self._prepare_reorder(payload)
        return self._prepare_channel_command(command_type, payload)

    def _prepare_profile_setting(self, command_type: str, payload: Mapping[str, Any]) -> _PreparedCommand:
        value_key = "enabled" if "restore" in command_type else "cc"
        self._exact(payload, {"profile_id", value_key})
        profile = self._load_profile(payload["profile_id"])
        before = copy.deepcopy(profile)
        if value_key == "enabled":
            profile["restore_fader_positions"] = self._strict_bool(payload[value_key], value_key)
        else:
            cc = self._optional_midi_cc(payload[value_key], value_key)
            if cc is not None:
                for summary in self._profiles.list_profiles():
                    other = self._load_profile(summary["id"])
                    if other["id"] != profile["id"] and other.get("midi_switch_cc") == cc:
                        raise _PayloadError(AuthorityErrorCode.CONFLICT, "profile MIDI CC is already assigned")
            profile["midi_switch_cc"] = cc
        return self._profile_replacement(
            before,
            profile,
            {"profile": {"id": profile["id"], value_key: payload[value_key]}},
        )

    def _prepare_add_channel(self, payload: Mapping[str, Any]) -> _PreparedCommand:
        required = {"profile_id"}
        optional = {"channel_id"}
        self._exact(payload, required, optional)
        before = self._load_profile(payload["profile_id"])
        if int(before["channel_count"]) >= self._capabilities().max_channels:
            raise _PayloadError(AuthorityErrorCode.CONFLICT, "receiver channel capacity reached")
        after = copy.deepcopy(before)
        channel = default_channels(1)[0]
        channel["index"] = len(after["channels"])
        if not self._capabilities().supports_midi:
            raise _PayloadError(AuthorityErrorCode.CONFLICT, "MIDI channels are unsupported")
        channel["is_midi"] = True
        if "channel_id" in payload:
            channel["channel_id"] = require_uuid(str(payload["channel_id"]), field_name="channel_id")
        after["channels"].append(channel)
        after["channel_count"] = len(after["channels"])
        after["channel_order"] = list(after.get("channel_order", range(len(before["channels"])))) + [channel["index"]]
        return self._profile_replacement(
            before,
            after,
            {"channels": {"added": channel["channel_id"]}},
            allow_resize=True,
        )

    def _prepare_delete_channels(self, payload: Mapping[str, Any]) -> _PreparedCommand:
        self._exact(payload, {"profile_id", "channel_ids"})
        profile_id = payload["profile_id"]
        channel_ids = payload["channel_ids"]
        if not isinstance(channel_ids, list) or not channel_ids:
            raise ValueError("channel_ids must be a non-empty list")
        normalized_ids = [require_uuid(value, field_name="channel_ids entry") for value in channel_ids]
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("channel_ids contains duplicates")
        before = self._load_profile(profile_id)
        by_id = {str(channel["channel_id"]): index for index, channel in enumerate(before["channels"])}
        try:
            indexes = sorted((by_id[channel_id] for channel_id in normalized_ids), reverse=True)
        except KeyError as exc:
            raise _PayloadError(AuthorityErrorCode.NOT_FOUND, f"channel not found: {exc.args[0]}") from exc
        if any(not bool(before["channels"][index].get("is_midi", False)) for index in indexes):
            raise _PayloadError(AuthorityErrorCode.CONFLICT, "only MIDI channels may be deleted remotely")
        after = copy.deepcopy(before)
        for index in indexes:
            after["channels"].pop(index)
        for new_index, channel in enumerate(after["channels"]):
            channel["index"] = new_index
        after["channel_count"] = len(after["channels"])
        old_order = before.get("channel_order", list(range(len(before["channels"]))))
        for removed_index in indexes:
            old_order = [
                old_index - 1 if old_index > removed_index else old_index
                for old_index in old_order
                if old_index != removed_index
            ]
        after["channel_order"] = old_order
        return self._profile_replacement(
            before,
            after,
            {"channels": {"deleted": normalized_ids}},
            allow_resize=True,
            destructive=True,
        )

    def _prepare_reorder(self, payload: Mapping[str, Any]) -> _PreparedCommand:
        self._exact(payload, {"profile_id", "channel_ids"})
        profile = self._load_profile(payload["profile_id"])
        raw_ids = payload["channel_ids"]
        if not isinstance(raw_ids, list):
            raise ValueError("channel_ids must be a list")
        ids = [require_uuid(item, field_name="channel_ids entry") for item in raw_ids]
        channels_by_id = {str(channel["channel_id"]): int(channel["index"]) for channel in profile["channels"]}
        if len(ids) != len(channels_by_id) or set(ids) != set(channels_by_id):
            raise _PayloadError(AuthorityErrorCode.INVALID_PAYLOAD, "channel_ids must be an exact permutation")
        old_order = self._profiles.get_channel_order(profile["id"])
        new_order = [channels_by_id[channel_id] for channel_id in ids]

        def apply_order(order: list[int]) -> None:
            self._profiles.set_channel_order(order, profile["id"])
            if profile["id"] == (self._profiles.active_profile_id or self._config.active_profile_id):
                self._config.settings_changed.emit()

        return _PreparedCommand(
            lambda: apply_order(new_order),
            lambda: apply_order(old_order),
            {"channel_order": ids},
        )

    def _prepare_channel_command(self, command_type: str, payload: Mapping[str, Any]) -> _PreparedCommand:
        fields = {
            "set_channel_label": ("label",),
            "set_channel_inverted": ("inverted",),
            "set_channel_mode": ("mode",),
            "set_channel_mappings": ("target_keys",),
            "set_channel_hardware_target": ("target_key",),
            "set_channel_v_sink": ("enabled",),
            "set_channel_routing_paused": ("target_key", "paused"),
            "set_channel_volume_midi_binding": ("cc", "midi_channel"),
            "set_channel_mute_midi_binding": ("cc", "midi_channel"),
            "set_channel_volume": ("volume",),
            "set_channel_mute": ("muted",),
        }
        if command_type not in fields:
            raise _PayloadError(AuthorityErrorCode.UNKNOWN_COMMAND_TYPE, command_type)
        self._exact(payload, {"profile_id", "channel_id", *fields[command_type]})
        before, index = self._profile_and_channel(payload)
        active = before["id"] == (self._profiles.active_profile_id or self._config.active_profile_id)
        if command_type in {"set_channel_volume", "set_channel_mute"} and not active:
            raise _PayloadError(AuthorityErrorCode.CONFLICT, "runtime commands require the active profile")
        after = copy.deepcopy(before)
        channel = after["channels"][index]

        if command_type == "set_channel_label":
            label = payload["label"]
            if label is not None and (not isinstance(label, str) or len(label) > MAX_LABEL_LENGTH):
                raise ValueError("label must be a bounded string or null")
            channel["label"] = label
        elif command_type == "set_channel_inverted":
            channel["inverted"] = self._strict_bool(payload["inverted"], "inverted")
        elif command_type == "set_channel_mode":
            mode = payload["mode"]
            if mode not in ALLOWED_CHANNEL_MODES:
                raise ValueError("invalid channel mode")
            if mode == "vsink":
                if not self._capabilities().supports_v_sink:
                    raise _PayloadError(AuthorityErrorCode.CONFLICT, "virtual sinks are unsupported")
                channel["mode"] = "app"
                channel["v_sink"] = True
                channel["hardware_id"] = None
            elif mode == "hardware":
                channel["mode"] = mode
                channel["app_names"] = []
                channel["routing_paused_apps"] = []
                channel["v_sink"] = False
            else:
                channel["mode"] = mode
                channel["hardware_id"] = None
        elif command_type == "set_channel_mappings":
            if channel.get("mode") == "hardware":
                raise _PayloadError(AuthorityErrorCode.CONFLICT, "hardware channels cannot map apps")
            target_keys = self._string_list(payload["target_keys"], "target_keys")
            mappings = self._resolve_mapping_keys(target_keys)
            special = [name for name in mappings if name.casefold() in SPECIAL_APPS]
            if special and len(mappings) != 1:
                raise _PayloadError(AuthorityErrorCode.CONFLICT, "special mappings must be exclusive")
            channel["app_names"] = mappings
            mapped_names = {name.casefold() for name in mappings}
            channel["routing_paused_apps"] = [
                name for name in channel.get("routing_paused_apps", []) if str(name).casefold() in mapped_names
            ]
            if mappings and mappings[0].casefold() == "system master":
                channel["v_sink"] = False
        elif command_type == "set_channel_hardware_target":
            if channel.get("mode") != "hardware":
                raise _PayloadError(AuthorityErrorCode.CONFLICT, "device targets require hardware mode")
            target = payload["target_key"]
            if target is not None:
                if not isinstance(target, str):
                    raise ValueError("target_key must be a string or null")
                self._validate_target_key(target)
                provider = self._inventory_provider
                resolver = getattr(provider, "resolve_hardware_key", None)
                try:
                    resolved_target = resolver(target) if callable(resolver) else target
                except KeyError as exc:
                    raise _PayloadError(
                        AuthorityErrorCode.NOT_FOUND,
                        "target is not in receiver inventory",
                    ) from exc
                if not callable(resolver) and target not in {item.key for item in self._inventory()}:
                    raise _PayloadError(AuthorityErrorCode.NOT_FOUND, "target is not in receiver inventory")
            else:
                resolved_target = None
            channel["hardware_id"] = resolved_target
        elif command_type == "set_channel_v_sink":
            enabled = self._strict_bool(payload["enabled"], "enabled")
            if enabled and channel.get("mode") == "hardware":
                raise _PayloadError(AuthorityErrorCode.CONFLICT, "hardware channels cannot use virtual sinks")
            if enabled and any(str(name).casefold() == "system master" for name in channel.get("app_names", [])):
                raise _PayloadError(AuthorityErrorCode.CONFLICT, "System Master cannot use a virtual sink")
            if enabled and not self._capabilities().supports_v_sink:
                raise _PayloadError(AuthorityErrorCode.CONFLICT, "virtual sinks are unsupported")
            channel["v_sink"] = enabled
        elif command_type == "set_channel_routing_paused":
            app_name = self._resolve_mapping_keys(
                self._string_list([payload["target_key"]], "target_key")
            )[0]
            paused = self._strict_bool(payload["paused"], "paused")
            if paused and "routing_pause" not in self._capabilities().features:
                raise _PayloadError(AuthorityErrorCode.CONFLICT, "routing pause is unsupported")
            if not isinstance(app_name, str) or app_name.casefold() in SPECIAL_APPS:
                raise ValueError("routing pause requires a regular app")
            canonical = next(
                (name for name in channel.get("app_names", []) if str(name).casefold() == app_name.casefold()),
                None,
            )
            if canonical is None:
                raise _PayloadError(AuthorityErrorCode.CONFLICT, "routing pause target is not mapped")
            existing = [
                name
                for name in channel.get("routing_paused_apps", [])
                if str(name).casefold() != app_name.casefold()
            ]
            if paused:
                existing.append(canonical)
            channel["routing_paused_apps"] = existing
            for shared in after["channels"]:
                if shared is channel:
                    continue
                shared_canonical = next(
                    (
                        name
                        for name in shared.get("app_names", [])
                        if str(name).casefold() == app_name.casefold()
                    ),
                    None,
                )
                if shared_canonical is None:
                    continue
                shared["routing_paused_apps"] = [
                    name
                    for name in shared.get("routing_paused_apps", [])
                    if str(name).casefold() != app_name.casefold()
                ]
                if paused:
                    shared["routing_paused_apps"].append(shared_canonical)
        elif command_type in {"set_channel_volume_midi_binding", "set_channel_mute_midi_binding"}:
            cc = self._optional_midi_cc(payload["cc"], "cc")
            midi_channel = self._strict_int(payload["midi_channel"], "midi_channel", minimum=0, maximum=15)
            if command_type == "set_channel_volume_midi_binding":
                channel["midi_cc"] = cc
                channel["midi_channel"] = midi_channel if cc is not None else 0
            else:
                channel["midi_mute_cc"] = cc
                channel["midi_mute_channel"] = midi_channel if cc is not None else 0
        elif command_type == "set_channel_volume":
            volume = self._finite_float(payload["volume"], "volume")
            if not 0.0 <= volume <= 1.0:
                raise ValueError("volume must be between 0 and 1")
            return self._runtime_volume_plan(before, index, volume, str(channel["channel_id"]))
        elif command_type == "set_channel_mute":
            muted = self._strict_bool(payload["muted"], "muted")
            return self._runtime_mute_plan(before, index, muted, str(channel["channel_id"]))

        changes: dict[str, Any]
        if command_type == "set_channel_volume":
            changes = {str(channel["channel_id"]): {"volume": float(channel["volume"])}}
        else:
            changes = {"channel": {"id": channel["channel_id"], "field": command_type.removeprefix("set_channel_")}}
        return self._profile_replacement(
            before,
            after,
            changes,
            volume=command_type == "set_channel_volume",
            runtime_volume_index=index if command_type == "set_channel_volume" else None,
        )

    def _runtime_volume_plan(
        self,
        profile: dict[str, Any],
        index: int,
        volume: float,
        channel_id: str,
    ) -> _PreparedCommand:
        old_volume = float(self._config.get_channel_volume(index))
        affected_indexes = [index]
        shared_channels = getattr(self._backend, "get_effective_shared_target_channels", None)
        if (
            self._config.midi_fader_feedback
            or self._config.remote_midi_role == "receive"
        ) and callable(shared_channels):
            affected_indexes = list(shared_channels(index))
        changes = {
            str(profile["channels"][affected_index]["channel_id"]): {"volume": volume}
            for affected_index in affected_indexes
        }
        changes.setdefault(channel_id, {"volume": volume})

        def set_value(value: float) -> None:
            self._config.set_channel_volume(index, value)
            if self._backend is not None:
                self._backend.set_channel_volume(index, value)

        return _PreparedCommand(
            lambda: set_value(volume),
            lambda: set_value(old_volume),
            changes,
            volume=True,
        )

    def _resolve_mapping_keys(self, keys: list[str]) -> list[str]:
        provider = self._inventory_provider
        resolver = getattr(provider, "resolve_mapping_keys", None)
        if callable(resolver):
            try:
                return list(resolver(keys))
            except KeyError as exc:
                raise _PayloadError(
                    AuthorityErrorCode.NOT_FOUND,
                    f"target is not in receiver inventory: {exc.args[0]}",
                ) from exc
        labels = {item.key: item.label for item in self._inventory()}
        try:
            return [labels[key] for key in keys]
        except KeyError as exc:
            raise _PayloadError(
                AuthorityErrorCode.NOT_FOUND,
                f"target is not in receiver inventory: {exc.args[0]}",
            ) from exc

    def _runtime_mute_plan(
        self,
        profile: dict[str, Any],
        index: int,
        muted: bool,
        channel_id: str,
    ) -> _PreparedCommand:
        if self._backend is None and self._runtime_mute_setter is None:
            raise _PayloadError(AuthorityErrorCode.CONFLICT, "no runtime mute adapter")
        old_muted = bool(self._backend.is_channel_muted(index)) if self._backend is not None else not muted

        def set_value(value: bool) -> None:
            if self._runtime_mute_setter is not None:
                self._runtime_mute_setter(index, value)
                return
            assert self._backend is not None
            if bool(self._backend.is_channel_muted(index)) != value:
                self._backend.toggle_mute(index)

        return _PreparedCommand(
            lambda: set_value(muted),
            lambda: set_value(old_muted),
            {"channel": {"id": channel_id, "muted": muted}},
        )

    def _profile_replacement(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        changes: dict[str, Any],
        *,
        allow_resize: bool = False,
        destructive: bool = False,
        volume: bool = False,
        runtime_volume_index: int | None = None,
    ) -> _PreparedCommand:
        def save(profile: dict[str, Any]) -> None:
            self._profiles.save_profile(profile, allow_resize=allow_resize)
            if profile["id"] == (self._profiles.active_profile_id or self._config.active_profile_id):
                previous_channels = self._config.all_channels()
                self._config.apply_profile(profile)
                self._emit_runtime_profile_diff(previous_channels, self._config.all_channels())
                if runtime_volume_index is not None and self._backend is not None:
                    self._backend.set_channel_volume(
                        runtime_volume_index,
                        float(profile["channels"][runtime_volume_index]["volume"]),
                    )

        return _PreparedCommand(
            lambda: save(after),
            lambda: save(before),
            changes,
            destructive=destructive,
            volume=volume,
        )

    def _select_profile(self, profile: dict[str, Any]) -> None:
        if self._profile_selector is not None:
            if not self._profile_selector(str(profile["id"])):
                raise RuntimeError(f"profile switch failed: {profile['id']}")
            return
        previous_channels = self._config.all_channels()
        self._profiles.switch(str(profile["id"]))
        self._config.apply_profile(profile)
        self._emit_runtime_profile_diff(previous_channels, self._config.all_channels())
        self._config.active_profile_id = str(profile["id"])
        self._config.save()
        if profile.get("restore_fader_positions") and self._backend is not None:
            volumes = [float(channel.get("volume", 1.0)) for channel in self._config.all_channels()]
            self._backend.apply_poti_volumes(volumes, force=True)

    def _emit_runtime_profile_diff(
        self,
        before: Sequence[Mapping[str, Any]],
        after: Sequence[Mapping[str, Any]],
    ) -> None:
        for index in range(max(len(before), len(after))):
            old = before[index] if index < len(before) else {}
            new = after[index] if index < len(after) else {}
            old_mappings = list(old.get("app_names", []))
            new_mappings = list(new.get("app_names", []))
            if old_mappings != new_mappings:
                self._config.mapping_changed.emit(index, new_mappings)
            old_v_sink = bool(old.get("v_sink", False))
            new_v_sink = bool(new.get("v_sink", False))
            if old_v_sink != new_v_sink:
                self._config.v_sink_changed.emit(index, new_v_sink)

        old_paused = {
            str(name).casefold(): str(name)
            for channel in before
            for name in channel.get("routing_paused_apps", [])
        }
        new_paused = {
            str(name).casefold(): str(name)
            for channel in after
            for name in channel.get("routing_paused_apps", [])
        }
        for folded in sorted(old_paused.keys() | new_paused.keys()):
            if (folded in old_paused) != (folded in new_paused):
                app_name = new_paused[folded] if folded in new_paused else old_paused[folded]
                self._config.routing_pause_changed.emit(
                    app_name,
                    folded in new_paused,
                )

    def _cached_result_for_transport(
        self,
        cached: CommandResult,
        transport_session_id: str,
    ) -> CommandResult:
        if cached.response.transport_session_id == transport_session_id:
            return cached
        if isinstance(cached.response, AckMessage):
            response: AckMessage | NackMessage = AckMessage(
                protocol_version=cached.response.protocol_version,
                schema_version=cached.response.schema_version,
                transport_session_id=transport_session_id,
                command_id=cached.response.command_id,
                revision=cached.response.revision,
            )
        else:
            response = NackMessage(
                protocol_version=cached.response.protocol_version,
                schema_version=cached.response.schema_version,
                transport_session_id=transport_session_id,
                command_id=cached.response.command_id,
                reason=cached.response.reason,
                current_epoch=cached.response.current_epoch,
                current_revision=cached.response.current_revision,
            )
        return CommandResult(
            accepted=cached.accepted,
            response=response,
            error_code=cached.error_code,
            publication=cached.publication,
        )

    def _load_profile(self, profile_id: Any) -> dict[str, Any]:
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError("profile_id must be a non-empty string")
        try:
            return cast(dict[str, Any], self._profiles.load(profile_id))
        except (FileNotFoundError, KeyError) as exc:
            raise _PayloadError(AuthorityErrorCode.NOT_FOUND, f"profile not found: {profile_id}") from exc

    def _profile_and_channel(self, payload: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        profile = self._load_profile(payload["profile_id"])
        channel_id = require_uuid(str(payload["channel_id"]), field_name="channel_id")
        for index, channel in enumerate(profile.get("channels", [])):
            if str(channel.get("channel_id")) == channel_id:
                return profile, index
        raise _PayloadError(AuthorityErrorCode.NOT_FOUND, f"channel not found: {channel_id}")

    def _nack(
        self,
        command: CommandMessage,
        code: AuthorityErrorCode,
        *,
        cache: bool = True,
    ) -> CommandResult:
        try:
            wire_reason = NackReason(code.value)
        except ValueError:
            # Until Layer 1 grows the corresponding enum member, retain a
            # decodable protocol NACK and expose the precise reason through
            # CommandResult.error_code.
            wire_reason = NackReason.INVALID_PAYLOAD
        result = CommandResult(
            accepted=False,
            response=NackMessage(
                protocol_version=PROTOCOL_VERSION,
                schema_version=SCHEMA_VERSION,
                transport_session_id=command.transport_session_id,
                command_id=command.command_id,
                reason=wire_reason.value,
                current_epoch=self.epoch,
                current_revision=self.revision,
            ),
            error_code=code,
        )
        if cache:
            self._cache_result(command.command_id, result)
        return result

    def _cache_result(self, command_id: str, result: CommandResult) -> None:
        self._result_cache[command_id] = result
        self._result_cache.move_to_end(command_id)
        while len(self._result_cache) > MAX_IDEMPOTENCY_CACHE:
            self._result_cache.popitem(last=False)
        reason = None
        if isinstance(result.response, NackMessage):
            reason = NackReason(result.response.reason)
        self._semantic_cache.put(
            command_id,
            CachedCommandResult(accepted=result.accepted, revision=result.revision, reason=reason),
        )

    def _require_main_thread(self) -> None:
        if QThread.currentThread() is not self.thread():
            raise RuntimeError("ReceiverMixerAuthority may only access managers on its Qt thread")

    @staticmethod
    def _exact(payload: Mapping[str, Any], required: set[str], optional: set[str] | None = None) -> None:
        allowed = required | (optional or set())
        missing = required - set(payload)
        extra = set(payload) - allowed
        if missing or extra:
            raise ValueError(f"payload fields mismatch; missing={sorted(missing)}, extra={sorted(extra)}")

    @staticmethod
    def _strict_bool(value: Any, field_name: str) -> bool:
        if not isinstance(value, bool):
            raise ValueError(f"{field_name} must be a boolean")
        return value

    @staticmethod
    def _strict_int(
        value: Any,
        field_name: str,
        *,
        minimum: int,
        maximum: int | None = None,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field_name} must be an integer")
        if value < minimum or (maximum is not None and value > maximum):
            raise ValueError(f"{field_name} is out of range")
        return value

    @staticmethod
    def _finite_float(value: Any, field_name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field_name} must be a number")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"{field_name} must be finite")
        return result

    @classmethod
    def _optional_midi_cc(cls, value: Any, field_name: str) -> int | None:
        if value is None:
            return None
        return cls._strict_int(value, field_name, minimum=0, maximum=127)

    @staticmethod
    def _name(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("name must be a string")
        name = value.strip()
        if not name or len(name) > MAX_NAME_LENGTH or not name.isprintable():
            raise ValueError("name must be non-empty, printable, and bounded")
        return name

    @staticmethod
    def _string_list(value: Any, field_name: str) -> list[str]:
        if not isinstance(value, list) or len(value) > MAX_OTHER_LIST:
            raise ValueError(f"{field_name} must be a bounded list")
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not item or len(item) > MAX_LABEL_LENGTH or not item.isprintable():
                raise ValueError(f"{field_name} entries must be non-empty printable strings")
            folded = item.casefold()
            if folded not in seen:
                result.append(item)
                seen.add(folded)
        return result

    @staticmethod
    def _validate_target_key(key: str) -> None:
        if (
            not isinstance(key, str)
            or not key
            or len(key) > MAX_TARGET_KEY_LENGTH
            or not key.isprintable()
            or key.startswith(("/", "\\"))
            or "://" in key
            or "\x00" in key
        ):
            raise ValueError("target key is not a canonical stable key")

    @classmethod
    def _validate_publication_changes(cls, changes: Mapping[str, Any]) -> None:
        validate_finite(changes)

        def inspect(value: Any) -> None:
            if isinstance(value, Mapping):
                forbidden = set(value) & FORBIDDEN_RAW_KEYS
                if forbidden:
                    raise ValueError(f"publication contains machine-local fields: {sorted(forbidden)}")
                for item in value.values():
                    inspect(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    inspect(item)

        inspect(changes)


__all__ = [
    "ALLOWED_AUTHORITY_COMMAND_TYPES",
    "ALLOWED_AUTHORITY_ERROR_CODES",
    "RECEIVER_COMMAND_TYPES",
    "REQUIRED_LAYER1_NACK_REASONS",
    "AuthorityErrorCode",
    "AuthorityStatus",
    "CommandContext",
    "CommandResult",
    "ControlSessionMetadata",
    "QueueReceipt",
    "ReceiverMixerAuthority",
    "ProtocolMessageSender",
    "ProfileSelector",
    "StatePublication",
    "ValidatedCommandEnvelope",
]
