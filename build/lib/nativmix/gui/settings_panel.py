"""
Settings panel for NativMix – shown above the channel sliders.

Provides:
- USB port selector (QComboBox) – only shows ports with real hardware
  (hwid / description not empty). Marks the currently connected port.
- Autostart toggle (QPushButton, checkable) – copies/removes .desktop file
  in ~/.config/autostart/ per Rule 5 (never uses sudo, no /etc paths)

Design philosophy: ZERO manual colors. 100% native Qt style.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import serial.tools.list_ports
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
)

logger = logging.getLogger(__name__)

_AUTOSTART_DIR  = Path.home() / ".config" / "autostart"
_AUTOSTART_FILE = _AUTOSTART_DIR / "nativmix.desktop"
_DESKTOP_SOURCES = [
    Path("/usr/share/applications/nativmix.desktop"),
    Path("/usr/local/share/applications/nativmix.desktop"),
    Path(__file__).parent.parent.parent.parent / "data" / "nativmix.desktop",
]


def _is_autostart_enabled() -> bool:
    return _AUTOSTART_FILE.exists()


def _enable_autostart() -> bool:
    source = next((p for p in _DESKTOP_SOURCES if p.exists()), None)
    if source is None:
        logger.warning("Autostart: no .desktop file found")
        return False
    try:
        _AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, _AUTOSTART_FILE)
        logger.info("Autostart enabled: %s → %s", source, _AUTOSTART_FILE)
        return True
    except OSError as exc:
        logger.error("Autostart enable failed: %s", exc)
        return False


def _disable_autostart() -> bool:
    try:
        _AUTOSTART_FILE.unlink(missing_ok=True)
        logger.info("Autostart disabled")
        return True
    except OSError as exc:
        logger.error("Autostart disable failed: %s", exc)
        return False


def _real_ports() -> list[serial.tools.list_ports_common.ListPortInfo]:
    """Return only ports that appear to have actual hardware attached."""
    result = []
    for info in serial.tools.list_ports.comports():
        hwid = (info.hwid or "").strip()
        desc = (info.description or "").strip()
        # Exclude generic "n/a" placeholders that have no real device
        if hwid and hwid.upper() != "N/A":
            result.append(info)
        elif desc and desc.lower() not in ("n/a", ""):
            result.append(info)
    return result


class SettingsPanel(QGroupBox):
    """
    Toolbar-style group box with port selector and autostart toggle.

    Signals
    -------
    port_changed(str)
        Emitted when the user picks a different serial port.
        Empty string → auto-detect.
    """

    port_changed = pyqtSignal(str)

    def __init__(self, config, connected_port: str | None = None, parent=None) -> None:
        super().__init__("Settings", parent)
        self._config = config
        self._connected_port: str | None = connected_port  # updated by main.py

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(10)

        layout.addWidget(QLabel("USB Port:"))

        self._port_box = QComboBox()
        self._port_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._port_box.setToolTip(
            "Select the Arduino serial port.\n"
            "'Auto' tries /dev/ttyACM0 and /dev/ttyUSB0 first.\n"
            "★ = currently connected port."
        )
        self._populate_ports()
        layout.addWidget(self._port_box)

        refresh_btn = QPushButton("↺")
        refresh_btn.setFixedWidth(32)
        refresh_btn.setToolTip("Refresh port list")
        refresh_btn.clicked.connect(self._populate_ports)
        layout.addWidget(refresh_btn)

        layout.addSpacing(16)

        self._autostart_btn = QPushButton(
            "Autostart: ON" if _is_autostart_enabled() else "Autostart: OFF"
        )
        self._autostart_btn.setCheckable(True)
        self._autostart_btn.setChecked(_is_autostart_enabled())
        self._autostart_btn.setToolTip(
            "Autostart on login – copies nativmix.desktop to ~/.config/autostart/"
        )
        self._autostart_btn.toggled.connect(self._on_autostart_toggled)
        layout.addWidget(self._autostart_btn)

        self._port_box.currentIndexChanged.connect(self._on_port_selected)

    # ------------------------------------------------------------------

    def mark_connected_port(self, port: str | None) -> None:
        """Called from main.py when the Arduino connects to update the ★ marker."""
        self._connected_port = port
        self._populate_ports(restore=port or self._port_box.currentData())

    def _populate_ports(self, restore: str | None = None) -> None:
        """Rebuild the combo box from currently available real serial ports."""
        self._port_box.blockSignals(True)
        if restore is None:
            restore = self._port_box.currentData() or self._config.hardware_port

        self._port_box.clear()
        self._port_box.addItem("Auto-detect", userData=None)

        for info in _real_ports():
            connected = (info.device == self._connected_port)
            prefix = "★ " if connected else ""
            label = f"{prefix}{info.device}"
            if info.description and info.description.lower() not in ("n/a", ""):
                label += f"  ({info.description})"
            self._port_box.addItem(label, userData=info.device)

        if restore:
            idx = self._port_box.findData(restore)
            if idx >= 0:
                self._port_box.setCurrentIndex(idx)

        self._port_box.blockSignals(False)

    @pyqtSlot(int)
    def _on_port_selected(self, index: int) -> None:
        port = self._port_box.itemData(index)   # None = Auto
        self._config.hardware_port = port
        self._config.save()
        self.port_changed.emit(port or "")
        logger.info("Port selection changed: %s", port or "auto")

    @pyqtSlot(bool)
    def _on_autostart_toggled(self, checked: bool) -> None:
        ok = _enable_autostart() if checked else _disable_autostart()
        actual = _is_autostart_enabled()
        self._autostart_btn.blockSignals(True)
        self._autostart_btn.setChecked(actual)
        self._autostart_btn.setText("Autostart: ON" if actual else "Autostart: OFF")
        self._autostart_btn.blockSignals(False)
        if not ok:
            logger.warning("Autostart toggle failed")
