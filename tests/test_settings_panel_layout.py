"""Settings panel: scroll shell + two-column toggle checkboxes."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QCheckBox, QGridLayout, QScrollArea  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_settings_panel_has_scroll_and_two_column_toggles(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from nativmix.gui.settings_panel import SETTINGS_PANEL_MIN_HEIGHT, SettingsPanel
    from nativmix.utils.config_manager import ConfigManager

    cfg = ConfigManager()
    panel = SettingsPanel(cfg)
    assert isinstance(panel._scroll, QScrollArea)
    assert panel._scroll.widget() is panel._content
    assert panel.minimumSizeHint().height() >= SETTINGS_PANEL_MIN_HEIGHT
    assert panel.minimumHeight() >= SETTINGS_PANEL_MIN_HEIGHT

    grids = panel._content.findChildren(QGridLayout)
    assert grids, "expected a QGridLayout for toggle checkboxes"
    toggle_grid = grids[0]
    boxes = [
        toggle_grid.itemAt(i).widget()
        for i in range(toggle_grid.count())
        if toggle_grid.itemAt(i) and isinstance(toggle_grid.itemAt(i).widget(), QCheckBox)
    ]
    assert len(boxes) >= 4
    # Two-column placement: first two items on row 0, next on row 1.
    assert toggle_grid.getItemPosition(0)[0] == 0
    assert toggle_grid.getItemPosition(1)[0] == 0
    assert toggle_grid.getItemPosition(2)[0] == 1


def test_populate_master_outputs_placeholder_when_empty(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from nativmix.gui.settings_panel import SettingsPanel
    from nativmix.utils.config_manager import ConfigManager

    panel = SettingsPanel(ConfigManager())
    panel.populate_master_outputs([], None)
    assert panel._master_box.count() == 1
    assert panel._master_box.itemData(0) is None

    panel.populate_master_outputs([("Speakers", "alsa_output.pci")], "alsa_output.pci")
    assert panel._master_box.count() == 1
    assert panel._master_box.itemData(0) == "alsa_output.pci"
    assert panel._master_box.currentIndex() == 0


def test_collect_real_sinks_filters_nativmix_and_dummy():
    from nativmix.audio.manager import PipeWireManager

    class _S:
        def __init__(self, name: str, description: str | None = None):
            self.name = name
            self.description = description

    class _Pulse:
        def sink_list(self):
            return [
                _S("NativMix_CH_0", "V-Sink"),
                _S("alsa_output.pci", "Speakers"),
                _S("auto_null"),
                _S("easyeffects_sink", "Easy Effects"),
                _S("foo.dummy.bar"),
            ]

    sinks = PipeWireManager._collect_real_sinks(_Pulse())
    names = [n for _, n in sinks]
    assert names == ["alsa_output.pci", "easyeffects_sink"]
    assert sinks[0][0] == "Speakers"
