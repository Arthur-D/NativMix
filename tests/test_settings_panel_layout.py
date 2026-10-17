"""Settings panel: fork responsive controls and upstream crossfader settings."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_settings_panel_preserves_responsive_layout_and_crossfader_controls(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from nativmix.gui.settings_panel import SettingsPanel
    from nativmix.utils.config_manager import ConfigManager

    cfg = ConfigManager()
    panel = SettingsPanel(cfg)
    assert panel._preferences_group.body.isAncestorOf(panel._crossfader_cb)
    assert panel.connection_status is not None
    assert not panel._crossfader_cb.isChecked()
    seen = []
    panel.crossfader_enabled_changed.connect(seen.append)
    panel._crossfader_cb.setChecked(True)
    assert seen == [True]
    panel.set_crossfader_enabled(False)
    assert seen == [True]
    panel._midi_edit_controls_cb.setChecked(False)
    assert not cfg.show_midi_edit_controls


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
