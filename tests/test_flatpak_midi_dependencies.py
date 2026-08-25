"""Flatpak MIDI dependency policy checks."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_flatpak_bundles_rtmidi_without_portmidi() -> None:
    requirements = (ROOT / "flatpak" / "requirements-linux.txt").read_text()
    manifest = (ROOT / "flatpak" / "io.github.ArthurD.NativMix.yml").read_text()
    dependencies = json.loads((ROOT / "flatpak" / "python3-deps.json").read_text())
    modules = dependencies["modules"]

    assert "python-rtmidi>=1.5" in requirements
    assert "name: portmidi" not in manifest
    assert "--device=all" in manifest

    rtmidi_module = next(module for module in modules if module["name"] == "python3-python-rtmidi")
    build_dependencies_command, rtmidi_command = rtmidi_module["build-commands"]
    assert "python-rtmidi==1.5.8" in rtmidi_command
    assert any(
        source["url"].endswith("python_rtmidi-1.5.8.tar.gz")
        and source["sha256"] == "7f9ade68b068ae09000ecb562ae9521da3a234361ad5449e83fc734544d004fa"
        for source in rtmidi_module["sources"]
    )

    target_match = re.search(r"--target=(\S+)", build_dependencies_command)
    assert target_match is not None
    build_dependencies_path = target_match.group(1)
    assert build_dependencies_path.startswith("${PWD}/")
    assert not build_dependencies_path.startswith("/tmp/")
    assert f"PYTHONPATH={build_dependencies_path} " in rtmidi_command
    assert f"PATH={build_dependencies_path}/bin:${{PATH}} " in rtmidi_command
    assert "--no-build-isolation" in rtmidi_command


def test_all_primary_packages_prefer_or_ship_rtmidi() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    windows_spec = (ROOT / "packaging" / "win" / "nativmix.spec").read_text()
    debian_control = (ROOT / "packaging" / "debian" / "control").read_text()
    suse_spec = (ROOT / "packaging" / "suse" / "nativmix.spec").read_text()

    assert '"python-rtmidi>=1.5"' in pyproject
    assert '"mido.backends.rtmidi"' in windows_spec
    assert '"mido.backends.portmidi"' not in windows_spec
    assert "python3-rtmidi" in debian_control
    assert "python3-python-rtmidi" in suse_spec
