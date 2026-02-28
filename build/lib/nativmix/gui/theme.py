"""
XDG Desktop Portal theming helper for NativMix.

Implements Rule 4: reads the system color scheme and accent color via the
org.freedesktop.portal.Settings D-Bus interface so the app adapts to
CachyOS / KDE / GNOME theming without hard-coded DE-specific paths.

The portal is the standard, DE-agnostic way to query this information under
Wayland.  It works on KDE Plasma, GNOME, and any other XDG-compliant desktop.
"""

from __future__ import annotations

import logging
from enum import IntEnum

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage, QDBusVariant

logger = logging.getLogger(__name__)

_PORTAL_SERVICE   = "org.freedesktop.portal.Desktop"
_PORTAL_PATH      = "/org/freedesktop/portal/desktop"
_PORTAL_INTERFACE = "org.freedesktop.portal.Settings"
_APPEARANCE_NS    = "org.freedesktop.appearance"
_COLOR_SCHEME_KEY = "color-scheme"
_ACCENT_COLOR_KEY = "accent-color"


class ColorScheme(IntEnum):
    """
    XDG color-scheme values as specified by the portal spec.

    0 = no preference
    1 = prefer dark
    2 = prefer light
    """
    NO_PREFERENCE = 0
    DARK  = 1
    LIGHT = 2


class ThemeWatcher(QObject):
    """
    Watches the XDG Desktop Portal for color-scheme and accent-color changes.

    Emits signals when the system theme changes so the main window can
    update its stylesheet dynamically.

    Signals
    -------
    color_scheme_changed(ColorScheme)
        Fired when the user switches between dark and light mode.
    accent_color_changed(tuple[float, float, float])
        Fired when the system accent color changes.
        The tuple contains (red, green, blue) in the range [0.0, 1.0].
    """

    color_scheme_changed = pyqtSignal(int)     # ColorScheme value
    accent_color_changed = pyqtSignal(object)  # (r, g, b) float tuple

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._scheme = ColorScheme.NO_PREFERENCE
        self._accent: tuple[float, float, float] = (0.38, 0.68, 1.0)  # default blue
        self._iface: QDBusInterface | None = None
        self._connected = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect to the session bus and read initial values."""
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            logger.warning("ThemeWatcher: session D-Bus not available; using defaults")
            return

        self._iface = QDBusInterface(
            _PORTAL_SERVICE, _PORTAL_PATH, _PORTAL_INTERFACE, bus
        )
        if not self._iface.isValid():
            logger.warning(
                "ThemeWatcher: portal interface not available (%s); using defaults",
                self._iface.lastError().message(),
            )
            self._iface = None
            return

        # Read initial values
        self._scheme  = self._read_color_scheme()
        self._accent  = self._read_accent_color()

        # Subscribe to SettingChanged signal
        bus.connect(
            _PORTAL_SERVICE,
            _PORTAL_PATH,
            _PORTAL_INTERFACE,
            "SettingChanged",
            self._on_setting_changed,
        )
        self._connected = True
        logger.info(
            "ThemeWatcher: connected (scheme=%s, accent=%.2f,%.2f,%.2f)",
            self._scheme.name, *self._accent,
        )

    def stop(self) -> None:
        """Disconnect from the session bus."""
        if self._connected:
            QDBusConnection.sessionBus().disconnect(
                _PORTAL_SERVICE, _PORTAL_PATH, _PORTAL_INTERFACE,
                "SettingChanged", self._on_setting_changed,
            )
            self._connected = False

    @property
    def color_scheme(self) -> ColorScheme:
        """Current resolved color scheme."""
        return self._scheme

    @property
    def is_dark(self) -> bool:
        """True when the system prefers a dark theme."""
        return self._scheme == ColorScheme.DARK

    @property
    def accent(self) -> tuple[float, float, float]:
        """Current accent color as (r, g, b) floats in [0.0, 1.0]."""
        return self._accent

    def accent_hex(self) -> str:
        """Current accent color as a CSS hex string, e.g. '#6199ff'."""
        r, g, b = (int(c * 255) for c in self._accent)
        return f"#{r:02x}{g:02x}{b:02x}"

    # ------------------------------------------------------------------
    # D-Bus helpers
    # ------------------------------------------------------------------

    def _read_portal_value(self, namespace: str, key: str):
        """Call org.freedesktop.portal.Settings.Read and return the inner variant."""
        if self._iface is None:
            return None
        reply: QDBusMessage = self._iface.call("Read", namespace, key)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            logger.debug("Portal Read(%s, %s) error: %s", namespace, key, reply.errorMessage())
            return None
        args = reply.arguments()
        if not args:
            return None
        # The reply is a variant wrapping another variant – unwrap both
        outer = args[0]
        inner = outer.variant() if hasattr(outer, "variant") else outer
        value = inner.variant() if hasattr(inner, "variant") else inner
        return value

    def _read_color_scheme(self) -> ColorScheme:
        value = self._read_portal_value(_APPEARANCE_NS, _COLOR_SCHEME_KEY)
        try:
            return ColorScheme(int(value))
        except (TypeError, ValueError):
            return ColorScheme.NO_PREFERENCE

    def _read_accent_color(self) -> tuple[float, float, float]:
        value = self._read_portal_value(_APPEARANCE_NS, _ACCENT_COLOR_KEY)
        try:
            # The accent color comes as a D-Bus struct of three doubles
            r, g, b = float(value[0]), float(value[1]), float(value[2])
            return (r, g, b)
        except (TypeError, IndexError, ValueError):
            return (0.38, 0.68, 1.0)  # default blue

    @pyqtSlot(str, str, QDBusVariant)
    def _on_setting_changed(self, namespace: str, key: str, value: QDBusVariant) -> None:
        """D-Bus slot: fires when any portal setting changes."""
        raw = value.variant()  # unwrap the QDBusVariant to the inner Python value
        if namespace != _APPEARANCE_NS:
            return
        if key == _COLOR_SCHEME_KEY:
            try:
                self._scheme = ColorScheme(int(raw))
            except (TypeError, ValueError):
                return
            logger.info("Color scheme changed: %s", self._scheme.name)
            self.color_scheme_changed.emit(int(self._scheme))
        elif key == _ACCENT_COLOR_KEY:
            try:
                self._accent = (float(raw[0]), float(raw[1]), float(raw[2]))
            except (TypeError, IndexError, ValueError):
                return
            logger.info("Accent color changed: %.2f,%.2f,%.2f", *self._accent)
            self.accent_color_changed.emit(self._accent)


def build_stylesheet(dark: bool, accent_hex: str) -> str:
    """
    Generate a QSS stylesheet string for the given color scheme.

    Args:
        dark:       True for dark mode, False for light mode.
        accent_hex: CSS hex color for accent elements (e.g. '#6199ff').

    Returns:
        A complete QSS string ready to pass to QApplication.setStyleSheet().
    """
    if dark:
        bg       = "#1e1e2e"
        surface  = "#2a2a3e"
        border   = "#3a3a5a"
        text     = "#cdd6f4"
        subtext  = "#a6adc8"
        handle   = accent_hex
        groove   = "#3a3a5a"
    else:
        bg       = "#f4f4f8"
        surface  = "#ffffff"
        border   = "#d0d0e0"
        text     = "#1e1e2e"
        subtext  = "#6c6c8a"
        handle   = accent_hex
        groove   = "#c8c8d8"

    return f"""
