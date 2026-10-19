from __future__ import annotations

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QPushButton, QSlider

import nativmix.hardware.midi as midi
from nativmix.gui import settings_panel
from nativmix.gui.settings_panel import SettingsPanel
from nativmix.utils.config_manager import ConfigManager


@pytest.fixture
def sidebar(tmp_config_path, tmp_profiles_dir, tmp_path, monkeypatch, qtbot):
    monkeypatch.setattr(settings_panel, "_real_ports", lambda: [])
    monkeypatch.setattr(settings_panel, "_systemd_unit_available", lambda: False)
    monkeypatch.setattr(settings_panel, "_is_autostart_enabled", lambda: False)
    monkeypatch.setattr(settings_panel, "update_checks_supported", lambda: True)
    monkeypatch.setattr(midi, "ensure_midi_backend", lambda: "rtmidi")
    monkeypatch.setattr(midi.mido, "get_input_names", lambda: ["Test Controller"])
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    preferences = QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat)
    panel = SettingsPanel(config, gui_settings=preferences)
    qtbot.addWidget(panel)
    panel.resize(panel.side_rail_width_hint(), 700)
    panel.show()
    return panel, config, preferences


def test_every_settings_control_has_exactly_one_sidebar_group(sidebar):
    panel, _config, _preferences = sidebar
    assert list(panel.sidebar_groups) == ["audio", "controllers", "profiles", "preferences", "remote", "debug"]
    assert [g.objectName() for g in panel.connection_status.groups] == [
        f"sidebar_{key}" for key in ("preferences", "audio", "controllers", "debug", "profiles", "remote")
    ]
    titles = [group.title() for group in panel.connection_status.groups]
    assert titles == sorted(titles, key=str.casefold)
    for widget_type in (QCheckBox, QComboBox, QLineEdit, QPushButton, QSlider):
        for control in panel.findChildren(widget_type):
            assert sum(group.body.isAncestorOf(control) for group in panel.sidebar_groups.values()) == 1


def test_sidebar_headings_preserve_visible_ampersands(sidebar):
    panel, _config, _preferences = sidebar
    assert panel._profile_group._toggle.text() == "Profile MIDI"
    toggle = panel._preferences_group._toggle
    assert toggle.text() == "Appearance && behavior"
    assert toggle.accessibleName() == "Appearance & behavior"


@pytest.mark.parametrize("target", ["local", "remote"])
def test_mixer_preference_restores_after_restart_and_remote_reconnect(sidebar, qtbot, target):
    panel, config, preferences = sidebar
    panel.set_remote_mixer_available(True)
    panel.select_mixer_target("remote")
    panel.select_mixer_target(target)
    panel.set_remote_mixer_available(False)
    assert panel.mixer_target == "local"
    assert preferences.value("mixer/target") == target
    preferences.sync()
    panel.close()

    restored = SettingsPanel(
        config, gui_settings=QSettings(preferences.fileName(), QSettings.Format.IniFormat)
    )
    qtbot.addWidget(restored)
    changes = QSignalSpy(restored.mixer_target_changed)
    assert restored.mixer_target == "local"
    restored.set_remote_mixer_available(True)
    assert restored.mixer_target == target
    assert list(changes) == ([["remote"]] if target == "remote" else [])
    restored.set_remote_mixer_available(False)
    assert restored.mixer_target == "local"
    restored.set_remote_mixer_available(True)
    assert restored.mixer_target == target


