"""
System tray icon for NativMix.

Implements Rule 3:
- Uses the application icon (nativmix.svg / installed icon theme name).
- Left-click toggles the main window.
- Right-click shows a context menu (Show/Hide, Settings, Quit).

app.setQuitOnLastWindowClosed(False) is set in main.py so that closing the
main window only hides it – the tray icon keeps the app alive.

Wayland / Cosmic notes
----------------------
setContextMenu() is intentionally kept: the StatusNotifierItem host
(Cosmic Panel) reads the menu registration via D-Bus and renders it natively.
Removing setContextMenu() breaks the right-click menu on compositors that do
not implement ActivationReason.Context delivery back to the Qt process.

Window activation uses QWindow.requestActivate() instead of
QWidget.activateWindow().  On Wayland, activateWindow() is a no-op due to
focus-stealing prevention; requestActivate() triggers the xdg_activation_v1
protocol (Qt 6.5+), which compositors honour.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from nativmix.utils.paths import get_icon_path

logger = logging.getLogger(__name__)


class TrayIcon(QSystemTrayIcon):
    """
    System tray icon that controls the main window visibility.

    Parameters
    ----------
    main_window:
        The MainWindow instance to show/hide on left-click.
    parent:
        Optional Qt parent.
    """

    def __init__(self, main_window, parent: QObject | None = None) -> None:
        icon_path = get_icon_path()
        if icon_path:
            icon = QIcon(str(icon_path))
        else:
            icon = QIcon.fromTheme("nativmix", QIcon.fromTheme("audio-volume-high"))
        super().__init__(icon, parent)

        self._window = main_window
        self._build_menu()

        self.setToolTip("NativMix – Volume Mixer")
        self.activated.connect(self._on_activated)

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        menu = QMenu()

        show_action = menu.addAction("Show / Hide")
        show_action.triggered.connect(self._toggle_window)

        menu.addSeparator()

        settings_action = menu.addAction("Settings …")
        settings_action.triggered.connect(self._open_settings)

        menu.addSeparator()

        quit_action = menu.addAction("Quit NativMix")
        quit_action.triggered.connect(self._quit_app)

        # Register with the StatusNotifier host so compositors render the
        # right-click menu natively (required on Wayland/Cosmic).
        self.setContextMenu(menu)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _quit_app(self) -> None:
        """Signal the main window to accept close, then quit the app."""
        self._window._force_quit = True
        # Defer to the next event-loop tick so the menu dismisses cleanly
        # before the event loop exits.
        QTimer.singleShot(0, QApplication.quit)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Toggle window visibility on left single-click or double-click."""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._toggle_window()

    def _toggle_window(self) -> None:
        if self._window.isVisible():
            self._window.hide()
        else:
            self._show_window()

    def _show_window(self) -> None:
        """Bring the main window to the foreground (Wayland-compatible).

        QWindow.requestActivate() uses the xdg_activation_v1 protocol on
        Wayland (Qt 6.5+), which the compositor honours.
        QWidget.activateWindow() is kept as a fallback for X11 sessions and
        Qt versions that do not yet expose a native window handle.
        """
        self._window.showNormal()
        self._window.raise_()
        handle = self._window.windowHandle()
        if handle is not None:
            handle.requestActivate()
        else:
            self._window.activateWindow()  # X11 / pre-6.5 fallback

    def _open_settings(self) -> None:
        """Show the main window and open the settings panel."""
        self._show_window()
        self._window._open_settings()
