"""XDG portal appearance monitoring and a Flatpak-safe Fusion fallback."""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import IntEnum
from typing import cast

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage, QDBusVariant
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

_PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_PORTAL_INTERFACE = "org.freedesktop.portal.Settings"
_APPEARANCE_NS = "org.freedesktop.appearance"
_COLOR_SCHEME_KEY = "color-scheme"
_ACCENT_COLOR_KEY = "accent-color"
_DEFAULT_ACCENT = (0.18, 0.56, 0.81)

_DARK = {
    "window": "#171A1F",
    "window_text": "#E2E8F0",
    "base": "#242A33",
    "alternate": "#1D222A",
    "button": "#252B35",
    "tooltip_base": "#111827",
    "tooltip_text": "#F8FAFC",
    "tooltip_border": "#536174",
    "bright_text": "#FF7B72",
    "disabled_text": "#9AA8BA",
    "placeholder": "#9AA8BA",
    "link": "#67C1F5",
    "mid": "#3B4452",
    "midlight": "#4A5565",
    "dark": "#0D1117",
    "light": "#536174",
}
_LIGHT = {
    "window": "#EEF2F7",
    "window_text": "#1F2937",
    "base": "#FFFFFF",
    "alternate": "#E3E9F1",
    "button": "#E3E9F1",
    "tooltip_base": "#FFFFFF",
    "tooltip_text": "#111827",
    "tooltip_border": "#9AA8BA",
    "bright_text": "#B42318",
    "disabled_text": "#667085",
    "placeholder": "#667085",
    "link": "#175CD3",
    "mid": "#C5CEDA",
    "midlight": "#DCE3EC",
    "dark": "#8996A8",
    "light": "#FFFFFF",
}


class ColorScheme(IntEnum):
    """XDG color-scheme values from org.freedesktop.appearance."""

    NO_PREFERENCE = 0
    DARK = 1
    LIGHT = 2


def resolve_prefer_dark(scheme: ColorScheme) -> bool:
    """Resolve the portal preference; no/invalid preference uses readable light."""
    return scheme == ColorScheme.DARK


def _normalise_accent(value: object) -> tuple[float, float, float]:
    if not isinstance(value, (tuple, list)):
        return _DEFAULT_ACCENT
    try:
        channels = tuple(float(channel) for channel in value)
    except ValueError:
        return _DEFAULT_ACCENT
    if len(channels) != 3 or not all(0.0 <= channel <= 1.0 for channel in channels):
        return _DEFAULT_ACCENT
    return channels


def _relative_luminance(color: QColor) -> float:
    channels = []
    for value in (color.redF(), color.greenF(), color.blueF()):
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast_ratio(first: QColor, second: QColor) -> float:
    lighter, darker = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _readable_accent(accent: QColor, backgrounds: tuple[QColor, ...], prefer_dark: bool) -> QColor:
    """Keep the portal hue when possible, shifting only enough for UI text use."""
    if all(_contrast_ratio(accent, background) >= 4.5 for background in backgrounds):
        return accent
    target = QColor("#FFFFFF" if prefer_dark else "#111827")
    for step in range(1, 101):
        amount = step / 100.0
        candidate = QColor.fromRgbF(
            accent.redF() * (1.0 - amount) + target.redF() * amount,
            accent.greenF() * (1.0 - amount) + target.greenF() * amount,
            accent.blueF() * (1.0 - amount) + target.blueF() * amount,
        )
        if all(_contrast_ratio(candidate, background) >= 4.5 for background in backgrounds):
            return candidate
    return target


