"""Unit tests for crossfader bar width and A/B menu eligibility helpers."""

from nativmix.gui.crossfader_layout import (
    channel_eligible_for_side,
    channel_scroll_content_height,
    channel_strip_block_width,
    crossfader_bar_width,
    edit_midi_needed_height,
    format_side_label_names,
    scroll_height_needs_hbar,
    strips_in_row_for_mode,
    window_min_width_for_strips,
)


def test_bar_width_clamps_2_to_6() -> None:
    assert crossfader_bar_width(1, 70) == 140  # min 2
    assert crossfader_bar_width(3, 70) == 210
    assert crossfader_bar_width(6, 70) == 420
    assert crossfader_bar_width(10, 70) == 420  # max 6


def test_bar_width_empty_rebuild_uses_two_strips() -> None:
    assert crossfader_bar_width(0, 70) == 140


def test_channel_strip_block_width_includes_spacing() -> None:
    assert channel_strip_block_width(0, 70, 6) == 0
    assert channel_strip_block_width(1, 70, 6) == 70
    assert channel_strip_block_width(3, 70, 6) == 70 * 3 + 6 * 2
    assert channel_strip_block_width(7, 85, 6) == 85 * 7 + 6 * 6


def test_scroll_height_needs_hbar_when_content_wider() -> None:
    assert scroll_height_needs_hbar(content_width=500, viewport_width=400) is True
    assert scroll_height_needs_hbar(content_width=400, viewport_width=400) is False
    assert scroll_height_needs_hbar(content_width=100, viewport_width=0) is False


def test_channel_scroll_content_height_uses_hints_not_stretched_height() -> None:
    """Laid-out strip height can include stretch; fitting must follow sizeHint only."""
    assert channel_scroll_content_height([400, 420], frame=2, hbar=16, needs_hbar=False) == 424
    assert channel_scroll_content_height([400, 420], frame=2, hbar=16, needs_hbar=True) == 440
    # Empty → minimum 1
    assert channel_scroll_content_height([], frame=0, hbar=0, needs_hbar=False) == 1


def test_edit_midi_needed_height_does_not_follow_current_window() -> None:
    """Grow target comes from content hint + chrome, not the already-tall window."""
    assert edit_midi_needed_height(content_hint=500, frame_chrome=40, floor=420) == 540
    assert edit_midi_needed_height(content_hint=300, frame_chrome=40, floor=420) == 420
    # Passing a large "current height" must not inflate the result.
    assert edit_midi_needed_height(content_hint=500, frame_chrome=40, floor=420) == 540


def test_window_min_width_follows_total_fader_block() -> None:
    """Window must not shrink below the full strip row (+ chrome)."""
    block = channel_strip_block_width(4, 70, 6)  # 298
    # Optional floor still works for callers that want a hard minimum.
    assert window_min_width_for_strips(block, horizontal_chrome=16, floor=400) == 400
    # MainWindow uses floor=0 so a short strip row does not leave a right-only gap.
    assert window_min_width_for_strips(block, horizontal_chrome=16, floor=0) == 314
    block7 = channel_strip_block_width(7, 70, 6)  # 526
    assert window_min_width_for_strips(block7, horizontal_chrome=16, floor=400) == 542


def test_strips_in_row_for_mode_ignores_hidden_usb_in_midi_only() -> None:
    """midi_only must not inflate min-width with hidden USB strips (hybrid count)."""
    # 4 USB + 2 MIDI still in the widget list
    flags = [False, False, False, False, True, True]
    assert strips_in_row_for_mode("midi_only", flags) == 2
    assert strips_in_row_for_mode("usb", flags) == 4
    assert strips_in_row_for_mode("hybrid", flags) == 6


def test_eligibility_blocks_control_and_opposite() -> None:
    sides = {0: "a", 1: "b", 2: "none"}
    assert channel_eligible_for_side("a", 2, control_index=3, sides=sides) is True
    assert channel_eligible_for_side("a", 3, control_index=3, sides=sides) is False
    assert channel_eligible_for_side("a", 1, control_index=None, sides=sides) is False  # on B
    assert channel_eligible_for_side("b", 0, control_index=None, sides=sides) is False  # on A
    assert channel_eligible_for_side("a", 0, control_index=None, sides=sides) is True  # already A


def test_format_side_label_names_empty():
    assert format_side_label_names([]) == ""


def test_format_side_label_names_joins():
    assert format_side_label_names(["CH 1"]) == "CH 1"
    assert format_side_label_names(["Game", "Voice"]) == "Game, Voice"


def test_format_side_label_names_skips_blanks():
    assert format_side_label_names(["", "CH 2", "  "]) == "CH 2"
