from __future__ import annotations

import json
import logging
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

_DEFAULT_CHANNELS_COUNT = 5


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


def _default_channels(count: int) -> list[dict]:
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

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def active_profile_id(self) -> str:
        return self._active_profile_id

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
        """Load a profile dict from disk without activating it."""
        path = self._dir / f"{profile_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Profile not found: {profile_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_profile(self, profile: dict) -> None:
        path = self._dir / f"{profile['id']}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        tmp.replace(path)

    # ── CRUD ──────────────────────────────────────────────────────────────

    def create(self, name: str, channel_count: int = _DEFAULT_CHANNELS_COUNT) -> str:
        """Create a new profile with default channels and return its ID."""
        new_id = _next_profile_id(self._dir)
        profile = {
            "id": new_id,
            "name": name,
            "channel_count": channel_count,
            "restore_fader_positions": False,
            "midi_switch_cc": None,
            "channels": _default_channels(channel_count),
        }
        self._save_profile(profile)
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
            pass
        logger.debug("Profile deleted: %s", profile_id)
        self.profile_list_changed.emit()

    def save_current(self, channels: list[dict]) -> None:
        """Persist the current channel state back to the active profile file."""
        if not self._active_profile_id:
            return
        profile = self.load(self._active_profile_id)
        profile["channels"] = channels
        self._save_profile(profile)
        logger.debug("Profile saved: %s", self._active_profile_id)

    # ── Switching ─────────────────────────────────────────────────────────

    def switch(self, profile_id: str) -> dict:
        """Activate a profile and return its dict. Emits profile_changed."""
        profile = self.load(profile_id)
        self._active_profile_id = profile_id
        logger.info("Profile switched to: %s (%s)", profile_id, profile.get("name"))
        self.profile_changed.emit(profile_id)
        return profile

    def switch_next(self) -> dict:
        """Switch to the next profile (wraps around)."""
        profiles = self.list_profiles()
        if not profiles:
            raise RuntimeError("No profiles available")
        ids = [p["id"] for p in profiles]
        try:
            idx = ids.index(self._active_profile_id)
        except ValueError:
            idx = -1
        return self.switch(ids[(idx + 1) % len(ids)])

    def switch_prev(self) -> dict:
        """Switch to the previous profile (wraps around)."""
        profiles = self.list_profiles()
        if not profiles:
            raise RuntimeError("No profiles available")
        ids = [p["id"] for p in profiles]
        try:
            idx = ids.index(self._active_profile_id)
        except ValueError:
            idx = 0
        return self.switch(ids[(idx - 1) % len(ids)])

    # ── Auto-create ────────────────────────────────────────────────────────

    def ensure_profile_for_hw(self, hw_channel_count: int) -> str:
        """
        Return active profile ID if hw can serve it, else auto-create a new profile.

        'Can serve' means hw_channel_count >= active profile's channel_count.
        Called on startup and port change only, never on manual switch.
        """
        if not self._active_profile_id:
            return self._active_profile_id

        try:
            active = self.load(self._active_profile_id)
        except FileNotFoundError:
            return self._active_profile_id

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
            return new_id
        return self._active_profile_id
