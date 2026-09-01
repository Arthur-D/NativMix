from __future__ import annotations

import copy
import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from nativmix.utils.channel_order import normalize_channel_order

logger = logging.getLogger(__name__)

_DEFAULT_CHANNELS_COUNT = 5


def _coerce_channel_count(value: Any, fallback: int) -> int:
    """Return a non-negative channel count parsed from ``value``.

    Args:
        value: Candidate channel count from persisted profile data.
        fallback: Value to use when ``value`` is missing/invalid/negative.
    """
    try:
        count = int(value)
    except (TypeError, ValueError):
        return fallback
    return count if count >= 0 else fallback


def _resolve_channel_index(channel: dict[str, Any], fallback: int) -> int:
    """Return a usable non-negative channel index.

    Rules:
    - Parse ``channel["index"]`` as int when possible.
    - If missing/invalid/negative, fall back to the channel's list position.

    Falling back to list position keeps repair deterministic and avoids invalid
    negative indexes corrupting channel identity during reconciliation.
    """
    raw = channel.get("index", fallback)
    if isinstance(raw, int):
        idx = raw
    else:
        try:
            idx = int(raw)
        except (TypeError, ValueError):
            idx = fallback
    return idx if idx >= 0 else fallback


def _resolve_channel_volume(value: Any) -> float:
    """Return a safe channel volume float; invalid values fall back to 1.0."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 1.0


def _normalize_midi_cc(value: Any) -> int | None:
    """Return a valid MIDI CC (0-127), or None for malformed values."""
    if value is None:
        return None
    try:
        cc = int(value)
    except (TypeError, ValueError):
        return None
    return cc if 0 <= cc <= 127 else None


def _normalize_midi_channel(value: Any) -> int:
    """Return a protocol MIDI channel clamped to 0-15."""
    try:
        channel = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(15, channel))


def _normalize_channel_midi_fields(channel: dict[str, Any]) -> bool:
    """Normalize the single volume binding and independent mute binding."""
    before = {
        key: copy.deepcopy(channel.get(key))
        for key in (
            "midi_cc",
            "midi_channel",
            "midi_bindings",
            "midi_mute_cc",
            "midi_mute_channel",
        )
    }
    raw_bindings = channel.get("midi_bindings")
    has_binding_slot = isinstance(raw_bindings, list) and bool(raw_bindings) and isinstance(raw_bindings[0], dict)
    if has_binding_slot:
        first = raw_bindings[0]
        binding_cc = _normalize_midi_cc(first.get("cc"))
        legacy_cc = _normalize_midi_cc(channel.get("midi_cc"))
        if binding_cc is not None or legacy_cc is None:
            volume_cc = binding_cc
            volume_channel = _normalize_midi_channel(first.get("midi_channel", 0))
        else:
            volume_cc = legacy_cc
            volume_channel = _normalize_midi_channel(channel.get("midi_channel", 0))
    else:
        volume_cc = _normalize_midi_cc(channel.get("midi_cc"))
        volume_channel = _normalize_midi_channel(channel.get("midi_channel", 0))

    channel["midi_cc"] = volume_cc
    channel["midi_channel"] = volume_channel
    if has_binding_slot:
        channel["midi_bindings"] = [{"cc": volume_cc, "midi_channel": volume_channel}]
    else:
        channel.pop("midi_bindings", None)
    channel["midi_mute_cc"] = _normalize_midi_cc(channel.get("midi_mute_cc"))
    channel["midi_mute_channel"] = _normalize_midi_channel(channel.get("midi_mute_channel", 0))
    after = {key: channel.get(key) for key in before}
    return before != after


def _merge_channel_into(base: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Merge duplicate-channel data into *base* without dropping mappings.

    Precedence rules:
    - ``app_names`` are unioned in order to preserve all bindings.
    - Scalar identity/config fields only fill missing/empty values in *base*.
    - Boolean flags are merged with logical OR so enabled states survive repair.
    - ``volume`` prefers the first non-default value (anything other than 1.0).
    """
    base_names = list(base.get("app_names", []))
    seen_names = set(base_names)
    for name in incoming.get("app_names", []):
        if name not in seen_names:
            base_names.append(name)
            seen_names.add(name)
    base["app_names"] = base_names
    paused_names = list(base.get("routing_paused_apps", []))
    paused_seen = {str(name).lower() for name in paused_names}
    for name in incoming.get("routing_paused_apps", []):
        if str(name).lower() not in paused_seen:
            paused_names.append(name)
            paused_seen.add(str(name).lower())
    if paused_names:
        base["routing_paused_apps"] = paused_names

    for key in ("label", "hardware_id"):
        if base.get(key) in (None, "") and incoming.get(key) not in (None, ""):
            base[key] = incoming.get(key)

    if base.get("midi_cc") is None and incoming.get("midi_cc") is not None:
        base["midi_cc"] = incoming["midi_cc"]
        base["midi_channel"] = incoming["midi_channel"]
        if "midi_bindings" in incoming:
            base["midi_bindings"] = copy.deepcopy(incoming["midi_bindings"])
        else:
            base.pop("midi_bindings", None)
    if base.get("midi_mute_cc") is None and incoming.get("midi_mute_cc") is not None:
        base["midi_mute_cc"] = incoming["midi_mute_cc"]
        base["midi_mute_channel"] = incoming["midi_mute_channel"]

    if base.get("mode") in (None, "") and incoming.get("mode") not in (None, ""):
        base["mode"] = incoming.get("mode")

    if not bool(base.get("inverted", False)) and bool(incoming.get("inverted", False)):
        base["inverted"] = True
    if not bool(base.get("v_sink", False)) and bool(incoming.get("v_sink", False)):
        base["v_sink"] = True
    if not bool(base.get("is_midi", False)) and bool(incoming.get("is_midi", False)):
        base["is_midi"] = True

    base_vol = _resolve_channel_volume(base.get("volume", 1.0))
    incoming_vol = _resolve_channel_volume(incoming.get("volume", 1.0))
    if base_vol == 1.0 and incoming_vol != 1.0:
        base["volume"] = incoming_vol


