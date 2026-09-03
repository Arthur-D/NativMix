from __future__ import annotations

from itertools import combinations

from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication, QWidget

import nativmix.hardware.midi as midi
from nativmix.gui import settings_panel
from nativmix.gui.settings_panel import SettingsPanel, _ResponsiveFlow
from nativmix.utils.config_manager import ConfigManager


def _activate(panel: SettingsPanel, width: int) -> None:
    panel.resize(width, 1200)
    panel.layout().activate()
    for flow in panel.findChildren(_ResponsiveFlow):
        flow.layout().activate()
    QApplication.processEvents()


def _visible_flow_widgets(flow: _ResponsiveFlow):
    return [
        item.widget()
        for index in range(flow.layout().count())
        if (item := flow.layout().itemAt(index)) is not None
        and item.widget() is not None
        and not item.widget().isHidden()
    ]


def _assert_flow_geometry_is_valid(flow: _ResponsiveFlow) -> None:
    widgets = _visible_flow_widgets(flow)
    assert all(flow.contentsRect().contains(widget.geometry()) for widget in widgets)
    assert all(not first.geometry().intersects(second.geometry()) for first, second in combinations(widgets, 2))
    for widget in widgets:
        direct_children = [child for child in widget.findChildren(QWidget) if child.parent() is widget]
        assert all(
            widget.contentsRect().contains(child.geometry())
            for child in direct_children
            if not child.isHidden()
        )


def _make_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot) -> SettingsPanel:
    monkeypatch.setattr(settings_panel, "_real_ports", lambda: [])
    monkeypatch.setattr(settings_panel, "_systemd_unit_available", lambda: False)
    monkeypatch.setattr(settings_panel, "_is_autostart_enabled", lambda: False)
    monkeypatch.setattr(settings_panel, "update_checks_supported", lambda: True)
    monkeypatch.setattr(midi, "ensure_midi_backend", lambda: "rtmidi")
    monkeypatch.setattr(midi.mido, "get_input_names", lambda: ["Test Controller"])
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    panel = SettingsPanel(config)
    qtbot.addWidget(panel)
    panel.show()
    return panel


def test_wide_settings_groups_reduce_height_and_narrow_groups_stack(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel = _make_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)

    _activate(panel, 1500)
    wide_height = panel.layout().heightForWidth(1500)
    hardware_widgets = _visible_flow_widgets(panel._hardware_flow)
    assert hardware_widgets[0].geometry().top() == hardware_widgets[1].geometry().top()
    _assert_flow_geometry_is_valid(panel._hardware_flow)

    _activate(panel, 560)
    narrow_height = panel.layout().heightForWidth(560)
    hardware_widgets = _visible_flow_widgets(panel._hardware_flow)
    assert hardware_widgets[0].geometry().top() < hardware_widgets[1].geometry().top()
    assert narrow_height - wide_height >= 75
    for flow in panel.findChildren(_ResponsiveFlow):
        _assert_flow_geometry_is_valid(flow)

    large_font = panel.font()
    large_font.setPointSize(large_font.pointSize() + 6)
    panel.setFont(large_font)
    _activate(panel, 760)
    for flow in panel.findChildren(_ResponsiveFlow):
        _assert_flow_geometry_is_valid(flow)


def test_resizing_preserves_controls_state_and_single_debounced_connection(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel = _make_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)
    port_box = panel._port_box
    role_box = panel._remote_midi_role_box
    port_spy = QSignalSpy(panel.port_changed)

    role_box.setCurrentIndex(role_box.findData("receive"))
    for width in (1500, 560, 1500):
        _activate(panel, width)
        _assert_flow_geometry_is_valid(panel._remote_midi_action_row)

    assert panel._port_box is port_box
    assert panel._remote_midi_role_box is role_box
    assert role_box.currentData() == "receive"

    port_box.setEditText("/dev/test-controller")
    qtbot.wait(550)
    assert len(port_spy) == 1
    assert port_spy[0][0] == "/dev/test-controller"
