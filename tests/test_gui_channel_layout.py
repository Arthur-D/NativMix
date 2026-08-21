"""GUI geometry regressions for crowded mixer channel layouts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PyQt6.QtCore import QPoint, QSettings, pyqtSignal

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import make_profile, write_profile

from nativmix.audio.base import AudioBackendBase
from nativmix.gui import main_window
from nativmix.gui.main_window import ChannelWidget, MainWindow, _AppRow
from nativmix.utils.config_manager import ConfigManager


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
    monkeypatch.setattr(
        main_window,
        "QSettings",
        lambda *_args: QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat),
    )
    window = MainWindow(config=config, backend=_LayoutBackend())
    qtbot.addWidget(window)
    window.finalize_ui()
    window.resize(1920, 549)
    window.show()
    window._toggle_settings_btn.setChecked(True)
    window._edit_midi_btn.setChecked(True)
    qtbot.wait(1)
    return window


def test_channel_minimum_width_honors_native_control_hints(
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

    assert channel.minimumWidth() >= channel._learn_btn.minimumWidth()
    assert channel.minimumWidth() >= channel._mute_learn_btn.minimumWidth()
    assert channel.minimumWidth() >= channel._remove_midi_btn.minimumSizeHint().width()
    assert channel.width() >= channel.minimumWidth()


def test_eighteen_channels_scroll_horizontally_without_compressing(layout_window, qtbot):
    window = layout_window
    qtbot.waitUntil(lambda: window._channel_scroll.horizontalScrollBar().maximum() > 0)

    assert window._channel_container.minimumWidth() > window._channel_scroll.viewport().width()
    assert all(channel.width() >= channel.minimumWidth() for channel in window._channels)
    assert window._channel_scroll.horizontalScrollBar().maximum() > 0


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
    assert channel._learn_btn.text() == "M16/CC127"
    assert channel._mute_learn_btn.text() == "M16/CC127"
    for button in buttons:
        assert button.width() >= button.minimumSizeHint().width()
        assert channel.contentsRect().contains(button.geometry())
    assert not buttons[0].geometry().intersects(buttons[1].geometry())
    assert not buttons[1].geometry().intersects(buttons[2].geometry())


def test_compact_edit_toggles_restore_valid_width_constraints(layout_window, qtbot):
    window = layout_window
    channel = window._channels[0]
    normal_bounds = (channel.minimumWidth(), channel.maximumWidth())

    window._compact_btn.setChecked(True)
    qtbot.wait(1)
    assert channel.minimumWidth() == channel.maximumWidth()
    assert not channel._learn_btn.isVisible()

    window._compact_btn.setChecked(False)
    window._edit_midi_btn.setChecked(True)
    qtbot.wait(1)
    assert (channel.minimumWidth(), channel.maximumWidth()) == normal_bounds
    assert channel._learn_btn.isVisible()
    assert channel.minimumWidth() <= channel.width() <= channel.maximumWidth()


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
