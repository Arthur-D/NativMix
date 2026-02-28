"""
Application entry point for NativMix.

Sets the process title (for task managers) and the desktop file name
(for Wayland icon association) before any Qt objects are created.
"""

from __future__ import annotations

import os
import sys
import platform
import logging

import setproctitle
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import QStyleFactory

APP_NAME = "nativmix"
DESKTOP_FILE = "nativmix.desktop"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    # Rename the process so task managers show "nativmix" instead of "python"
    setproctitle.setproctitle(APP_NAME)

    app = QApplication(sys.argv)

    # Required for Wayland to associate the window with the correct .desktop entry
    app.setDesktopFileName(DESKTOP_FILE)
    app.setApplicationName("NativMix")
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName("knoellix")

    # Keep running when the main window is closed (tray icon keeps the app alive)
    app.setQuitOnLastWindowClosed(False)

    # Platform guard
    os_name = platform.system()
    if os_name != "Linux":
        if os_name == "Windows":
            raise NotImplementedError("Windows backend (WASAPI) is not yet implemented.")
        raise RuntimeError(f"Unsupported platform: {os_name}")

    from nativmix.audio.manager import PipeWireManager
    from nativmix.hardware.arduino import ArduinoThread
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.gui.main_window import MainWindow
    from nativmix.gui.tray_icon import TrayIcon

    # ── Config ─────────────────────────────────────────────────────────
    config = ConfigManager()

    # ── Audio backend ───────────────────────────────────────────────────
    backend = PipeWireManager(config=config)

    # ── Arduino thread ──────────────────────────────────────────────────
    arduino = ArduinoThread(
        port=config.hardware_port,
        num_channels=config.num_channels,
        inverted=config.invert_map,
        threshold=config.threshold,
    )

    # ── GUI ─────────────────────────────────────────────────────────────
    window = MainWindow(config=config, backend=backend)

    tray = TrayIcon(main_window=window)
    if not tray.isSystemTrayAvailable():
        logger.warning("System tray not available – running without tray icon")
    else:
        tray.show()

    # ── Signal wiring ───────────────────────────────────────────────────
    # Arduino poti values → audio backend (volume control)
    arduino.volumes_changed.connect(backend.apply_poti_volumes)
    # Arduino poti values → GUI sliders (visual feedback)
    arduino.volumes_changed.connect(window.on_volumes_changed)
    # Dynamic channel count → GUI rebuild + config update
    arduino.channel_count_changed.connect(window.on_channel_count_changed)
    # Port selector → immediate reconnect on the chosen port
    window.settings_panel.port_changed.connect(
        lambda port: arduino.set_port(port if port else None)
    )
    # Arduino connected → mark port with ★ in the combo box
    arduino.connection_changed.connect(
        lambda connected: (
            logger.info("Arduino %s", "connected" if connected else "disconnected"),
            window.settings_panel.mark_connected_port(arduino.current_port if connected else None),
        )
    )
    # Live-Update for inversion flags and threshold without restart
    config.settings_changed.connect(lambda: arduino.reload_settings(config))

    # ── Start background threads ────────────────────────────────────────
    backend.start()
    arduino.start()

    # ── Show window ─────────────────────────────────────────────────────
    window.show()

    exit_code = app.exec()

    # ── Clean shutdown ──────────────────────────────────────────────────
    arduino.stop()
    backend.stop()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
