from __future__ import annotations

import uuid

import nativmix.hardware.midi as midi
from nativmix.gui.settings_panel import SettingsPanel
from nativmix.utils.config_manager import ConfigManager


def _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot) -> tuple[SettingsPanel, ConfigManager]:
    monkeypatch.setattr(midi, "ensure_midi_backend", lambda: "rtmidi")
    monkeypatch.setattr(midi.mido, "get_input_names", lambda: ["Controller"])
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    config.input_mode = "midi_only"
    panel = SettingsPanel(config)
    qtbot.addWidget(panel)
    return panel, config


def test_usb_blank_sender_is_blocked_until_mode_and_physical_controller_are_selected(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    monkeypatch.setattr(midi, "ensure_midi_backend", lambda: "rtmidi")
    monkeypatch.setattr(midi.mido, "get_input_names", lambda: ["ROTO-CONTROL MIDI 1"])
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    panel = SettingsPanel(config)
    qtbot.addWidget(panel)

    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("send"))

    assert config.remote_midi_role == "send"
    assert not panel._midi_box.isEnabled()
    assert panel._midi_box.findData("ROTO-CONTROL MIDI 1") >= 0
    assert panel._remote_midi_status_label.text() == (
        "Remote Send blocked: set Input Mode to USB + MIDI or MIDI Only."
    )

    panel._input_mode_box.setCurrentIndex(2)

    assert config.input_mode == "midi_only"
    assert panel._midi_box.isEnabled()
    assert panel._remote_midi_status_label.text() == (
        "Remote Send blocked: select a physical MIDI controller in MIDI Hardware."
    )

    panel._midi_box.setCurrentIndex(panel._midi_box.findData("ROTO-CONTROL MIDI 1"))

    assert config.midi_device == "ROTO-CONTROL MIDI 1"
    assert panel._remote_midi_status_label.text() == "Starting Remote Send; waiting for a desktop..."


def test_remote_role_views_are_explicit_and_receive_disables_local_midi(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)

    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("send"))
    assert config.remote_midi_role == "send"
    assert not panel._remote_midi_send_row.isHidden()
    assert panel._remote_midi_receive_row.isHidden()
    assert panel._midi_box.isEnabled()
    virtual_index = panel._midi_box.findData("VIRTUAL_PORT")
    assert virtual_index >= 0
    assert not panel._midi_box.model().item(virtual_index).isEnabled()
    assert "physical MIDI controller" in panel._remote_midi_status_label.text()

    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("receive"))
    assert config.remote_midi_role == "receive"
    assert not panel._remote_midi_receive_row.isHidden()
    assert not panel._midi_box.isEnabled()
    assert "not encrypted or authenticated" in panel._remote_midi_warning.text()


def test_receive_connect_persists_only_explicitly_selected_peer(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)
    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("receive"))

    peer_id = str(uuid.uuid4())
    peers = [{"id": peer_id, "name": "Studio Laptop", "host": "laptop.local"}]
    panel.apply_remote_midi_state(1, "receive", "connecting", "Choose a laptop", peers, "", "")

    assert config.remote_midi_peer_id == ""
    panel._on_remote_midi_connect_clicked()
    assert config.remote_midi_peer_id == peer_id
    assert config.remote_midi_peer_name == "Studio Laptop"


def test_remote_state_rejects_stale_generation_and_wrong_role(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)
    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("receive"))

    panel.apply_remote_midi_state(4, "receive", "connecting", "Current", [], "", "")
    panel.apply_remote_midi_state(3, "receive", "error_critical", "Stale", [], "", "")
    panel.apply_remote_midi_state(5, "send", "stable", "Wrong role", [], "", "")

    assert config.remote_midi_role == "receive"
    assert panel._remote_midi_status_label.text() == "Current"


def test_remote_name_is_debounced_before_persistence(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)
    original = config.remote_midi_name

    panel._remote_midi_name_edit.setText("Laptop Controller")
    assert config.remote_midi_name == original
    qtbot.wait(550)

    assert config.remote_midi_name == "Laptop Controller"


def test_remote_sleep_preference_and_authoritative_status(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)

    assert panel._prevent_remote_sleep_cb.isChecked()
    panel._prevent_remote_sleep_cb.setChecked(False)
    assert config.prevent_remote_sleep is False

    panel.apply_sleep_inhibitor_status("unavailable", "Desktop portal is unavailable.")
    assert panel._sleep_inhibitor_status_label.text() == "Sleep prevention: Unavailable"
    assert panel._sleep_inhibitor_status_label.toolTip() == "Desktop portal is unavailable."


def test_remote_mixer_permission_is_receive_only_and_persistent(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)

    assert not panel._allow_remote_mixer_editing_cb.isChecked()
    assert not panel._allow_remote_mixer_editing_cb.isEnabled()

    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("receive"))
    assert panel._allow_remote_mixer_editing_cb.isEnabled()
    assert "unencrypted, unauthenticated" in panel._remote_mixer_warning.text()

    panel._allow_remote_mixer_editing_cb.setChecked(True)
    assert config.allow_remote_mixer_editing is True

    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("send"))
    assert not panel._allow_remote_mixer_editing_cb.isEnabled()
    assert config.allow_remote_mixer_editing is True


def test_remote_sync_status_is_distinct_and_generation_guarded(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, _config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)

    panel.apply_remote_sync_status(3, "Connected", "Current control session")
    panel.apply_remote_sync_status(2, "Conflict", "Stale update")

    assert panel._remote_sync_status_label.text() == "Mixer sync: Connected"
    assert panel._remote_sync_status_label.toolTip() == "Current control session"
