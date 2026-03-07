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
import logging.handlers
import argparse
from pathlib import Path

import setproctitle
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import QStyleFactory
from PyQt6.QtGui import QIcon
from PyQt6.QtNetwork import QLocalSocket, QLocalServer
from PyQt6.QtCore import pyqtSignal, QObject

APP_NAME = "nativmix"
# Qt6 setDesktopFileName requires the name WITHOUT the .desktop suffix
DESKTOP_FILE = "nativmix"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def setup_final_logging(debug_enabled: bool) -> None:
    """
    Replace basicConfig handlers with a RotatingFileHandler + console handler.
    Called after the ConfigManager is loaded so we know the desired log level.
    """
    from nativmix.utils.paths import get_log_dir
    log_dir = get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "nativmix.log"

    level = logging.DEBUG if debug_enabled else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    for handler in root.handlers[:]:
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    logger.info("Logging initialized (level=%s, file=%s)", logging.getLevelName(level), log_file)

    # Log detected platform and all resolved paths for diagnostics
    from nativmix.utils.paths import log_platform_info
    log_platform_info()

def _ipc_socket_name() -> str:
    """
    Return a user-specific, fully qualified socket path.

    Qt6 on Linux/Wayland resolves QLocalSocket names relative to
    QDir::tempPath() (/tmp) by default, but only when the name does NOT
    start with a slash.  Using an absolute path avoids the ambiguity and
    ensures both the server and the client agree on the same file.
    A per-user suffix prevents collisions when multiple users are logged in.
    """
    uid = os.getuid()
    return f"/tmp/nativmix_ipc_{uid}.sock"


IPC_SERVER_NAME = _ipc_socket_name()


class IpcServer(QObject):
    toggle_mute_requested = pyqtSignal(int)
    show_window_requested = pyqtSignal()  # emitted when a second instance sends "show"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)
        # Clean up stale socket file left over from a previous crash.
        QLocalServer.removeServer(IPC_SERVER_NAME)
        if self.server.listen(IPC_SERVER_NAME):
            logger.info("IPC Server listening on '%s'", IPC_SERVER_NAME)
        else:
            logger.error("IPC Server failed to listen: %s", self.server.errorString())
        self.server.newConnection.connect(self._on_new_connection)

    def _on_new_connection(self):
        socket = self.server.nextPendingConnection()
        socket.readyRead.connect(lambda: self._handle_ready_read(socket))
        socket.disconnected.connect(socket.deleteLater)

    def _handle_ready_read(self, socket):
        data = socket.readAll().data().decode("utf-8").strip()
        if data.startswith("toggle_mute:"):
            try:
                ch_idx = int(data.split(":")[1])
                self.toggle_mute_requested.emit(ch_idx)
            except ValueError:
                logger.error("Invalid IPC message: %s", data)
        elif data == "show":
            self.show_window_requested.emit()
        socket.disconnectFromServer()


