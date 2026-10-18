"""Validation for profile-owned channel background colors."""

import re


def validate_channel_color(value: object) -> str | None:
    """Accept an opaque RGB hex color or the default theme (None)."""
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", value) is None:
        raise ValueError("channel background color must be #RRGGBB or null")
    return value.lower()
