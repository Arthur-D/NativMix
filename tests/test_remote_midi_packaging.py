from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_remote_discovery_dependency_is_declared_for_supported_packages() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (ROOT / "flatpak" / "requirements-linux.txt").read_text(encoding="utf-8")
    windows_spec = (ROOT / "packaging" / "win" / "nativmix.spec").read_text(encoding="utf-8")
    debian_control = (ROOT / "packaging" / "debian" / "control").read_text(encoding="utf-8")
    suse_spec = (ROOT / "packaging" / "suse" / "nativmix.spec").read_text(encoding="utf-8")
    fedora_spec = (ROOT / "packaging" / "fedora" / "nativmix.spec").read_text(encoding="utf-8")

    assert '"zeroconf>=0.151"' in pyproject
    assert "zeroconf>=0.151" in requirements
    assert 'collect_all("zeroconf")' in windows_spec
    assert '"nativmix.hardware.remote_midi"' in windows_spec
    assert "python3-zeroconf" in debian_control
    assert "python3-zeroconf" in suse_spec
    assert "python3-zeroconf" in fedora_spec


def test_flatpak_pins_remote_discovery_and_keeps_network_scope_minimal() -> None:
    dependencies = json.loads((ROOT / "flatpak" / "python3-deps.json").read_text(encoding="utf-8"))
    modules = {module["name"]: module for module in dependencies["modules"]}
    manifest = (ROOT / "flatpak" / "io.github.ArthurD.NativMix.yml").read_text(encoding="utf-8")

    ifaddr = modules["python3-ifaddr"]
    zeroconf = modules["python3-zeroconf"]

    assert "ifaddr==0.2.0" in ifaddr["build-commands"][0]
    assert ifaddr["sources"][0]["sha256"] == "085e0305cfe6f16ab12d72e2024030f5d52674afad6911bb1eee207177b8a748"
    assert "zeroconf>=0.151" in zeroconf["build-commands"][0]
    assert zeroconf["sources"][0]["sha256"] == "7b514f47729ae5b9b42f066163ad509d020c34cacdda3c5efb972f3336213318"
    assert "--share=network" in manifest
    assert "org.freedesktop.Avahi" not in manifest
