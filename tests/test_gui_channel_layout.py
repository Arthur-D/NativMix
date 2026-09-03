"""GUI geometry regressions for crowded mixer channel layouts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PyQt6.QtCore import QPoint, QRect, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QApplication, QStyle, QStyleFactory, QStyleOptionToolButton

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import make_profile, write_profile

from nativmix.audio.base import AudioBackendBase
from nativmix.gui import main_window, settings_panel
from nativmix.gui.main_window import ChannelWidget, MainWindow, _AppRow
from nativmix.utils.config_manager import ConfigManager
from nativmix.utils.profile_manager import ProfileManager


class _LayoutBackend(AudioBackendBase):
    other_apps_changed = pyqtSignal(list)
    unresolved_targets_changed = pyqtSignal(set)
    status_changed = pyqtSignal(str, str)
    capability_changed = pyqtSignal(str, bool)

    gain_control_supported = True
    v_sink_supported = True
    v_sink_capability_reason = ""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def get_real_sinks(self) -> list:
        return []

    def get_real_sources(self) -> list:
        return []

    def get_active_streams(self) -> list:
        return []

    def get_unresolved_targets(self) -> set:
        return set()

    def get_default_sink_name(self) -> None:
        return None


def _make_midi_config(tmp_config_path, tmp_profiles_dir, channel_count: int) -> ConfigManager:
    channels = []
    for index in range(channel_count):
        channel = make_profile(channel_count=channel_count)["channels"][index]
        channel.update(
            {
                "is_midi": True,
                "midi_cc": 127,
                "midi_channel": 15,
                "midi_mute_cc": 127,
                "midi_mute_channel": 15,
                "app_names": ["System Master"] if index == 0 else [],
            }
        )
        channels.append(channel)
    profile = make_profile(channel_count=channel_count, channels=channels)
    write_profile(tmp_profiles_dir, profile)
    tmp_config_path.write_text(
        json.dumps(
            {
                "version": 7,
                "active_profile": profile["id"],
                "hardware": {
                    "num_channels": 0,
                    "input_mode": "midi_only",
                    "midi_channel_count": channel_count,
                },
                "settings": {
                    "compact_mode": False,
                    "show_invert_option": False,
                    "transparency": False,
                    "stay_open": True,
                },
            }
        )
    )
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    config.apply_profile(profile)
    return config


@pytest.fixture
def layout_window(tmp_config_path, tmp_profiles_dir, tmp_path, monkeypatch, qtbot):
    config = _make_midi_config(tmp_config_path, tmp_profiles_dir, 18)
    monkeypatch.setattr(settings_panel, "update_checks_supported", lambda: True)
    monkeypatch.setattr(settings_panel, "_real_ports", lambda: [])
    monkeypatch.setattr(
        main_window,
        "QSettings",
        lambda *_args: QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat),
    )
    window = MainWindow(config=config, backend=_LayoutBackend())
    qtbot.addWidget(window)
    window.finalize_ui()
    window.resize(1600, 549)
    window.show()
    window._toggle_settings_btn.setChecked(True)
    window._edit_midi_btn.setChecked(True)
    qtbot.wait(1)
    return window


def test_channel_width_is_dense_and_honors_native_control_hints(
    tmp_config_path,
    tmp_profiles_dir,
    qtbot,
):
    config = _make_midi_config(tmp_config_path, tmp_profiles_dir, 1)
    channel = ChannelWidget(0, config, _LayoutBackend(), is_midi=True)
    qtbot.addWidget(channel)
    channel.set_edit_mode(True)
    channel.show()
    qtbot.wait(1)

    font_relative_cap = channel.fontMetrics().horizontalAdvance("MMMMMMMMMM")
    assert channel.minimumWidth() <= font_relative_cap
    assert channel.minimumWidth() >= channel._learn_btn.minimumSizeHint().width()
    assert channel.minimumWidth() >= channel._mute_learn_btn.minimumSizeHint().width()
    assert channel.minimumWidth() >= channel._remove_midi_btn.minimumSizeHint().width()
    assert channel.width() == channel.minimumWidth() == channel.maximumWidth()


def test_eighteen_channels_scroll_horizontally_without_compressing(layout_window, qtbot):
    window = layout_window
    qtbot.waitUntil(lambda: window._channel_scroll.horizontalScrollBar().maximum() > 0)

    assert window._channel_container.minimumWidth() > window._channel_scroll.viewport().width()
    assert all(channel.width() >= channel.minimumWidth() for channel in window._channels)
    assert window._channel_scroll.horizontalScrollBar().maximum() > 0
    strip_pitch = window._channels[0].width() + window._ch_layout.spacing()
    assert window._channel_scroll.viewport().width() / strip_pitch >= 14


def test_midi_controls_fit_and_do_not_overlap_at_minimum_width(
    tmp_config_path,
    tmp_profiles_dir,
    qtbot,
):
    config = _make_midi_config(tmp_config_path, tmp_profiles_dir, 1)
    channel = ChannelWidget(0, config, _LayoutBackend(), is_midi=True)
    qtbot.addWidget(channel)
    channel.set_edit_mode(True)
    channel.resize(channel.minimumWidth(), channel.minimumSizeHint().height())
    channel.show()
    qtbot.wait(1)

    buttons = (channel._learn_btn, channel._mute_learn_btn, channel._remove_midi_btn)
    assert channel._learn_btn.text() == "16:127"
    assert channel._mute_learn_btn.text() == "16:127"
    assert "MIDI channel 16, CC 127" in channel._learn_btn.toolTip()
    assert "MIDI channel 16, CC 127" in channel._learn_btn.accessibleName()
    for button in buttons:
        assert button.width() >= button.minimumSizeHint().width()
        assert channel.contentsRect().contains(button.geometry())
    assert not buttons[0].geometry().intersects(buttons[1].geometry())
    assert not buttons[1].geometry().intersects(buttons[2].geometry())

    for button in (channel._learn_btn, channel._mute_learn_btn):
        option = QStyleOptionToolButton()
        button.initStyleOption(option)
        main_rect = button.style().subControlRect(
            QStyle.ComplexControl.CC_ToolButton,
            option,
            QStyle.SubControl.SC_ToolButton,
            button,
        )
        arrow_rect = button.style().subControlRect(
            QStyle.ComplexControl.CC_ToolButton,
            option,
            QStyle.SubControl.SC_ToolButtonMenu,
            button,
        )
        icon_rect = QRect(
            main_rect.left() + 2,
            main_rect.center().y() - button.iconSize().height() // 2,
            button.iconSize().width(),
            button.iconSize().height(),
        )
        text_rect = QRect(
            main_rect.left() + button.iconSize().width() + 6,
            main_rect.top(),
            arrow_rect.left() - main_rect.left() - button.iconSize().width() - 8,
            main_rect.height(),
        )
        assert not icon_rect.intersects(text_rect)
        assert not icon_rect.intersects(arrow_rect)
        assert not text_rect.intersects(arrow_rect)
        assert button.fontMetrics().horizontalAdvance(button.text()) <= text_rect.width()


def test_compact_edit_toggles_restore_valid_width_constraints(layout_window, qtbot):
    window = layout_window
    channel = window._channels[0]
    normal_bounds = (channel.minimumWidth(), channel.maximumWidth())

    window._compact_btn.setChecked(True)
    window._channel_scroll.ensureWidgetVisible(window._channels[0]._sep)
    qtbot.wait(1)
    assert channel.minimumWidth() == channel.maximumWidth()
    assert not channel._learn_btn.isVisible()

    window._compact_btn.setChecked(False)
    window._edit_midi_btn.setChecked(True)
    qtbot.wait(1)
    assert (channel.minimumWidth(), channel.maximumWidth()) == normal_bounds
    assert channel._learn_btn.isVisible()
    assert channel.minimumWidth() <= channel.width() <= channel.maximumWidth()


@pytest.mark.parametrize("style_name", ["Fusion", "Breeze"])
def test_dense_controls_fit_available_styles(
    style_name,
    tmp_config_path,
    tmp_profiles_dir,
    qtbot,
):
    if style_name not in QStyleFactory.keys():
        pytest.skip(f"{style_name} style is unavailable")
    app = QApplication.instance()
    previous_style = app.style().objectName()
    app.setStyle(style_name)
    try:
        config = _make_midi_config(tmp_config_path, tmp_profiles_dir, 1)
        channel = ChannelWidget(0, config, _LayoutBackend(), is_midi=True)
        qtbot.addWidget(channel)
        channel.set_edit_mode(True)
        channel.show()
        qtbot.wait(1)

        assert channel.width() == channel.minimumWidth()
        assert channel.width() <= channel.fontMetrics().horizontalAdvance("MMMMMMMMMM")
        assert channel._learn_btn.width() >= channel._learn_btn.minimumSizeHint().width()
        assert channel._mute_learn_btn.width() >= channel._mute_learn_btn.minimumSizeHint().width()
    finally:
        app.setStyle(previous_style)


def test_settings_toggles_share_one_row(layout_window):
    panel = layout_window.settings_panel
    checkboxes = [
        panel._transparency_cb,
        panel._show_invert_cb,
        panel._auto_search_cb,
    ]
    if hasattr(panel, "_update_checks_cb"):
        checkboxes.append(panel._update_checks_cb)

    assert len(checkboxes) == 4
    y_positions = [checkbox.mapTo(panel, QPoint()).y() for checkbox in checkboxes]
    assert max(y_positions) - min(y_positions) <= 2


def test_narrow_viewport_keeps_dense_strips_scrollable(layout_window, qtbot):
    window = layout_window
    window.resize(700, 700)
    qtbot.waitUntil(lambda: window._channel_scroll.horizontalScrollBar().maximum() > 0)

    assert all(channel.width() == channel.minimumWidth() for channel in window._channels)
    assert window._channel_scroll.horizontalScrollBar().maximum() > 0


def test_short_window_scrolls_settings_vertically_without_horizontal_overflow(layout_window, qtbot):
    window = layout_window
    window.resize(700, 420)
    qtbot.waitUntil(lambda: window._settings_scroll.verticalScrollBar().maximum() > 0)

    assert window._settings_scroll.horizontalScrollBar().maximum() == 0
    assert window._settings_scroll.viewport().width() >= window.settings_panel.width()
    assert window._channel_scroll.horizontalScrollBar().maximum() > 0


def test_small_mixer_remains_bounded_and_assignment_names_elide(tmp_path, monkeypatch, qtbot):
    config_path = tmp_path / "small-config.json"
    profiles_dir = tmp_path / "small-profiles"
    profiles_dir.mkdir()
    config = _make_midi_config(config_path, profiles_dir, 3)
    monkeypatch.setattr(
        main_window,
        "QSettings",
        lambda *_args: QSettings(str(tmp_path / "small-gui.ini"), QSettings.Format.IniFormat),
    )
    window = MainWindow(config=config, backend=_LayoutBackend())
    qtbot.addWidget(window)
    window.finalize_ui()
    window.resize(1000, 700)
    window.show()
    qtbot.wait(1)

    assert window._channel_scroll.horizontalScrollBar().maximum() == 0
    assert all(channel.width() <= channel.maximumWidth() for channel in window._channels)
    assert window._channels[-1].geometry().right() < window._channel_scroll.viewport().width()

    row = _AppRow("A very long application or device assignment", lambda: None)
    qtbot.addWidget(row)
    row.resize(90, row.sizeHint().height())
    row.show()
    qtbot.wait(1)
    assert row._name_label.text().endswith("…")
    assert "A very long application or device assignment" in row._name_label.toolTip()

    special_row = _AppRow("System Master", lambda: None)
    qtbot.addWidget(special_row)
    special_row.resize(90, special_row.sizeHint().height())
    special_row.show()
    qtbot.wait(1)
    assert special_row._name_label.text().startswith("System")
    assert special_row._name_label.toolTip() == "App: System Master"


def test_short_viewport_exposes_vertical_scroll_without_covering_controls(layout_window, qtbot):
    window = layout_window
    qtbot.waitUntil(lambda: window._channel_scroll.verticalScrollBar().maximum() > 0)
    scroll_bar = window._channel_scroll.horizontalScrollBar()
    viewport_bottom = (
        window._channel_scroll.viewport().mapTo(window._channel_scroll, QPoint()).y()
        + window._channel_scroll.viewport().height()
    )
    scroll_bar_top = scroll_bar.mapTo(window._channel_scroll, QPoint()).y()

    assert window._channel_container.minimumHeight() > window._channel_scroll.viewport().height()
    assert scroll_bar_top >= viewport_bottom
    assert window._channels[0]._remove_midi_btn.geometry().bottom() <= window._channels[0].contentsRect().bottom()


def test_channel_reorder_persists_without_renumbering_mappings(
    tmp_config_path,
    tmp_profiles_dir,
    tmp_path,
    monkeypatch,
    qtbot,
):
    config = _make_midi_config(tmp_config_path, tmp_profiles_dir, 3)
    profile_manager = ProfileManager(profiles_dir=tmp_profiles_dir)
    profile_manager.set_active_silently("profile-1")
    monkeypatch.setattr(
        main_window,
        "QSettings",
        lambda *_args: QSettings(str(tmp_path / "reorder-gui.ini"), QSettings.Format.IniFormat),
    )
    window = MainWindow(config=config, backend=_LayoutBackend(), profile_manager=profile_manager)
    qtbot.addWidget(window)
    original_channels = config.all_channels()

    window._move_channel_by_step(1, -1)

    assert window._visual_channel_order() == [1, 0, 2]
    assert profile_manager.load("profile-1")["channel_order"] == [1, 0, 2]
    assert config.all_channels() == original_channels


def test_reorder_grip_is_accessible_and_excluded_from_frameless_move(layout_window, qtbot):
    window = layout_window
    window._compact_btn.setChecked(True)
    qtbot.wait(20)
    grip = window._channels[0]._sep
    window._channel_scroll.ensureWidgetVisible(grip, 0, 0)
    qtbot.wait(1)
    grip_center = grip.mapTo(window, grip.rect().center())
    label_center = window._channels[0]._ch_label.mapTo(window, window._channels[0]._ch_label.rect().center())

    assert "Reorder channel" in grip.accessibleName()
    assert "Left/Right" in grip.toolTip()
    assert grip.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert window._hit_channel_reorder_grip(grip_center)
    assert not window._hit_channel_reorder_grip(label_center)


def test_keyboard_reorder_works_in_compact_mode(layout_window, qtbot):
    window = layout_window
    window._compact_btn.setChecked(True)
    grip = window._channels[1]._sep

    qtbot.keyClick(grip, Qt.Key.Key_Left)

    assert window._visual_channel_order()[:3] == [1, 0, 2]


def test_drag_edge_autoscrolls_crowded_channel_area(layout_window, qtbot):
    window = layout_window
    window.resize(700, 700)
    scroll_bar = window._channel_scroll.horizontalScrollBar()
    qtbot.waitUntil(lambda: scroll_bar.maximum() > 0)
    scroll_bar.setValue(scroll_bar.maximum() // 2)
    before = scroll_bar.value()
    viewport = window._channel_scroll.viewport()
    edge_global = viewport.mapToGlobal(QPoint(viewport.width() - 1, viewport.height() // 2))

    window._on_channel_drag_started(window._channels[8].channel_index)
    window._drag_global_pos = edge_global
    window._autoscroll_channel_drag()
    window._on_channel_drag_finished(window._channels[8].channel_index, edge_global)

    assert scroll_bar.value() > before


def test_rightward_drag_inserts_between_adjacent_channels(layout_window):
    window = layout_window
    source = window._channels[0]
    left_neighbor = window._channels[1]
    right_neighbor = window._channels[2]
    between_x = (left_neighbor.geometry().center().x() + right_neighbor.geometry().center().x()) // 2
    global_pos = window._channel_container.mapToGlobal(QPoint(between_x, source.geometry().center().y()))

    window._on_channel_drag_started(source.channel_index)
    window._on_channel_drag_moved(source.channel_index, global_pos)
    window._on_channel_drag_finished(source.channel_index, global_pos)

    assert window._visual_channel_order()[:3] == [1, 0, 2]


def test_paused_app_row_uses_theme_disabled_color_and_precise_tooltip(qtbot):
    row = _AppRow("Firefox", lambda: None)
    qtbot.addWidget(row)
    row.set_routing_paused(True)

    expected = QApplication.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
    actual = row._name_label.palette().color(QPalette.ColorRole.WindowText)
    assert actual == expected
    assert "routing is paused" in row._name_label.toolTip()
    assert "volume and mute still apply" in row._name_label.toolTip()
