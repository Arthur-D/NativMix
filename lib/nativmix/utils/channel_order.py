"""Normalization helpers for per-profile mixer strip order."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def normalize_channel_order(order: Any, channel_ids: Iterable[int]) -> list[int]:
    """Return a complete, duplicate-free order containing only current channel IDs."""
    valid_ids = [int(channel_id) for channel_id in channel_ids]
    valid = set(valid_ids)
    normalized: list[int] = []
    seen: set[int] = set()

    if isinstance(order, list):
        for raw_id in order:
            try:
                channel_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if channel_id in valid and channel_id not in seen:
                normalized.append(channel_id)
                seen.add(channel_id)

    normalized.extend(channel_id for channel_id in valid_ids if channel_id not in seen)
    return normalized


def order_after_remove(order: Any, removed_id: int) -> list[int]:
    """Remove a channel identity and shift later sequential identities down."""
    normalized: list[int] = []
    if isinstance(order, list):
        for raw_id in order:
            try:
                channel_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if channel_id == removed_id:
                continue
            normalized.append(channel_id - 1 if channel_id > removed_id else channel_id)
    return normalized
