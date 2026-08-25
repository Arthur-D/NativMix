import json

import pytest

import nativmix.hardware.midi as midi
from nativmix.utils.midi_ports import match_midi_port, midi_device_key, normalize_midi_device_name

QUALIFIED_PORT = "Roto-Control:Roto-Control MIDI 1 16:0"
SAVED_PORT = "Roto-Control MIDI 1"


def _write_midi_config(path, device: str = SAVED_PORT) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 7,
                "hardware": {
                    "port": None,
                    "auto_search_device": True,
                    "num_channels": 5,
                    "input_mode": "midi_only",
                    "midi_device": device,
                    "midi_channel_count": 5,
                    "baud_rate": 9600,
                },
                "settings": {
                    "threshold": 0.01,
                    "transparency": False,
                    "compact_mode": False,
                    "stay_open": False,
                    "show_invert_option": False,
                    "debug_logging": False,
                    "midi_fader_feedback": False,
                },
            }
        )
    )


@pytest.fixture
def midi_panel(tmp_config_path, tmp_profiles_dir, monkeypatch, qtbot):
    from nativmix.gui.settings_panel import SettingsPanel
    from nativmix.utils.config_manager import ConfigManager

    _write_midi_config(tmp_config_path)
    monkeypatch.setattr(midi, "ensure_midi_backend", lambda: "rtmidi")
    monkeypatch.setattr(midi.mido, "get_input_names", lambda: [])
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    panel = SettingsPanel(config)
    qtbot.addWidget(panel)
    return panel, config


def _selected_midi_items(panel) -> list[tuple[str, str]]:
    return [
        (panel._midi_box.itemText(index), panel._midi_box.itemData(index))
        for index in range(panel._midi_box.count())
        if panel._midi_box.itemData(index) == SAVED_PORT
    ]


def test_old_portmidi_name_matches_qualified_rtmidi_port() -> None:
    assert normalize_midi_device_name(QUALIFIED_PORT) == SAVED_PORT
    assert midi_device_key(QUALIFIED_PORT) == midi_device_key(SAVED_PORT)
    assert match_midi_port([QUALIFIED_PORT], SAVED_PORT) == QUALIFIED_PORT


def test_distinct_clients_with_generic_midi_port_names_remain_distinct() -> None:
    first = "Device A:MIDI 1 20:0"
    second = "Device B:MIDI 1 21:0"

    assert midi_device_key(first) != midi_device_key(second)
    assert normalize_midi_device_name(first) == "Device A:MIDI 1"
    assert normalize_midi_device_name(second) == "Device B:MIDI 1"


def test_output_feedback_uses_same_stable_port_identity() -> None:
    output = "Roto-Control:Roto-Control MIDI 1 131:0"
    assert midi._match_midi_port([output], SAVED_PORT) == output


def test_disconnected_suffix_is_removed_from_loaded_and_saved_config(
    tmp_config_path,
    tmp_profiles_dir,
) -> None:
    from nativmix.utils.config_manager import ConfigManager

    _write_midi_config(tmp_config_path, f"{SAVED_PORT} (Disconnected)")
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)

    assert config.midi_device == SAVED_PORT
    assert json.loads(tmp_config_path.read_text())["hardware"]["midi_device"] == SAVED_PORT


def test_placeholder_enumeration_then_connected_is_atomic(midi_panel) -> None:
    panel, config = midi_panel

    panel.apply_midi_device_state(1, "error_temporary", "Disconnected", SAVED_PORT, [], "")
    assert _selected_midi_items(panel) == [(f"{SAVED_PORT} (Disconnected)", SAVED_PORT)]

    panel.apply_midi_device_state(2, "connecting", "Connecting", SAVED_PORT, [QUALIFIED_PORT], "")
    assert _selected_midi_items(panel) == [(f"{SAVED_PORT} (Disconnected)", SAVED_PORT)]

    panel.apply_midi_device_state(
        2,
        "stable",
        f"♫: {SAVED_PORT}",
        SAVED_PORT,
        [QUALIFIED_PORT],
        QUALIFIED_PORT,
    )
    assert _selected_midi_items(panel) == [(SAVED_PORT, SAVED_PORT)]
    assert panel._midi_status_label.text() == f"♫: {SAVED_PORT}"
    assert config.midi_device == SAVED_PORT


def test_stale_disconnect_cannot_override_newer_connected_state(midi_panel) -> None:
    panel, _config = midi_panel
    panel.apply_midi_device_state(
        4,
        "stable",
        f"♫: {SAVED_PORT}",
        SAVED_PORT,
        [QUALIFIED_PORT],
        QUALIFIED_PORT,
    )

    panel.apply_midi_device_state(3, "error_temporary", "Disconnected", SAVED_PORT, [], "")

    assert _selected_midi_items(panel) == [(SAVED_PORT, SAVED_PORT)]
    assert panel._midi_status_label.text() == f"♫: {SAVED_PORT}"


def test_connected_before_and_after_inventory_refresh_agree(midi_panel) -> None:
    panel, _config = midi_panel
    panel.apply_midi_device_state(
        5,
        "stable",
        f"♫: {SAVED_PORT}",
        SAVED_PORT,
        [],
        QUALIFIED_PORT,
    )
    assert _selected_midi_items(panel) == [(SAVED_PORT, SAVED_PORT)]

    panel.apply_midi_device_state(
        5,
        "stable",
        f"♫: {SAVED_PORT}",
        SAVED_PORT,
        [QUALIFIED_PORT],
        QUALIFIED_PORT,
    )
    assert _selected_midi_items(panel) == [(SAVED_PORT, SAVED_PORT)]
    assert panel._midi_status_label.text() == f"♫: {SAVED_PORT}"


def test_repeated_unplug_replug_cycles_do_not_duplicate_or_persist_suffix(midi_panel) -> None:
    panel, config = midi_panel

    for generation in range(1, 9, 2):
        panel.apply_midi_device_state(
            generation,
            "error_temporary",
            "MIDI Disconnected - Retrying...",
            SAVED_PORT,
            [],
            "",
        )
        assert _selected_midi_items(panel) == [(f"{SAVED_PORT} (Disconnected)", SAVED_PORT)]

        panel.apply_midi_device_state(
            generation + 1,
            "stable",
            f"♫: {SAVED_PORT}",
            SAVED_PORT,
            [QUALIFIED_PORT, QUALIFIED_PORT],
            QUALIFIED_PORT,
        )
        assert _selected_midi_items(panel) == [(SAVED_PORT, SAVED_PORT)]

    panel._on_midi_device_selected(panel._midi_box.currentIndex())
    assert config.midi_device == SAVED_PORT
