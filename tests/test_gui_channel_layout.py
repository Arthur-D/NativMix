"""GUI geometry regressions for crowded mixer channel layouts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QPoint, QRect, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QPalette
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication, QMenu, QPushButton, QStyle, QStyleFactory, QStyleOptionToolButton

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import make_profile, write_profile

from nativmix.audio.base import AudioBackendBase
from nativmix.gui import main_window, settings_panel
from nativmix.gui.main_window import ChannelWidget, MainWindow, _AppRow, _TargetPicker
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


def test_crossfader_settings_do_not_replay_hardware_or_replace_side_column(layout_window, monkeypatch):
    window = layout_window
    sync = MagicMock()
    monkeypatch.setattr(window, "sync_ui_to_hardware", sync)
    requests = QSignalSpy(window.crossfader_reapply_requested)
    bases = [channel["volume"] for channel in window._config.all_channels()]
    side_panel = window._side_panel
    window._on_crossfader_enabled_changed(True)
    window._on_crossfader_side_assignment_toggled("a", 0, True)
    window._on_crossfader_position_changed(0.75)
    assert not window._crossfader_bar.isHidden()
    assert len(requests) == 3
    sync.assert_not_called()
    assert window._side_panel is side_panel
    assert bases == [channel["volume"] for channel in window._config.all_channels()]


def test_hidden_midi_edit_controls_stay_hidden_after_compact_toggle(layout_window):
    window = layout_window
    window._config.show_midi_edit_controls = False
    window._on_compact_toggled(True)
    window._on_compact_toggled(False)
    assert window._add_midi_btn.isHidden()
    assert window._edit_midi_btn.isHidden()
    assert not window._edit_midi_btn.isChecked()


def test_usb_crossfader_control_stays_frozen_across_backend_feedback(
    tmp_config_path, tmp_profiles_dir, tmp_path, monkeypatch, qtbot,
):
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    monkeypatch.setattr(
        main_window, "QSettings",
        lambda *_args: QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat),
    )
    window = MainWindow(config=config, backend=_LayoutBackend())
    qtbot.addWidget(window)
    window._on_crossfader_enabled_changed(True)
    window._set_crossfader_control(0)
    control = window._channels[0]
    control.set_mute_state(False)
    control.set_gain_control_supported(True)
    control.set_volume(0.8)
    assert control._level_label.text() == "XF"
    assert not control._slider.isEnabled()
    assert not control._mute_btn.isEnabled()
    assert control._add_btn.isEnabled()
    window._set_crossfader_control(None)
    assert control._slider.isEnabled()


@pytest.mark.parametrize("mute", [False, True])
def test_clear_individual_cc_cancels_learn_and_preserves_other_mappings(
    tmp_config_path, tmp_profiles_dir, qtbot, mute,
):
    config = _make_midi_config(tmp_config_path, tmp_profiles_dir, 2)
    channel = ChannelWidget(0, config, _LayoutBackend(), is_midi=True)
    qtbot.addWidget(channel)
    button = channel._mute_learn_btn if mute else channel._learn_btn
    menu = channel._mute_midi_menu if mute else channel._vol_midi_menu
    rebuild = channel._rebuild_mute_midi_menu if mute else channel._rebuild_vol_midi_menu
    button.click()
    assert button.isChecked()
    rebuild()
    with qtbot.waitSignal(config.settings_changed):
        next(action for action in menu.actions() if action.text() == "Clear").trigger()

    assert not button.isChecked()
    assert button.text() == "1:—"
    assert config.get_midi_cc(0) == (127 if mute else None)
    assert config.get_midi_mute_cc(0) == (None if mute else 127)
    assert config.get_midi_cc(1) == 127
    assert config.get_midi_mute_cc(1) == 127


def test_midi_collision_banner_visible_with_settings_closed(layout_window):
    window = layout_window
    window._toggle_settings_btn.setChecked(False)
    assert not window.settings_panel.isVisible()
    assert window._midi_cc_banner.isVisible()
    assert "MIDI channel 16 / CC 127" in window._midi_cc_banner.toolTip()
    assert "Channel 1 volume" in window.settings_panel.midi_cc_warning.text()


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
    window._toggle_settings_btn.setChecked(False)
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


def test_media_learn_uses_channel_mapping_and_survives_profile_reload(layout_window):
    if main_window.is_windows():
        pytest.skip("Media playback controls are Linux-only")
    channel = layout_window._channels[1]
    button = channel._media_learn_btn
    assert button is not None
    channel.set_edit_mode(True)
    assert button.isVisible()
    layout_window._config.set_app_names(channel.channel_index, ["Spotify"])
    button.click()
    layout_window.on_midi_cc_received(7, 20, 127)
    binding = layout_window._config.get_media_binding(channel.channel_index)
    assert binding == {"cc": 20, "midi_channel": 7, "mode": "momentary"}
    assert not button.isChecked()
    profiles = layout_window._config._profile_manager
    saved = profiles.load(layout_window._config.active_profile_id)
    assert saved["channels"][channel.channel_index]["media_binding"] == binding
    assert saved["channels"][channel.channel_index]["app_names"] == ["Spotify"]
    channel.set_compact_mode(True)
    assert not button.isVisible()


def test_settings_toggles_are_grouped_by_purpose(layout_window):
    panel = layout_window.settings_panel
    checkboxes = [
        panel._transparency_cb,
        panel._show_invert_cb,
        panel._auto_search_cb,
    ]
    if hasattr(panel, "_update_checks_cb"):
        checkboxes.append(panel._update_checks_cb)

    assert panel._rail_device_group.body.isAncestorOf(panel._auto_search_cb)
    for checkbox in checkboxes:
        if checkbox is not panel._auto_search_cb:
            assert panel._preferences_group.body.isAncestorOf(checkbox)


def test_narrow_viewport_keeps_dense_strips_scrollable(layout_window, qtbot):
    window = layout_window
    window.resize(700, 700)
    qtbot.waitUntil(lambda: window._channel_scroll.horizontalScrollBar().maximum() > 0)

    assert all(channel.width() == channel.minimumWidth() for channel in window._channels)
    assert window._channel_scroll.horizontalScrollBar().maximum() > 0


def test_short_window_scrolls_settings_vertically_without_horizontal_overflow(layout_window, qtbot):
    window = layout_window
    for group in window.settings_panel.sidebar_groups.values():
        group.set_expanded(True)
    window.resize(700, 420)
    scroll = window.settings_panel.connection_status
    qtbot.waitUntil(lambda: scroll.verticalScrollBar().maximum() > 0)

    assert scroll.horizontalScrollBar().maximum() == 0
    assert scroll.viewport().width() >= scroll.widget().width()
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
    window.resize(700, 420)
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


@pytest.fixture
def target_picker(qtbot):
    picker = _TargetPicker(
        [
            ("Firefox", "Firefox", "app", True),
            ("Spotify", "Spotify", "app", True),
            ("System Master", "System Master", "app", True),
            ("Other Apps", "Other Apps", "app", True),
            ("sink:headset", "Headset", "hardware", True),
            ("source:mic", "Microphone", "hardware", False),
        ],
        "app",
        {"Firefox"},
        True,
    )
    qtbot.addWidget(picker)
    return picker


def test_assignment_selection_is_staged_and_applies_multiple_apps(target_picker):
    spy = QSignalSpy(target_picker.applied)
    choices = {key: checkbox for checkbox, key, _mode, _special in target_picker._choices}
    choices["Spotify"].setChecked(True)
    assert not spy

    target_picker._apply()

    assert list(spy) == [["app", ["Firefox", "Spotify"]]]


@pytest.mark.parametrize("target", ["System Master", "Other Apps", "sink:headset", "source:mic"])
def test_special_or_device_selection_replaces_other_assignments(target_picker, target):
    spy = QSignalSpy(target_picker.applied)
    choices = {key: checkbox for checkbox, key, _mode, _special in target_picker._choices}
    choices["Spotify"].setChecked(True)
    choices[target].setChecked(True)
    target_picker._apply()

    assert len(spy) == 1
    assert spy[0] == ["hardware" if ":" in target else "app", [target]]

    choices["Spotify"].setChecked(True)
    assert not choices[target].isChecked()


def test_manual_app_clears_device_and_preserves_regular_multi_selection(target_picker):
    spy = QSignalSpy(target_picker.applied)
    choices = {key: checkbox for checkbox, key, _mode, _special in target_picker._choices}
    choices["sink:headset"].setChecked(True)
    target_picker._manual_name.setText("Pinned app")
    choices["Spotify"].setChecked(True)
    target_picker._apply()

    assert spy[0] == ["app", ["Spotify", "Pinned app"]]


def test_assignments_apply_without_changing_volume_or_midi_bindings(
    tmp_config_path, tmp_profiles_dir, qtbot,
):
    config = _make_midi_config(tmp_config_path, tmp_profiles_dir, 1)
    config.set_channel_volume(0, 0.37)
    channel = ChannelWidget(0, config, _LayoutBackend(), is_midi=True)
    qtbot.addWidget(channel)
    menu = QMenu(channel)
    channel._apply_target_selection(menu, "app", ["Firefox", "Spotify"])
    assert config.get_app_names(0) == ["Firefox", "Spotify"]
    assert config.get_channel_volume(0) == 0.37
    assert config.get_midi_cc(0) == 127

    staged = channel._build_target_picker()
    qtbot.addWidget(staged)
    choices = {key: checkbox for checkbox, key, _mode, _special in staged._choices}
    assert choices["Firefox"].isChecked()
    assert "(unavailable)" in choices["Firefox"].text()
    choices["Firefox"].setChecked(False)
    staged.close()
    assert config.get_app_names(0) == ["Firefox", "Spotify"]

    channel._apply_target_selection(menu, "hardware", ["sink:headset"])
    assert config.get_channel_mode(0) == "hardware"
    assert config.get_hardware_id(0) == "sink:headset"
    assert config.get_app_names(0) == []
    assert not config.is_v_sink_enabled(0)

    channel._apply_target_selection(menu, "app", ["Other Apps"])
    assert config.get_channel_mode(0) == "app"
    assert config.get_hardware_id(0) is None
    assert config.get_app_names(0) == ["Other Apps"]
    assert channel._vsink_cb.isHidden()


def test_normal_strips_replace_persistent_app_list_with_assignment_and_options(layout_window, qtbot):
    window = layout_window
    window._edit_midi_btn.setChecked(False)
    channel = window._channels[1]
    assert channel._app_list_scroll.isHidden()
    assert not hasattr(channel, "_mode_cb")
    assert channel._invert_cb.isHidden()
    assert channel._add_btn.isVisible()
    assert channel._options_btn.isVisible()
    assert channel.minimumHeight() < 400

    channel._rebuild_options_menu()
    actions = {action.text(): action for action in channel._options_menu.actions()}
    actions["Invert fader"].trigger()
    assert window._config.get_effective_inversion(1)
    actions["Learn volume CC"].trigger()
    assert channel.is_waiting_for_volume_learn()
    channel.cancel_learn()
    channel._rebuild_options_menu()
    assert any(action.text() == "Learn play/pause CC" for action in channel._options_menu.actions())

    window._edit_midi_btn.setChecked(True)
    channel._selection_cb.click()
    assert channel.channel_index in window._selected_channels
    assert channel._selection_cb.isChecked()
    window._clear_selection()
    assert not channel._selection_cb.isChecked()


def test_compact_mode_keeps_footer_inside_window_and_hides_special_vsinks(layout_window, qtbot):
    window = layout_window
    window._toggle_settings_btn.setChecked(True)
    window._compact_btn.setChecked(True)
    qtbot.wait(30)
    footer = window.settings_panel.connection_status
    assert footer.isVisible()
    assert window.rect().contains(footer.mapTo(window, footer.rect().bottomRight()))
    window._compact_btn.setChecked(False)
    assert window._channels[0]._vsink_cb.isHidden()


def test_disclosure_supports_keyboard_and_updates_arrow(layout_window, qtbot):
    group = layout_window.settings_panel._preferences_group
    assert group.body.isHidden()
    qtbot.keyClick(group._toggle, Qt.Key.Key_Space)
    assert group.body.isVisible()
    assert group._toggle.arrowType() == Qt.ArrowType.DownArrow


def test_inline_inversion_preference_still_works_without_affecting_compact_mode(layout_window):
    window = layout_window
    window._config.show_invert_option = True
    window._on_settings_updated()
    channel = window._channels[1]
    assert channel._invert_cb.isVisible()
    channel._rebuild_options_menu()
    invert = next(action for action in channel._options_menu.actions() if action.text() == "Invert fader")
    invert.trigger()
    assert channel._invert_cb.isChecked()
    window._compact_btn.setChecked(True)
    assert channel._invert_cb.isHidden()
    channel.update_settings()
    assert channel._invert_cb.isHidden()


def test_long_channel_names_elide_without_losing_rename_value(layout_window, qtbot):
    channel = layout_window._channels[1]
    name = "A long channel name that must not widen the mixer"
    channel._on_rename(name)
    qtbot.wait(1)
    assert channel._ch_label.text().endswith("…")
    assert channel._ch_label.fullText() == name
    assert name in channel._ch_label.toolTip()


def test_palette_change_keeps_window_and_footer_readable(layout_window):
    from nativmix.gui.settings_panel import _palette_contrast_ratio
    from nativmix.gui.theme import build_fusion_fallback_palette

    app = QApplication.instance()
    previous = app.palette()
    window = layout_window
    window.settings_panel.set_audio_mode("stable", "Connected")
    try:
        dark = build_fusion_fallback_palette(True)
        app.setPalette(dark)
        app.processEvents()
        background = dark.color(QPalette.ColorRole.Window)
        assert f"rgba({background.red()}, {background.green()}, {background.blue()}, 255)" in window.styleSheet()
        label_palette = window.settings_panel._audio_mode_label.palette()
        assert _palette_contrast_ratio(
            label_palette.color(QPalette.ColorRole.WindowText), background,
        ) >= 4.5
    finally:
        app.setPalette(previous)


def test_empty_profile_has_actionable_message(layout_window):
    window = layout_window
    for channel in window._channels:
        channel.hide()
    window._update_empty_state()
    assert window._empty_state.isVisible()
    assert "Add a channel" in window._empty_state.text()
    assert window._channel_scroll.isHidden()


def test_channel_actions_sit_in_side_column_left_of_mixer(layout_window, qtbot):
    window = layout_window
    window._toggle_settings_btn.setChecked(True)
    qtbot.wait(1)
    side = window._side_panel
    assert window._add_midi_btn.text() == "+ Add Channel"
    assert window._edit_midi_btn.text() == "✏ Edit Channel"
    assert window._add_midi_btn.parentWidget() is side
    assert side.isAncestorOf(window.settings_panel.connection_status)
    side_right = side.mapTo(window, side.rect().topRight()).x()
    assert side_right < window._channel_scroll.mapTo(window, QPoint(0, 0)).x()
    assert side.width() >= window.settings_panel.side_rail_width_hint()
    assert window._add_midi_btn.width() <= side.width()


def test_narrow_window_moves_side_column_below_mixer(layout_window, qtbot):
    window = layout_window
    window._toggle_settings_btn.setChecked(False)
    window.setMinimumWidth(0)
    window.resize(main_window.SIDE_PANEL_NARROW_WIDTH - 60, 549)
    qtbot.wait(1)
    side = window._side_panel
    assert side.mapTo(window, QPoint(0, 0)).y() >= window._channel_scroll.mapTo(
        window, window._channel_scroll.rect().bottomLeft()
    ).y()
    window.resize(1200, 549)
    qtbot.wait(1)
    assert side.mapTo(window, QPoint(0, 0)).y() < window._channel_scroll.mapTo(
        window, window._channel_scroll.rect().bottomLeft()
    ).y()


def test_bulk_actions_omit_redundant_midi_label(layout_window):
    window = layout_window
    window._channels[1]._selection_cb.click()
    assert window._bulk_delete_btn.text() == "Delete 1 channel"
    assert "MIDI" not in window._bulk_delete_btn.text()


def test_fader_grows_with_window_height_and_stays_bounded(layout_window, qtbot):
    window = layout_window
    window._toggle_settings_btn.setChecked(False)
    window.resize(1600, 549)
    qtbot.wait(1)
    slider = window._channels[0]._slider
    small = slider.height()
    assert small >= main_window.FADER_MIN_HEIGHT
    window.resize(1600, 1400)
    qtbot.wait(1)
    assert slider.height() > small
    assert slider.height() <= main_window.FADER_MAX_HEIGHT


def test_fader_scale_gets_denser_with_height(layout_window, qtbot):
    window = layout_window
    window._toggle_settings_btn.setChecked(False)
    scale = window._channels[0]._fader_scale
    window.resize(1600, 420)
    qtbot.wait(1)
    small_step, _minor = scale.marks()
    window.resize(1600, 1400)
    qtbot.wait(1)
    large_step, _minor = scale.marks()
    assert large_step == 5
    assert small_step > large_step
    slider = window._channels[0]._slider
    assert slider.handle_center_y(100) < slider.handle_center_y(0)


def test_toolbar_mixer_target_switch_follows_remote_availability(layout_window, qtbot):
    window = layout_window
    panel = window.settings_panel
    assert window._target_switch.isHidden()
    panel.set_remote_mixer_available(True)
    assert not window._target_switch.isHidden()
    window._target_remote_btn.click()
    assert panel._mixer_target_box.currentData() == "remote"
    panel._mixer_target_box.setCurrentIndex(0)
    assert window._target_local_btn.isChecked()
    panel._mixer_target_box.setCurrentIndex(1)
    panel.set_remote_mixer_available(False)
    assert window._target_switch.isHidden()
    assert window._target_local_btn.isChecked()
    assert panel._mixer_target_box.currentData() == "local"


def test_sidebar_controls_use_original_settings_actions(layout_window, qtbot):
    window = layout_window
    window._toggle_settings_btn.setChecked(True)
    panel = window.settings_panel
    assert window._side_panel.isAncestorOf(panel.connection_status)

    panel.populate_master_outputs([("Speakers", "speakers"), ("Headphones", "phones")], "phones")
    assert panel._master_box.currentText() == "Headphones"
    spy = QSignalSpy(panel.master_output_changed)
    panel._master_box.setCurrentIndex(0)
    panel._master_box.activated.emit(0)
    assert panel._master_box.currentIndex() == 0
    assert len(spy) == 1
    assert spy[0] == ["speakers"]
    panel._midi_fader_feedback_cb.click()
    assert panel._midi_fader_feedback_cb.isChecked()
    assert panel._config.midi_fader_feedback
    panel._midi_fader_feedback_cb.setChecked(False)
    assert not panel._config.midi_fader_feedback

    assert panel._rail_remote_label.fullText() == "Remote: Off"
    panel._rail_remote_group.set_expanded(True)
    panel._remote_midi_receive_cb.click()
    assert panel._remote_midi_receive_cb.isChecked()
    assert panel._config.remote_midi_receive_enabled
    assert panel._rail_remote_label.fullText() != "Remote: Off"
    panel._remote_midi_receive_cb.setChecked(False)
    assert panel._rail_remote_label.fullText() == "Remote: Off"


def test_remote_sidebar_owns_settings_without_widening_mixer(layout_window, qtbot):
    window = layout_window
    panel = window.settings_panel
    window._toggle_settings_btn.setChecked(True)
    window.resize(1200, 800)
    qtbot.wait(20)
    original_width = window._side_panel.width()
    panel._rail_remote_group.set_expanded(True)
    panel._remote_midi_send_cb.setChecked(True)
    panel._remote_midi_receive_cb.setChecked(True)
    qtbot.wait(20)

    assert window.settings_panel.parentWidget() is window._side_panel
    assert window._side_panel.isAncestorOf(panel._remote_midi_connect_btn)
    assert panel._remote_midi_send_cb.isVisible()
    assert panel._remote_midi_receive_cb.isVisible()
    assert panel._remote_midi_connect_btn.isVisible()
    assert window._side_panel.width() == original_width
    scroll = panel.connection_status
    assert scroll.widget().width() <= scroll.viewport().width()
    for control in (
        panel._mixer_target_box,
        panel._remote_midi_name_edit,
        panel._remote_midi_peer_box,
        panel._remote_midi_connect_btn,
        panel._allow_remote_mixer_editing_cb,
    ):
        assert control.width() >= control.minimumSizeHint().width()
    panel._rail_remote_group.set_expanded(False)
    assert not panel._remote_midi_connect_btn.isVisible()
    assert panel._rail_remote_label.isVisible()

    window.resize(560, 800)
    panel._rail_remote_group.set_expanded(True)
    qtbot.wait(20)
    assert panel._remote_midi_connect_btn.isVisible()
    assert scroll.widget().width() <= scroll.viewport().width()
    assert panel._remote_midi_connect_btn.width() >= panel._remote_midi_connect_btn.minimumSizeHint().width()


def test_side_rail_groups_follow_input_mode_and_compact_mode(layout_window, qtbot):
    window = layout_window
    window._toggle_settings_btn.setChecked(True)
    panel = window.settings_panel
    group = panel._rail_device_group
    group.set_expanded(True)
    assert group.body.isVisible()
    panel._input_mode_box.setCurrentIndex(0)
    qtbot.wait(1)
    assert group.body.isVisible()
    assert panel._midi_device_row.isHidden()
    assert panel._input_mode_box.isVisible()
    assert panel._usb_status_label.isVisible()
    panel._input_mode_box.setCurrentIndex(2)
    qtbot.wait(1)
    assert group.body.isVisible()

    group._toggle.click()
    assert group.body.isHidden()
    group._toggle.click()
    assert group.body.isVisible()

    window._compact_btn.setChecked(True)
    qtbot.wait(30)
    assert all(g.body.isHidden() for g in panel.connection_status.groups)
    assert panel._audio_mode_label.isVisible()
    window._compact_btn.setChecked(False)
    qtbot.wait(1)
    assert group.body.isVisible()


def test_side_rail_reset_audio_uses_existing_panic_signal(layout_window, qtbot, monkeypatch):
    questions = []
    monkeypatch.setattr(
        main_window.QMessageBox,
        "question",
        lambda *args, **_kwargs: questions.append(args) or main_window.QMessageBox.StandardButton.Cancel,
    )
    panel = layout_window.settings_panel
    layout_window._toggle_settings_btn.setChecked(True)
    qtbot.wait(1)
    spy = QSignalSpy(panel.panic_triggered)
    panel._debug_box.set_expanded(True)
    qtbot.wait(1)
    panel._panic_btn.click()
    assert len(spy) == 1
    assert len(questions) == 1
    side = layout_window._side_panel
    button = panel._panic_btn
    assert side.isAncestorOf(button)
    assert panel.isAncestorOf(button)
    assert sum("Reset Audio" in b.text() for b in side.findChildren(QPushButton)) == 1


def test_sidebar_debug_controls_share_existing_actions(layout_window, qtbot):
    panel = layout_window.settings_panel
    group = panel._debug_box
    assert panel.connection_status.isAncestorOf(group)
    assert panel.isAncestorOf(group)
    assert not group.is_expanded()
    group._toggle.click()
    qtbot.wait(1)
    for control in (
        panel._debug_logging_cb, panel._open_log_folder_btn, panel._panic_btn, panel._midi_panic_btn,
    ):
        assert group.body.isAncestorOf(control)
        assert control.isVisible()
    midi_spy = QSignalSpy(panel.midi_panic_triggered)
    panel._midi_panic_btn.click()
    assert len(midi_spy) == 1
    panel._debug_logging_cb.click()
    assert panel._config.debug_logging == panel._debug_logging_cb.isChecked()


def test_sidebar_scrolls_only_when_expanded_controls_exceed_available_height(layout_window, qtbot):
    window = layout_window
    panel = window.settings_panel
    window._toggle_settings_btn.setChecked(True)
    panel._rail_remote_group.set_expanded(True)
    panel._debug_box.set_expanded(True)
    panel._remote_midi_send_cb.setChecked(True)
    panel._remote_midi_receive_cb.setChecked(True)
    rail = panel.connection_status

    window.resize(1200, 1200)
    qtbot.wait(30)
    assert rail.verticalScrollBar().maximum() == 0
    assert rail.widget().width() <= rail.viewport().width()
    assert panel._remote_midi_group.height() > 340
    assert rail.viewport().rect().contains(panel._prevent_remote_sleep_cb.mapTo(
        rail.viewport(), panel._prevent_remote_sleep_cb.rect().bottomRight(),
    ))

    window.resize(1200, 500)
    qtbot.wait(30)
    assert rail.verticalScrollBar().maximum() > 0
    assert rail.widget().width() <= rail.viewport().width()
    rail.ensureWidgetVisible(panel._midi_panic_btn)
    qtbot.wait(1)
    assert rail.viewport().rect().contains(panel._midi_panic_btn.mapTo(
        rail.viewport(), panel._midi_panic_btn.rect().bottomRight(),
    ))

    panel._remote_midi_group.set_expanded(False)
    panel._debug_box.set_expanded(False)
    qtbot.wait(30)
    assert rail.verticalScrollBar().maximum() == 0


def test_sidebar_splitter_width_and_disclosures_restore_on_restart(layout_window, qtbot):
    window = layout_window
    window.resize(1600, 800)
    window.settings_panel._rail_audio_group.set_expanded(True)
    window.settings_panel._preferences_group.set_expanded(True)
    window.settings_panel._remote_midi_group.set_expanded(False)
    qtbot.wait(10)
    window._workspace_splitter.moveSplitter(440, 1)
    qtbot.wait(10)
    width = window._side_panel.width()
    assert width >= 430
    assert window.settings.value("sidebar/width", type=int) == width
    window.settings.sync()

    restored = MainWindow(config=window._config, backend=_LayoutBackend())
    qtbot.addWidget(restored)
    restored.finalize_ui()
    restored.resize(1600, 800)
    restored.show()
    qtbot.wait(10)
    panel = restored.settings_panel
    assert panel.isVisible()
    assert abs(restored._side_panel.width() - width) <= 2
    assert panel._rail_audio_group.is_expanded()
    assert panel._preferences_group.is_expanded()
    assert not panel._remote_midi_group.is_expanded()

    restored._toggle_settings_btn.setChecked(False)
    assert not panel.isVisible()
    assert panel._preferences_group.is_expanded()
    restored._toggle_settings_btn.setChecked(True)
    qtbot.wait(10)
    assert abs(restored._side_panel.width() - width) <= 2
    assert panel._preferences_group.is_expanded()


def test_hidden_sidebar_restores_and_tray_settings_reopens_it(layout_window, qtbot):
    window = layout_window
    window.settings_panel._debug_box.set_expanded(True)
    window._toggle_settings_btn.setChecked(False)
    window.settings.sync()
    restored = MainWindow(config=window._config, backend=_LayoutBackend())
    qtbot.addWidget(restored)
    restored.finalize_ui()
    restored.show()
    assert not restored.settings_panel.isVisible()
    restored.open_settings()
    qtbot.wait(10)
    assert restored.settings_panel.isVisible()
    assert restored.settings_panel._debug_box.is_expanded()


def test_sidebar_splitter_can_shrink_below_previous_default(layout_window, qtbot):
    window = layout_window
    panel = window.settings_panel
    window.resize(1200, 800)
    for group in panel.sidebar_groups.values():
        group.set_expanded(True)
    qtbot.wait(10)
    minimum = panel.side_rail_width_hint()
    window._workspace_splitter.moveSplitter(minimum, 1)
    qtbot.wait(10)

    assert window._side_panel.width() <= minimum + 2
    assert window._side_panel.width() < 320
    assert window.settings.value("sidebar/width", type=int) == window._side_panel.width()
    rail = panel.connection_status
    assert rail.widget().width() <= rail.viewport().width()
    assert rail.horizontalScrollBar().maximum() == 0