def build_fusion_fallback_palette(
    prefer_dark: bool,
    accent: tuple[float, float, float] = _DEFAULT_ACCENT,
) -> QPalette:
    """Build the dedicated Flatpak Fusion palette."""
    colors = _DARK if prefer_dark else _LIGHT
    palette = QPalette()
    highlight = _readable_accent(
        QColor.fromRgbF(*_normalise_accent(accent)),
        (QColor(colors["window"]), QColor(colors["button"])),
        prefer_dark,
    )
    selection_candidates = (QColor("#FFFFFF"), QColor("#111827"))
    highlighted_text = max(selection_candidates, key=lambda color: _contrast_ratio(color, highlight))

    role_colors = {
        QPalette.ColorRole.Window: colors["window"],
        QPalette.ColorRole.WindowText: colors["window_text"],
        QPalette.ColorRole.Base: colors["base"],
        QPalette.ColorRole.AlternateBase: colors["alternate"],
        QPalette.ColorRole.Text: colors["window_text"],
        QPalette.ColorRole.Button: colors["button"],
        QPalette.ColorRole.ButtonText: colors["window_text"],
        QPalette.ColorRole.ToolTipBase: colors["tooltip_base"],
        QPalette.ColorRole.ToolTipText: colors["tooltip_text"],
        QPalette.ColorRole.BrightText: colors["bright_text"],
        QPalette.ColorRole.PlaceholderText: colors["placeholder"],
        QPalette.ColorRole.Link: colors["link"],
        QPalette.ColorRole.LinkVisited: colors["link"],
        QPalette.ColorRole.Mid: colors["mid"],
        QPalette.ColorRole.Midlight: colors["midlight"],
        QPalette.ColorRole.Dark: colors["dark"],
        QPalette.ColorRole.Light: colors["light"],
        QPalette.ColorRole.Shadow: colors["dark"],
    }
    for role, value in role_colors.items():
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            palette.setColor(group, role, QColor(value))
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        palette.setColor(group, QPalette.ColorRole.Highlight, highlight)
        palette.setColor(group, QPalette.ColorRole.HighlightedText, highlighted_text)

    disabled_text = QColor(colors["disabled_text"])
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.ToolTipText,
        QPalette.ColorRole.PlaceholderText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled_text)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Window, QColor(colors["window"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor(colors["alternate"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, QColor(colors["mid"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, QColor(colors["mid"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText, disabled_text)
    return palette


def fusion_tooltip_stylesheet(prefer_dark: bool) -> str:
    """Return the one narrow stylesheet needed for reliable Fusion tooltips."""
    colors = _DARK if prefer_dark else _LIGHT
    return (
        "QToolTip {"
        f" color: {colors['tooltip_text']};"
        f" background-color: {colors['tooltip_base']};"
        f" border: 1px solid {colors['tooltip_border']};"
        " padding: 4px;"
        " }"
    )


class ThemeWatcher(QObject):
    """Watch XDG portal color-scheme and accent-color settings."""

    color_scheme_changed = pyqtSignal(int)
    accent_color_changed = pyqtSignal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._scheme = ColorScheme.NO_PREFERENCE
        self._accent = _DEFAULT_ACCENT
        self._iface: QDBusInterface | None = None
        self._connected = False

    def start(self) -> None:
        """Subscribe before reading initial values so startup cannot miss a change."""
        if self._connected:
            return
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            logger.warning("ThemeWatcher: session D-Bus not available; using defaults")
            return
        self._iface = QDBusInterface(_PORTAL_SERVICE, _PORTAL_PATH, _PORTAL_INTERFACE, bus)
        if not self._iface.isValid():
            logger.warning(
                "ThemeWatcher: portal interface not available (%s); using defaults",
                self._iface.lastError().message(),
            )
            self._iface = None
            return
        if not bus.connect(
            _PORTAL_SERVICE,
            _PORTAL_PATH,
            _PORTAL_INTERFACE,
            "SettingChanged",
            self._on_setting_changed,
        ):
            logger.warning("ThemeWatcher: could not subscribe to portal settings; using initial values only")
        else:
            self._connected = True
        self._scheme = self._read_color_scheme()
        self._accent = self._read_accent_color()

    def stop(self) -> None:
        """Disconnect from the session bus."""
        if self._connected:
            QDBusConnection.sessionBus().disconnect(
                _PORTAL_SERVICE,
                _PORTAL_PATH,
                _PORTAL_INTERFACE,
                "SettingChanged",
                self._on_setting_changed,
            )
            self._connected = False

    @property
    def color_scheme(self) -> ColorScheme:
        return self._scheme

    @property
    def is_dark(self) -> bool:
        return resolve_prefer_dark(self._scheme)

    @property
    def accent(self) -> tuple[float, float, float]:
        return self._accent

    def accent_hex(self) -> str:
        r, g, b = (int(channel * 255) for channel in self._accent)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _read_portal_value(self, namespace: str, key: str) -> object | None:
        if self._iface is None:
            return None
        reply: QDBusMessage = self._iface.call("Read", namespace, key)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            logger.debug("Portal Read(%s, %s) error: %s", namespace, key, reply.errorMessage())
            return None
        args = reply.arguments()
        if not args:
            return None
        value = args[0]
        while hasattr(value, "variant"):
            value = value.variant()
        return cast(object, value)

    def _read_color_scheme(self) -> ColorScheme:
        value = self._read_portal_value(_APPEARANCE_NS, _COLOR_SCHEME_KEY)
        if not isinstance(value, int):
            return ColorScheme.NO_PREFERENCE
        try:
            return ColorScheme(value)
        except ValueError:
            return ColorScheme.NO_PREFERENCE

    def _read_accent_color(self) -> tuple[float, float, float]:
        return _normalise_accent(self._read_portal_value(_APPEARANCE_NS, _ACCENT_COLOR_KEY))

    @pyqtSlot(str, str, QDBusVariant)
    def _on_setting_changed(self, namespace: str, key: str, value: QDBusVariant) -> None:
        if namespace != _APPEARANCE_NS:
            return
        raw = value.variant()
        if key == _COLOR_SCHEME_KEY:
            try:
                scheme = ColorScheme(int(raw))
            except (TypeError, ValueError):
                scheme = ColorScheme.NO_PREFERENCE
            if scheme != self._scheme:
                self._scheme = scheme
                self.color_scheme_changed.emit(int(scheme))
        elif key == _ACCENT_COLOR_KEY:
            accent = _normalise_accent(raw)
            if accent != self._accent:
                self._accent = accent
                self.accent_color_changed.emit(accent)


class FlatpakFusionTheme(QObject):
    """Apply portal appearance changes once per effective Fusion palette."""

    def __init__(self, app: QApplication, watcher: ThemeWatcher) -> None:
        super().__init__(app)
        self._app = app
        self._watcher = watcher
        self._last_applied: tuple[bool, tuple[float, float, float]] | None = None
        self._started = False
        style_sheet = getattr(app, "styleSheet", None)
        self._base_stylesheet = style_sheet() if callable(style_sheet) else ""

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._watcher.color_scheme_changed.connect(self._on_color_scheme_changed)
        self._watcher.accent_color_changed.connect(self._on_accent_changed)
        self._watcher.start()
        self._apply()

    @pyqtSlot(int)
    def _on_color_scheme_changed(self, _scheme: int) -> None:
        self._apply()

    @pyqtSlot(object)
    def _on_accent_changed(self, _accent: object) -> None:
        self._apply()

    def _apply(self) -> None:
        state = (self._watcher.is_dark, self._watcher.accent)
        if state == self._last_applied:
            return
        self._last_applied = state
        self._app.setPalette(build_fusion_fallback_palette(*state))
        set_stylesheet = getattr(self._app, "setStyleSheet", None)
        if callable(set_stylesheet):
            tooltip = fusion_tooltip_stylesheet(state[0])
            set_stylesheet(f"{self._base_stylesheet}\n{tooltip}" if self._base_stylesheet else tooltip)
        logger.info("Applied Flatpak Fusion palette (%s)", "dark" if state[0] else "light")


def configure_application_theme(
    app: QApplication,
    *,
    is_flatpak: bool,
    watcher_factory: Callable[[QObject], ThemeWatcher] = ThemeWatcher,
) -> FlatpakFusionTheme | None:
    """Install the fallback only when Flatpak is actually using Fusion."""
    style = app.style()
    style_name = style.objectName() if style is not None else ""
    if not is_flatpak:
        logger.info("Using native Qt style without palette override: %s", style_name or "<unknown>")
        return None
    if style_name.casefold() != "fusion":
        logger.info("Using sandbox-provided Qt style without palette override: %s", style_name or "<unknown>")
        return None

    watcher = watcher_factory(app)
    controller = FlatpakFusionTheme(app, watcher)
    controller.start()
    logger.info("Flatpak Fusion fallback follows XDG portal appearance settings")
    return controller
