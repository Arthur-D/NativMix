"""Pure layout and A/B assignment eligibility helpers for the crossfader bar."""

from __future__ import annotations


def crossfader_bar_width(num_channels: int, strip_width: int) -> int:
    n = max(0, int(num_channels))
    w = max(1, int(strip_width))
    return max(2, min(6, n if n > 0 else 2)) * w


def channel_strip_block_width(num_channels: int, strip_width: int, spacing: int = 6) -> int:
    """Total width of the visible strip row including inter-strip spacing."""
    n = max(0, int(num_channels))
    if n == 0:
        return 0
    w = max(1, int(strip_width))
    s = max(0, int(spacing))
    return n * w + (n - 1) * s


def scroll_height_needs_hbar(content_width: int, viewport_width: int) -> bool:
    """True when the channel row is wider than the scroll viewport (reserve h-bar)."""
    vp = int(viewport_width)
    if vp <= 0:
        return False
    return int(content_width) > vp


def channel_scroll_content_height(
    strip_size_hints: list[int],
    *,
    frame: int = 0,
    hbar: int = 0,
    needs_hbar: bool = False,
) -> int:
    """Scroll-area height from strip *sizeHints* only (never stretched widget height).

    Channel strips use a bottom stretch; after ``setWidgetResizable(True)`` their
    laid-out ``height()`` tracks the previous FixedHeight and would ratchet.
    """
    if not strip_size_hints:
        return 1
    h = max(int(x) for x in strip_size_hints)
    h += max(0, int(frame)) * 2
    if needs_hbar:
        h += max(0, int(hbar))
    return max(1, h)


def edit_midi_needed_height(
    content_hint: int,
    *,
    frame_chrome: int = 0,
    floor: int = 420,
) -> int:
    """Minimum window height for Edit MIDI — content-based, not current geometry."""
    return max(int(floor), int(content_hint) + max(0, int(frame_chrome)))


def window_min_width_for_strips(
    block_width: int,
    *,
    horizontal_chrome: int = 16,
    floor: int = 400,
) -> int:
    """Minimum window width: full fader row + side chrome, never below *floor*."""
    return max(int(floor), int(block_width) + max(0, int(horizontal_chrome)))


def channel_eligible_for_side(
    side: str,
    channel_index: int,
    *,
    control_index: int | None,
    sides: dict[int, str],
) -> bool:
    if control_index is not None and channel_index == control_index:
        return False
    other = "b" if side == "a" else "a"
    if (sides.get(channel_index, "none") or "none").lower() == other:
        return False
    return True


def format_side_label_names(names: list[str]) -> str:
    parts = [n.strip() for n in names if n and n.strip()]
    return ", ".join(parts)
