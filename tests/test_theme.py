"""Tests for portal-driven Flatpak Fusion theming."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtDBus import QDBusVariant
from PyQt6.QtGui import QColor, QPalette

from nativmix.gui.theme import (
    ColorScheme,
    FlatpakFusionTheme,
    ThemeWatcher,
    apply_ui_theme,
    build_fusion_fallback_palette,
    configure_application_theme,
    fusion_tooltip_stylesheet,
    remember_native_style,
    resolve_prefer_dark,
)


def _relative_luminance(color: QColor) -> float:
    channels = []
    for value in (color.redF(), color.greenF(), color.blueF()):
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(first: QColor, second: QColor) -> float:
    lighter, darker = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


class FakeWatcher(QObject):
    color_scheme_changed = pyqtSignal(int)
    accent_color_changed = pyqtSignal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.scheme = ColorScheme.NO_PREFERENCE
        self.accent = (0.18, 0.56, 0.81)
        self.start_count = 0

    @property
    def is_dark(self) -> bool:
        return resolve_prefer_dark(self.scheme)

    def start(self) -> None:
        self.start_count += 1

    def set_scheme(self, scheme: ColorScheme) -> None:
        self.scheme = scheme
        self.color_scheme_changed.emit(int(scheme))


class FakeStyle:
    def __init__(self, name: str) -> None:
        self._name = name

    def objectName(self) -> str:
        return self._name


class FakeApp(QObject):
    def __init__(self, style: str) -> None:
        super().__init__()
        self._style = FakeStyle(style)
        self.palettes: list[QPalette] = []
        self.stylesheets: list[str] = []

    def style(self) -> FakeStyle:
        return self._style

    def setPalette(self, palette: QPalette) -> None:
        self.palettes.append(palette)

    def styleSheet(self) -> str:
        return "QLabel { padding: 1px; }"

    def setStyleSheet(self, stylesheet: str) -> None:
        self.stylesheets.append(stylesheet)


@pytest.mark.parametrize("prefer_dark", [False, True])
def test_flatpak_fusion_palette_is_readable(prefer_dark: bool) -> None:
    palette = build_fusion_fallback_palette(prefer_dark)
    contrast_pairs = (
        (QPalette.ColorRole.WindowText, QPalette.ColorRole.Window),
        (QPalette.ColorRole.Text, QPalette.ColorRole.Base),
        (QPalette.ColorRole.ButtonText, QPalette.ColorRole.Button),
        (QPalette.ColorRole.ToolTipText, QPalette.ColorRole.ToolTipBase),
        (QPalette.ColorRole.HighlightedText, QPalette.ColorRole.Highlight),
    )
    for foreground, background in contrast_pairs:
        assert _contrast(palette.color(foreground), palette.color(background)) >= 4.5
    disabled = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
    window = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Window)
    assert _contrast(disabled, window) >= 3.0


def test_flatpak_fusion_follows_live_portal_changes() -> None:
    app = FakeApp("fusion")
    watcher = FakeWatcher(app)
    controller = FlatpakFusionTheme(app, watcher)  # type: ignore[arg-type]
    controller.start()

    assert len(app.palettes) == 1
    assert app.palettes[-1].color(QPalette.ColorRole.Window).lightness() > 128
    watcher.set_scheme(ColorScheme.DARK)
    assert len(app.palettes) == 2
    assert app.palettes[-1].color(QPalette.ColorRole.Window).lightness() < 128
    watcher.accent = (0.8, 0.2, 0.4)
    watcher.accent_color_changed.emit(watcher.accent)
    assert len(app.palettes) == 3
    highlight = app.palettes[-1].color(QPalette.ColorRole.Highlight)
    assert _contrast(highlight, app.palettes[-1].color(QPalette.ColorRole.Window)) >= 4.5


def test_duplicate_portal_values_do_not_reapply_palette() -> None:
    app = FakeApp("fusion")
    watcher = FakeWatcher(app)
    controller = FlatpakFusionTheme(app, watcher)  # type: ignore[arg-type]
    controller.start()
    controller.start()
    watcher.set_scheme(ColorScheme.NO_PREFERENCE)
    watcher.accent_color_changed.emit(watcher.accent)
    assert watcher.start_count == 1
    assert len(app.palettes) == 1


def test_invalid_live_portal_scheme_falls_back_once() -> None:
    watcher = ThemeWatcher()
    watcher._scheme = ColorScheme.DARK
    changes: list[int] = []
    watcher.color_scheme_changed.connect(changes.append)

    watcher._on_setting_changed(
        "org.freedesktop.appearance",
        "color-scheme",
        QDBusVariant(99),
    )
    watcher._on_setting_changed(
        "org.freedesktop.appearance",
        "color-scheme",
        QDBusVariant("invalid"),
    )

    assert watcher.color_scheme == ColorScheme.NO_PREFERENCE
    assert changes == [int(ColorScheme.NO_PREFERENCE)]


@pytest.mark.parametrize("scheme", [ColorScheme.NO_PREFERENCE, 99, -1, None])
def test_unknown_scheme_has_deterministic_light_fallback(scheme: object) -> None:
    try:
        parsed = ColorScheme(scheme)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        parsed = ColorScheme.NO_PREFERENCE
    assert resolve_prefer_dark(parsed) is False
    palette = build_fusion_fallback_palette(resolve_prefer_dark(parsed))
    assert _contrast(palette.color(QPalette.ColorRole.Text), palette.color(QPalette.ColorRole.Base)) >= 4.5


@pytest.mark.parametrize("prefer_dark", [False, True])
@pytest.mark.parametrize("accent", [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (0.9, 0.2, 0.7)])
def test_portal_accent_remains_readable_where_ui_uses_highlight_as_text(
    prefer_dark: bool,
    accent: tuple[float, float, float],
) -> None:
    palette = build_fusion_fallback_palette(prefer_dark, accent)
    highlight = palette.color(QPalette.ColorRole.Highlight)
    assert _contrast(highlight, palette.color(QPalette.ColorRole.Window)) >= 4.5
    assert _contrast(highlight, palette.color(QPalette.ColorRole.Button)) >= 4.5


def test_native_install_retains_selected_style_and_palette() -> None:
    app = FakeApp("breeze")
    watcher_factory = Mock()
    assert configure_application_theme(  # type: ignore[arg-type]
        app,
        is_flatpak=False,
        watcher_factory=watcher_factory,
    ) is None
    watcher_factory.assert_not_called()
    assert app.palettes == []


def test_flatpak_retains_working_non_fusion_style() -> None:
    app = FakeApp("breeze")
    watcher_factory = Mock()
    assert configure_application_theme(  # type: ignore[arg-type]
        app,
        is_flatpak=True,
        watcher_factory=watcher_factory,
    ) is None
    watcher_factory.assert_not_called()
    assert app.palettes == []


def test_flatpak_fusion_installs_one_watcher_and_preserves_existing_stylesheet() -> None:
    app = FakeApp("fusion")
    watchers: list[FakeWatcher] = []

    def factory(parent: QObject) -> FakeWatcher:
        watcher = FakeWatcher(parent)
        watchers.append(watcher)
        return watcher

    controller = configure_application_theme(  # type: ignore[arg-type]
        app,
        is_flatpak=True,
        watcher_factory=factory,  # type: ignore[arg-type]
    )
    assert controller is not None
    assert len(watchers) == 1
    assert watchers[0].start_count == 1
    assert len(app.palettes) == 1
    assert app.stylesheets[-1].startswith("QLabel { padding: 1px; }")
    assert "QToolTip" in app.stylesheets[-1]


def _relative_luminance_byte(color: QColor) -> float:
    def _channel(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = color.red(), color.green(), color.blue()
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def _contrast_ratio(fg: QColor, bg: QColor) -> float:
    l1 = _relative_luminance_byte(fg)
    l2 = _relative_luminance_byte(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, ColorScheme.NO_PREFERENCE),
        (1, ColorScheme.DARK),
        (2, ColorScheme.LIGHT),
    ],
)
def test_color_scheme_from_portal_value(value: int, expected: ColorScheme) -> None:
    assert ColorScheme(value) == expected


def test_resolve_prefer_dark_portal_dark() -> None:
    assert resolve_prefer_dark(ColorScheme.DARK) is True


def test_resolve_prefer_dark_portal_light() -> None:
    assert resolve_prefer_dark(ColorScheme.LIGHT) is False


@pytest.mark.parametrize(
    ("scheme", "expected_when_qt_unknown"),
    [
        (ColorScheme.DARK, True),
        (ColorScheme.LIGHT, False),
    ],
)
def test_theme_watcher_is_dark_follows_portal(qtbot, scheme: ColorScheme, expected_when_qt_unknown: bool) -> None:
    watcher = ThemeWatcher()
    watcher._scheme = scheme
    assert watcher.is_dark is expected_when_qt_unknown


def test_theme_watcher_default_accent_is_tuple(qtbot) -> None:
    watcher = ThemeWatcher()
    assert len(watcher.accent) == 3
    assert all(0.0 <= c <= 1.0 for c in watcher.accent)


def test_theme_watcher_accent_hex_format(qtbot) -> None:
    watcher = ThemeWatcher()
    watcher._accent = (0.23, 0.64, 0.91)
    assert watcher.accent_hex() == "#3aa3e8"


@pytest.mark.parametrize("prefer_dark", [True, False])
def test_fusion_palette_tooltip_contrast(prefer_dark: bool) -> None:
    palette = build_fusion_fallback_palette(prefer_dark)
    tip_fg = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.ToolTipText)
    tip_bg = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.ToolTipBase)
    # WCAG AA for normal text is 4.5:1; keep a comfortable margin for Fusion.
    assert _contrast_ratio(tip_fg, tip_bg) >= 7.0


@pytest.mark.parametrize("prefer_dark", [True, False])
def test_fusion_palette_window_text_contrast(prefer_dark: bool) -> None:
    palette = build_fusion_fallback_palette(prefer_dark)
    fg = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText)
    bg = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Window)
    assert _contrast_ratio(fg, bg) >= 7.0


@pytest.mark.parametrize("prefer_dark", [True, False])
def test_fusion_tooltip_stylesheet_contains_colors(prefer_dark: bool) -> None:
    css = fusion_tooltip_stylesheet(prefer_dark)
    assert "QToolTip" in css
    assert "background-color:" in css
    assert "color:" in css


def test_apply_ui_theme_nativmix_uses_custom_palette(qapp) -> None:
    remember_native_style(qapp)
    apply_ui_theme(qapp, "nativmix")
    highlight = qapp.palette().color(QPalette.ColorGroup.Active, QPalette.ColorRole.Highlight)
    # Cool-blue accent from dedicated NativMix palette (light or dark).
    assert highlight.name().lower() in {"#3ba4e8", "#2f8fcf"}


def test_apply_ui_theme_system_clears_tooltip_stylesheet(qapp) -> None:
    remember_native_style(qapp)
    apply_ui_theme(qapp, "nativmix")
    assert "QToolTip" in qapp.styleSheet()
    apply_ui_theme(qapp, "system")
    assert qapp.styleSheet() == ""