def normalize_profile_channels(channels: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    """Return canonical channels plus ``repair_applied`` flag.

    Canonical form means:
    - exactly one channel per stable index identity;
    - channels sorted by index;
    - sequential ``index`` values from 0..N-1.

    When duplicates share the same index, their data is merged deterministically
    via :func:`_merge_channel_into` so compatible mappings are preserved.
    """
    by_index: dict[int, dict[str, Any]] = {}
    repaired = False
    for pos, raw in enumerate(channels):
        if not isinstance(raw, dict):
            raw = {}
            repaired = True
        ch = copy.deepcopy(raw)
        if _normalize_channel_midi_fields(ch):
            repaired = True
        idx = _resolve_channel_index(ch, fallback=pos)
        if ch.get("index") != idx:
            repaired = True

        existing = by_index.get(idx)
        if existing is None:
            ch["index"] = idx
            if not isinstance(ch.get("app_names", []), list):
                ch["app_names"] = []
                repaired = True
            paused_present = "routing_paused_apps" in ch
            paused = ch.get("routing_paused_apps", [])
            mapped_by_name = {str(name).lower(): name for name in ch["app_names"]}
            normalized_paused = []
            if isinstance(paused, list):
                normalized_paused = [
                    mapped_by_name[str(name).lower()]
                    for name in paused
                    if str(name).lower() in mapped_by_name
                    and str(name).lower() not in {"system master", "other apps"}
                ]
            if paused_present and paused != normalized_paused:
                repaired = True
            if normalized_paused:
                ch["routing_paused_apps"] = list(dict.fromkeys(normalized_paused))
            else:
                ch.pop("routing_paused_apps", None)
            by_index[idx] = ch
            continue

        repaired = True
        _merge_channel_into(existing, ch)
        _normalize_channel_midi_fields(existing)

    normalized: list[dict[str, Any]] = []
    for new_index, old_index in enumerate(sorted(by_index)):
        ch = by_index[old_index]
        if ch.get("index") != new_index:
            repaired = True
        ch["index"] = new_index
        normalized.append(ch)

    if len(channels) != len(normalized):
        repaired = True
    return normalized, repaired


def reconcile_profile_channels(
    channels: list[Any],
    *,
    expected_count: Any,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Return canonical profile channels repaired to ``expected_count``.

    Args:
        channels: Raw profile channel payload.
        expected_count: Canonical channel_count value from profile metadata.

    Returns:
        A tuple of:
            - repaired channels list with stable sequential indexes
            - canonical channel count used for reconciliation
            - bool flag indicating whether any repair was applied

    Notes:
        When ``expected_count`` is invalid/missing, fallback is ``len(normalized)``
        because no persisted canonical count is available at this layer. Higher-level
        apply/save paths still enforce anti-expansion guardrails when switching/saving.
    """
    normalized, repaired = normalize_profile_channels(channels)
    canonical_count = _coerce_channel_count(expected_count, len(normalized))

    if len(normalized) > canonical_count:
        normalized = normalized[:canonical_count]
        repaired = True
    elif len(normalized) < canonical_count:
        padded = default_channels(canonical_count)
        for idx, ch in enumerate(normalized):
            padded[idx] = ch
        normalized = padded
        repaired = True

    for idx, ch in enumerate(normalized):
        if ch.get("index") != idx:
            repaired = True
        ch["index"] = idx

    return normalized, canonical_count, repaired


def _next_profile_id(profiles_dir: Path) -> str:
    existing = {
        int(p.stem.split("-")[1])
        for p in profiles_dir.glob("profile-*.json")
        if p.stem.split("-")[1].isdigit()
    }
    n = 1
    while n in existing:
        n += 1
    return f"profile-{n}"


def default_channels(count: int) -> list[dict[str, Any]]:
    return [
        {
            "index": i,
            "label": None,
            "is_midi": False,
            "app_names": [],
            "midi_cc": None,
            "midi_mute_cc": None,
            "midi_channel": 0,
            "midi_mute_channel": 0,
            "inverted": False,
            "v_sink": False,
            "mode": "app",
            "hardware_id": None,
            "volume": 1.0,
        }
        for i in range(count)
    ]


class ProfileManager(QObject):
    """
    Manages NativMix profiles stored as individual JSON files.

    Each profile contains channel-level configuration (app assignments,
    MIDI CC, labels, fader positions). Global hardware settings remain
    in config.json and are NOT part of any profile.
    """

    profile_changed = pyqtSignal(str)    # profile_id — emitted after every switch
    profile_list_changed = pyqtSignal()  # emitted after create / rename / delete
    _routine_save_suspensions: dict[str, int] = {}

    def __init__(
        self,
        profiles_dir: Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if profiles_dir is None:
            from nativmix.utils.paths import get_config_dir
            profiles_dir = get_config_dir() / "profiles"
        self._dir = profiles_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._dir_key = str(self._dir.resolve())
        self._active_profile_id: str = ""
        self._direct_cc_map: dict[int, str] = {}
        self._rebuild_direct_cc_map()

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def active_profile_id(self) -> str:
        return self._active_profile_id

    def set_active_silently(self, profile_id: str) -> None:
        """Set the active profile ID without emitting profile_changed."""
        self._active_profile_id = profile_id

    @property
    def direct_cc_map(self) -> dict[int, str]:
        """Mapping of MIDI CC number → profile_id for direct-switch CCs."""
        return dict(self._direct_cc_map)

    @property
    def active_profile(self) -> dict:
        if not self._active_profile_id:
            raise RuntimeError("No active profile set")
        return self.load(self._active_profile_id)

    # ── File I/O ──────────────────────────────────────────────────────────

    def list_profiles(self) -> list[dict]:
        """Return all profiles sorted by numeric ID."""
        profiles = []
        for p in sorted(self._dir.glob("profile-*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                profiles.append({
                    "id": data.get("id", p.stem),
                    "name": data.get("name", p.stem),
                    "channel_count": data.get("channel_count", 0),
                })
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read profile %s: %s", p, exc)
        return profiles

    def load(self, profile_id: str) -> dict:
        """Load a profile dict from disk without activating it.

        Reconciles ``channel_count`` with the actual number of channels stored
        in the ``channels`` list.  When a mismatch is found the corrected value
        is written back to disk so subsequent calls see a consistent file and
        the warning is emitted exactly once per mismatch.
        """
        path = self._dir / f"{profile_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Profile not found: {profile_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        channels = data.get("channels", [])
        expected = data.get("channel_count", len(channels))
        canonical_channels, canonical_count, repair_applied = reconcile_profile_channels(
            channels,
            expected_count=expected,
        )
        count_mismatch = data.get("channel_count") != canonical_count
        needs_save = repair_applied or count_mismatch
        if repair_applied:
            logger.warning(
                "Profile %s: contamination repair applied (channels %d → %d, canonical_count=%d)",
                profile_id,
                len(channels),
                len(canonical_channels),
                canonical_count,
            )
        elif count_mismatch:
            logger.warning(
                "Profile %s: invalid channel_count=%r repaired to %d",
                profile_id,
                data.get("channel_count"),
                canonical_count,
            )
        data["channels"] = canonical_channels
        data["channel_count"] = canonical_count
        channel_ids = [int(channel["index"]) for channel in canonical_channels]
        raw_channel_order = data.get("channel_order")
        channel_order = normalize_channel_order(raw_channel_order, channel_ids)
        if raw_channel_order is not None and raw_channel_order != channel_order:
            data["channel_order"] = channel_order
            needs_save = True

        # Persist the correction so subsequent load() calls see a consistent
        # file and warnings are emitted only once.
        if needs_save:
            try:
                self._save_profile(data)
            except OSError as exc:
                logger.warning(
                    "Could not persist reconciled profile %s: %s",
                    profile_id, exc,
                )
        return data

    def _save_profile(self, profile: dict, *, allow_resize: bool = False) -> None:
        # Guard: never persist a channel list that exceeds the profile's own
        # canonical template length.  Runtime pollution (e.g. stale channels
        # left over from a previous larger profile) must not bleed into a
        # saved file.  Pass allow_resize=True only when the channel_count is
        # being deliberately changed (e.g. the user adds a MIDI channel).
        channels = profile.get("channels", [])
        channel_count = _coerce_channel_count(profile.get("channel_count"), len(channels))
        if not allow_resize and len(channels) > channel_count:
            logger.error(
                "_save_profile %s: refusing to persist %d channels beyond "
                "canonical template (%d) – truncating to prevent profile "
                "inflation.  Pass allow_resize=True to allow intentional resize.",
                profile.get("id"),
                len(channels),
                channel_count,
            )
            profile = {**profile, "channels": channels[:channel_count]}

        path = self._dir / f"{profile['id']}.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.error("Failed to write profile %s: %s", profile.get("id"), exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _rebuild_direct_cc_map(self) -> None:
        """Rebuild the in-memory CC→profile_id map from all profile files."""
        cc_map: dict[int, str] = {}
        for p in self._dir.glob("profile-*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                cc = data.get("midi_switch_cc")
                if cc is not None:
                    cc_map[int(cc)] = data.get("id", p.stem)
            except (json.JSONDecodeError, OSError, ValueError) as exc:
                logger.debug("Could not read profile %s for CC map: %s", p, exc)
        self._direct_cc_map = cc_map

    def save_profile(self, profile: dict, *, allow_resize: bool = False) -> None:
        """Write a profile dict to disk. The profile must have a valid 'id' field.

        Args:
            profile:   Profile dict to persist.
            allow_resize: When *True*, allow the channel list to exceed the stored
                          ``channel_count`` (e.g. user explicitly added channels).
                          Leave *False* (the default) for all routine saves so that
                          inflated runtime channels are never written back to disk.
        """
        self._save_profile(profile, allow_resize=allow_resize)
        self._rebuild_direct_cc_map()

    @contextmanager
    def suspend_routine_save_current(self):
        """Temporarily ignore routine ``save_current()`` calls during structural mutations."""
        current_depth = self._routine_save_suspensions.get(self._dir_key, 0)
        self._routine_save_suspensions[self._dir_key] = current_depth + 1
        try:
            yield
        finally:
            remaining_depth = max(
                0,
                self._routine_save_suspensions.get(self._dir_key, 0) - 1,
            )
            if remaining_depth:
                self._routine_save_suspensions[self._dir_key] = remaining_depth
            else:
                self._routine_save_suspensions.pop(self._dir_key, None)

    # ── CRUD ──────────────────────────────────────────────────────────────

    def create(
        self,
        name: str,
        channel_count: int = _DEFAULT_CHANNELS_COUNT,
        channels: list[dict] | None = None,
        channel_order: list[int] | None = None,
    ) -> str:
        """Create a new profile and return its ID.

        If *channels* is provided, it is used as-is; otherwise blank defaults
        are generated from *channel_count*.
        """
        new_id = _next_profile_id(self._dir)
        profile = {
            "id": new_id,
            "name": name,
            "channel_count": channel_count,
            "restore_fader_positions": False,
            "midi_switch_cc": None,
            "channels": channels if channels is not None else default_channels(channel_count),
            "channel_order": normalize_channel_order(channel_order, range(channel_count)),
        }
        self._save_profile(profile)
        self._rebuild_direct_cc_map()
        logger.debug("Profile created: %s (%s)", new_id, name)
        self.profile_list_changed.emit()
        return new_id

    def rename(self, profile_id: str, name: str) -> None:
        """Rename a profile (updates name field, ID stays the same)."""
        profile = self.load(profile_id)
        profile["name"] = name
        self._save_profile(profile)
        logger.debug("Profile renamed: %s → %s", profile_id, name)
        self.profile_list_changed.emit()

    def delete(self, profile_id: str) -> None:
        """Delete a profile. Raises ValueError if it is the last one."""
        if len(self.list_profiles()) <= 1:
            raise ValueError("Cannot delete the last profile")
        path = self._dir / f"{profile_id}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            logger.debug("Profile file already gone: %s", path)
        self._rebuild_direct_cc_map()
        logger.debug("Profile deleted: %s", profile_id)
        self.profile_list_changed.emit()

    def save_current(
        self,
        channels: list[dict],
        *,
        allow_resize: bool = False,
        target_channel_count: int | None = None,
    ) -> None:
        """Persist the current channel state back to the active profile file.

        When ``allow_resize`` is used together with ``target_channel_count``, the
        resize is anchored to the active profile's stored canonical template
        length instead of blindly trusting the current runtime list length. This
        preserves existing channels while preventing stale tail entries from a
        previously larger runtime profile from being persisted as part of an
        intentional add/remove operation.
        """
        if not self._active_profile_id:
            return
        if self._routine_save_suspensions.get(self._dir_key, 0) and not allow_resize:
            logger.debug(
                "save_current %s: skipped routine save during guarded profile mutation",
                self._active_profile_id,
            )
            return
        profile = self.load(self._active_profile_id)
        normalized_current, normalized_repair = normalize_profile_channels(channels)
        stored_channels, stored_count, stored_repair = reconcile_profile_channels(
            profile.get("channels", []),
            expected_count=profile.get("channel_count"),
        )
        current_channel_count = len(normalized_current)

        # Non-resize save invariant: if the runtime has *fewer* channels than
        # the stored profile, writing the partial runtime list would overwrite
        # the stored prefix with stale/blank data while keeping the stored tail
        # intact — a pattern that silently corrupts well-formed profiles (see
        # the "14 → 31 canonicalization" regression).  Reject the save so that
        # the stored profile is never overwritten by a smaller runtime snapshot.
        # Intentional channel-count changes must go through allow_resize=True.
        if not allow_resize and current_channel_count < stored_count:
            logger.warning(
                "save_current %s: refusing non-resize save – runtime has %d "
                "channel(s) but stored profile declares %d; this would "
                "overwrite stored channels with a partial/stale runtime "
                "snapshot.  Use allow_resize=True for intentional resizes.",
                self._active_profile_id,
                current_channel_count,
                stored_count,
            )
            return

        if allow_resize:
            resolved_target_count = (
                _coerce_channel_count(target_channel_count, current_channel_count)
                if target_channel_count is not None
                else current_channel_count
            )
        else:
            resolved_target_count = stored_count

        resize_source = normalized_current
        if not allow_resize or target_channel_count is not None:
            resize_source, _, _ = reconcile_profile_channels(
                copy.deepcopy(stored_channels),
                expected_count=resolved_target_count,
            )
            current_prefix_len = min(len(normalized_current), resolved_target_count)
            if allow_resize and target_channel_count is not None:
                current_prefix_len = min(current_prefix_len, stored_count)
            for idx in range(current_prefix_len):
                resize_source[idx] = normalized_current[idx]

        canonical_channels, canonical_count, count_repair = reconcile_profile_channels(
            resize_source,
            expected_count=resolved_target_count,
        )
        if allow_resize and target_channel_count is not None and resolved_target_count > stored_count:
            for idx in range(stored_count, min(resolved_target_count, current_channel_count, len(canonical_channels))):
                canonical_channels[idx]["is_midi"] = bool(normalized_current[idx].get("is_midi", False))
        profile["channels"] = canonical_channels
        profile["channel_count"] = canonical_count
        profile["channel_order"] = normalize_channel_order(
            profile.get("channel_order"),
            (int(channel["index"]) for channel in canonical_channels),
        )
        repair_applied = (
            normalized_repair
            or stored_repair
            or count_repair
            or canonical_count != stored_count
        )
        if repair_applied or len(channels) != canonical_count:
            logger.info(
                "save_current %s: canonicalized channels %d → %d",
                self._active_profile_id,
                len(channels),
                canonical_count,
            )
        self._save_profile(profile, allow_resize=allow_resize)
        logger.debug("Profile saved: %s", self._active_profile_id)

    def get_channel_order(self, profile_id: str | None = None) -> list[int]:
        """Return the normalized visual channel order for a profile."""
        target = profile_id or self._active_profile_id
        if not target:
            return []
        profile = self.load(target)
        channel_ids = [int(channel["index"]) for channel in profile.get("channels", [])]
        return normalize_channel_order(profile.get("channel_order"), channel_ids)

    def set_channel_order(self, order: list[int], profile_id: str | None = None) -> list[int]:
        """Normalize and persist visual order without changing channel identities."""
        target = profile_id or self._active_profile_id
        if not target:
            return []
        profile = self.load(target)
        channel_ids = [int(channel["index"]) for channel in profile.get("channels", [])]
        normalized = normalize_channel_order(order, channel_ids)
        profile["channel_order"] = normalized
        self._save_profile(profile)
        return normalized

    # ── Switching ─────────────────────────────────────────────────────────

    def switch(self, profile_id: str) -> None:
        """Activate a profile. Emits profile_changed. No-op if already active."""
        if profile_id == self._active_profile_id:
            logger.info("Already on profile %s — no switch needed", profile_id)
            return
        profile = self.load(profile_id)
        self._active_profile_id = profile_id
        logger.info("Profile switched to: %s (%s)", profile_id, profile.get("name"))
        self.profile_changed.emit(profile_id)

    def switch_next(self) -> None:
        """Switch to the next profile (wraps around). No-op if only one profile."""
        profiles = self.list_profiles()
        if not profiles:
            raise RuntimeError("No profiles available")
        if len(profiles) == 1:
            logger.info("Only one profile available — cannot switch to next")
            return
        ids = [p["id"] for p in profiles]
        try:
            idx = ids.index(self._active_profile_id)
        except ValueError:
            idx = -1
        self.switch(ids[(idx + 1) % len(ids)])

    def switch_prev(self) -> None:
        """Switch to the previous profile (wraps around). No-op if only one profile."""
        profiles = self.list_profiles()
        if not profiles:
            raise RuntimeError("No profiles available")
        if len(profiles) == 1:
            logger.info("Only one profile available — cannot switch to previous")
            return
        ids = [p["id"] for p in profiles]
        try:
            idx = ids.index(self._active_profile_id)
        except ValueError:
            idx = 0
        self.switch(ids[(idx - 1) % len(ids)])

    # ── Auto-create ────────────────────────────────────────────────────────

    def ensure_profile_for_hw(self, hw_channel_count: int) -> None:
        """
        Ensure an active profile compatible with hw_channel_count exists.

        If hw_channel_count is smaller than the active profile's channel_count,
        auto-creates a new profile sized to hw_channel_count and activates it.
        Called on startup and port change only, never on manual switch.
        """
        if not self._active_profile_id:
            return

        try:
            active = self.load(self._active_profile_id)
        except FileNotFoundError:
            return

        if hw_channel_count < active.get("channel_count", 0):
            names = {p["name"] for p in self.list_profiles()}
            n = len(names) + 1
            candidate = f"Profile {n}"
            while candidate in names:
                n += 1
                candidate = f"Profile {n}"
            new_id = self.create(candidate, channel_count=hw_channel_count)
            logger.info(
                "Hardware has %d channels, active profile needs %d — auto-created %s",
                hw_channel_count, active["channel_count"], new_id,
            )
            self.switch(new_id)
