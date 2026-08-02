from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

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

    for key in ("label", "midi_cc", "midi_mute_cc", "hardware_id"):
        if base.get(key) in (None, "") and incoming.get(key) not in (None, ""):
            base[key] = incoming.get(key)

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
        idx = _resolve_channel_index(ch, fallback=pos)
        if ch.get("index") != idx:
            repaired = True

        existing = by_index.get(idx)
        if existing is None:
            ch["index"] = idx
            if not isinstance(ch.get("app_names", []), list):
                ch["app_names"] = []
                repaired = True
            by_index[idx] = ch
            continue

        repaired = True
        _merge_channel_into(existing, ch)

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

    # ── CRUD ──────────────────────────────────────────────────────────────

    def create(
        self,
        name: str,
        channel_count: int = _DEFAULT_CHANNELS_COUNT,
        channels: list[dict] | None = None,
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
        profile = self.load(self._active_profile_id)
        normalized_current, normalized_repair = normalize_profile_channels(channels)
        stored_count = _coerce_channel_count(
            profile.get("channel_count"),
            len(normalized_current),
        )
        current_channel_count = len(normalized_current)
        if allow_resize:
            resolved_target_count = (
                _coerce_channel_count(target_channel_count, current_channel_count)
                if target_channel_count is not None
                else current_channel_count
            )
        else:
            resolved_target_count = min(stored_count, current_channel_count)

        resize_source = normalized_current
        if allow_resize and target_channel_count is not None:
            resize_source = copy.deepcopy(profile.get("channels", []))[: min(stored_count, resolved_target_count)]
            current_prefix_len = min(len(normalized_current), stored_count, resolved_target_count)
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
        repair_applied = normalized_repair or count_repair or canonical_count != stored_count
        if repair_applied or len(channels) != canonical_count:
            logger.info(
                "save_current %s: canonicalized channels %d → %d",
                self._active_profile_id,
                len(channels),
                canonical_count,
            )
        self._save_profile(profile, allow_resize=allow_resize)
        logger.debug("Profile saved: %s", self._active_profile_id)

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
