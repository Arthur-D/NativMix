from __future__ import annotations

import uuid

import pytest
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QLabel

import nativmix.hardware.midi as midi
from nativmix.gui.settings_panel import SettingsPanel, _palette_contrast_ratio
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
    assert panel._remote_sync_status_label.fullText() == "Mixer sync: Unavailable"
    assert "set Input Mode" in panel._remote_sync_status_label.toolTip()

    panel._input_mode_box.setCurrentIndex(2)

    assert config.input_mode == "midi_only"
    assert panel._midi_box.isEnabled()
    assert panel._remote_midi_status_label.text() == (
        "Remote Send blocked: select a physical MIDI controller in MIDI Hardware."
    )
    assert panel._remote_sync_status_label.fullText() == "Mixer sync: Unavailable"
    assert "select a physical MIDI controller" in panel._remote_sync_status_label.toolTip()

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
    assert "unencrypted and unauthenticated" in panel._remote_midi_role_box.toolTip()


def test_send_role_preserves_and_exposes_local_feedback_preference(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)
    assert not config.midi_fader_feedback

    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("send"))

    assert panel._midi_fader_feedback_cb.isEnabled()
    assert not panel._midi_fader_feedback_cb.isChecked()
    assert not config.midi_fader_feedback

    panel._midi_fader_feedback_cb.setChecked(True)
    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("off"))
    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("send"))

    assert panel._midi_fader_feedback_cb.isEnabled()
    assert panel._midi_fader_feedback_cb.isChecked()
    assert config.midi_fader_feedback


def test_receive_role_preserves_and_exposes_feedback_preference(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)
    config.midi_fader_feedback = True
    panel._update_hardware_ui_state()

    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("receive"))

    assert panel._midi_fader_feedback_cb.isEnabled()
    assert panel._midi_fader_feedback_cb.isChecked()
    panel._midi_fader_feedback_cb.setChecked(False)
    assert not config.midi_fader_feedback

    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("off"))
    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("receive"))
    assert panel._midi_fader_feedback_cb.isEnabled()
    assert not panel._midi_fader_feedback_cb.isChecked()


def test_direct_sync_state_transitions_replace_accessible_description(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, _config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)
    panel.apply_remote_sync_status(1, "Connected", "Previous connected details")

    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("send"))
    panel._midi_box.setCurrentIndex(panel._midi_box.findData("Controller"))

    assert panel._remote_sync_status_label.fullText() == "Mixer sync: Waiting for receiver"
    assert panel._remote_sync_status_label.accessibleDescription() == "Mixer sync: Waiting for receiver"

    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("receive"))

    assert panel._remote_sync_status_label.fullText() == "Mixer sync: Permission disabled"
    assert panel._remote_sync_status_label.accessibleDescription() == "Mixer sync: Permission disabled"


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
    assert panel._remote_midi_connect_btn.accessibleName() == "Disconnect remote controller"
    assert "Disconnect" in panel._remote_midi_connect_btn.accessibleDescription()
    assert "Disconnect" in panel._remote_midi_connect_btn.toolTip()

    panel._on_remote_midi_connect_clicked()
    assert panel._remote_midi_connect_btn.accessibleName() == "Connect remote controller"
    assert "Connect" in panel._remote_midi_connect_btn.accessibleDescription()
    assert "Connect" in panel._remote_midi_connect_btn.toolTip()


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
    assert "unencrypted and unauthenticated" in panel._allow_remote_mixer_editing_cb.toolTip()
    assert "observe or spoof" in panel._allow_remote_mixer_editing_cb.toolTip()
    assert "No Internet support" in panel._allow_remote_mixer_editing_cb.toolTip()

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


@pytest.mark.parametrize(
    "status",
    [
        "Permission disabled",
        "Syncing",
        "Reconnecting",
        "Connected",
        "Unavailable",
        "Version incompatible",
    ],
)
def test_remote_sync_row_renders_every_lifecycle_state(
    status,
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, _config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)
    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("receive"))

    panel.apply_remote_sync_status(3, status, f"{status} details")

    assert panel._remote_sync_status_label.fullText() == f"Mixer sync: {status}"
    assert panel._remote_sync_status_label.toolTip() == f"{status} details"
    assert panel._remote_sync_status_label.accessibleName() == f"Mixer sync: {status}"
    assert panel._remote_sync_status_label.accessibleDescription() == f"{status} details"
    assert panel._remote_sync_status_label.parentWidget() is panel._remote_midi_action_row


