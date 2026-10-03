"""Tests for Flatpak runtime detection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from nativmix.utils import paths as paths_mod


def test_is_flatpak_true_with_env(monkeypatch) -> None:
    monkeypatch.setenv("FLATPAK_ID", "net.knoellix.NativMix")
    assert paths_mod.is_flatpak() is True


def test_is_flatpak_false_without_env_or_marker(monkeypatch) -> None:
    monkeypatch.delenv("FLATPAK_ID", raising=False)

    class _FakePath:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def is_file(self) -> bool:
            return False

    monkeypatch.setattr(paths_mod, "Path", _FakePath)
    assert paths_mod.is_flatpak() is False


@pytest.mark.parametrize("app_id", ["io.github.ArthurD.NativMix", "net.knoellix.NativMix"])
def test_flatpak_icon_matches_exported_desktop_identity(tmp_path, monkeypatch, qtbot, app_id) -> None:
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtWidgets import QWidget

    from nativmix.gui.tray_icon import TrayIcon

    icon_dir = tmp_path / "icons"
    icon_dir.mkdir()
    image = QImage(32, 32, QImage.Format.Format_ARGB32)
    image.fill(QColor("#176da3"))
    icon_path = icon_dir / f"{app_id}.png"
    assert image.save(str(icon_path))
    monkeypatch.setenv("FLATPAK_ID", app_id)
    monkeypatch.setattr(
        paths_mod, "Path",
        lambda value: icon_dir if str(value) == "/app/share/icons/hicolor/256x256/apps" else Path(value),
    )
    monkeypatch.setattr(paths_mod, "_LOCAL_ASSETS", None)
    monkeypatch.setattr(paths_mod, "_SYSTEM_ASSETS", None)

    assert paths_mod.get_desktop_file_name() == app_id
    assert paths_mod.get_icon_path() == icon_path
    window = QWidget()
    qtbot.addWidget(window)
    tray = TrayIcon(window)
    assert not tray.icon().isNull()
    assert not tray.icon().pixmap(24, 24).isNull()


def test_native_desktop_identity_and_asset_resolution_are_preserved(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(paths_mod, "is_flatpak", lambda: False)
    monkeypatch.setattr(paths_mod, "_LOCAL_ASSETS", tmp_path)
    monkeypatch.setattr(paths_mod, "_SYSTEM_ASSETS", None)
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"existing asset")

    assert paths_mod.get_desktop_file_name() == "nativmix"
    assert paths_mod.get_icon_path() == icon


def test_flatpak_without_icon_returns_none_for_identity_aware_theme_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FLATPAK_ID", "io.github.ArthurD.NativMix")
    monkeypatch.setattr(paths_mod, "Path", lambda _value: tmp_path)
    monkeypatch.setattr(paths_mod, "_LOCAL_ASSETS", None)
    monkeypatch.setattr(paths_mod, "_SYSTEM_ASSETS", None)

    assert paths_mod.get_icon_path() is None
