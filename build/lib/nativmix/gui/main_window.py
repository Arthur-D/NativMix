"""
Main window for NativMix.

Design philosophy: ZERO manual colors, ZERO QSS.
100% native Qt style via QApplication.style() and QPalette.
Theme adapts automatically when KDE switches dark ↔ light
via QApplication.paletteChanged (emitted by Qt itself).

Layout:
    ┌────────────────────────────────────────────────────┐
    │  SettingsPanel (port combo, autostart toggle)      │
    ├──────┬──────┬──────┬──────┐                        │
    │ CH 1 │ CH 2 │ CH 3 │ CH 4 │  …  (QScrollArea)     │
    │slider│slider│slider│slider│                        │
    │  ↕   │  ↕   │  ↕   │  ↕   │                        │
    │[apps]│[apps]│[apps]│[apps]│                        │
    │[inv] │[inv] │[inv] │[inv] │                        │
    └──────┴──────┴──────┴──────┘                        │
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QSize, pyqtSlot
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from nativmix.gui.settings_panel import SettingsPanel

if TYPE_CHECKING:
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.audio.manager import PipeWireManager

logger = logging.getLogger(__name__)

_LOGOS_DIR        = Path(__file__).parent.parent.parent.parent / "assets" / "logos"
_CHANNEL_MIN_WIDTH = 110
_CHANNEL_MAX_WIDTH = 140


# ---------------------------------------------------------------------------
# App icon helper
# ---------------------------------------------------------------------------

def _app_icon(app_name: str) -> QIcon:
    lower = app_name.lower().replace(" ", "-")
    for ext in ("svg", "png"):
        candidate = _LOGOS_DIR / f"{lower}.{ext}"
        if candidate.exists():
            return QIcon(str(candidate))
    icon = QIcon.fromTheme(lower)
    if not icon.isNull():
        return icon
    return QIcon.fromTheme("audio-volume-high")


# ---------------------------------------------------------------------------
# Single mapped-app row (icon + name + remove button)
# ---------------------------------------------------------------------------

class _AppRow(QWidget):
    """[icon] [name] [×]  – one per assigned app inside a channel."""

    def __init__(self, app_name: str, on_remove, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)

        # Highlight special mappings
        if app_name in ("System Master", "Other Apps"):
            self.setAutoFillBackground(True)
            pal = self.palette()
            # Use a slightly accented background from the native palette
            accent = pal.color(pal.ColorRole.Highlight)
            accent.setAlpha(40)
            pal.setColor(pal.ColorRole.Window, accent)
            self.setPalette(pal)

        icon_label = QLabel()
        icon_label.setPixmap(_app_icon(app_name).pixmap(QSize(16, 16)))
        icon_label.setFixedSize(QSize(18, 18))

        name_label = QLabel()
        name_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        name_label.setToolTip(app_name)
        if app_name in ("System Master", "Other Apps"):
            font = name_label.font()
            font.setBold(True)
            name_label.setFont(font)
        
        elided = name_label.fontMetrics().elidedText(
            app_name, Qt.TextElideMode.ElideRight, 68
        )
        name_label.setText(elided)

        remove_btn = QToolButton()
        remove_btn.setText("×")
        remove_btn.setFixedSize(QSize(18, 18))
        remove_btn.setAutoRaise(True)
        remove_btn.setToolTip(f"Remove {app_name}")
        remove_btn.clicked.connect(on_remove)

        layout.addWidget(icon_label)
        layout.addWidget(name_label)
        layout.addWidget(remove_btn)


# ---------------------------------------------------------------------------
# Per-channel column
# ---------------------------------------------------------------------------

class ChannelWidget(QFrame):
    """
    One vertical mixer channel column.

    Contains (top → bottom):
      level label → slider → CH number → separator →
      app list (with × buttons) → + App button → Invert checkbox
    """

    def __init__(
        self,
        channel_index: int,
        config: ConfigManager,
        backend: PipeWireManager,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._ch     = channel_index
        self._config = config
        self._backend = backend

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)
        self.setMinimumWidth(_CHANNEL_MIN_WIDTH)
        self.setMaximumWidth(_CHANNEL_MAX_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        # ── Level label ────────────────────────────────────────────────
        self._level_label = QLabel("—")
        self._level_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        small = self._level_label.font()
        small.setPointSize(9)
        self._level_label.setFont(small)

        # ── Slider ─────────────────────────────────────────────────────
        self._slider = QSlider(Qt.Orientation.Vertical)
        self._slider.setRange(0, 100)
        self._slider.setValue(50)
        self._slider.setEnabled(False)      # display-only; hardware drives it
        self._slider.setMinimumHeight(140)
        self._slider.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        ch_label = QLabel(f"CH {channel_index + 1}")
        ch_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        tiny = ch_label.font()
        tiny.setPointSize(8)
        ch_label.setFont(tiny)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)

        # ── App list ───────────────────────────────────────────────────
        self._app_list_widget = QWidget()
        self._app_list_layout = QVBoxLayout(self._app_list_widget)
        self._app_list_layout.setContentsMargins(0, 0, 0, 0)
        self._app_list_layout.setSpacing(2)

        # ── Add-stream button ──────────────────────────────────────────
        self._add_btn = QPushButton("+ App")
        self._add_btn.setToolTip("Assign an active audio stream to this channel")
        self._add_btn.clicked.connect(self._open_stream_picker)

        # ── Invert checkbox ────────────────────────────────────────────
        self._invert_cb = QCheckBox("Invert")
        self._invert_cb.setToolTip(
            "Invert slider direction for this channel\n(0 ADC = 100% volume)"
        )
        self._invert_cb.setChecked(self._config.get_effective_inversion(channel_index))
        self._invert_cb.toggled.connect(self._on_invert_toggled)

        # ── Root layout ────────────────────────────────────────────────
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 8, 6, 8)
        layout.setSpacing(4)
        layout.addWidget(self._level_label)
        layout.addWidget(self._slider, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(ch_label)
        layout.addWidget(sep)
        layout.addWidget(self._app_list_widget)
        layout.addWidget(self._add_btn)
        layout.addWidget(self._invert_cb)
        layout.addStretch()

        self._refresh_app_list()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_volume(self, volume: float) -> None:
        pct = int(volume * 100)
        self._slider.setValue(pct)
        self._level_label.setText(f"{pct} %")

    def refresh(self) -> None:
        self._refresh_app_list()

    # ------------------------------------------------------------------
    # App list
    # ------------------------------------------------------------------

    def _refresh_app_list(self) -> None:
        while self._app_list_layout.count():
            item = self._app_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for name in self._config.get_app_names(self._ch):
            self._app_list_layout.addWidget(
                _AppRow(name, on_remove=lambda _=False, n=name: self._remove_app(n))
            )

    def _remove_app(self, app_name: str) -> None:
        self._config.remove_app_name(self._ch, app_name)
        self._config.save()
        self._refresh_app_list()

    # ------------------------------------------------------------------
    # Stream picker
    # ------------------------------------------------------------------

    def _open_stream_picker(self) -> None:
        streams = self._backend.get_active_streams()
        
        # Determine which apps are assigned elsewhere, and which are here
        already_here = set(self._config.get_app_names(self._ch))
        assigned_elsewhere = set()
        for i in range(self._config.num_channels):
            if i != self._ch:
                assigned_elsewhere.update(self._config.get_app_names(i))

        menu = QMenu(self)

        # Build list of candidate app names from active streams
        candidates: set[str] = set()
        for s in streams:
            name = s.app_name
            # Global filter: ignore internal pulse/speech-dispatcher streams
            if "speech-dispatcher" in name.lower() or "dummy" in name.lower():
                continue
            candidates.add(name)

        # Always offer the special pseudo-apps
        candidates.add("System Master")
        candidates.add("Other Apps")

        # Sort: Special apps first, then alphabetically
        def sort_key(name: str) -> tuple[int, str]:
            if name == "System Master": return (0, name)
            if name == "Other Apps":    return (1, name)
            return (2, name.lower())

        added_actions = 0
        for name in sorted(candidates, key=sort_key):
            # Exclusivity: skip if assigned to another channel
            if name in assigned_elsewhere:
                continue

            action = menu.addAction(_app_icon(name), name)
            action.setCheckable(True)
            action.setChecked(name in already_here)
            if name in ("System Master", "Other Apps"):
                font = action.font()
                font.setBold(True)
                action.setFont(font)

            action.triggered.connect(
                lambda _=False, n=name: self._on_stream_picked(n)
            )
            added_actions += 1

        if added_actions == 0:
            a = menu.addAction("No available streams")
            a.setEnabled(False)

        menu.exec(self._add_btn.mapToGlobal(self._add_btn.rect().bottomLeft()))

    def _on_stream_picked(self, app_name: str) -> None:
        current = self._config.get_app_names(self._ch)
        if app_name in current:
            self._config.remove_app_name(self._ch, app_name)
        else:
            self._config.update_mapping(app_name, self._ch)
        self._config.save()
        self._refresh_app_list()

    # ------------------------------------------------------------------
    # Inversion
    # ------------------------------------------------------------------

    @pyqtSlot(bool)
    def _on_invert_toggled(self, checked: bool) -> None:
        self._config.set_inverted(self._ch, checked)
        self._config.save()
        logger.info("Channel %d inversion: %s", self._ch, checked)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """
    NativMix main mixer window.

    Pure native Qt style – no QSS, no manual palette colors.
    Responds to KDE dark/light theme switches via QApplication.paletteChanged.
    """

    def __init__(self, config: ConfigManager, backend: PipeWireManager, parent=None) -> None:
        super().__init__(parent)
        self._config  = config
        self._backend = backend
        self._channels: list[ChannelWidget] = []

        self.setWindowTitle("NativMix")
        self.setWindowIcon(QIcon.fromTheme("nativmix", QIcon.fromTheme("audio-volume-high")))
        self.setMinimumHeight(380)

        # ── Central widget ─────────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Collapsible Settings Area ──────────────────────────────────
        self._toggle_settings_btn = QToolButton()
        self._toggle_settings_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle_settings_btn.setText("Show Settings")
        self._toggle_settings_btn.setArrowType(Qt.ArrowType.RightArrow)
        self._toggle_settings_btn.setCheckable(True)
        self._toggle_settings_btn.setChecked(False)
        self._toggle_settings_btn.toggled.connect(self._on_settings_toggled)
        self._toggle_settings_btn.setStyleSheet("QToolButton { border: none; font-weight: bold; }")

        root.addWidget(self._toggle_settings_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        self.settings_panel = SettingsPanel(config)
        self.settings_panel.setVisible(False)
        root.addWidget(self.settings_panel)

        # ── Scrollable channel area ────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        self._ch_layout = QHBoxLayout(container)
        self._ch_layout.setContentsMargins(0, 0, 0, 0)
        self._ch_layout.setSpacing(6)
        self._ch_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

        scroll.setWidget(container)
        root.addWidget(scroll)

        # ── Build initial channels ─────────────────────────────────────
        self._rebuild_channels()
        self._update_window_width()

        # ── Signal connections ─────────────────────────────────────────
        self._config.mapping_changed.connect(self._on_mapping_changed)

        # Qt emits paletteChanged when the system theme switches – no CSS needed
        QApplication.instance().paletteChanged.connect(self._on_palette_changed)

    # ------------------------------------------------------------------
    # Channel management
    # ------------------------------------------------------------------

    def _rebuild_channels(self) -> None:
        for ch in self._channels:
            self._ch_layout.removeWidget(ch)
            ch.deleteLater()
        self._channels.clear()

        for i in range(self._config.num_channels):
            w = ChannelWidget(i, self._config, self._backend)
            self._channels.append(w)
            self._ch_layout.addWidget(w)

    def _update_window_width(self) -> None:
        n = max(1, len(self._channels))
        self.setFixedWidth(min(n * (_CHANNEL_MAX_WIDTH + 6) + 32, 960))

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @pyqtSlot(list)
    def on_volumes_changed(self, volumes: list[float]) -> None:
        for i, vol in enumerate(volumes):
            if i < len(self._channels):
                self._channels[i].set_volume(vol)

    @pyqtSlot(int)
    def on_channel_count_changed(self, n: int) -> None:
        if n == len(self._channels):
            return
        logger.info("Channel count changed to %d – rebuilding GUI", n)
        self._config.num_channels = n
        self._config.save()
        self._rebuild_channels()
        self._update_window_width()

    @pyqtSlot(int, list)
    def _on_mapping_changed(self, channel_index: int, _names: list[str]) -> None:
        """
        Refresh ALL channels when a mapping changes, so the + App menus
        immediately reflect the new exclusivity rules.
        """
        for ch in self._channels:
            ch.refresh()

    @pyqtSlot(bool)
    def _on_settings_toggled(self, checked: bool) -> None:
        self.settings_panel.setVisible(checked)
        self._toggle_settings_btn.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )
        self._toggle_settings_btn.setText("Hide Settings" if checked else "Show Settings")

    def _on_palette_changed(self, _palette=None) -> None:
        """
        Called by Qt when the system theme changes (dark ↔ light).

        No action needed: Qt automatically repaints all widgets using the
        new palette. This slot exists as a hook for future per-widget tweaks.
        """
        logger.debug("System palette changed – widgets repaint automatically")

    def refresh_stream_list(self) -> None:
        """No-op: stream picker fetches data on-demand."""

    # ------------------------------------------------------------------
    # Close → hide (tray keeps the app alive)
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