/* ── NativMix QSS ─────────────────────────────────────────────── */
QMainWindow, QWidget {{
    background-color: {bg};
    color: {text};
    font-family: "Inter", "Segoe UI", sans-serif;
    font-size: 13px;
}}

/* ── Slider (vertical) ─────────────────────────────────────────── */
QSlider::groove:vertical {{
    background: {groove};
    width: 6px;
    border-radius: 3px;
}}
QSlider::handle:vertical {{
    background: {handle};
    width: 18px;
    height: 18px;
    margin: 0 -6px;
    border-radius: 9px;
    border: 2px solid {bg};
}}
QSlider::handle:vertical:hover {{
    background: {handle}cc;
}}
QSlider::sub-page:vertical {{
    background: {handle}55;
    border-radius: 3px;
}}

/* ── Channel frame ─────────────────────────────────────────────── */
QFrame#channel_frame {{
    background-color: {surface};
    border: 1px solid {border};
    border-radius: 10px;
}}

/* ── Labels ────────────────────────────────────────────────────── */
QLabel#app_label {{
    color: {text};
    font-weight: 600;
    font-size: 12px;
}}
QLabel#level_label {{
    color: {subtext};
    font-size: 11px;
}}
QLabel#channel_num_label {{
    color: {subtext};
    font-size: 10px;
}}

/* ── Tray context menu ─────────────────────────────────────────── */
QMenu {{
    background-color: {surface};
    border: 1px solid {border};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 20px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {handle}33;
    color: {text};
}}
QMenu::separator {{
    height: 1px;
    background: {border};
    margin: 4px 8px;
}}

/* ── Scrollbar ─────────────────────────────────────────────────── */
QScrollBar:horizontal {{
    height: 6px;
    background: {groove};
    border-radius: 3px;
}}
QScrollBar::handle:horizontal {{
    background: {handle};
    border-radius: 3px;
    min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
"""
