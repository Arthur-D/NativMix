"""GUI-facing mixer state and command boundary.

The local implementation delegates to the existing managers and audio backend.
The remote implementation owns only an in-memory canonical snapshot and sends
typed commands; it never receives references to laptop persistence or audio.
"""

from __future__ import annotations

import copy
import logging
import math
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from nativmix.remote_sync.authority import RECEIVER_COMMAND_TYPES
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
    SCHEMA_VERSION,
    ChannelRecord,
    ProfileRecord,
    Snapshot,
    TargetInventoryItem,
    apply_volume_delta,
    parse_snapshot,
)
from nativmix.remote_sync.state import COMMAND_APPLY_DEADLINE_SECONDS, MAX_PENDING_COMMANDS, SubscriberState

logger = logging.getLogger(__name__)

ProtocolSender = Callable[[Any, int, str], None]
MAX_CONTROLLER_ORIGINS = 512


@dataclass(frozen=True)
class RemoteSyncSession:
    """Immutable worker publication for one correlated control transport."""

    generation: int
    role: str
    selected_peer_id: str | None
    connected_peer_id: str | None
    connected_peer_name: str | None
    transport_session_id: str | None
    available: bool
    detail: str = ""


@dataclass
class _PendingIntent:
    command_type: str
    payload: dict[str, Any]
    control_key: str
    command_id: str | None = None
    sent_at: float | None = None
    acknowledged_revision: int | None = None


def _channel_to_dict(channel: ChannelRecord) -> dict[str, Any]:
    return {
        "channel_id": channel.id,
        "index": channel.index,
        "label": channel.label,
        "is_midi": channel.is_midi,
        "mode": channel.mode,
        "app_names": list(channel.mappings),
        "hardware_id": channel.hardware_target_key,
        "routing_paused_apps": list(channel.routing_paused_apps),
        "inverted": channel.inverted,
        "v_sink": channel.v_sink,
        "midi_cc": channel.volume_cc,
        "midi_channel": channel.volume_channel,
        "midi_mute_cc": channel.mute_cc,
        "midi_mute_channel": channel.mute_channel,
        "volume": channel.saved_fader_volume,
    }