def test_mixer_sync_status_is_visible_on_send_action_row_without_duplicate(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, _config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)
    panel._midi_box.setCurrentIndex(panel._midi_box.findData("Controller"))
    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("send"))
    panel.resize(760, 549)
    panel.show()
    qtbot.wait(1)

    name_y = panel._remote_midi_name_edit.mapTo(panel, panel._remote_midi_name_edit.rect().center()).y()
    sync_y = panel._remote_sync_status_label.mapTo(panel, panel._remote_sync_status_label.rect().center()).y()
    assert abs(name_y - sync_y) <= 2
    assert panel._remote_sync_status_label.isVisible()
    assert panel._remote_sync_status_label.fullText() == "Mixer sync: Waiting for receiver"
    assert not panel._remote_midi_status_label.isVisible()
    visible_sync_labels = [
        label
        for label in panel.findChildren(QLabel)
        if label.isVisible()
        and (
            (hasattr(label, "fullText") and label.fullText().startswith("Mixer sync:"))
            or label.text().startswith("Mixer sync:")
        )
    ]
    assert visible_sync_labels == [panel._remote_sync_status_label]


def test_remote_layout_is_compact_elided_and_has_no_visible_warning_label(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, _config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)
    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("receive"))
    peer_id = str(uuid.uuid4())
    panel.apply_remote_midi_state(
        2,
        "receive",
        "connecting",
        "Connecting",
        [
            {
                "id": peer_id,
                "name": "A laptop sender name long enough to require narrow-window elision",
                "host": "192.0.2.40",
                "controller_name": "Roto-Control MIDI 1 with a long descriptive suffix",
            }
        ],
        "",
        "",
    )
    panel.apply_remote_sync_status(
        2,
        "Reconnecting",
        "Remote mixer endpoint is reconnecting after a refused connection.",
    )
    panel.resize(760, 549)
    panel.show()
    qtbot.wait(1)

    row_y = panel._remote_midi_peer_box.mapTo(panel, panel._remote_midi_peer_box.rect().center()).y()
    sync_y = panel._remote_sync_status_label.mapTo(panel, panel._remote_sync_status_label.rect().center()).y()
    assert abs(row_y - sync_y) <= 2
    assert panel._remote_sync_status_label.fullText() == "Mixer sync: Reconnecting"
    assert not panel._remote_midi_status_label.isVisible()
    visible_warnings = [
        label
        for label in panel.findChildren(QLabel)
        if label.isVisible() and "trusted local network" in label.text().casefold()
    ]
    assert visible_warnings == []

    panel.resize(1920, 549)
    qtbot.wait(1)
    assert panel.height() <= 549


def test_controller_name_fallback_live_update_and_stale_generation(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, _config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)
    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("receive"))
    peer_id = str(uuid.uuid4())

    panel.apply_remote_midi_state(
        5,
        "receive",
        "stable",
        "Ready",
        [{"id": peer_id, "name": "Laptop", "host": "192.0.2.10", "controller_name": ""}],
        "",
        "",
    )
    assert "Remote controller" in panel._remote_midi_peer_box.itemText(0)

    panel.apply_remote_midi_state(
        7,
        "receive",
        "stable",
        "Ready",
        [{"id": peer_id, "name": "Laptop", "host": "192.0.2.10", "controller_name": "Roto-Control MIDI 1"}],
        "",
        "",
    )
    panel.apply_remote_midi_state(
        6,
        "receive",
        "stable",
        "Stale",
        [{"id": peer_id, "name": "Laptop", "host": "192.0.2.10", "controller_name": "Old Controller"}],
        "",
        "",
    )
    assert "Roto-Control MIDI 1" in panel._remote_midi_peer_box.itemText(0)
    assert "Old Controller" not in panel._remote_midi_peer_box.itemText(0)


def test_remote_status_palette_has_light_and_dark_contrast(
    tmp_config_path,
    tmp_profiles_dir,
    monkeypatch,
    qtbot,
) -> None:
    panel, _config = _remote_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot)
    panel._remote_midi_role_box.setCurrentIndex(panel._remote_midi_role_box.findData("receive"))
    for background, text, link in (
        (QColor("#f7f7f7"), QColor("#202020"), QColor("#0057ae")),
        (QColor("#242424"), QColor("#f0f0f0"), QColor("#80bfff")),
    ):
        palette = panel.palette()
        palette.setColor(QPalette.ColorRole.Window, background)
        palette.setColor(QPalette.ColorRole.WindowText, text)
        palette.setColor(QPalette.ColorRole.Link, link)
        panel.setPalette(palette)
        panel._remote_sync_status_label.setPalette(palette)
        panel.apply_remote_sync_status(20, "Unavailable", "TCP 5006 refused")
        foreground = panel._remote_sync_status_label.palette().color(QPalette.ColorRole.WindowText)
        assert _palette_contrast_ratio(foreground, background) >= 4.5
        assert panel._remote_sync_status_label.styleSheet() == ""
