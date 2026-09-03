"""Stable, privacy-bounded receiver target inventory."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from nativmix.remote_sync.schema import TargetInventoryItem

_SPECIAL_TARGETS = (
    ("pseudo:system-master", "System Master"),
    ("pseudo:other-apps", "Other Apps"),
)


def _stable_key(kind: str, identity: str) -> str:
    normalized = identity.strip().casefold().encode("utf-8")
    return f"{kind}:{hashlib.sha256(normalized).hexdigest()[:24]}"


@dataclass(frozen=True)
class _Target:
    item: TargetInventoryItem
    local_value: str
    target_type: str


class ReceiverTargetInventory:
    """Cache safe wire items while retaining raw backend IDs only in memory."""

    def __init__(self, config_manager: Any, backend: Any) -> None:
        self._config = config_manager
        self._backend = backend
        self._targets: dict[str, _Target] = {}
        self._hardware_keys: dict[str, str] = {}
        self.refresh()

    def __call__(self) -> list[TargetInventoryItem]:
        return [self._targets[key].item for key in sorted(self._targets)]

    def refresh(self) -> bool:
        targets: dict[str, _Target] = {}
        hardware_keys: dict[str, str] = {}
        for key, label in _SPECIAL_TARGETS:
            targets[key] = _Target(TargetInventoryItem(key, label, "output", True), label, "pseudo")

        active_names: dict[str, str] = {}
        get_active_streams = getattr(self._backend, "get_active_streams", None)
        if callable(get_active_streams):
            for stream in get_active_streams():
                name = str(getattr(stream, "app_name", "")).strip()
                if name and name.isprintable():
                    active_names.setdefault(name.casefold(), name)

        configured_names = {
            str(name).casefold(): str(name)
            for channel in self._config.all_channels()
            for name in channel.get("app_names", [])
            if str(name).casefold() not in {"system master", "other apps"}
        }
        for folded, label in {**configured_names, **active_names}.items():
            key = _stable_key("app", folded)
            targets[key] = _Target(
                TargetInventoryItem(key, label, "output", folded in active_names),
                label,
                "app",
            )

        configured_hardware = {
            str(channel.get("hardware_id"))
            for channel in self._config.all_channels()
            if channel.get("hardware_id")
        }
        for raw_id in configured_hardware:
            key = _stable_key("device", raw_id)
            previous = self._targets.get(key)
            label = previous.item.label if previous is not None else "Unavailable device"
            targets[key] = _Target(
                TargetInventoryItem(key, label, "output", False),
                raw_id,
                "device",
            )
            hardware_keys[raw_id] = key

        get_real_sinks = getattr(self._backend, "get_real_sinks", None)
        if callable(get_real_sinks):
            for description, raw_id in get_real_sinks():
                label = str(description).strip()
                local_value = str(raw_id)
                if not label or not label.isprintable() or not local_value:
                    continue
                key = _stable_key("device", local_value)
                targets[key] = _Target(
                    TargetInventoryItem(key, label, "output", True),
                    local_value,
                    "device",
                )
                hardware_keys[local_value] = key

        changed = targets != self._targets
        self._targets = targets
        self._hardware_keys = hardware_keys
        return changed

    def resolve_mapping_keys(self, keys: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for key in keys:
            target = self._targets.get(key)
            if target is None or target.target_type not in {"app", "pseudo"}:
                raise KeyError(key)
            folded = target.local_value.casefold()
            if folded not in seen:
                result.append(target.local_value)
                seen.add(folded)
        return result

    def resolve_hardware_key(self, key: str) -> str:
        target = self._targets.get(key)
        if target is None or target.target_type != "device":
            raise KeyError(key)
        return target.local_value

    def key_for_hardware_value(self, value: str) -> str:
        return self._hardware_keys.get(value, _stable_key("device", value))


__all__ = ["ReceiverTargetInventory"]