def main() -> None:
    # Rename the process so task managers show "nativmix" instead of "python"
    setproctitle.setproctitle(APP_NAME)

    # ── CLI Parsing ──────────────────────────────────────────────────────────
    # Parse args BEFORE creating QApplication so the IPC client path works
    # even without a Wayland/X11 display (e.g. KDE global shortcuts).
    parser = argparse.ArgumentParser(description="NativMix Hardware Volume Mixer")
    parser.add_argument("--toggle-mute", type=int, metavar="CHANNEL_INDEX",
                        help="Toggle mute for a specific channel via IPC (0-indexed)")
    args, unknown = parser.parse_known_args()

    # ── Single-Instance Guard (pure Python, no Qt/display needed) ────────────
    # Try to connect to an already-running NativMix instance via the Unix
    # domain socket.  This works from KDE global shortcuts, Wayland compositors,
    # and terminals equally because it requires NO display connection.
    import socket as _socket
    _sock_path = IPC_SERVER_NAME   # /tmp/nativmix_ipc_<uid>.sock
    try:
        _ipc = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        _ipc.settimeout(1.0)
        _ipc.connect(_sock_path)
        # Connection succeeded → another instance is running
        if args.toggle_mute is not None:
            msg = f"toggle_mute:{args.toggle_mute}"
        else:
            msg = "show"
        _ipc.sendall(msg.encode("utf-8"))
        _ipc.close()
        # Small logging without full setup (basicConfig already ran at module level)
        logging.getLogger(__name__).info(
            "Forwarded '%s' to running instance and exiting.", msg
        )
        sys.exit(0)
    except (_socket.timeout, ConnectionRefusedError, FileNotFoundError):
        pass  # No existing instance – continue as primary
    finally:
        try:
            _ipc.close()
        except Exception:
            pass

    # ── Module Path Debug ────────────────────────────────────────────────────
    import nativmix
    import nativmix.gui.settings_panel as _sp
    logger.debug("nativmix loaded from: %s", nativmix.__file__)
    logger.debug("settings_panel loaded from: %s", _sp.__file__)
    logger.debug("Python: %s", sys.executable)

    app = QApplication(sys.argv)




    # ── Main GUI Mode ──
    # ── Wayland App-Identity (Critical for KDE) ──
    app.setApplicationName("nativmix")
    app.setApplicationDisplayName("NativMix")
    app.setDesktopFileName(DESKTOP_FILE)

    from nativmix.utils.paths import get_icon_path
    icon_path = get_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))

    # ── Dynamic Theme & Fallback Engine ──
    # Retrieve all available styles, convert to lower case for insensitive matching
    available_styles = {s.lower(): s for s in QStyleFactory.keys()}
    
    # Priority 1: kvantum (Plasma transparency/blur engines)
    # Priority 2: breeze (Plasma standard)
    # Priority 3: fusion (Qt standard fallback)
    chosen_style = None
    for pref in ("kvantum", "breeze", "fusion"):
        if pref in available_styles:
            chosen_style = available_styles[pref]
            app.setStyle(chosen_style)
            logger.info("Theme engine loaded: %s", chosen_style)
            break
            
    # If we fell all the way back to fusion (which defaults to bright gray),
    # force a dark palette to prevent blinding the user.
    if chosen_style and chosen_style.lower() == "fusion":
        from PyQt6.QtGui import QPalette, QColor
        dark_palette = QPalette()
        dark_palette.setColor(QPalette.ColorRole.Window, QColor(45, 45, 45))
        dark_palette.setColor(QPalette.ColorRole.WindowText, QColor(208, 208, 208))
        dark_palette.setColor(QPalette.ColorRole.Base, QColor(30, 30, 30))
        dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(45, 45, 45))
        dark_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(208, 208, 208))
        dark_palette.setColor(QPalette.ColorRole.ToolTipText, QColor(208, 208, 208))
        dark_palette.setColor(QPalette.ColorRole.Text, QColor(208, 208, 208))
        dark_palette.setColor(QPalette.ColorRole.Button, QColor(45, 45, 45))
        dark_palette.setColor(QPalette.ColorRole.ButtonText, QColor(208, 208, 208))
        dark_palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
        app.setPalette(dark_palette)
        logger.info("Applied dark fallback palette for Fusion")

    # Required for Wayland to associate the window with the correct .desktop entry
    # (Already fully set via Wayland App-Identity above)

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

    # ── Final Logging: file + level from config ─────────────────────────
    setup_final_logging(config.debug_logging)

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
    # Backend mute updates → GUI Mute Buttons
    backend.mute_state_changed.connect(window.on_mute_state_changed)
    
    # Arduino poti values → audio backend (volume control)
    arduino.volumes_changed.connect(backend.apply_poti_volumes)
    # Arduino poti values → GUI sliders (visual feedback)
    arduino.volumes_changed.connect(window.on_volumes_changed)
    backend.channel_volume_changed.connect(window.on_channel_volume_changed)
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
    # Routing update: when the GUI changes a channel mapping, the backend must
    # immediately move the affected audio stream to/from the V-Sink.
    config.mapping_changed.connect(backend._on_mapping_changed)


    # ── Start background threads ────────────────────────────────────────
    backend.start()
    arduino.start()

    # ── IPC Server ──
    ipc_server = IpcServer(parent=app)
    ipc_server.toggle_mute_requested.connect(backend.toggle_mute)
    # "show" IPC command: bring the existing window to the foreground
    ipc_server.show_window_requested.connect(window.show)
    ipc_server.show_window_requested.connect(window.raise_)
    ipc_server.show_window_requested.connect(window.activateWindow)

    # ── Show window ─────────────────────────────────────────────────────
    # Window visibility is handled by the tray icon (show/hide on click)

    exit_code = app.exec()

    # ── Clean shutdown ──────────────────────────────────────────────────
    arduino.stop()
    backend.stop()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
