"""GUI geometry regressions for crowded mixer channel layouts."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QAbstractAnimation, QEvent, QPoint, QPointF, QRect, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QMouseEvent, QPalette
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QMenu,
    QPushButton,
    QStyle,
    QStyleFactory,
    QStyleOptionSlider,
    QStyleOptionToolButton,
)

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import make_profile, write_profile

from nativmix.audio.base import AudioBackendBase
from nativmix.gui import main_window, settings_panel
from nativmix.gui.main_window import ChannelWidget, MainWindow, _AppRow, _TargetPicker
from nativmix.utils.config_manager import ConfigManager
from nativmix.utils.profile_manager import ProfileManager


class _LayoutBackend(AudioBackendBase):
    target_inventory_changed = pyqtSignal()
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
    qtbot.addWidget(window._volume_warning_overlay)
    window.finalize_ui()
    window.resize(1600, 549)
    window.show()
    window._toggle_settings_btn.setChecked(True)
    window._edit_midi_btn.setChecked(True)
    qtbot.wait(1)
    return window


@pytest.mark.parametrize("source", ["gui", "usb", "midi"])
def test_unmapped_channel_warns_for_each_input_source(layout_window, caplog, source):
    window = layout_window
    channel = window._channels[1]
    assert channel._target_warning_active
    assert window._volume_warning_overlay.isHidden()
    with caplog.at_level(logging.WARNING):
        if source == "gui":
            channel._slider.setValue(30)
            channel._slider.setValue(40)
        elif source == "usb":
            window.on_volumes_changed([0.5, 0.3])
            window.on_volumes_changed([0.5, 0.4])
        else:
            window.on_channel_volume_requested(1, 0.3)
            window.on_channel_volume_requested(1, 0.4)
    assert channel._target_warning_active
    warnings = [record for record in caplog.records if "controls no audio" in record.message]
    assert len(warnings) == 1
    assert "channel 2" in warnings[0].message
    assert "Edit Channels" in warnings[0].message
    channel.set_compact_mode(True)
    assert channel._target_warning_active
    assert window._channels[2]._target_warning_active
    assert window._volume_warning_overlay._detail.isHidden()
    assert window._volume_warning_overlay._detail.text() == ""


def test_unused_unmapped_channels_are_grey_but_do_not_notify_after_refresh(layout_window, caplog):
    channel = layout_window._channels[1]
    assert channel._target_warning_active
    with caplog.at_level(logging.WARNING):
        channel.refresh()
        channel.set_volume(0.3)
        channel.set_compact_mode(True)
        channel.set_compact_mode(False)
    assert channel._target_warning_active
    assert layout_window._volume_warning_overlay.isHidden()
    assert not any("controls no audio" in record.message for record in caplog.records)


def test_volume_notification_is_independent_non_interactive_and_restarts_for_five_seconds(
    layout_window, qtbot,
):
    window = layout_window
    overlay = window._volume_warning_overlay
    window.hide()
    window.on_channel_volume_requested(1, 0.3)
    assert window.isHidden()
    assert overlay.isVisible()
    assert overlay.parentWidget() is None
    assert overlay.windowType() == Qt.WindowType.ToolTip
    assert overlay.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert overlay._title.font().pointSize() >= 18
    assert overlay._timer.isSingleShot()
    assert overlay._timer.interval() == 5000
    qtbot.wait(150)
    remaining = overlay._timer.remainingTime()
    assert 0 < remaining < 4900
    window.on_channel_volume_requested(1, 0.4)
    assert overlay._timer.remainingTime() > remaining
    assert overlay._timer.remainingTime() > 4900
    qtbot.waitUntil(overlay.isHidden, timeout=5500)


def test_osd_survives_interaction_with_another_channel(layout_window, qtbot):
    window = layout_window
    overlay = window._volume_warning_overlay
    window._channels[1]._slider.setValue(30)
    qtbot.wait(1)
    assert overlay.isVisible()
    assert QApplication.activeWindow() is not overlay
    qtbot.mouseClick(window._channels[2]._slider, Qt.MouseButton.LeftButton)
    window._channels[2]._slider.setValue(40)
    qtbot.wait(1)
    assert overlay.isVisible()
    assert "Local channel 3" in overlay._title.text()
    assert QApplication.activeWindow() is not overlay
    assert overlay._timer.remainingTime() > 4900


def test_warning_greys_channel_without_muting_and_restores_saved_color(layout_window):
    window = layout_window
    channel = window._channels[1]
    window._config.set_channel_background_color(1, "#ff8000")
    window._config.set_app_names(1, ["System Master"])
    channel.refresh()
    original_style = channel._slider.styleSheet()
    window._config.set_app_names(1, [])
    channel.refresh()
    assert channel._target_warning_active
    channel.warn_if_unmapped()
    assert channel._target_warning_active
    assert not channel._muted
    assert channel._slider.isEnabled()
    assert "background: transparent;" in channel._slider.styleSheet().split("QSlider::add-page")[1].split("}")[0]
    assert "#ff8000" not in channel._slider.styleSheet()
    grey = QApplication.palette().color(QPalette.ColorRole.Mid).name()
    assert channel._ch_label._background_color == QColor(grey)
    assert window._config.get_channel_background_color(1) == "#ff8000"
    assert channel.findChild(main_window.QLabel, "mapping_warning") is None
    channel.set_mute_state(True)
    window._config.set_app_names(1, ["System Master"])
    channel.refresh()
    assert not channel._target_warning_active
    assert channel._muted
    assert not channel._slider.isEnabled()
    assert channel._ch_label._background_color == QColor("#ff8000")
    channel.set_mute_state(False)
    assert channel._slider.styleSheet() == original_style


def test_notification_reports_latest_channel_without_throttling_display(layout_window, caplog):
    window = layout_window
    overlay = window._volume_warning_overlay
    with caplog.at_level(logging.DEBUG, logger="nativmix.gui.main_window"):
        window.on_channel_volume_requested(1, 0.3)
        assert "Local channel 2" in overlay._title.text()
        window.on_channel_volume_requested(2, 0.3)
        overlay._timer.timeout.emit()
    applied = [record.message for record in caplog.records if "Volume OSD update applied:" in record.message]
    assert len(applied) == 2
    assert "Local channel 2" in applied[0]
    assert "Local channel 3" in applied[1]
    assert all("visible=True" in message for message in applied)
    assert any(
        "Volume OSD expired:" in record.message and "Local channel 3" in record.message
        for record in caplog.records
    )
    assert "Local channel 3" in overlay._title.text()
    assert overlay.isHidden()
    window.on_channel_volume_requested(2, 0.4)
    assert overlay.isVisible()
    assert overlay._timer.isActive()


@pytest.mark.parametrize("target", ["System Master", "Other Apps", "Offline application"])
def test_saved_audio_target_is_not_unmapped(layout_window, caplog, target):
    window = layout_window
    window._config.set_app_names(1, [target])
    channel = window._channels[1]
    channel.refresh()
    assert not channel._target_warning_active
    with caplog.at_level(logging.WARNING):
        window.on_channel_volume_requested(1, 0.3)
    assert not any("controls no audio" in record.message for record in caplog.records)


def test_hardware_assignment_clears_grey_state_and_removing_it_greys_immediately(layout_window, caplog, monkeypatch):
    window = layout_window
    channel = window._channels[1]
    window._config.set_channel_mode(1, "hardware")
    channel.refresh()
    assert channel._target_warning_active
    channel.warn_if_unmapped()
    assert channel._target_warning_active
    monkeypatch.setattr(window._backend, "get_real_sinks", lambda: [("Device", "offline-device")])
    window._config.set_hardware_id(1, "sink:offline-device")
    channel.refresh()
    assert not channel._target_warning_active
    window._config.set_hardware_id(1, None)
    channel.refresh()
    assert channel._target_warning_active
    with caplog.at_level(logging.WARNING):
        channel.warn_if_unmapped()
    assert "no audio target assigned" in caplog.text
    assert channel._target_warning_active


@pytest.mark.parametrize("target", ["sink:headset", "source:microphone", "Offline application"])
@pytest.mark.parametrize("source", ["gui", "usb", "midi"])
def test_unavailable_target_warns_only_after_use_and_clears_on_recovery(
    layout_window, caplog, monkeypatch, target, source,
):
    window = layout_window
    channel = window._channels[1]
    available = [False]
    hardware = target.startswith(("sink:", "source:"))
    if hardware:
        window._config.set_channel_mode(1, "hardware")
        window._config.set_hardware_id(1, target)
        name = target.split(":", 1)[1]
        getter = "get_real_sinks" if target.startswith("sink:") else "get_real_sources"
        monkeypatch.setattr(window._backend, getter, lambda: [(name, name)] if available[0] else [])
    else:
        window._config.set_app_names(1, [target])
        monkeypatch.setattr(window._backend, "get_unresolved_targets", lambda: set() if available[0] else {target})
    channel.refresh()
    assert not channel._target_warning_active
    with caplog.at_level(logging.WARNING):
        if source == "gui":
            channel._slider.setValue(30)
            channel._slider.setValue(40)
        elif source == "usb":
            window.on_volumes_changed([1.0, 0.3])
            window.on_volumes_changed([1.0, 0.4])
        else:
            window.on_channel_volume_requested(1, 0.3)
            window.on_channel_volume_requested(1, 0.4)
    assert channel._target_warning_active
    assert "Audio target unavailable" in window._volume_warning_overlay._title.text()
    assert window._volume_warning_overlay._detail.text() == target.split(":")[-1]
    assert sum("Audio target unavailable:" in record.message for record in caplog.records) == 1
    channel.set_compact_mode(True)
    assert channel._target_warning_active
    available[0] = True
    if hardware:
        window._backend.target_inventory_changed.emit()
    else:
        window._backend.unresolved_targets_changed.emit(set())
    assert not channel._target_warning_active
    available[0] = False
    channel.refresh()
    assert not channel._target_warning_active
    if hardware:
        assert window._config.get_hardware_id(1) == target
    else:
        assert window._config.get_app_names(1) == [target]


def test_partial_app_availability_warns_without_discarding_assignments(layout_window, monkeypatch):
    window = layout_window
    window._config.set_app_names(1, ["Running app", "Missing app"])
    monkeypatch.setattr(window._backend, "get_unresolved_targets", lambda: {"Missing app"})
    channel = window._channels[1]
    channel.warn_if_unmapped()
    assert "Audio target unavailable" in window._volume_warning_overlay._title.text()
    assert window._volume_warning_overlay._detail.text() == "Missing app"
    assert window._config.get_app_names(1) == ["Running app", "Missing app"]


def test_mapping_unused_grey_channel_to_unavailable_app_still_requires_use(layout_window, monkeypatch):
    window = layout_window
    channel = window._channels[1]
    assert channel._target_warning_active
    monkeypatch.setattr(window._backend, "get_unresolved_targets", lambda: {"Missing app"})
    window._config.set_app_names(1, ["Missing app"])
    channel.refresh()
    assert not channel._target_warning_active
    assert window._volume_warning_overlay.isHidden()
    channel.warn_if_unmapped()
    assert channel._target_warning_active
    assert window._volume_warning_overlay._detail.text() == "Missing app"
    window._config.set_app_names(1, [])
    channel.warn_if_unmapped()
    assert window._volume_warning_overlay._detail.text() == ""
    assert window._volume_warning_overlay._detail.isHidden()


def test_unassigned_midi_reports_protocol_channel_and_cc(layout_window, caplog, monkeypatch):
    window = layout_window
    now = [10.0]
    monkeypatch.setattr(main_window.time, "monotonic", lambda: now[0])
    with caplog.at_level(logging.WARNING):
        window.on_midi_cc_received(3, 21, 64)
        window.on_midi_cc_received(3, 21, 65)
        now[0] += 5.0
        window.on_midi_cc_received(3, 21, 66)
    assert not window._volume_warning_overlay.isHidden()
    assert window._volume_warning_overlay._detail.text() == "Channel 4, CC 21"
    assert "Learn" not in window._volume_warning_overlay._detail.text()
    assert "Learn" in caplog.text
    assert sum("Unassigned MIDI input" in record.message for record in caplog.records) == 2
    window._volume_warning_overlay._timer.timeout.emit()
    assert window._volume_warning_overlay.isHidden()


def test_bound_midi_with_no_audio_target_warns_about_the_mixer_channel(layout_window, caplog):
    window = layout_window
    window._config.set_midi_cc(1, 21, midi_channel=3)
    with caplog.at_level(logging.WARNING):
        window.on_midi_cc_received(3, 21, 64)
    assert "channel 2" in caplog.text
    assert "controls no audio" in caplog.text
    assert not window._volume_warning_overlay.isHidden()
    assert "Local channel 2" in window._volume_warning_overlay._title.text()
    assert window._channels[1]._target_warning_active


def test_unassigned_midi_throttles_each_control_independently(layout_window, caplog):
    window = layout_window
    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            window.on_midi_cc_received(3, 21, 64)
            window.on_midi_cc_received(3, 22, 64)
    assert sum("Unassigned MIDI input" in record.message for record in caplog.records) == 2


@pytest.mark.parametrize("binding", ["mute", "media", "next", "previous", "crossfader"])
def test_non_volume_midi_binding_does_not_warn(layout_window, caplog, binding):
    window = layout_window
    config = window._config
    if binding == "mute":
        config.set_midi_mute_cc(1, 21, midi_channel=3)
    elif binding == "media":
        config.set_media_binding(-1, 21, 3, "momentary")
    elif binding == "next":
        config.profile_midi_next_cc = 21
    elif binding == "previous":
        config.profile_midi_prev_cc = 21
    else:
        config.set_crossfader_enabled(True)
        config.set_crossfader_midi_binding(21, midi_channel=3)
    with caplog.at_level(logging.WARNING):
        window.on_midi_cc_received(3, 21, 64)
    assert window._volume_warning_overlay.isHidden()
    assert not any("Unassigned MIDI input" in record.message for record in caplog.records)


@pytest.mark.parametrize("learn", ["volume", "mute", "media", "profile"])
def test_midi_learn_does_not_report_unassigned_input(layout_window, caplog, learn):
    window = layout_window
    channel = window._channels[1]
    if learn == "volume":
        channel.start_volume_learn()
    elif learn == "mute":
        channel._mute_learn_btn.setChecked(True)
    elif learn == "media":
        channel._media_learn_btn.setChecked(True)
    else:
        window.settings_panel._start_profile_cc_learn("next")
    with caplog.at_level(logging.WARNING):
        window.on_midi_cc_received(3, 21, 0)
    assert window._volume_warning_overlay.isHidden()
    assert not any("Unassigned MIDI input" in record.message for record in caplog.records)


def test_crossfader_control_has_no_audio_mapping_warning(layout_window, caplog, monkeypatch):
    window = layout_window
    window._config.set_crossfader_enabled(True)
    monkeypatch.setattr(window._config, "get_crossfader_usb_channel_index", lambda: 1)
    window.refresh_crossfader_ui()
    channel = window._channels[1]
    assert not channel._target_warning_active
    with caplog.at_level(logging.WARNING):
        window.on_volumes_changed([0.5, 0.3])
    assert not any("controls no audio" in record.message for record in caplog.records)


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


def test_channel_edit_actions_return_after_compact_toggle(layout_window):
    window = layout_window
    window._on_compact_toggled(True)
    assert window._add_midi_btn.isHidden()
    assert window._edit_midi_btn.isHidden()
    window._on_compact_toggled(False)
    assert not window._add_midi_btn.isHidden()
    assert not window._edit_midi_btn.isHidden()
    assert window._edit_midi_btn.isChecked()


def test_channel_setup_controls_follow_edit_channels(layout_window, qtbot):
    window = layout_window
    channel = window._channels[1]
    assert not channel._add_btn.isHidden()
    assert not channel._options_btn.isHidden()
    assert channel._vsink_action.isVisible()
    assert not hasattr(channel, "_vsink_cb")
    with_controls = channel.minimumHeight()
    window._edit_midi_btn.setChecked(False)
    qtbot.wait(1)
    for strip in window._channels:
        assert strip._add_btn.isHidden()
        assert strip._options_btn.isHidden()
    assert channel.minimumHeight() < with_controls
    window._edit_midi_btn.setChecked(True)
    assert not channel._add_btn.isHidden()
    assert channel._vsink_action.isVisible()


def test_compact_mode_keeps_sidebar_unchanged(layout_window, qtbot):
    window = layout_window
    groups = window.settings_panel.sidebar_groups
    groups["preferences"].set_expanded(True)
    window._compact_btn.setChecked(True)
    qtbot.wait(20)
    assert all(not group.isHidden() for group in groups.values())
    assert all(group._toggle.isVisible() for group in groups.values())
    assert groups["preferences"].body.isVisible()


def test_usb_mode_shows_channel_actions_with_add_disabled(layout_window, qtbot):
    window = layout_window
    window._edit_midi_btn.setChecked(False)
    window._config.input_mode = "usb"
    window.refresh_layout()
    assert not window._add_midi_btn.isHidden()
    assert not window._add_midi_btn.isEnabled()
    assert "Arduino" in window._add_midi_btn.toolTip()
    assert not window._edit_midi_btn.isHidden()
    window._config.input_mode = "midi_only"
    window.refresh_layout()
    assert window._add_midi_btn.isEnabled()


def test_fader_handle_is_wider_and_meets_scale_ticks(layout_window):
    channel = layout_window._channels[1]
    slider = channel._slider
    option = QStyleOptionSlider()
    slider.initStyleOption(option)
    handle = slider.style().subControlRect(
        QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, slider
    )
    assert handle.width() == main_window.FADER_HANDLE_WIDTH >= 19
    scale = channel._fader_scale
    scale_right = scale.mapTo(channel, QPoint(scale.width(), 0)).x()
    assert slider.mapTo(channel, handle.topLeft()).x() == scale_right


def test_compact_strip_ends_at_channel_name(layout_window, qtbot):
    window = layout_window
    window._compact_btn.setChecked(True)
    qtbot.wait(50)
    channel = window._channels[1]
    label_bottom = channel._ch_label.mapTo(channel, QPoint(0, channel._ch_label.height())).y()
    assert channel.layout().minimumSize().height() - label_bottom <= 8


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

    expected_width = max(
        channel.fontMetrics().horizontalAdvance("MMMMMMM"),
        channel._learn_btn.minimumSizeHint().width(),
        channel._mute_learn_btn.minimumSizeHint().width(),
    )
    assert channel.minimumWidth() == expected_width
    assert channel.minimumWidth() >= channel._learn_btn.minimumSizeHint().width()
    assert channel.minimumWidth() >= channel._mute_learn_btn.minimumSizeHint().width()
    assert channel.width() == channel.minimumWidth() == channel.maximumWidth()


def test_empty_channel_assignment_label_is_app_without_removing_device_support(layout_window):
    channel = layout_window._channels[0]
    layout_window._config.set_app_names(channel.channel_index, [])
    channel._update_target_summary()
    assert channel._add_btn.text() == "App"
    assert "Select apps or one audio device." in channel._add_btn.toolTip()


def test_eighteen_channels_scroll_horizontally_without_compressing(layout_window, qtbot):
    window = layout_window
    window._toggle_settings_btn.setChecked(False)
    window.resize(1300, 549)
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

    buttons = tuple(
        button for button in (channel._learn_btn, channel._mute_learn_btn, channel._media_learn_btn)
        if button is not None
    )
    assert channel._learn_btn.text() == "16:127"
    assert channel._mute_learn_btn.text() == "16:127"
    assert "MIDI channel 16, CC 127" in channel._learn_btn.toolTip()
    assert "MIDI channel 16, CC 127" in channel._learn_btn.accessibleName()
    for button in buttons:
        assert button.width() >= button.minimumSizeHint().width()
        assert channel.contentsRect().contains(button.geometry())
    for upper, lower in zip(buttons, buttons[1:], strict=False):
        assert not upper.geometry().intersects(lower.geometry())

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
        panel._compact_cb,
        panel._stay_open_cb,
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
    window._channel_scroll.setFixedHeight(window._channel_container.minimumSizeHint().height() - 20)
    qtbot.waitUntil(lambda: window._channel_scroll.verticalScrollBar().maximum() > 0)
    scroll_bar = window._channel_scroll.horizontalScrollBar()
    viewport_bottom = (
        window._channel_scroll.viewport().mapTo(window._channel_scroll, QPoint()).y()
        + window._channel_scroll.viewport().height()
    )
    scroll_bar_top = scroll_bar.mapTo(window._channel_scroll, QPoint()).y()

    assert window._channel_container.minimumHeight() > window._channel_scroll.viewport().height()
    assert scroll_bar_top >= viewport_bottom
    last_button = window._channels[0]._media_learn_btn or window._channels[0]._mute_learn_btn
    assert last_button.geometry().bottom() <= window._channels[0].contentsRect().bottom()


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
    assert window._hit_channel_reorder_grip(label_center)


@pytest.mark.parametrize("surface", ["strip", "fader_space", "percentage"])
def test_empty_strip_surfaces_select_but_never_reorder(layout_window, qtbot, surface):
    window = layout_window
    source = window._channels[0]
    widget = {
        "strip": source,
        "fader_space": source._slider.parentWidget(),
        "percentage": source._level_label,
    }[surface]
    press = QPoint(0, 2)
    qtbot.mousePress(widget, Qt.MouseButton.LeftButton, pos=press)
    assert source._selected
    target = window._channel_container.mapToGlobal(window._channels[3].geometry().center())
    move = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(widget.mapFromGlobal(target)), QPointF(target),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, move)
    qtbot.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=widget.mapFromGlobal(target))
    assert source._sep._press_global is None
    assert window._drag_channel_index is None
    assert window._visual_channel_order() == list(range(18))


def test_drag_handle_reorders_unselected_channel_with_animated_preview(layout_window, qtbot, monkeypatch):
    window = layout_window
    monkeypatch.setattr(window, "_reorder_animation_duration", lambda: 120)
    source = window._channels[0]
    grip = source._sep
    assert grip.cursor().shape() == Qt.CursorShape.OpenHandCursor
    assert grip.height() >= 10
    assert not source._selected

    press = grip.rect().center()
    qtbot.mousePress(grip, Qt.MouseButton.LeftButton, pos=press)
    neighbor, after = window._channels[1], window._channels[2]
    x = (neighbor.geometry().center().x() + after.geometry().center().x()) // 2
    target = window._channel_container.mapToGlobal(QPoint(x, source.geometry().center().y()))
    move = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(grip.mapFromGlobal(target)), QPointF(target),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(grip, move)

    assert window._drag_channel_index == 0
    preview = window._drag_preview
    assert preview is not None
    assert preview.isVisible()
    assert isinstance(source.graphicsEffect(), QGraphicsOpacityEffect)
    assert window._visual_channel_order()[:3] == [1, 0, 2]
    animation = neighbor._reorder_animation
    assert animation.state() == QAbstractAnimation.State.Running
    assert animation.endValue() == window._channel_slot_rects[1].topLeft()

    qtbot.mouseRelease(grip, Qt.MouseButton.LeftButton, pos=grip.mapFromGlobal(target))
    assert window._visual_channel_order()[:3] == [1, 0, 2]
    assert window._drag_channel_index is None
    assert window._drag_preview is None
    assert source.graphicsEffect() is None
    assert not source._selected
    assert not window._drag_autoscroll_timer.isActive()
    qtbot.waitUntil(lambda: animation.state() == QAbstractAnimation.State.Stopped, timeout=1000)
    assert neighbor.pos() == window._channel_slot_rects[1].topLeft()


def test_disabled_style_animation_moves_channels_immediately(layout_window, monkeypatch):
    window = layout_window
    monkeypatch.setattr(window, "_reorder_animation_duration", lambda: 0)
    window._move_channel_by_step(1, -1)
    assert window._visual_channel_order()[:2] == [1, 0]
    assert window._channels[0]._reorder_animation is None


def test_slider_press_does_not_start_reorder(layout_window, qtbot):
    channel = layout_window._channels[0]
    qtbot.mousePress(channel._slider, Qt.MouseButton.LeftButton, pos=channel._slider.rect().center())
    assert channel._sep._press_global is None
    qtbot.mouseRelease(channel._slider, Qt.MouseButton.LeftButton)


def _accept_label_dialog(monkeypatch, color: str | None) -> None:
    def accept(dialog):
        if color is None:
            dialog._use_theme_color()
        else:
            monkeypatch.setattr(main_window.QColorDialog, "getColor", lambda *_args: QColor(color))
            dialog._choose_color()
        return main_window.QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window._ChannelLabelDialog, "exec", accept)


@pytest.mark.parametrize("color", ["#ffffff", "#000000", "#ff0000", "#168aad", "#808080", "#ffff00"])
def test_custom_name_label_color_chooses_contrasting_text_and_selection_border(layout_window, monkeypatch, color):
    channel = layout_window._channels[0]
    channel._config.set_channel_background_color(0, color)
    channel.refresh_theme()
    background = QColor(color)
    for selected in (False, True, False):
        channel.set_selected(selected)
        assert not channel.autoFillBackground()
        assert channel._ch_label.grab().toImage().pixelColor(0, 0) == background
        image = channel.grab().toImage()
        if selected:
            assert image.pixelColor(1, channel.height() // 2) == background
        else:
            assert image.pixelColor(1, channel.height() // 2) != background
        foreground = channel._ch_label.palette().color(QPalette.ColorRole.WindowText)
        assert settings_panel._palette_contrast_ratio(foreground, background) >= 4.5
        assert channel._level_label.palette().color(QPalette.ColorRole.WindowText) == (
            QApplication.palette().color(QPalette.ColorRole.WindowText)
        )
        assert channel._level_label.font().weight() == QFont.Weight.DemiBold
    channel.set_mute_state(True)
    channel.refresh_theme()
    assert channel._level_label.font().strikeOut()
    _accept_label_dialog(monkeypatch, None)
    channel._edit_label()
    assert channel._config.get_channel_background_color(0) is None
    assert not channel.autoFillBackground()


def test_custom_color_survives_theme_change_and_default_restores_theme(layout_window, monkeypatch):
    channel = layout_window._channels[0]
    previous = QApplication.palette()
    changed = QPalette(previous)
    changed.setColor(QPalette.ColorRole.Window, QColor("#333333"))
    changed.setColor(QPalette.ColorRole.WindowText, QColor("#eeeeee"))
    changed.setColor(QPalette.ColorRole.ButtonText, QColor("#eeeeee"))
    try:
        channel._config.set_channel_background_color(0, "#ffffff")
        QApplication.setPalette(changed)
        channel.refresh_theme()
        assert channel.palette().color(QPalette.ColorRole.Window).name() == "#333333"
        assert channel._ch_label.grab().toImage().pixelColor(0, 0).name() == "#ffffff"
        assert channel._ch_label.palette().color(QPalette.ColorRole.WindowText).name() == "#000000"
        _accept_label_dialog(monkeypatch, None)
        channel._edit_label()
        assert channel.palette().color(QPalette.ColorRole.Window).name() == "#333333"
        assert channel._add_btn.palette().color(QPalette.ColorRole.ButtonText).name() == "#eeeeee"
    finally:
        QApplication.setPalette(previous)


def test_channel_color_picker_and_cancel_use_existing_facade(layout_window, monkeypatch):
    channel = layout_window._channels[0]
    _accept_label_dialog(monkeypatch, "#bedbad")
    channel._edit_label()
    assert channel._config.get_channel_background_color(0) == "#bedbad"
    _accept_label_dialog(monkeypatch, "#bedbad")
    monkeypatch.setattr(main_window.QColorDialog, "getColor", lambda *_args: QColor())

    def cancel_picker(dialog):
        dialog._choose_color()
        return main_window.QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window._ChannelLabelDialog, "exec", cancel_picker)
    channel._edit_label()
    assert channel._config.get_channel_background_color(0) == "#bedbad"


def test_multiple_local_colors_survive_settings_refresh_and_restart(
    layout_window, tmp_profiles_dir, tmp_config_path, monkeypatch, qtbot
):
    window = layout_window
    profiles = ProfileManager(profiles_dir=tmp_profiles_dir)
    profiles.set_active_silently("profile-1")
    window._local_mixer.profiles = profiles
    colors = ["#ff0000", "#168aad", "#ffff00"]
    for channel, color in zip(window._channels, colors, strict=False):
        _accept_label_dialog(monkeypatch, color)
        channel._edit_label()
        qtbot.wait(1)
        for previous, expected in zip(window._channels, colors[:channel.channel_index + 1], strict=False):
            displayed = (
                QColor(QApplication.palette().color(QPalette.ColorRole.Mid).name())
                if previous._target_warning_active else QColor(expected)
            )
            assert previous._ch_label.grab().toImage().pixelColor(0, 0) == displayed
            assert window._config.get_channel_background_color(previous.channel_index) == expected
    assert [ch["background_color"] for ch in profiles.load("profile-1")["channels"][:3]] == colors
    restarted = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    restarted.apply_profile(profiles.load("profile-1"))
    assert [restarted.get_channel_background_color(i) for i in range(3)] == colors


def test_label_text_color_survives_other_channel_color_and_stylesheet_changes(layout_window):
    window = layout_window
    first, second = window._channels[:2]
    window._config.set_channel_background_color(0, "#ffffff")
    first.refresh_theme()
    window._config.set_channel_background_color(1, "#ffffff")
    second.refresh_theme()
    window._config.transparency = not window._config.transparency
    window._apply_transparency()
    for channel in (first, second):
        assert channel._ch_label.palette().color(QPalette.ColorRole.WindowText).name() == "#000000"


def test_double_click_label_edits_name_and_color_together(layout_window, qtbot, monkeypatch):
    channel = layout_window._channels[0]
    edited = []

    def fake_exec(dialog):
        edited.append(dialog)
        dialog._name_edit.setText("  Music  ")
        monkeypatch.setattr(main_window.QColorDialog, "getColor", lambda *_args: QColor("#ff0000"))
        dialog._choose_color()
        preview = dialog._preview.palette().color(QPalette.ColorRole.WindowText)
        assert settings_panel._palette_contrast_ratio(preview, QColor("#ff0000")) >= 4.5
        return main_window.QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window._ChannelLabelDialog, "exec", fake_exec)
    qtbot.mouseDClick(channel._ch_label, Qt.MouseButton.LeftButton)
    assert edited
    assert channel._ch_label.fullText() == "Music"
    assert channel._config.get_channel_background_color(0) == "#ff0000"
    assert channel._ch_label.grab().toImage().pixelColor(0, 0) == QColor("#ff0000")


def test_label_dialog_cancel_and_theme_reset(layout_window, monkeypatch):
    channel = layout_window._channels[0]
    channel._config.set_channel_background_color(0, "#00ff00")
    original = channel._ch_label.fullText()

    def cancel(dialog):
        dialog._name_edit.setText("Ignored")
        dialog._use_theme_color()
        return main_window.QDialog.DialogCode.Rejected

    monkeypatch.setattr(main_window._ChannelLabelDialog, "exec", cancel)
    channel._edit_label()
    assert channel._ch_label.fullText() == original
    assert channel._config.get_channel_background_color(0) == "#00ff00"

    def reset(dialog):
        dialog._use_theme_color()
        dialog._name_edit.setText("")
        assert not dialog._buttons.button(main_window.QDialogButtonBox.StandardButton.Ok).isEnabled()
        dialog._name_edit.setText(original)
        return main_window.QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window._ChannelLabelDialog, "exec", reset)
    channel._edit_label()
    assert channel._config.get_channel_background_color(0) is None


@pytest.mark.parametrize("surface", ["strip", "name", "percentage"])
def test_first_click_selects_without_arming_reorder(layout_window, qtbot, surface):
    window = layout_window
    channel = window._channels[0]
    widget = {
        "strip": channel, "name": channel._ch_label,
        "percentage": channel._level_label,
    }[surface]
    qtbot.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(0, 2))
    assert channel._selected
    assert window._selected_channels == {0}
    assert channel._sep._press_global is None
    qtbot.mouseMove(widget, QPoint(150, 2))
    qtbot.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(150, 2))
    assert window._visual_channel_order() == list(range(18))
    assert window._drag_channel_index is None
    qtbot.mouseClick(window._channels[1], Qt.MouseButton.LeftButton, pos=QPoint(0, 2))
    assert not channel._selected
    assert window._selected_channels == {1}
    qtbot.mouseClick(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, pos=QPoint(0, 2))
    assert window._selected_channels == {0, 1}
    qtbot.mouseClick(
        window._channels[3], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, pos=QPoint(0, 2)
    )
    assert window._selected_channels == {0, 1, 2, 3}


def test_clicking_selected_strip_deselects_and_bulk_actions_follow_channel_buttons(layout_window, qtbot):
    window = layout_window
    channel = window._channels[0]
    qtbot.mouseClick(channel, Qt.MouseButton.LeftButton, pos=QPoint(0, 2))
    assert window._selected_channels == {0}
    assert window._bulk_delete_btn.isVisibleTo(window)
    layout = window._channel_actions_layout
    order = [layout.itemAt(i).widget() for i in range(layout.count())]
    assert order[:3] == [window._add_midi_btn, window._edit_midi_btn, window._bulk_delete_btn]
    qtbot.mouseClick(channel, Qt.MouseButton.LeftButton, pos=QPoint(0, 2))
    assert window._selected_channels == set()
    assert not channel._selected
    assert not window._bulk_delete_btn.isVisibleTo(window)
    assert window._visual_channel_order() == list(range(18))


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
    tmp_config_path, tmp_profiles_dir, qtbot, monkeypatch,
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

    prompts: list[str] = []
    reply = [main_window.QMessageBox.StandardButton.Cancel]
    monkeypatch.setattr(
        main_window.QMessageBox, "warning",
        lambda _parent, _title, text, *_args: prompts.append(text) or reply[0],
    )
    channel._apply_target_selection(menu, "hardware", ["sink:headset"])
    assert "app assignments" in prompts[-1]
    assert config.get_channel_mode(0) == "app"
    assert config.get_app_names(0) == ["Firefox", "Spotify"]

    reply[0] = main_window.QMessageBox.StandardButton.Ok
    channel._apply_target_selection(menu, "hardware", ["sink:headset"])
    assert config.get_channel_mode(0) == "hardware"
    assert config.get_hardware_id(0) == "sink:headset"
    assert config.get_app_names(0) == []
    assert not config.is_v_sink_enabled(0)

    channel._apply_target_selection(menu, "app", ["Other Apps"])
    assert "hardware target" in prompts[-1]
    assert config.get_channel_mode(0) == "app"
    assert config.get_hardware_id(0) is None
    assert config.get_app_names(0) == ["Other Apps"]
    assert not channel._vsink_action.isVisible()


def test_normal_strips_replace_persistent_app_list_with_assignment_and_options(layout_window, qtbot):
    window = layout_window
    channel = window._channels[1]
    assert channel._app_list_scroll.isHidden()
    assert not hasattr(channel, "_mode_cb")
    assert not hasattr(channel, "_invert_cb")
    assert channel._add_btn.isVisible()
    assert channel._options_btn.isVisible()
    assert channel.minimumHeight() < 400

    channel._rebuild_options_menu()
    actions = {action.text(): action for action in channel._options_menu.actions()}
    actions["Invert fader"].trigger()
    assert window._config.get_effective_inversion(1)
    assert set(actions) & {
        "Edit name and color…", "Name label color…", "Use theme background", "Learn volume CC",
        "Learn mute CC", "Volume MIDI channel", "Mute MIDI channel", "Learn play/pause CC",
        "Play/pause MIDI options",
    } == set()
    menus = [action.menu() for action in channel._options_menu.actions() if action.menu() is not None]
    assert menus == []

    assert not hasattr(channel, "_selection_cb")
    assert not hasattr(channel, "_remove_midi_btn")
    qtbot.mouseClick(channel, Qt.MouseButton.LeftButton, pos=QPoint(0, 2))
    assert channel.channel_index in window._selected_channels
    assert channel._selected
    window._clear_selection()
    assert not channel._selected


def test_vsink_option_preserves_state_and_capability_gating(layout_window):
    channel = layout_window._channels[1]
    channel._rebuild_options_menu()
    action = next(action for action in channel._options_menu.actions() if action.text() == "V-Sink")
    assert action.isVisible()
    assert action.isEnabled()
    assert not action.isChecked()
    action.trigger()
    assert channel._config.is_v_sink_enabled(channel.channel_index)
    assert action.isChecked()

    channel._rebuild_options_menu()
    assert action in channel._options_menu.actions()
    assert action.isChecked()
    channel.set_v_sink_supported(False, "Routing unavailable")
    assert action.isChecked()
    assert not action.isEnabled()
    assert "Routing unavailable" in action.toolTip()
    channel.set_v_sink_supported(True)
    assert action.isEnabled()
    action.trigger()
    assert not channel._config.is_v_sink_enabled(channel.channel_index)


def test_appearance_controls_keep_existing_window_behavior(layout_window):
    window = layout_window
    panel = window.settings_panel
    assert not hasattr(panel, "_show_invert_cb")
    assert not hasattr(panel, "_mixer_target_box")
    assert window._compact_btn is panel._compact_cb
    assert window._pin_btn is panel._stay_open_cb
    panel._stay_open_cb.setChecked(True)
    assert window._config.stay_open
    panel._stay_open_cb.setChecked(False)
    assert not window._config.stay_open
    panel._compact_cb.setChecked(True)
    assert window._config.compact_mode
    assert all(channel._compact_mode for channel in window._channels)
    panel._compact_cb.setChecked(False)
    assert not window._config.compact_mode
    assert all(not channel._compact_mode for channel in window._channels)


def test_compact_mode_keeps_footer_inside_window_and_hides_special_vsinks(layout_window, qtbot):
    window = layout_window
    window._toggle_settings_btn.setChecked(True)
    window._compact_btn.setChecked(True)
    qtbot.wait(30)
    footer = window.settings_panel.connection_status
    assert footer.isVisible()
    assert window.rect().contains(footer.mapTo(window, footer.rect().bottomRight()))
    window._compact_btn.setChecked(False)
    assert not window._channels[0]._vsink_action.isVisible()


def test_disclosure_supports_keyboard_and_updates_arrow(layout_window, qtbot):
    group = layout_window.settings_panel._preferences_group
    assert group.body.isHidden()
    qtbot.keyClick(group._toggle, Qt.Key.Key_Space)
    assert group.body.isVisible()
    assert group._toggle.arrowType() == Qt.ArrowType.DownArrow


def test_legacy_inversion_visibility_preference_does_not_restore_inline_controls(layout_window):
    window = layout_window
    window._config.show_invert_option = True
    window._on_settings_updated()
    channel = window._channels[1]
    assert not hasattr(channel, "_invert_cb")
    channel._rebuild_options_menu()
    invert = next(action for action in channel._options_menu.actions() if action.text() == "Invert fader")
    invert.trigger()
    assert channel._invert_action.isChecked()
    window._compact_btn.setChecked(True)
    assert channel._options_btn.isHidden()
    channel.update_settings()
    assert channel._options_btn.isHidden()


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


def test_channel_actions_follow_mixer_target_in_toolbar(layout_window, qtbot):
    window = layout_window
    window._toggle_settings_btn.setChecked(True)
    window.settings_panel.set_remote_mixer_available(True)
    qtbot.wait(1)
    side = window._side_panel
    assert window._add_midi_btn.text() == "+ Add Channel"
    assert window._edit_midi_btn.text() == "✏ Edit Channels"
    assert window._add_midi_btn.parentWidget() is window._channel_actions
    remote = window._target_remote_btn
    add = window._add_midi_btn
    edit = window._edit_midi_btn
    assert remote.mapTo(window, remote.rect().topRight()).x() < add.mapTo(window, QPoint(0, 0)).x()
    assert add.mapTo(window, add.rect().topRight()).x() < edit.mapTo(window, QPoint(0, 0)).x()
    assert abs(remote.mapTo(window, remote.rect().center()).y() - add.mapTo(window, add.rect().center()).y()) <= 1
    assert not side.isAncestorOf(add)
    assert side.isAncestorOf(window.settings_panel.connection_status)
    side_right = side.mapTo(window, side.rect().topRight()).x()
    assert side_right < window._channel_scroll.mapTo(window, QPoint(0, 0)).x()
    assert side.width() >= window.settings_panel.side_rail_width_hint()
    window.settings_panel.set_remote_mixer_available(False)
    assert window._add_midi_btn.isVisible()


def test_narrow_window_moves_side_column_below_mixer(layout_window, qtbot):
    window = layout_window
    window._toggle_settings_btn.setChecked(True)
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


@pytest.mark.parametrize("width", [700, 1600])
def test_hidden_settings_reclaims_entire_sidebar_space(layout_window, qtbot, width):
    window = layout_window
    window.resize(width, 800)
    window._toggle_settings_btn.setChecked(False)
    qtbot.wait(10)
    assert window._side_panel.isHidden()
    assert window._mixer_area.geometry() == window._workspace_splitter.rect()
    assert window._channel_scroll.mapTo(window._mixer_area, QPoint(0, 0)).x() == 0
    qtbot.mouseClick(window._channels[1], Qt.MouseButton.LeftButton, pos=QPoint(0, 2))
    qtbot.wait(10)
    assert window._bulk_delete_btn.isVisible()
    assert window._channel_actions.isAncestorOf(window._bulk_delete_btn)
    assert window._mixer_area.geometry() == window._workspace_splitter.rect()
    window._toggle_settings_btn.setChecked(True)
    qtbot.wait(10)
    assert window._side_panel.isVisible()


@pytest.mark.parametrize("font_increase", [0, 6])
def test_mute_icon_shares_percentage_row_and_strikes_muted_value(layout_window, qtbot, monkeypatch, font_increase):
    channel = layout_window._channels[0]
    font = channel.font()
    font.setPointSize(font.pointSize() + font_increase)
    channel.setFont(font)
    channel.set_volume(1.0)
    qtbot.wait(10)
    button = channel._mute_btn
    label = channel._level_label
    assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    assert button.text() == ""
    assert not button.icon().isNull()
    assert button.width() >= 24
    assert button.height() >= 24
    assert abs(button.geometry().center().y() - label.geometry().center().y()) <= 1
    assert button.geometry().right() < label.geometry().left()
    assert channel.rect().contains(label.geometry())
    assert label.width() >= label.minimumSizeHint().width()
    unmuted_icon = button.icon().cacheKey()
    channel.set_mute_state(True)
    assert label.font().strikeOut()
    assert button.accessibleName() == "Unmute channel 1"
    assert button.icon().cacheKey() != unmuted_icon
    assert not channel._slider.isEnabled()
    channel.set_volume(0.42)
    assert label.font().strikeOut()
    assert label.text() == "42 %"
    channel.set_mute_state(False)
    assert not label.font().strikeOut()
    assert button.accessibleName() == "Mute channel 1"
    assert channel._slider.isEnabled()
    toggle_mute = MagicMock()
    monkeypatch.setattr(channel._backend, "toggle_mute", toggle_mute)
    button.click()
    toggle_mute.assert_called_once_with(0)


@pytest.mark.parametrize("width", [700, 1600])
@pytest.mark.parametrize("font_increase", [0, 6])
def test_toolbar_channel_actions_fit_at_narrow_and_large_fonts(layout_window, qtbot, width, font_increase):
    window = layout_window
    font = window.font()
    font.setPointSize(font.pointSize() + font_increase)
    window.setFont(font)
    window.settings_panel.set_remote_mixer_available(True)
    window.resize(width, 900)
    qtbot.wait(10)
    controls = [
        window._target_local_btn, window._target_remote_btn,
        window._add_midi_btn, window._edit_midi_btn,
    ]
    previous_right = -1
    for control in controls:
        assert control.isVisible()
        assert control.width() >= control.minimumSizeHint().width()
        rect = QRect(control.mapTo(window, QPoint(0, 0)), control.size())
        assert window.rect().contains(rect)
        assert rect.left() > previous_right
        previous_right = rect.right()


def test_top_bar_is_left_aligned_with_profile_menu_first(
    tmp_config_path, tmp_profiles_dir, tmp_path, monkeypatch, qtbot
):
    config = _make_midi_config(tmp_config_path, tmp_profiles_dir, 3)
    profile_manager = ProfileManager(profiles_dir=tmp_profiles_dir)
    profile_manager.set_active_silently("profile-1")
    monkeypatch.setattr(
        main_window,
        "QSettings",
        lambda *_args: QSettings(str(tmp_path / "top-bar-gui.ini"), QSettings.Format.IniFormat),
    )
    window = MainWindow(config=config, backend=_LayoutBackend(), profile_manager=profile_manager)
    qtbot.addWidget(window)
    window.resize(1800, 700)
    window.show()
    qtbot.wait(10)

    profile_btn = window._profile_add_btn
    assert profile_btn.text() == "Profile"
    assert not [
        label for label in window.findChildren(main_window.QLabel)
        if label.text() == "Profile" and label.isVisible()
    ]
    controls = [
        window._toggle_settings_btn, profile_btn, window._profile_combo,
        window._add_midi_btn, window._edit_midi_btn,
    ]
    lefts = [control.mapTo(window, QPoint(0, 0)).x() for control in controls]
    assert lefts == sorted(lefts)
    last = window._edit_midi_btn
    assert last.mapTo(window, last.rect().topRight()).x() < window.width() // 2


def test_fader_fill_follows_label_color_and_cap_is_rectangular(layout_window):
    channel = layout_window._channels[0]
    accent = QApplication.palette().color(QPalette.ColorRole.Highlight).name()
    assert f"background: {accent};" in channel._slider.styleSheet()
    assert "border-radius: 1px;" in channel._slider.styleSheet()
    assert "border-radius: 7px" not in channel._slider.styleSheet()
    channel._config.set_channel_background_color(0, "#ff8000")
    channel.refresh()
    assert "background: #ff8000;" in channel._slider.styleSheet()
    channel._config.set_channel_background_color(0, None)
    channel.refresh()
    assert "background: #ff8000;" not in channel._slider.styleSheet()
    assert f"background: {accent};" in channel._slider.styleSheet()


def test_muted_fader_drops_level_fill_but_keeps_borders(layout_window):
    channel = layout_window._channels[0]
    channel._config.set_channel_background_color(0, "#ff8000")
    channel.refresh()
    label_background = channel._ch_label._background_color
    unmuted = channel._slider.styleSheet()
    assert "background: #ff8000;" in unmuted

    channel.set_mute_state(True)
    muted = channel._slider.styleSheet()
    assert "background: #ff8000;" not in muted
    assert "border: 1px solid #aa5500;" in muted
    assert muted.count("border: 1px solid") == unmuted.count("border: 1px solid")
    assert channel._ch_label._background_color == label_background

    channel.set_mute_state(False)
    assert channel._slider.styleSheet() == unmuted


def test_fader_handle_draws_ticks_beside_value(layout_window, qtbot):
    channel = layout_window._channels[0]
    slider = channel._slider
    slider.setValue(50)
    slider.repaint()
    image = slider.grab().toImage()
    ratio = image.devicePixelRatio()
    inner = slider.handle_rect().adjusted(1, 1, -1, -1)
    tick_y = inner.top() + (inner.height() - 1) // 2
    text = slider.palette().color(QPalette.ColorRole.WindowText)
    alpha = main_window.FADER_MINOR_TICK_ALPHA

    def pixel(x: int, y: int = tick_y) -> QColor:
        return image.pixelColor(int((x + 0.5) * ratio), int((y + 0.5) * ratio))

    for x in (inner.left(), inner.right()):
        background = pixel(x, inner.top())
        expected = [
            round(alpha * fg + (1 - alpha) * bg)
            for fg, bg in (
                (text.red(), background.red()),
                (text.green(), background.green()),
                (text.blue(), background.blue()),
            )
        ]
        actual = pixel(x)
        assert [actual.red(), actual.green(), actual.blue()] == pytest.approx(expected, abs=2)
        assert actual != text


def test_bulk_actions_omit_redundant_midi_label(layout_window, qtbot):
    window = layout_window
    qtbot.mouseClick(window._channels[1], Qt.MouseButton.LeftButton, pos=QPoint(0, 2))
    assert window._bulk_delete_btn.text() == "Delete 1 channel"
    assert "MIDI" not in window._bulk_delete_btn.text()


def test_fader_handle_shows_volume_that_fits_at_100(layout_window, monkeypatch):
    slider = layout_window._channels[1]._slider
    slider.setValue(100)
    handle = slider.handle_rect()
    metrics = QFontMetrics(slider.value_font())
    assert metrics.horizontalAdvance("100") <= handle.width() - 4
    assert metrics.capHeight() <= handle.height() - 4
    assert metrics.capHeight() >= 5
    drawn = []
    original = main_window.QPainter.drawText

    def record(painter, *args):
        drawn.append(args)
        return original(painter, *args)

    monkeypatch.setattr(main_window.QPainter, "drawText", record)
    slider.grab()
    labels = [args for args in drawn if args[-1] == "100"]
    assert labels
    point = labels[0][0]
    assert handle.left() + 2 <= point.x()
    assert point.x() + metrics.horizontalAdvance("100") <= handle.right() - 1
    assert handle.top() + 2 <= point.y() - metrics.capHeight()
    assert point.y() <= handle.bottom() - 1


def test_fader_fills_window_height_without_edit_controls(layout_window, qtbot):
    window = layout_window
    window._toggle_settings_btn.setChecked(False)
    window._edit_midi_btn.setChecked(False)
    window.resize(1600, 549)
    qtbot.wait(1)
    channel = window._channels[0]
    slider = channel._slider
    small = slider.height()
    assert small >= main_window.FADER_MIN_HEIGHT
    window.resize(1600, 1400)
    qtbot.wait(1)
    assert slider.height() > 560
    viewport = window._channel_scroll.viewport()
    label_bottom = channel._ch_label.mapTo(viewport, QPoint(0, channel._ch_label.height())).y()
    assert viewport.height() - label_bottom <= 12


def test_fader_scale_gets_denser_with_height(layout_window, qtbot):
    window = layout_window
    window._toggle_settings_btn.setChecked(False)
    window._edit_midi_btn.setChecked(False)
    scale = window._channels[0]._fader_scale
    window.resize(1600, 420)
    qtbot.wait(1)
    small_step, _minor = scale.marks()
    window.resize(1600, 1000)
    qtbot.wait(1)
    assert scale.marks() == (5, 1)
    window.resize(1600, 2400)
    qtbot.wait(1)
    assert scale.marks() == (1, 0)
    assert small_step > 5
    slider = window._channels[0]._slider
    assert slider.handle_center_y(100) < slider.handle_center_y(0)


def test_toolbar_mixer_target_switch_follows_remote_availability(layout_window, qtbot):
    window = layout_window
    panel = window.settings_panel
    assert window._target_switch.isHidden()
    panel.set_remote_mixer_available(True)
    assert not window._target_switch.isHidden()
    window._target_remote_btn.click()
    assert panel.mixer_target == "remote"
    assert window.settings.value("mixer/target") == "remote"
    window._target_local_btn.click()
    assert window._target_local_btn.isChecked()
    assert window.settings.value("mixer/target") == "local"
    window._target_remote_btn.click()
    panel.set_remote_mixer_available(False)
    assert window._target_switch.isHidden()
    assert window._target_local_btn.isChecked()
    assert panel.mixer_target == "local"
    assert window.settings.value("mixer/target") == "remote"
    panel.set_remote_mixer_available(True)
    assert window._target_remote_btn.isChecked()
    assert panel.mixer_target == "remote"


def test_empty_mixer_toolbar_group_hides_in_compact_mode(layout_window):
    window = layout_window
    window._compact_btn.setChecked(True)
    assert window._mixer_controls.isHidden()
    window.settings_panel.set_remote_mixer_available(True)
    assert window._mixer_controls.isVisible()
    assert window._add_midi_btn.isHidden()
    window.settings_panel.set_remote_mixer_available(False)
    assert window._mixer_controls.isHidden()
    window._compact_btn.setChecked(False)
    assert window._mixer_controls.isVisible()


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


def test_side_rail_groups_follow_input_mode_and_ignore_compact_mode(layout_window, qtbot):
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
    assert group.body.isVisible()
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


def test_sidebar_default_preferred_width_is_240(layout_window, qtbot):
    window = layout_window
    assert not window.settings.contains("sidebar/width")
    assert window._sidebar_width == max(240, window.settings_panel.side_rail_width_hint())
    window.resize(1300, 800)
    minimum = window.settings_panel.side_rail_width_hint()
    assert minimum <= 240
    window._workspace_splitter.moveSplitter(240, 1)
    qtbot.wait(10)
    assert window._side_panel.width() == 240
    assert window.settings.value("sidebar/width", type=int) == 240


def test_input_mode_round_trip_preserves_inactive_channel_mappings(layout_window):
    window = layout_window
    config = window._config
    config._data["hardware"]["num_channels"] = 5
    config.set_app_names(7, ["Spotify"])
    config.set_app_names(12, ["Discord"])

    config.input_mode = "usb"
    window.refresh_layout()
    assert config.get_app_names(7) == ["Spotify"]
    assert config.find_channel_for_app("Spotify") is None
    assert config.find_channels_for_app("Discord") == []

    config.input_mode = "midi_only"
    window.refresh_layout()
    assert config.get_app_names(12) == ["Discord"]
    assert config.find_channel_for_app("Spotify") == 7
    assert config.find_channels_for_app("Discord") == [12]


def test_crossfader_control_confirms_before_clearing_assignments(
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
    config.set_app_names(1, ["Spotify"])
    reply = [main_window.QMessageBox.StandardButton.Cancel]
    monkeypatch.setattr(main_window.QMessageBox, "warning", lambda *_args: reply[0])

    window._set_crossfader_control(1)
    assert config.get_crossfader_usb_channel_index() is None
    assert config.get_app_names(1) == ["Spotify"]

    reply[0] = main_window.QMessageBox.StandardButton.Ok
    window._set_crossfader_control(1)
    assert config.get_crossfader_usb_channel_index() == 1
    assert config.get_app_names(1) == []
