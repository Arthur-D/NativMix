import json
from pathlib import Path
import pytest


@pytest.fixture()
def tmp_profiles_dir(tmp_path: Path) -> Path:
    d = tmp_path / "profiles"
    d.mkdir()
    return d


@pytest.fixture()
def tmp_config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.json"


def make_profile(
    profile_id: str = "profile-1",
    name: str = "Profile 1",
    channel_count: int = 5,
    restore_fader_positions: bool = False,
    midi_switch_cc: int | None = None,
    channels: list | None = None,
) -> dict:
    if channels is None:
        channels = [
            {
                "index": i,
                "label": None,
                "is_midi": False,
                "app_names": [],
                "midi_cc": None,
                "midi_mute_cc": None,
                "inverted": False,
                "v_sink": False,
                "mode": "app",
                "hardware_id": None,
                "volume": 1.0,
            }
            for i in range(channel_count)
        ]
    return {
        "id": profile_id,
        "name": name,
        "channel_count": channel_count,
        "restore_fader_positions": restore_fader_positions,
        "midi_switch_cc": midi_switch_cc,
        "channels": channels,
    }


def write_profile(profiles_dir: Path, profile: dict) -> Path:
    p = profiles_dir / f"{profile['id']}.json"
    p.write_text(json.dumps(profile, indent=2))
    return p