class LocalMixerFacade(QObject):
    """Preserve the existing local mixer behavior behind the GUI boundary."""

    state_changed = pyqtSignal()
    status_changed = pyqtSignal(str, str)
    pending_changed = pyqtSignal(str, bool)

    is_remote = False
    receiver_name = ""
    sync_status = "Local"
    sync_detail = "Controlling this computer."

    def __init__(self, config: Any, profiles: Any, backend: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.profiles = profiles
        self.backend = backend

    @property
    def active_profile_id(self) -> str:
        return str(self.profiles.active_profile_id) if self.profiles is not None else ""

    @property
    def active_profile_name(self) -> str:
        if self.profiles is None or not self.active_profile_id:
            return ""
        return str(self.profiles.load(self.active_profile_id).get("name", ""))

    @property
    def input_mode(self) -> str:
        return str(self.config.input_mode)

    @property
    def num_channels(self) -> int:
        return int(self.config.num_channels)

    @property
    def hw_channel_count(self) -> int:
        return int(self.config.hw_channel_count)

    @property
    def show_invert_option(self) -> bool:
        return bool(self.config.show_invert_option)

    @property
    def gain_control_supported(self) -> bool:
        return bool(getattr(self.backend, "gain_control_supported", True))

    @property
    def v_sink_supported(self) -> bool:
        return bool(getattr(self.backend, "v_sink_supported", True))

    @property
    def supports_midi(self) -> bool:
        return True

    @property
    def v_sink_capability_reason(self) -> str:
        return str(getattr(self.backend, "v_sink_capability_reason", ""))

    def all_channels(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.config.all_channels())

    def list_profiles(self) -> list[dict[str, Any]]:
        if self.profiles is None:
            return []
        return cast(list[dict[str, Any]], self.profiles.list_profiles())

    def load_profile(self, profile_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.profiles.load(profile_id))

    def get_channel_order(self) -> list[int]:
        if self.profiles is None:
            return [int(channel["index"]) for channel in self.all_channels()]
        return cast(list[int], self.profiles.get_channel_order())

    def set_channel_order(self, order: list[int]) -> None:
        if self.profiles is not None:
            self.profiles.set_channel_order(order)

    def create_profile(self, name: str, channel_count: int) -> str:
        return str(self.profiles.create(name, channel_count=channel_count))

    def duplicate_profile(self, profile_id: str, name: str) -> str:
        source = self.profiles.load(profile_id)
        return str(
            self.profiles.create(
                name,
                channel_count=int(source["channel_count"]),
                channels=source["channels"],
                channel_order=source.get("channel_order"),
            )
        )

    def rename_profile(self, profile_id: str, name: str) -> None:
        self.profiles.rename(profile_id, name)

    def select_profile(self, profile_id: str) -> None:
        # The composition root owns the complete local switch transaction.
        del profile_id

    def delete_profile(self, profile_id: str) -> None:
        self.profiles.delete(profile_id)

    def set_profile_restore_fader_positions(self, profile_id: str, enabled: bool) -> None:
        profile = self.profiles.load(profile_id)
        profile["restore_fader_positions"] = enabled
        self.profiles.save_profile(profile)

    def set_profile_midi_switch_cc(self, profile_id: str, cc: int | None) -> None:
        profile = self.profiles.load(profile_id)
        profile["midi_switch_cc"] = cc
        self.profiles.save_profile(profile)

    def add_midi_channel(self) -> None:
        self.config.add_midi_channel()

    def remove_midi_channels(self, indices: list[int]) -> None:
        self.config.remove_midi_channels(indices)

    def remove_midi_channel(self, index: int) -> None:
        self.config.remove_midi_channel(index)

    def get_channel_volume(self, index: int) -> float:
        return float(self.config.get_channel_volume(index))

    def set_channel_volume(self, index: int, volume: float) -> None:
        self.backend.set_channel_volume(index, volume)

    def is_channel_muted(self, index: int) -> bool:
        return bool(self.backend.is_channel_muted(index))

    def toggle_mute(self, index: int) -> None:
        self.backend.toggle_mute(index)

    def get_channel_label(self, index: int) -> str | None:
        return cast(str | None, self.config.get_channel_label(index))

    def set_channel_label(self, index: int, label: str) -> None:
        self.config.set_channel_label(index, label)
        self.config.save()

    def get_effective_inversion(self, index: int) -> bool:
        return bool(self.config.get_effective_inversion(index))

    def set_inverted(self, index: int, inverted: bool) -> None:
        self.config.set_inverted(index, inverted)
        self.config.save()

    def get_channel_mode(self, index: int) -> str:
        return str(self.config.get_channel_mode(index))

    def change_channel_mode(self, index: int, mode: str) -> None:
        self.config.set_channel_mode(index, mode)
        if mode == "hardware":
            self.config.set_app_names(index, [])
            if self.config.is_v_sink_enabled(index):
                self.set_v_sink_enabled(index, False)
        else:
            self.config.set_hardware_id(index, None)
        self.config.save()

    def get_app_names(self, index: int) -> list[str]:
        return cast(list[str], self.config.get_app_names(index))

    def toggle_mapping(self, index: int, target_key: str) -> None:
        current = self.get_app_names(index)
        if target_key in current:
            self.config.remove_app_name(index, target_key)
        else:
            self.config.update_mapping(target_key, index)
        self.config.save()

    def remove_app_name(self, index: int, name: str) -> None:
        self.config.remove_app_name(index, name)
        self.config.save()

    def get_hardware_id(self, index: int) -> str | None:
        return cast(str | None, self.config.get_hardware_id(index))

    def toggle_hardware_target(self, index: int, target_key: str) -> None:
        current = self.get_hardware_id(index)
        self.config.set_hardware_id(index, None if current == target_key else target_key)
        self.config.save()

    def clear_hardware_target(self, index: int) -> None:
        self.config.set_hardware_id(index, None)
        self.config.save()

    def is_app_routing_paused(self, index: int, name: str) -> bool:
        return bool(self.config.is_app_routing_paused(index, name))

    def set_app_routing_paused(self, index: int, name: str, paused: bool) -> None:
        self.config.set_app_routing_paused(index, name, paused)

    def is_v_sink_enabled(self, index: int) -> bool:
        return bool(self.config.is_v_sink_enabled(index))

    def set_v_sink_enabled(self, index: int, enabled: bool) -> None:
        self.config.set_v_sink_enabled(index, enabled)
        self.config.save()
        if enabled and self.v_sink_supported:
            self.backend.enable_v_sink(index)
        else:
            self.backend.disable_v_sink(index)

    def get_midi_cc(self, index: int) -> int | None:
        return cast(int | None, self.config.get_midi_cc(index))

    def get_midi_channel(self, index: int) -> int:
        return int(self.config.get_midi_channel(index))

    def set_midi_cc(self, index: int, cc: int | None, midi_channel: int | None = None) -> None:
        self.config.set_midi_cc(index, cc, midi_channel=midi_channel)

    def set_midi_channel(self, index: int, midi_channel: int) -> None:
        self.config.set_midi_channel(index, midi_channel)

    def get_midi_mute_cc(self, index: int) -> int | None:
        return cast(int | None, self.config.get_midi_mute_cc(index))

    def get_midi_mute_channel(self, index: int) -> int:
        return int(self.config.get_midi_mute_channel(index))

    def set_midi_mute_cc(self, index: int, cc: int | None, midi_channel: int | None = None) -> None:
        self.config.set_midi_mute_cc(index, cc, midi_channel=midi_channel)

    def set_midi_mute_channel(self, index: int, midi_channel: int) -> None:
        self.config.set_midi_mute_channel(index, midi_channel)

    def get_unresolved_targets(self) -> set[str]:
        getter = getattr(self.backend, "get_unresolved_targets", None)
        return set(getter()) if callable(getter) else set()

    def get_target_inventory(self, mode: str) -> list[TargetInventoryItem]:
        if mode == "hardware":
            result = [
                TargetInventoryItem(f"sink:{name}", desc, "output", True)
                for desc, name in self.backend.get_real_sinks()
                if not name.startswith("NativMix_")
            ]
            result.extend(
                TargetInventoryItem(f"source:{name}", desc, "input", True)
                for desc, name in self.backend.get_real_sources()
            )
            return result
        names = {
            stream.app_name
            for stream in self.backend.get_active_streams()
            if "speech-dispatcher" not in stream.app_name.lower() and "dummy" not in stream.app_name.lower()
        }
        names.update(("System Master", "Other Apps"))
        return [
            TargetInventoryItem(name, name, "output", True)
            for name in sorted(names, key=lambda item: (item not in {"System Master", "Other Apps"}, item.casefold()))
        ]

    def get_target_label(self, key: str) -> str:
        for item in self.get_target_inventory("hardware"):
            if item.key == key:
                return str(item.label)
        return key.split(":", 1)[1] if ":" in key else key

    def is_pending(self, control_key: str) -> bool:
        del control_key
        return False

    def control_key(self, index: int, field: str) -> str:
        return f"channel:{index}:{field}"


class RemoteMixerFacade(QObject):
    """Receiver-authoritative, memory-only mixer model and command facade."""

    state_changed = pyqtSignal()
    status_changed = pyqtSignal(str, str)
    pending_changed = pyqtSignal(str, bool)
    rejection = pyqtSignal(str)
    active_changed = pyqtSignal(bool)
    controller_correction_requested = pyqtSignal(int, float, object)

    is_remote = True

    def __init__(
        self,
        sender: ProtocolSender,
        *,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._sender = sender
        self._clock = clock
        self._subscriber = SubscriberState()
        self._snapshot: Snapshot | None = None
        self._session: RemoteSyncSession | None = None
        self._last_generation = -1
        self._control_session_id = str(uuid.uuid4())
        self._inflight: _PendingIntent | None = None
        self._queue: deque[_PendingIntent] = deque()
        self.receiver_name = ""
        self.sync_status = "MIDI-only"
        self.sync_detail = "AppleMIDI is available; mirrored mixer state is not connected."
        self._deadline_timer = QTimer(self)
        self._deadline_timer.setInterval(250)
        self._deadline_timer.timeout.connect(self.expire_pending)
        self._deadline_timer.start()
        self._snapshot_requested_at: float | None = None
        self._snapshot_request_id: str | None = None
        self._last_info_log_at: dict[str, float] = {}
        self._controller_origins: OrderedDict[tuple[str, int, int, int], Any] = OrderedDict()
        self._latest_controller_sequence: dict[tuple[int, int], int] = {}

    @property
    def active(self) -> bool:
        return self._snapshot is not None and self._session is not None and self._session.available

    @property
    def active_profile_id(self) -> str:
        return self._snapshot.active_profile_id if self._snapshot is not None else ""

    @property
    def active_profile_name(self) -> str:
        return self._snapshot.active_profile_name if self._snapshot is not None else ""

    @property
    def input_mode(self) -> str:
        channels = self.all_channels()
        if channels and all(channel["is_midi"] for channel in channels):
            return "midi_only"
        return "hybrid"

    @property
    def num_channels(self) -> int:
        return len(self.all_channels())

    @property
    def hw_channel_count(self) -> int:
        return sum(not bool(channel["is_midi"]) for channel in self.all_channels())

    @property
    def show_invert_option(self) -> bool:
        return True

    @property
    def gain_control_supported(self) -> bool:
        if self._snapshot is None:
            return False
        return all(state.capability_state != "unsupported" for state in self._snapshot.runtime_states)

    @property
    def v_sink_supported(self) -> bool:
        return bool(self._snapshot and self._snapshot.capabilities.supports_v_sink)

    @property
    def supports_midi(self) -> bool:
        return bool(self._snapshot and self._snapshot.capabilities.supports_midi)

    @property
    def v_sink_capability_reason(self) -> str:
        return "" if self.v_sink_supported else "The receiver cannot create NativMix virtual sinks."

    def _active_profile(self) -> ProfileRecord | None:
        if self._snapshot is None:
            return None
        return next(
            (profile for profile in self._snapshot.profiles if profile.id == self._snapshot.active_profile_id),
            None,
        )

    def _channel(self, index: int) -> ChannelRecord | None:
        profile = self._active_profile()
        if profile is None:
            return None
        return next((channel for channel in profile.channels if channel.index == index), None)

    def _channel_id(self, index: int) -> str:
        channel = self._channel(index)
        if channel is None:
            raise IndexError(index)
        return str(channel.id)

    def all_channels(self) -> list[dict[str, Any]]:
        profile = self._active_profile()
        return [_channel_to_dict(channel) for channel in profile.channels] if profile is not None else []

    def list_profiles(self) -> list[dict[str, Any]]:
        if self._snapshot is None:
            return []
        return [
            {"id": profile.id, "name": profile.name, "channel_count": profile.channel_count}
            for profile in self._snapshot.profiles
        ]

    def load_profile(self, profile_id: str) -> dict[str, Any]:
        if self._snapshot is None:
            raise KeyError(profile_id)
        profile = next((item for item in self._snapshot.profiles if item.id == profile_id), None)
        if profile is None:
            raise KeyError(profile_id)
        return cast(dict[str, Any], profile.to_canonical())

    def get_channel_order(self) -> list[int]:
        profile = self._active_profile()
        if self._snapshot is None or profile is None:
            return []
        indexes = {channel.id: channel.index for channel in profile.channels}
        return [indexes[channel_id] for channel_id in self._snapshot.channel_order if channel_id in indexes]

    def set_channel_order(self, order: list[int]) -> None:
        self._submit_channel_order(order)

    @pyqtSlot(object)
    def begin_session(self, raw_session: Any) -> None:
        role = getattr(raw_session, "role", "")
        role_value = getattr(role, "value", role)
        session = RemoteSyncSession(
            generation=int(raw_session.generation),
            role=str(role_value),
            selected_peer_id=getattr(raw_session, "selected_peer_id", None),
            connected_peer_id=getattr(raw_session, "connected_peer_id", None),
            connected_peer_name=getattr(raw_session, "connected_peer_name", None),
            transport_session_id=getattr(raw_session, "transport_session_id", None),
            available=bool(getattr(raw_session, "available", False)),
            detail=str(getattr(raw_session, "detail", "")),
        )
        if session.generation < self._last_generation:
            return
        self._last_generation = session.generation
        if session.role != "send" or not session.available or not session.transport_session_id:
            self.dispose(session.detail or "Mixer synchronization disconnected; AppleMIDI remains available.")
            return
        if self._session is not None and session.generation < self._session.generation:
            return
        identity = (session.generation, session.transport_session_id, session.connected_peer_id)
        current_identity = (
            self._session.generation,
            self._session.transport_session_id,
            self._session.connected_peer_id,
        ) if self._session is not None else None
        if identity == current_identity:
            return
        self._clear_pending()
        self._snapshot = None
        self._subscriber = SubscriberState()
        self._snapshot_request_id = None
        self._session = session
        self.receiver_name = session.connected_peer_name or "receiver"
        self._control_session_id = str(uuid.uuid4())
        self._controller_origins.clear()
        self._latest_controller_sequence.clear()
        self._set_status("Syncing", "MIDI connected; requesting mixer permission and a fresh receiver snapshot.")
        self._request_snapshot()
        self.state_changed.emit()

    def dispose(self, detail: str = "Remote mixer control is off.") -> None:
        had_state = self._snapshot is not None or self._session is not None
        was_active = self.active
        self._clear_pending()
        self._snapshot = None
        self._subscriber = SubscriberState()
        self._snapshot_requested_at = None
        self._snapshot_request_id = None
        self._session = None
        self.receiver_name = ""
        self._controller_origins.clear()
        self._latest_controller_sequence.clear()
        self._set_status("MIDI-only", detail)
        if had_state:
            self.state_changed.emit()
        if was_active:
            self.active_changed.emit(False)

    def apply_transport_status(self, status: str, detail: str) -> None:
        """Expose friendly setup phases while AppleMIDI remains independent."""
        if status == "Connected":
            if self._snapshot is None:
                self._set_status("Syncing", "MIDI connected; requesting mixer permission and snapshot.")
            return
        if status in {"Reconnecting", "Version incompatible"}:
            self.dispose(detail)
            self._set_status(status, f"{detail} AppleMIDI remains available.")
        elif self._session is None:
            self._set_status("MIDI-only" if status == "Permission disabled" else status, detail)

    @pyqtSlot(object)
    def handle_envelope(self, envelope: Any) -> None:
        session = self._session
        if session is None:
            return
        role = getattr(envelope.role, "value", envelope.role)
        if (
            role != "send"
            or envelope.generation != session.generation
            or envelope.transport_session_id != session.transport_session_id
            or envelope.connected_peer_id != session.connected_peer_id
        ):
            logger.debug("Discarding stale or mismatched remote mixer envelope")
            return
        message = envelope.message
        try:
            if isinstance(message, SnapshotMessage):
                if self.sync_status == "Permission disabled":
                    self._set_status("Syncing", "Receiver permission enabled; applying a fresh canonical snapshot.")
                self._apply_snapshot(parse_snapshot(message.snapshot))
            elif isinstance(message, DeltaMessage):
                self._apply_delta(message)
            elif isinstance(message, AckMessage):
                self._apply_ack(message)
            elif isinstance(message, NackMessage):
                self._apply_nack(message)
        except (TypeError, ValueError) as exc:
            publication_type = getattr(message, "to_wire", lambda: {"type": type(message).__name__})().get("type")
            revision = (
                message.snapshot.get("revision")
                if isinstance(message, SnapshotMessage)
                else getattr(message, "revision", None)
            )
            logger.warning(
                "Remote mixer publication rejected: type=%s revision=%s error=%s",
                publication_type,
                revision,
                exc,
            )
            self._resynchronize(
                "Conflict/resynchronizing",
                "Receiver state validation failed; requesting a fresh snapshot.",
            )

    @pyqtSlot(object)
    def note_local_controller_origin(self, origin: Any) -> None:
        """Remember a local physical event until its canonical publication arrives."""
        session = self._session
        if (
            session is None
            or int(getattr(origin, "generation", -1)) != session.generation
            or str(getattr(origin, "transport_session_id", "")) != session.transport_session_id
        ):
            logger.debug("Ignoring local controller origin from a replaced session")
            return
        binding = (int(origin.midi_channel), int(origin.control))
        key = (session.transport_session_id, int(origin.rtp_sequence), *binding)
        self._controller_origins[key] = origin
        self._controller_origins.move_to_end(key)
        self._latest_controller_sequence[binding] = int(origin.local_sequence)
        while len(self._controller_origins) > MAX_CONTROLLER_ORIGINS:
            self._controller_origins.popitem(last=False)

    def _apply_snapshot(self, snapshot: Snapshot) -> None:
        was_active = self.active
        old_epoch = self._subscriber.epoch
        if not self._subscriber.apply_snapshot(snapshot):
            self._resynchronize("Conflict/resynchronizing", "Receiver snapshot hash mismatch.")
            return
        if old_epoch is not None and old_epoch != snapshot.epoch:
            self._clear_pending()
        self._snapshot = snapshot
        self._snapshot_requested_at = None
        self._snapshot_request_id = None
        self._set_status("Connected", f"Controlling {self.receiver_name} - {snapshot.active_profile_name}")
        if not was_active:
            self._log_info_rate_limited(
                f"active:{self._session.generation if self._session else -1}",
                "Remote mixer model activated: receiver=%r revision=%d channels=%d",
                self.receiver_name,
                snapshot.revision,
                len(self.all_channels()),
            )
        self.state_changed.emit()
        if not was_active:
            self.active_changed.emit(True)
        self._finish_acknowledged()

    def _apply_delta(self, message: DeltaMessage) -> None:
        if self._snapshot is None:
            self._resynchronize("Conflict/resynchronizing", "A delta arrived before the initial snapshot.")
            return
        if (
            message.revision == self._subscriber.revision
            and message.resulting_hash == self._subscriber.content_hash
        ):
            return
        volumes = message.changes.get("volumes")
        if not isinstance(volumes, Mapping):
            self._resynchronize("Conflict/resynchronizing", "Unsupported receiver delta; requesting a fresh snapshot.")
            return
        canonical_volumes: dict[str, float] = {}
        for channel_id, raw_change in volumes.items():
            if (
                not isinstance(raw_change, Mapping)
                or set(raw_change) != {"volume"}
                or isinstance(raw_change["volume"], bool)
                or not isinstance(raw_change["volume"], (int, float))
                or not math.isfinite(float(raw_change["volume"]))
                or not 0.0 <= float(raw_change["volume"]) <= 1.0
            ):
                self._resynchronize("Conflict/resynchronizing", "Invalid receiver volume delta.")
                return
            canonical_volumes[str(channel_id)] = float(raw_change["volume"])
        origins = message.changes.get("origins", {})
        if not isinstance(origins, Mapping) or any(channel_id not in canonical_volumes for channel_id in origins):
            self._resynchronize("Conflict/resynchronizing", "Invalid receiver volume provenance.")
            return
        candidate = apply_volume_delta(
            self._snapshot,
            epoch=message.receiver_epoch,
            revision=message.revision,
            resulting_hash=message.resulting_hash,
            volumes=canonical_volumes,
        )
        if not self._subscriber.apply_delta(
            epoch=message.receiver_epoch,
            base_revision=message.base_revision,
            resulting_revision=message.revision,
            resulting_hash=message.resulting_hash,
            verify_hash=lambda: candidate.content_hash,
        ):
            self._resynchronize("Conflict/resynchronizing", "Receiver revision gap or hash mismatch.")
            return
        self._snapshot = candidate
        self._set_status("Connected", f"Controlling {self.receiver_name} - {self.active_profile_name}")
        session = self._session
        logger.debug(
            "Sender canonical volume acknowledgement applied: generation=%d session=%s revision=%d controls=%s",
            session.generation if session is not None else -1,
            session.transport_session_id if session is not None else "none",
            message.revision,
            sorted(canonical_volumes),
        )
        self.state_changed.emit()
        self._dispatch_controller_corrections(canonical_volumes, origins, message.revision)
        self._finish_acknowledged()

    def _dispatch_controller_corrections(
        self,
        canonical_volumes: Mapping[str, float],
        origins: Mapping[str, Any],
        revision: int,
    ) -> None:
        handled: set[tuple[str, int, int, int]] = set()
        for channel_id, raw_origin in origins.items():
            if not isinstance(raw_origin, Mapping):
                continue
            try:
                session_id = str(raw_origin["transport_session_id"])
                rtp_sequence = int(raw_origin["rtp_sequence"])
                midi_channel = int(raw_origin["midi_channel"])
                control = int(raw_origin["control"])
                requested_volume = float(raw_origin["requested_volume"])
            except (KeyError, TypeError, ValueError):
                logger.debug("Ignoring malformed controller provenance at revision %d", revision)
                continue
            key = (session_id, rtp_sequence, midi_channel, control)
            if key in handled:
                continue
            handled.add(key)
            local = self._controller_origins.pop(key, None)
            binding = (midi_channel, control)
            canonical = canonical_volumes[channel_id]
            if local is None:
                logger.debug(
                    "Controller feedback dropped: origin=remote_controller revision=%d session=%s "
                    "control=%s sequence=%d reason=unknown_or_expired",
                    revision,
                    session_id,
                    binding,
                    rtp_sequence,
                )
                continue
            latest = self._latest_controller_sequence.get(binding, -1)
            if int(local.local_sequence) != latest:
                logger.debug(
                    "Controller feedback dropped: origin=remote_controller revision=%d session=%s "
                    "control=%s sequence=%d reason=newer_local_input",
                    revision,
                    session_id,
                    binding,
                    rtp_sequence,
                )
                continue
            if abs(canonical - requested_volume) <= 0.5 / 127.0:
                logger.debug(
                    "Controller feedback suppressed: origin=remote_controller revision=%d session=%s "
                    "control=%s sequence=%d reason=canonical_ack",
                    revision,
                    session_id,
                    binding,
                    rtp_sequence,
                )
                continue
            logger.debug(
                "Controller feedback applied: origin=remote_controller revision=%d session=%s "
                "control=%s sequence=%d reason=newer_correction",
                revision,
                session_id,
                binding,
                rtp_sequence,
            )
            self.controller_correction_requested.emit(int(local.channel_index), canonical, raw_origin)

    def _apply_ack(self, message: AckMessage) -> None:
        pending = self._inflight
        if pending is None or pending.command_id != message.command_id:
            return
        pending.acknowledged_revision = message.revision
        self._finish_acknowledged()

    def _finish_acknowledged(self) -> None:
        pending = self._inflight
        if (
            pending is None
            or pending.acknowledged_revision is None
            or self._subscriber.revision < pending.acknowledged_revision
        ):
            return
        key = pending.control_key
        self._inflight = None
        if not any(item.control_key == key for item in self._queue):
            self.pending_changed.emit(key, False)
        self._send_next()

    def _apply_nack(self, message: NackMessage) -> None:
        pending = self._inflight
        if message.reason == "permission_disabled":
            if self._snapshot is not None and message.current_revision < self._snapshot.revision:
                logger.debug("Ignoring stale remote permission denial at revision %d", message.current_revision)
                return
            was_active = self.active
            had_state = self._snapshot is not None
            self._clear_pending()
            self._snapshot = None
            self._subscriber = SubscriberState()
            self._snapshot_requested_at = None
            self._set_status(
                "Permission disabled",
                "Receiver permission is disabled. AppleMIDI remains available.",
            )
            self._log_info_rate_limited(
                f"permission-disabled:{self._session.generation if self._session else -1}",
                "Remote mixer model revoked by receiver permission",
            )
            if had_state:
                self.state_changed.emit()
            if was_active:
                self.active_changed.emit(False)
            return
        if pending is None or pending.command_id != message.command_id:
            return
        friendly = {
            "permission_disabled": "Receiver permission is disabled.",
            "protocol_incompatible": "NativMix versions are incompatible.",
            "schema_incompatible": "NativMix versions are incompatible.",
            "stale_epoch": "The receiver restarted.",
            "stale_revision": "The receiver changed first.",
            "conflict": "The receiver rejected the conflicting edit.",
            "not_found": "The selected receiver target no longer exists.",
            "rate_limited": "The receiver is busy; wait before trying again.",
            "destructive_rate_limited": "Too many destructive requests; wait before trying again.",
        }.get(message.reason, f"Receiver rejected the command ({message.reason}).")
        self.rejection.emit(friendly)
        status = "Permission disabled" if message.reason == "permission_disabled" else "Conflict/resynchronizing"
        if "incompatible" in message.reason:
            status = "Version incompatible"
        self._resynchronize(status, f"{friendly} AppleMIDI remains available.")

    def _set_status(self, status: str, detail: str) -> None:
        changed = status != self.sync_status or detail != self.sync_detail
        self.sync_status = status
        self.sync_detail = detail
        if changed:
            self.status_changed.emit(status, detail)

    def _request_snapshot(self) -> None:
        session = self._session
        if session is None or not session.transport_session_id:
            return
        if self._snapshot_request_id is None:
            self._snapshot_request_id = str(uuid.uuid4())
        request = SnapshotRequest(
            protocol_version=PROTOCOL_VERSION,
            schema_version=SCHEMA_VERSION,
            transport_session_id=session.transport_session_id,
            request_id=self._snapshot_request_id,
        )
        self._snapshot_requested_at = self._clock()
        self._log_info_rate_limited(
            f"snapshot-request:{session.generation}:{session.transport_session_id}",
            "Remote mixer requesting canonical snapshot: generation=%d receiver=%r",
            session.generation,
            self.receiver_name,
        )
        self._sender(request, session.generation, session.transport_session_id)

    def _log_info_rate_limited(
        self,
        key: str,
        message: str,
        *args: Any,
        interval: float = 5.0,
    ) -> None:
        now = self._clock()
        last = self._last_info_log_at.get(key)
        if last is not None and now - last < interval:
            return
        self._last_info_log_at[key] = now
        logger.info(message, *args)

    def _resynchronize(self, status: str, detail: str) -> None:
        session = self._session
        logger.warning(
            "Remote mixer model became stale while TCP remains connected: generation=%d session=%s revision=%d "
            "reason=%s",
            session.generation if session is not None else -1,
            session.transport_session_id if session is not None else "none",
            self._subscriber.revision,
            detail,
        )
        self._clear_pending()
        self._subscriber.require_snapshot()
        self._set_status(status, detail)
        self._request_snapshot()

    def _clear_pending(self) -> None:
        keys = {item.control_key for item in self._queue}
        if self._inflight is not None:
            keys.add(self._inflight.control_key)
        self._inflight = None
        self._queue.clear()
        for key in keys:
            self.pending_changed.emit(key, False)

    @pyqtSlot()
    def expire_pending(self) -> None:
        if (
            self._session is not None
            and self._snapshot_requested_at is not None
            and self._clock() - self._snapshot_requested_at >= COMMAND_APPLY_DEADLINE_SECONDS
        ):
            self._set_status(
                "Syncing",
                "The receiver has not answered the snapshot request; retrying while AppleMIDI remains available.",
            )
            self._request_snapshot()
            return
        pending = self._inflight
        if pending is None or pending.sent_at is None:
            return
        if self._clock() - pending.sent_at >= COMMAND_APPLY_DEADLINE_SECONDS:
            self.rejection.emit("Receiver command timed out.")
            self._resynchronize(
                "Conflict/resynchronizing",
                "Receiver command timed out; requesting a fresh snapshot without retrying the edit.",
            )

    def _submit(self, command_type: str, payload: Mapping[str, Any], control_key: str) -> None:
        if command_type not in RECEIVER_COMMAND_TYPES:
            raise ValueError(f"Unsupported receiver command: {command_type}")
        if not self.active:
            self.rejection.emit("Remote mixer is not synchronized.")
            return
        intent = _PendingIntent(command_type, copy.deepcopy(dict(payload)), control_key)
        if command_type == "set_channel_volume":
            for queued in reversed(self._queue):
                if queued.control_key == control_key:
                    queued.payload = intent.payload
                    return
        if len(self._queue) + (self._inflight is not None) >= MAX_PENDING_COMMANDS:
            self.rejection.emit("Too many pending remote edits.")
            self._resynchronize("Conflict/resynchronizing", "Pending command limit reached; refreshing receiver state.")
            return
        self._queue.append(intent)
        self.pending_changed.emit(control_key, True)
        self._send_next()

    def _send_next(self) -> None:
        if self._inflight is not None or not self._queue or not self.active:
            return
        session = self._session
        if session is None or session.transport_session_id is None:
            return
        intent = self._queue.popleft()
        intent.command_id = str(uuid.uuid4())
        intent.sent_at = self._clock()
        command = CommandMessage(
            protocol_version=PROTOCOL_VERSION,
            schema_version=SCHEMA_VERSION,
            transport_session_id=session.transport_session_id,
            control_session_id=self._control_session_id,
            command_id=intent.command_id,
            receiver_epoch=str(self._subscriber.epoch),
            expected_revision=self._subscriber.revision,
            command_type=intent.command_type,
            payload=intent.payload,
        )
        self._inflight = intent
        self._sender(command, session.generation, session.transport_session_id)

    def is_pending(self, control_key: str) -> bool:
        return (
            self._inflight is not None and self._inflight.control_key == control_key
        ) or any(item.control_key == control_key for item in self._queue)

    def control_key(self, index: int, field: str) -> str:
        return f"channel:{self._channel_id(index)}:{field}"

    def _profile_payload(self) -> dict[str, str]:
        return {"profile_id": self.active_profile_id}

    def _channel_payload(self, index: int) -> dict[str, str]:
        return {"profile_id": self.active_profile_id, "channel_id": self._channel_id(index)}

    def create_profile(self, name: str, channel_count: int) -> None:
        self._submit("create_profile", {"name": name, "channel_count": channel_count}, "profiles")

    def duplicate_profile(self, profile_id: str, name: str) -> None:
        self._submit("duplicate_profile", {"profile_id": profile_id, "name": name}, "profiles")

    def rename_profile(self, profile_id: str, name: str) -> None:
        self._submit("rename_profile", {"profile_id": profile_id, "name": name}, "profiles")

    def select_profile(self, profile_id: str) -> None:
        self._submit("select_profile", {"profile_id": profile_id}, "profiles")

    def delete_profile(self, profile_id: str) -> None:
        self._submit("delete_profile", {"profile_id": profile_id}, "profiles")

    def set_profile_restore_fader_positions(self, profile_id: str, enabled: bool) -> None:
        self._submit(
            "set_profile_restore_fader_positions",
            {"profile_id": profile_id, "enabled": enabled},
            "profile:restore",
        )

    def set_profile_midi_switch_cc(self, profile_id: str, cc: int | None) -> None:
        self._submit(
            "set_profile_midi_switch_cc",
            {"profile_id": profile_id, "cc": cc},
            "profile:direct-midi",
        )

    def add_midi_channel(self) -> None:
        self._submit("add_midi_channel", self._profile_payload(), "channels")

    def remove_midi_channels(self, indices: list[int]) -> None:
        ids = [self._channel_id(index) for index in indices]
        self._submit(
            "delete_midi_channels",
            {**self._profile_payload(), "channel_ids": ids},
            "channels",
        )

    def remove_midi_channel(self, index: int) -> None:
        self.remove_midi_channels([index])

    def _submit_channel_order(self, order: list[int]) -> None:
        ids = [self._channel_id(index) for index in order]
        self._submit("reorder_channels", {**self._profile_payload(), "channel_ids": ids}, "channel-order")

    def get_channel_volume(self, index: int) -> float:
        if self._snapshot is None:
            return 1.0
        channel_id = self._channel_id(index)
        state = next((item for item in self._snapshot.runtime_states if item.channel_id == channel_id), None)
        return state.effective_volume if state is not None else 1.0

    def set_channel_volume(self, index: int, volume: float) -> None:
        self._submit(
            "set_channel_volume",
            {**self._channel_payload(index), "volume": float(volume)},
            f"channel:{self._channel_id(index)}:volume",
        )

    def is_channel_muted(self, index: int) -> bool:
        if self._snapshot is None:
            return False
        channel_id = self._channel_id(index)
        state = next((item for item in self._snapshot.runtime_states if item.channel_id == channel_id), None)
        return bool(state and state.muted)

    def toggle_mute(self, index: int) -> None:
        self._submit(
            "set_channel_mute",
            {**self._channel_payload(index), "muted": not self.is_channel_muted(index)},
            f"channel:{self._channel_id(index)}:mute",
        )

    def get_channel_label(self, index: int) -> str | None:
        channel = self._channel(index)
        return channel.label if channel is not None else None

    def set_channel_label(self, index: int, label: str) -> None:
        self._submit(
            "set_channel_label",
            {**self._channel_payload(index), "label": label},
            f"channel:{self._channel_id(index)}:label",
        )

    def get_effective_inversion(self, index: int) -> bool:
        channel = self._channel(index)
        return bool(channel and channel.inverted)

    def set_inverted(self, index: int, inverted: bool) -> None:
        self._submit(
            "set_channel_inverted",
            {**self._channel_payload(index), "inverted": inverted},
            f"channel:{self._channel_id(index)}:inverted",
        )

    def get_channel_mode(self, index: int) -> str:
        channel = self._channel(index)
        return channel.mode if channel is not None else "app"

    def change_channel_mode(self, index: int, mode: str) -> None:
        self._submit(
            "set_channel_mode",
            {**self._channel_payload(index), "mode": mode},
            f"channel:{self._channel_id(index)}:mode",
        )

    def get_app_names(self, index: int) -> list[str]:
        channel = self._channel(index)
        return list(channel.mappings) if channel is not None else []

    def _mapping_key(self, label: str) -> str:
        if self._snapshot is None:
            raise KeyError(label)
        item = next(
            (target for target in self._snapshot.inventory if target.label.casefold() == label.casefold()),
            None,
        )
        if item is None:
            raise KeyError(label)
        return str(item.key)

    def toggle_mapping(self, index: int, target_key: str) -> None:
        current_keys = [self._mapping_key(label) for label in self.get_app_names(index)]
        if target_key in current_keys:
            current_keys.remove(target_key)
        elif target_key.startswith("pseudo:"):
            current_keys = [target_key]
        elif any(key.startswith("pseudo:") for key in current_keys):
            current_keys = [target_key]
        else:
            current_keys.append(target_key)
        self._submit(
            "set_channel_mappings",
            {**self._channel_payload(index), "target_keys": current_keys},
            f"channel:{self._channel_id(index)}:mappings",
        )

    def remove_app_name(self, index: int, name: str) -> None:
        self.toggle_mapping(index, self._mapping_key(name))

    def get_hardware_id(self, index: int) -> str | None:
        channel = self._channel(index)
        return channel.hardware_target_key if channel is not None else None

    def toggle_hardware_target(self, index: int, target_key: str) -> None:
        self._submit(
            "set_channel_hardware_target",
            {
                **self._channel_payload(index),
                "target_key": None if self.get_hardware_id(index) == target_key else target_key,
            },
            f"channel:{self._channel_id(index)}:hardware",
        )

    def clear_hardware_target(self, index: int) -> None:
        current = self.get_hardware_id(index)
        if current is not None:
            self.toggle_hardware_target(index, current)

    def is_app_routing_paused(self, index: int, name: str) -> bool:
        channel = self._channel(index)
        return bool(channel and name.casefold() in {item.casefold() for item in channel.routing_paused_apps})

    def set_app_routing_paused(self, index: int, name: str, paused: bool) -> None:
        self._submit(
            "set_channel_routing_paused",
            {**self._channel_payload(index), "target_key": self._mapping_key(name), "paused": paused},
            f"channel:{self._channel_id(index)}:routing:{name.casefold()}",
        )

    def is_v_sink_enabled(self, index: int) -> bool:
        channel = self._channel(index)
        return bool(channel and channel.v_sink)

    def set_v_sink_enabled(self, index: int, enabled: bool) -> None:
        self._submit(
            "set_channel_v_sink",
            {**self._channel_payload(index), "enabled": enabled},
            f"channel:{self._channel_id(index)}:v-sink",
        )

    def get_midi_cc(self, index: int) -> int | None:
        channel = self._channel(index)
        return channel.volume_cc if channel is not None else None

    def get_midi_channel(self, index: int) -> int:
        channel = self._channel(index)
        return channel.volume_channel if channel is not None else 0

    def set_midi_cc(self, index: int, cc: int | None, midi_channel: int | None = None) -> None:
        channel = self.get_midi_channel(index) if midi_channel is None else midi_channel
        self._submit_midi_binding(index, cc, channel, False)

    def set_midi_channel(self, index: int, midi_channel: int) -> None:
        self._submit_midi_binding(index, self.get_midi_cc(index), midi_channel, False)

    def get_midi_mute_cc(self, index: int) -> int | None:
        channel = self._channel(index)
        return channel.mute_cc if channel is not None else None

    def get_midi_mute_channel(self, index: int) -> int:
        channel = self._channel(index)
        return channel.mute_channel if channel is not None else 0

    def set_midi_mute_cc(self, index: int, cc: int | None, midi_channel: int | None = None) -> None:
        self._submit_midi_binding(
            index,
            cc,
            self.get_midi_mute_channel(index) if midi_channel is None else midi_channel,
            True,
        )

    def set_midi_mute_channel(self, index: int, midi_channel: int) -> None:
        self._submit_midi_binding(index, self.get_midi_mute_cc(index), midi_channel, True)

    def _submit_midi_binding(self, index: int, cc: int | None, midi_channel: int, mute: bool) -> None:
        kind = "mute" if mute else "volume"
        self._submit(
            f"set_channel_{kind}_midi_binding",
            {**self._channel_payload(index), "cc": cc, "midi_channel": midi_channel},
            f"channel:{self._channel_id(index)}:{kind}-midi",
        )

    def get_unresolved_targets(self) -> set[str]:
        profile = self._active_profile()
        if self._snapshot is None or profile is None:
            return set()
        unresolved_ids = {state.channel_id for state in self._snapshot.runtime_states if state.unresolved}
        return {
            mapping
            for channel in profile.channels
            if channel.id in unresolved_ids
            for mapping in channel.mappings
        }

    def get_target_inventory(self, mode: str) -> list[TargetInventoryItem]:
        if self._snapshot is None:
            return []
        prefix = "device:" if mode == "hardware" else ("app:", "pseudo:")
        return [item for item in self._snapshot.inventory if item.key.startswith(prefix)]

    def get_target_label(self, key: str) -> str:
        if self._snapshot is None:
            return key
        item = next((target for target in self._snapshot.inventory if target.key == key), None)
        return item.label if item is not None else key
