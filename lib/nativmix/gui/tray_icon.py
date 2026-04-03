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
        self._window.set_force_quit()
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
        visible = self._window.isVisible()
        logger.debug("_toggle_window: isVisible=%s", visible)
        if visible:
            self._window.hide()
        else:
            self._show_window()

    def _show_window(self) -> None:
        """Bring the main window to the foreground (Wayland-compatible).

        Sets _show_requested on the window before calling showNormal() to
        suppress the changeEvent auto-hide that fires on Wayland because
        Qt.WindowType.Tool windows do not receive compositor focus.
        The flag is cleared after 500 ms — long enough for the compositor
        focus round-trip, short enough not to block user-initiated hides.
        """
        self._window.set_show_requested(True)
        g = self._window.geometry()
        flags = self._window.windowFlags()
        screen = self._window.screen()
        logger.debug(
            "_show_window: geometry=(%d,%d %dx%d) flags=0x%x screen=%s",
            g.x(), g.y(), g.width(), g.height(),
            int(flags),
            screen.name() if screen else None,
        )
        if screen:
            sg = screen.availableGeometry()
            logger.debug(
                "_show_window: screen available geometry=(%d,%d %dx%d)",
                sg.x(), sg.y(), sg.width(), sg.height(),
            )
        logger.debug("_show_window: calling showNormal()")
        self._window.showNormal()
        self._window.raise_()
        handle = self._window.windowHandle()
        if handle is not None:
            logger.debug("_show_window: calling requestActivate() on windowHandle")
            handle.requestActivate()
        else:
            logger.debug("_show_window: no windowHandle, falling back to activateWindow()")
            self._window.activateWindow()
        # Log geometry AFTER showNormal to catch compositor repositioning
        QTimer.singleShot(200, self._log_post_show_state)
        QTimer.singleShot(500, self._clear_show_guard)

    def _log_post_show_state(self) -> None:
        g = self._window.geometry()
        logger.debug(
            "_show_window [200ms after]: isVisible=%s isActiveWindow=%s geometry=(%d,%d %dx%d)",
            self._window.isVisible(),
            self._window.isActiveWindow(),
            g.x(), g.y(), g.width(), g.height(),
        )

    def _clear_show_guard(self) -> None:
        logger.debug("_clear_show_guard: clearing _show_requested")
        self._window.set_show_requested(False)

    def _open_settings(self) -> None:
        """Show the main window and open the settings panel."""
        self._show_window()
        self._window._open_settings()