@pytest.mark.parametrize("key", ["audio", "controllers", "remote", "profiles", "preferences", "debug"])
def test_disclosure_preferences_restore_on_new_panel(sidebar, qtbot, key):
    panel, config, preferences = sidebar
    group = panel.sidebar_groups[key]
    assert not group.is_expanded()
    group._toggle.click()
    assert group.is_expanded()
    preferences.sync()
    panel.close()

    restored_settings = QSettings(preferences.fileName(), QSettings.Format.IniFormat)
    restored = SettingsPanel(config, gui_settings=restored_settings)
    qtbot.addWidget(restored)
    assert restored.sidebar_groups[key].is_expanded()
    assert all(not g.is_expanded() for name, g in restored.sidebar_groups.items() if name != key)
    restored.sidebar_groups[key].set_expanded(False)
    restored_settings.sync()
    again = SettingsPanel(config, gui_settings=QSettings(preferences.fileName(), QSettings.Format.IniFormat))
    qtbot.addWidget(again)
    assert not again.sidebar_groups[key].is_expanded()


def test_input_mode_changes_preserve_disclosure_preferences(sidebar):
    panel, _config, preferences = sidebar
    panel._rail_device_group.set_expanded(True)
    panel._remote_midi_group.set_expanded(True)
    panel._input_mode_box.setCurrentIndex(2)
    assert panel._rail_device_group.is_expanded()
    assert panel._remote_midi_group.is_expanded()
    assert preferences.value("sidebar/groups/controllers", False, type=bool)
    assert preferences.value("sidebar/groups/remote", False, type=bool)
    assert panel._rail_device_group.body.isVisible()


@pytest.mark.parametrize("font_increase", [0, 6])
def test_resizing_keeps_expanded_controls_inside_sidebar(sidebar, qtbot, font_increase):
    panel, _config, _preferences = sidebar
    font = panel.font()
    font.setPointSize(font.pointSize() + font_increase)
    panel.setFont(font)
    for group in panel.sidebar_groups.values():
        group.set_expanded(True)
    panel._remote_midi_send_cb.setChecked(True)
    panel._remote_midi_receive_cb.setChecked(True)
    panel._input_mode_box.setCurrentIndex(1)

    minimum_width = panel.side_rail_width_hint()
    for width in (minimum_width, 560, minimum_width):
        panel.resize(max(width, panel.side_rail_width_hint()), 700)
        qtbot.wait(10)
        rail = panel.connection_status
        assert rail.horizontalScrollBar().maximum() == 0
        assert rail.widget().width() <= rail.viewport().width()
        for control in (
            panel._input_mode_box, panel._port_box, panel._midi_box, panel._master_box,
            panel._remote_midi_connect_btn, panel._profile_next_learn_btn, panel._autostart_btn,
        ):
            assert control.width() >= control.minimumSizeHint().width()
            assert rail.widget().rect().contains(control.mapTo(rail.widget(), control.rect().bottomRight()))


def test_sidebar_minimum_is_content_sized_not_a_fixed_280_pixels(sidebar, qtbot):
    panel, _config, _preferences = sidebar
    font = panel.font()
    font.setPointSize(9)
    panel.setFont(font)
    for group in panel.sidebar_groups.values():
        group.set_expanded(True)
    panel.resize(panel.side_rail_width_hint(), 500)
    qtbot.wait(10)
    assert panel.width() <= 260
    assert panel.connection_status.verticalScrollBar().maximum() > 0
    assert panel.connection_status.horizontalScrollBar().maximum() == 0
    assert panel.connection_status.widget().width() <= panel.connection_status.viewport().width()


def test_resizing_preserves_state_and_single_debounced_connection(sidebar, qtbot):
    panel, config, _preferences = sidebar
    panel._rail_device_group.set_expanded(True)
    panel._remote_midi_group.set_expanded(True)
    port_box = panel._port_box
    receive_cb = panel._remote_midi_receive_cb
    port_spy = QSignalSpy(panel.port_changed)
    receive_cb.setChecked(True)
    for width in (560, 320, 560):
        panel.resize(width, 700)
    assert panel._port_box is port_box
    assert panel._remote_midi_receive_cb is receive_cb
    assert config.remote_midi_receive_enabled
    port_box.setEditText("/dev/test-controller")
    qtbot.wait(550)
    assert len(port_spy) == 1
    assert port_spy[0][0] == "/dev/test-controller"
