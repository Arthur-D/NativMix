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
import os
from typing import TYPE_CHECKING, cast

from PyQt6.QtCore import QEvent, QPoint, QSettings, QSignalBlocker, QSize, Qt, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QAction, QColor, QCursor, QDesktopServices, QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from nativmix.gui.mixer_facade import LocalMixerFacade, RemoteMixerFacade
from nativmix.gui.settings_panel import SettingsPanel
from nativmix.utils.config_manager import ConfigManager
from nativmix.utils.midi_values import midi_cc_to_volume
from nativmix.utils.paths import is_windows
from nativmix.utils.qt_utils import _slot_guard
from nativmix.utils.update_checker import RELEASE_PAGE_URL, UpdateChecker

if TYPE_CHECKING:
    from nativmix.audio.base import AudioBackendBase
    from nativmix.audio.manager import PipeWireManager
    from nativmix.hardware.arduino import ArduinoThread
    from nativmix.hardware.midi import MidiThread
    from nativmix.utils.profile_manager import ProfileManager

logger = logging.getLogger(__name__)
MixerFacade = LocalMixerFacade | RemoteMixerFacade


def _format_midi_binding(midi_channel: int, cc: int | None, empty: str) -> str:
    """Format a compact protocol-channel/CC label."""
    return f"{midi_channel + 1}:{cc}" if cc is not None else f"{midi_channel + 1}:{empty}"


def _describe_midi_binding(kind: str, midi_channel: int, cc: int | None) -> str:
    """Return the full accessible description for a compact MIDI binding."""
    binding = (
        f"MIDI channel {midi_channel + 1}, CC {cc}"
        if cc is not None
        else f"MIDI channel {midi_channel + 1}, unassigned"
    )
    return f"{kind} MIDI binding: {binding}"


def _is_gnome_x11() -> bool:
    """True if running on GNOME under X11 (xcb platform)."""
    if QGuiApplication.platformName() != "xcb":
        return False
    return "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()


def _is_kde_x11() -> bool:
    """True if running on KDE Plasma under X11 (xcb platform)."""
    if QGuiApplication.platformName() != "xcb":
        return False
    return "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()



# ---------------------------------------------------------------------------
# Editable channel label (double-click to rename)
# ---------------------------------------------------------------------------

class _EditableChannelLabel(QLabel):
    """QLabel that opens a rename dialog on double-click.

    Single Ctrl-Click or Shift-Click emits ``select_requested`` (with the
    raw modifiers int) so the parent ChannelWidget can forward the event to
    MainWindow for multi-strip selection handling.
    """

    rename_requested = pyqtSignal(str)
    #: Emitted when the label is clicked with Ctrl or Shift held.
    #: Carries the raw modifiers value (int cast of Qt.KeyboardModifiers).
    select_requested = pyqtSignal(int)

    def mousePressEvent(self, event) -> None:
        mods = event.modifiers()
        ctrl = Qt.KeyboardModifier.ControlModifier
        shift = Qt.KeyboardModifier.ShiftModifier
        if mods & (ctrl | shift):
            self.select_requested.emit(mods.value)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        text, ok = QInputDialog.getText(
            self, "Rename Channel", "Name:", text=self.text()
        )
        if ok and text.strip():
            self.rename_requested.emit(text.strip())
        super().mouseDoubleClickEvent(event)


class _ChannelReorderGrip(QFrame):
    """Channel separator with mouse and keyboard reorder gestures."""

    drag_started = pyqtSignal(int)
    drag_moved = pyqtSignal(int, object)
    drag_finished = pyqtSignal(int, object)
    move_requested = pyqtSignal(int, int)

    def __init__(self, channel_index: int, parent=None) -> None:
        super().__init__(parent)
        self._channel_index = channel_index
        self._press_global: QPoint | None = None
        self._dragging = False
        self.setFrameShape(QFrame.Shape.HLine)
        self.setFrameShadow(QFrame.Shadow.Sunken)
        self.setMinimumHeight(9)
        self.setCursor(QCursor(Qt.CursorShape.SizeHorCursor))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"Reorder channel {channel_index + 1}")
        self.setToolTip(
            "Drag to reorder this channel visually. "
            "Right-click or use Left/Right while focused for keyboard reordering."
        )
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        move_left = QAction("Move channel left", self)
        move_left.triggered.connect(lambda _checked=False: self.move_requested.emit(self._channel_index, -1))
        menu.addAction(move_left)
        move_right = QAction("Move channel right", self)
        move_right.triggered.connect(lambda _checked=False: self.move_requested.emit(self._channel_index, 1))
        menu.addAction(move_right)
        menu.exec(self.mapToGlobal(pos))

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Left:
            self.move_requested.emit(self._channel_index, -1)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Right:
            self.move_requested.emit(self._channel_index, 1)
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._dragging = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._press_global is not None and event.buttons() & Qt.MouseButton.LeftButton:
            global_pos = event.globalPosition().toPoint()
            drag_distance = (global_pos - self._press_global).manhattanLength()
            if not self._dragging and drag_distance >= QApplication.startDragDistance():
                self._dragging = True
                self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
                self.grabMouse()
                self.drag_started.emit(self._channel_index)
            if self._dragging:
                self.drag_moved.emit(self._channel_index, global_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._press_global is not None:
            if QWidget.mouseGrabber() is self:
                self.releaseMouse()
            if self._dragging:
                self.drag_finished.emit(self._channel_index, event.globalPosition().toPoint())
            self._press_global = None
            self._dragging = False
            self.setCursor(QCursor(Qt.CursorShape.SizeHorCursor))
            event.accept()
            return
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# Single mapped-app row (remove button + name)
# ---------------------------------------------------------------------------

class _AppRow(QWidget):
    """[×] [name]  – one per assigned app inside a channel."""

    routing_pause_toggled = pyqtSignal(str, bool)

    def __init__(self, app_name: str, on_remove, parent=None) -> None:
        super().__init__(parent)
        self.app_name = app_name
        self._routing_paused = False
        self._unresolved = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._remove_btn = QToolButton()
        self._remove_btn.setIcon(QIcon.fromTheme('list-remove'))
        self._remove_btn.setFixedSize(QSize(18, 18))
        self._remove_btn.setAutoRaise(True)
        self._remove_btn.setToolTip("Remove app.")
        self._remove_btn.clicked.connect(on_remove)

        self._name_label = QLabel()
        self._name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._name_label.setToolTip(f"App: {app_name}")
        if app_name in ("System Master", "Other Apps"):
            font = self._name_label.font()
            font.setBold(True)
            self._name_label.setFont(font)

        self._name_label.setText(app_name)

        layout.addWidget(self._remove_btn)
        layout.addWidget(self._name_label)

        if app_name.lower() not in {"system master", "other apps"}:
            self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.customContextMenuRequested.connect(self._show_context_menu)
        self.update_dynamic_styles()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_elided_name()

    def _update_elided_name(self) -> None:
        available_width = self._name_label.contentsRect().width()
        if available_width <= 0:
            return
        self._name_label.setText(
            self._name_label.fontMetrics().elidedText(
                self.app_name,
                Qt.TextElideMode.ElideRight,
                available_width,
            )
        )

    def set_name_tooltip(self, text: str) -> None:
        """Set the tooltip on the app name label."""
        self._name_label.setToolTip(text)

    def set_unresolved(self, unresolved: bool) -> None:
        """
        Update the visual state to indicate whether the target app is currently
        visible in the audio graph.  When *unresolved* is True, the label is
        rendered in italic and the tooltip is updated with a sandbox hint.
        The binding is never removed — this is a display-only indicator.
        """
        self._unresolved = unresolved
        font = self._name_label.font()
        font.setItalic(unresolved)
        self._name_label.setFont(font)
        self._update_tooltip()

    def set_receiver_availability(self, available: bool | None) -> None:
        """Render only receiver-authored availability; unknown metadata stays neutral."""
        self._unresolved = available is False
        font = self._name_label.font()
        font.setItalic(self._unresolved)
        self._name_label.setFont(font)
        if available is False:
            self._name_label.setToolTip(
                f"Receiver target '{self.app_name}' is currently unavailable. "
                "The mapping is preserved."
            )
        elif available is True:
            self._name_label.setToolTip(f"Receiver target: {self.app_name}")

    def set_routing_paused(self, paused: bool) -> None:
        """Show a theme-safe routing-only pause state."""
        self._routing_paused = paused
        self.update_dynamic_styles()
        self._update_tooltip()

    def _update_tooltip(self) -> None:
        if self._routing_paused:
            self._name_label.setToolTip(
                f"App: {self.app_name}\n"
                "NativMix automatic routing is paused; volume and mute still apply."
            )
        elif self._unresolved:
            self._name_label.setToolTip(
                f"⚠ '{self.app_name}' is not currently visible in the audio graph.\n"
                "The binding is preserved and will be applied when the app reappears.\n"
                "(In Flatpak: app streams in other sandboxes may not be visible.)"
            )
        else:
            self._name_label.setToolTip(f"App: {self.app_name}")

    def _show_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        label = "Resume NativMix routing" if self._routing_paused else "Pause NativMix routing"
        action = QAction(label, self)
        action.setToolTip(
            "Only automatic stream moves are paused. The mapping, volume, and mute controls remain active."
        )
        action.triggered.connect(
            lambda _checked=False: self.routing_pause_toggled.emit(self.app_name, not self._routing_paused)
        )
        menu.addAction(action)
        menu.exec(self.mapToGlobal(pos))

    def update_dynamic_styles(self) -> None:
        """Tint the X button to match the system Highlight color and apply custom hover state."""
        palette = QApplication.palette()
        accent_color = palette.color(QPalette.ColorRole.Highlight)
        accent_hex = accent_color.name()

        base_icon = QIcon.fromTheme('list-remove').pixmap(18, 18)

        if not base_icon.isNull():
            tinted = QPixmap(base_icon.size())
            tinted.fill(Qt.GlobalColor.transparent)
            painter = QPainter(tinted)
            painter.drawPixmap(0, 0, base_icon)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(tinted.rect(), accent_color)
            painter.end()
            self._remove_btn.setIcon(QIcon(tinted))

        btn_style = f"""
        QToolButton:hover {{
            background-color: {accent_hex};
            border-radius: 4px;
        }}
        """
        self._remove_btn.setStyleSheet(btn_style)

        # Also color the app name label
        # Use QPalette instead of setStyleSheet to avoid breaking native tooltips on Wayland
        pal = self._name_label.palette()
        label_color = (
            palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
            if self._routing_paused
            else accent_color
        )
        pal.setColor(QPalette.ColorRole.WindowText, label_color)
        self._name_label.setPalette(pal)


# ---------------------------------------------------------------------------
# Per-channel column
# ---------------------------------------------------------------------------

class ChannelWidget(QFrame):
    """
    One vertical mixer channel column.

    Contains (top → bottom):
      level label → slider → CH number → separator →
      mode switch → app list (with × buttons)/hw display →
      + App / + Gerät button → Toggles (Invert/VSink)
    """

    #: Emitted when the channel label is Ctrl- or Shift-clicked.
    #: Carries (channel_index, modifiers_int) so MainWindow can handle
    #: multi-strip selection without needing access to internal widgets.
    strip_clicked = pyqtSignal(int, int)

    def __init__(
        self,
        channel_index: int,
        config: ConfigManager | MixerFacade,
        backend: PipeWireManager | MixerFacade,
        is_midi: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._ch     = channel_index
        self._config: MixerFacade = (
            LocalMixerFacade(config, None, backend, self)
            if isinstance(config, ConfigManager)
            else cast(MixerFacade, config)
        )
        self._backend = backend
        self.is_midi_channel = is_midi
        self._show_midi_bindings = is_midi or self._config.is_remote
        self._selected = False
        self._compact_mode = False
        self._edit_mode = False
        self._remote_editable = True
        self._muted: bool = False
        self._gain_control_supported: bool = True
        self._v_sink_supported: bool = True
        logger.debug("Creating ChannelWidget: index=%d, is_midi=%s", channel_index, is_midi)
        if hasattr(self._config, "pending_changed"):
            self._config.pending_changed.connect(self._on_pending_changed)

        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        # ── Mute Button ────────────────────────────────────────────────
        self._mute_btn = QToolButton()
        self._mute_btn.setIcon(QIcon.fromTheme("audio-volume-high"))
        self._mute_btn.setToolTip("Toggle mute.")
        self._mute_btn.clicked.connect(lambda checked=False: self._config.toggle_mute(self._ch))

        # ── Level label ────────────────────────────────────────────────
        self._level_label = QLabel("—")
        self._level_label.setObjectName("pct_label")
        self._level_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        small = self._level_label.font()
        small.setPointSize(9)
        self._level_label.setFont(small)

        # Reduced opacity applied later during update_accent_colors

        # ── Slider ─────────────────────────────────────────────────────
        self._slider = QSlider(Qt.Orientation.Vertical)
        self._slider.setRange(0, 100)

        # Initial volume sync from config
        init_vol = self._config.get_channel_volume(self._ch)
        self._slider.setFixedHeight(180)
        self._slider.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._slider.valueChanged.connect(self._on_slider_changed)

        # Explicitly set initial volume to update label AND slider
        self.set_volume(init_vol)

        default_label = f"MIDI {channel_index + 1}" if self.is_midi_channel else f"CH {channel_index + 1}"
        label_text = self._config.get_channel_label(channel_index) or default_label
        self._ch_label = _EditableChannelLabel(label_text)
        self._ch_label.setObjectName("ch_label")
        self._ch_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._ch_label.setToolTip("Double-click to rename")
        self._ch_label.rename_requested.connect(self._on_rename)
        self._ch_label.select_requested.connect(
            lambda mods: self.strip_clicked.emit(self._ch, mods)
        )
        tiny = self._ch_label.font()
        tiny.setPointSize(8)
        self._ch_label.setFont(tiny)

        # Accent palette applied later during update_accent_colors

        self._sep = _ChannelReorderGrip(channel_index)

        # ── Gain unsupported badge (hidden until capability_changed fires) ──
        self._gain_unsupported_badge = QLabel("⚠ Vol. ctrl unavailable")
        self._gain_unsupported_badge.setObjectName("gain_unsupported_badge")
        self._gain_unsupported_badge.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._gain_unsupported_badge.setToolTip(
            "Volume control unavailable\n"
            "(PipeWire gain node not available in this runtime)"
        )
        badge_font = self._gain_unsupported_badge.font()
        badge_font.setPointSize(7)
        self._gain_unsupported_badge.setFont(badge_font)
        self._gain_unsupported_badge.setStyleSheet("color: orange; font-weight: bold;")
        self._gain_unsupported_badge.setWordWrap(True)
        self._gain_unsupported_badge.setVisible(False)

        # ── Mode Switch ────────────────────────────────────────────────
        self._mode_cb = QCheckBox("Device")
        self._mode_cb.setToolTip("Toggle between App Mode and Hardware Mode.")
        self._mode_cb.clicked.connect(self._on_mode_toggled)

        # ── App list / HW Selection display ────────────────────────────
        self._app_list_widget = QWidget()
        self._app_list_widget.setObjectName("app_list_widget")
        self._app_list_layout = QVBoxLayout(self._app_list_widget)
        self._app_list_layout.setContentsMargins(0, 0, 0, 0)
        self._app_list_layout.setSpacing(2)

        self._app_list_scroll = QScrollArea()
        self._app_list_scroll.setWidgetResizable(True)
        self._app_list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._app_list_scroll.viewport().setAutoFillBackground(False)
        self._app_list_scroll.setStyleSheet("QScrollArea, #app_list_widget { background: transparent; }")
        self._app_list_scroll.setFixedHeight(90)
        self._app_list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._app_list_scroll.setWidget(self._app_list_widget)

        # ── Add-stream / Add-HW button ─────────────────────────────────
        self._add_btn = QPushButton()
        self._add_btn.clicked.connect(self._open_picker)



        # ── Toggle Controls ────────────────────────────────────────────
        self._toggles_layout = QVBoxLayout()
        self._toggles_layout.setContentsMargins(0, 4, 0, 0)
        self._toggles_layout.setSpacing(4)

        # Invert checkbox
        self._invert_cb = QCheckBox("Inv")
        self._invert_cb.setToolTip("Invert slider direction.")
        self._invert_cb.setChecked(self._config.get_effective_inversion(channel_index))
        sp_inv = self._invert_cb.sizePolicy()
        sp_inv.setRetainSizeWhenHidden(True)
        self._invert_cb.setSizePolicy(sp_inv)
        self._invert_cb.toggled.connect(self._on_invert_toggled)
        self._invert_cb.setVisible(self._config.show_invert_option)

        # V-Sink checkbox
        self._vsink_cb = QCheckBox("V-Sink")
        self._vsink_cb.setToolTip("Route audio through a virtual sink.")
        self._vsink_cb.setChecked(self._config.is_v_sink_enabled(channel_index))
        sp_vsink = self._vsink_cb.sizePolicy()
        sp_vsink.setRetainSizeWhenHidden(True)
        self._vsink_cb.setSizePolicy(sp_vsink)
        self._vsink_cb.toggled.connect(self._on_vsink_toggled)
        backend_v_sink_supported = getattr(self._backend, "v_sink_supported", True)
        if not isinstance(backend_v_sink_supported, bool):
            backend_v_sink_supported = True
        self.set_v_sink_supported(
            backend_v_sink_supported,
            getattr(self._backend, "v_sink_capability_reason", ""),
        )

        self._toggles_layout.addWidget(self._mode_cb)
        self._toggles_layout.addWidget(self._vsink_cb)
        self._toggles_layout.addWidget(self._invert_cb)

        # Initialize Mode UI State
        is_hw = (self._config.get_channel_mode(self._ch) == "hardware")
        self._mode_cb.setChecked(is_hw)
        self._apply_mode_ui(is_hw)

        # ── Setup size policies for consistency ───────────────────────
        # We always want the app list and toggles to exist so columns align.
        # Use setRetainSizeWhenHidden(True) if they ever get hidden.
        sp_scroll = self._app_list_scroll.sizePolicy()
        sp_scroll.setRetainSizeWhenHidden(True)
        self._app_list_scroll.setSizePolicy(sp_scroll)

        sp_add = self._add_btn.sizePolicy()
        sp_add.setRetainSizeWhenHidden(True)
        self._add_btn.setSizePolicy(sp_add)

        # ── Root layout ────────────────────────────────────────────────
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 2, 1, 2)
        layout.setSpacing(1)

        layout.addWidget(self._mute_btn, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._level_label)
        layout.addWidget(self._slider, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._gain_unsupported_badge)
        layout.addWidget(self._ch_label)
        layout.addWidget(self._sep)

        layout.addWidget(self._app_list_scroll)
        layout.addWidget(self._add_btn)
        layout.addLayout(self._toggles_layout)

        # ── MIDI UI Elements (Bottom) ──────────────────────────────────
        if self._show_midi_bindings:
            self._learn_btn = QToolButton()
            self._learn_btn.setIcon(QIcon.fromTheme('media-record'))
            self._learn_btn.setText("Learn")
            self._learn_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self._learn_btn.setCheckable(True)
            self._learn_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._learn_btn.setMinimumHeight(24)
            self._learn_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
            self._learn_btn.setIconSize(QSize(14, 14))
            self._learn_btn.setStyleSheet("QToolButton { padding: 1px 2px; }")

            # Initial text: show current CC if assigned
            current_cc = self._config.get_midi_cc(self._ch)
            self._set_midi_button_binding(
                self._learn_btn,
                "Volume",
                self._config.get_midi_channel(self._ch),
                current_cc,
            )
            self._vol_midi_menu = QMenu(self._learn_btn)
            self._learn_btn.setMenu(self._vol_midi_menu)
            self._vol_midi_menu.aboutToShow.connect(self._rebuild_vol_midi_menu)
            self._learn_btn.clicked.connect(self._on_learn_clicked)

            self._remove_midi_btn = QToolButton()
            self._remove_midi_btn.setIcon(QIcon.fromTheme('list-remove'))
            self._remove_midi_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            self._remove_midi_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._remove_midi_btn.setMinimumHeight(24)
            self._remove_midi_btn.setToolTip("Remove this MIDI channel.")
            self._remove_midi_btn.setAccessibleName("Remove MIDI channel")
            self._remove_midi_btn.setIconSize(QSize(14, 14))
            self._remove_midi_btn.setStyleSheet("QToolButton { padding: 1px 2px; }")
            self._remove_midi_btn.clicked.connect(self._on_remove_midi_clicked)

            self._mute_learn_btn = QToolButton()
            self._mute_learn_btn.setIcon(QIcon.fromTheme('audio-volume-muted'))
            self._mute_learn_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self._mute_learn_btn.setCheckable(True)
            self._mute_learn_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._mute_learn_btn.setMinimumHeight(24)
            self._mute_learn_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
            self._mute_learn_btn.setIconSize(QSize(14, 14))
            self._mute_learn_btn.setStyleSheet("QToolButton { padding: 1px 2px; }")
            current_mute_cc = self._config.get_midi_mute_cc(self._ch)
            self._set_midi_button_binding(
                self._mute_learn_btn,
                "Mute",
                self._config.get_midi_mute_channel(self._ch),
                current_mute_cc,
            )
            self._mute_midi_menu = QMenu(self._mute_learn_btn)
            self._mute_learn_btn.setMenu(self._mute_midi_menu)
            self._mute_midi_menu.aboutToShow.connect(self._rebuild_mute_midi_menu)
            self._mute_learn_btn.clicked.connect(self._on_mute_learn_clicked)

            midi_controls_layout = QVBoxLayout()
            midi_controls_layout.setContentsMargins(0, 2, 0, 0)
            midi_controls_layout.setSpacing(2)
            midi_controls_layout.addWidget(self._learn_btn)
            midi_controls_layout.addWidget(self._mute_learn_btn)
            if self.is_midi_channel:
                midi_controls_layout.addWidget(self._remove_midi_btn)
            layout.addLayout(midi_controls_layout)

            controls_visible = self._config.is_remote
            self._learn_btn.setVisible(controls_visible)
            self._mute_learn_btn.setVisible(controls_visible)
            self._remove_midi_btn.setVisible(False)

        layout.addStretch()
        self._restore_width_constraints()
        self._update_minimum_height()

        self.refresh_theme()
        self._refresh_app_list()

    @staticmethod
    def _set_midi_button_binding(
        button: QToolButton,
        kind: str,
        midi_channel: int,
        cc: int | None,
    ) -> None:
        description = _describe_midi_binding(kind, midi_channel, cc)
        button.setText(_format_midi_binding(midi_channel, cc, "—"))
        button.setAccessibleName(description)
        button.setToolTip(
            f"{description}.\n"
            f"Click to learn the {kind.lower()} CC; use the arrow to select MIDI channel 1-16."
        )

    def _restore_width_constraints(self) -> None:
        # Keep strips font-relative and independent of long assignment/binding labels.
        # Those labels elide or use compact notation, while overflow remains scrollable.
        control_widths = [self.fontMetrics().horizontalAdvance("MMMMMMMMM")]
        if self._show_midi_bindings:
            control_widths.extend(
                button.minimumSizeHint().width()
                for button in (self._learn_btn, self._mute_learn_btn)
            )
        if self.is_midi_channel:
            control_widths.append(self._remove_midi_btn.minimumSizeHint().width())
        self._normal_min_width = max(control_widths)
        self._normal_max_width = self._normal_min_width
        self.setFixedWidth(self._normal_min_width)

    def _update_minimum_height(self) -> None:
        layout = self.layout()
        if layout is None:
            return
        layout.activate()
        self.setMinimumHeight(layout.minimumSize().height())

    @_slot_guard
    def _rebuild_vol_midi_menu(self) -> None:
        self._vol_midi_menu.clear()
        current = self._config.get_midi_channel(self._ch)
        for display_channel in range(1, 17):
            action = self._vol_midi_menu.addAction(f"MIDI channel {display_channel}")
            action.setCheckable(True)
            action.setChecked(display_channel - 1 == current)
            action.triggered.connect(
                lambda _checked=False, value=display_channel - 1: self._set_vol_midi_channel(value)
            )

    @_slot_guard
    def _rebuild_mute_midi_menu(self) -> None:
        self._mute_midi_menu.clear()
        current = self._config.get_midi_mute_channel(self._ch)
        for display_channel in range(1, 17):
            action = self._mute_midi_menu.addAction(f"MIDI channel {display_channel}")
            action.setCheckable(True)
            action.setChecked(display_channel - 1 == current)
            action.triggered.connect(
                lambda _checked=False, value=display_channel - 1: self._set_mute_midi_channel(value)
            )

    @_slot_guard
    def _set_vol_midi_channel(self, midi_channel: int) -> None:
        self._config.set_midi_channel(self._ch, midi_channel)
        self._refresh_vol_learn_label()

    @_slot_guard
    def _set_mute_midi_channel(self, midi_channel: int) -> None:
        self._config.set_midi_mute_channel(self._ch, midi_channel)
        self._refresh_mute_learn_label()

    def _refresh_vol_learn_label(self) -> None:
        self._set_midi_button_binding(
            self._learn_btn,
            "Volume",
            self._config.get_midi_channel(self._ch),
            self._config.get_midi_cc(self._ch),
        )

    def _refresh_mute_learn_label(self) -> None:
        self._set_midi_button_binding(
            self._mute_learn_btn,
            "Mute",
            self._config.get_midi_mute_channel(self._ch),
            self._config.get_midi_mute_cc(self._ch),
        )

    @_slot_guard
    def _on_learn_clicked(self, checked: bool) -> None:
        if checked:
            self._learn_btn.setText("Cancel")
            self._learn_btn.setAccessibleName("Cancel volume MIDI learn")
            # Visual feedback that we're listening
            pal = self._learn_btn.palette()
            pal.setColor(QPalette.ColorRole.ButtonText, QColor("red"))
            self._learn_btn.setPalette(pal)
            logger.debug("Channel %d entering MIDI Learn mode", self._ch)
        else:
            self._refresh_vol_learn_label()
            self._learn_btn.setPalette(QApplication.palette())

    def update_midi_cc(self, cc_number: int, midi_channel: int = 0) -> None:
        """Update the button text to show the newly assigned CC and uncheck."""
        self._learn_btn.setChecked(False)
        if self._config.is_remote:
            self._refresh_vol_learn_label()
        else:
            self._set_midi_button_binding(self._learn_btn, "Volume", midi_channel, cc_number)
        self._learn_btn.setPalette(QApplication.palette())
        logger.debug("Channel %d MIDI M%d/CC%d updated", self._ch, midi_channel + 1, cc_number)

    @_slot_guard
    def _on_mute_learn_clicked(self, checked: bool) -> None:
        if checked:
            self._mute_learn_btn.setText("Cancel")
            self._mute_learn_btn.setAccessibleName("Cancel mute MIDI learn")
            pal = self._mute_learn_btn.palette()
            pal.setColor(QPalette.ColorRole.ButtonText, QColor("red"))
            self._mute_learn_btn.setPalette(pal)
            logger.debug("Channel %d entering Mute CC Learn mode", self._ch)
        else:
            self._refresh_mute_learn_label()
            self._mute_learn_btn.setPalette(QApplication.palette())

    def update_midi_mute_cc(self, cc_number: int, midi_channel: int = 0) -> None:
        """Update the mute-CC button text after a successful learn."""
        self._mute_learn_btn.setChecked(False)
        if self._config.is_remote:
            self._refresh_mute_learn_label()
        else:
            self._set_midi_button_binding(self._mute_learn_btn, "Mute", midi_channel, cc_number)
        self._mute_learn_btn.setPalette(QApplication.palette())
        logger.debug("Channel %d Mute M%d/CC%d updated", self._ch, midi_channel + 1, cc_number)

    def set_edit_mode(self, visible: bool) -> None:
        """Show or hide the Learn, Mute-CC, and Delete buttons."""
        if not self._show_midi_bindings:
            return
        self._edit_mode = visible
        controls_visible = (visible or self._config.is_remote) and not self._compact_mode
        self._learn_btn.setVisible(controls_visible)
        self._mute_learn_btn.setVisible(controls_visible)
        self._remove_midi_btn.setVisible(controls_visible and self.is_midi_channel)
        self._update_minimum_height()

    def set_compact_mode(self, compact: bool) -> None:
        """Hide app list and controls below the separator; separator stays visible."""
        self._compact_mode = compact
        # Freeze width so fader spacing doesn't change when app list is hidden
        if compact:
            self.setFixedWidth(self.width())
        else:
            self._restore_width_constraints()

        # Tighten bottom margin in compact mode to remove empty space below separator
        self.layout().setContentsMargins(1, 2, 1, 1 if compact else 2)

        # Toggle RetainSizeWhenHidden so hidden widgets release their space
        for widget in (self._app_list_scroll, self._add_btn):
            sp = widget.sizePolicy()
            sp.setRetainSizeWhenHidden(not compact)
            widget.setSizePolicy(sp)

        # _invert_cb has RetainSizeWhenHidden=True by default; toggle it so
        # compact mode can actually shrink the layout.
        sp_inv = self._invert_cb.sizePolicy()
        sp_inv.setRetainSizeWhenHidden(not compact)
        self._invert_cb.setSizePolicy(sp_inv)

        self._sep.setVisible(True)
        self._app_list_scroll.setVisible(not compact)
        self._add_btn.setVisible(not compact)
        if compact:
            for i in range(self._toggles_layout.count()):
                item = self._toggles_layout.itemAt(i)
                if item and item.widget():
                    item.widget().setVisible(False)
        else:
            # Restore proper visibility — invert respects its setting
            self._mode_cb.setVisible(True)
            self._vsink_cb.setVisible(True)
            self._invert_cb.setVisible(self._config.show_invert_option)
        if self._show_midi_bindings:
            controls_visible = (self._edit_mode or self._config.is_remote) and not compact
            self._learn_btn.setVisible(controls_visible)
            self._mute_learn_btn.setVisible(controls_visible)
            self._remove_midi_btn.setVisible(controls_visible and self.is_midi_channel)
        self._update_minimum_height()

    @property
    def channel_index(self) -> int:
        """Zero-based index of this channel."""
        return self._ch

    def set_selected(self, selected: bool) -> None:
        """Highlight or de-highlight this strip as part of a multi-selection."""
        self._selected = selected
        if selected:
            accent_hex = QApplication.palette().color(QPalette.ColorRole.Highlight).name()
            # Use a QSS border for reliable cross-theme accent-coloured highlight.
            # Class-name selector limits the rule to this widget only; background:
            # transparent ensures the themed background remains visible.
            self.setStyleSheet(
                f"ChannelWidget {{ border: 2px solid {accent_hex}; "
                "border-radius: 3px; background: transparent; }}"
            )
        else:
            self.setStyleSheet("")
        self.update()

    def is_waiting_for_volume_learn(self) -> bool:
        """Return True if the volume Learn button is active."""
        return self._show_midi_bindings and self._learn_btn.isChecked()

    def is_waiting_for_mute_learn(self) -> bool:
        """Return True if the Mute-CC Learn button is active."""
        return self._show_midi_bindings and self._mute_learn_btn.isChecked()

    def is_waiting_for_midi(self) -> bool:
        """Return True if any Learn button is active (used for connection-reset)."""
        return self.is_waiting_for_volume_learn() or self.is_waiting_for_mute_learn()

    def cancel_learn(self) -> None:
        """Cancel any active MIDI learn without assigning a CC."""
        if not self._show_midi_bindings:
            return
        if self._learn_btn.isChecked():
            self._learn_btn.setChecked(False)
            self._on_learn_clicked(False)
        if self._mute_learn_btn.isChecked():
            self._mute_learn_btn.setChecked(False)
            self._on_mute_learn_clicked(False)

    def start_volume_learn(self) -> None:
        """Enter volume MIDI-CC learn mode programmatically (for bulk learn)."""
        if not self._show_midi_bindings or self._learn_btn.isChecked():
            return
        self._learn_btn.setChecked(True)
        self._on_learn_clicked(True)

    @_slot_guard
    def _on_remove_midi_clicked(self, checked: bool = False) -> None:
        reply = QMessageBox.question(
            self, "Remove MIDI Channel",
            f"Are you sure you want to remove {self._ch_label.text()}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._config.remove_midi_channel(self._ch)
            # Rebuild is triggered via settings_changed in config_manager;
            # _rebuild_channels handles widget cleanup — no deleteLater() needed here.

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_volume(self, volume: float) -> None:
        pct = int(volume * 100)
        if self._slider.value() != pct:
            logger.debug(
                "Channel UI volume update suppressed user command: channel=%d source=programmatic value=%d",
                self._ch,
                pct,
            )
        blocker = QSignalBlocker(self._slider)
        self._slider.setValue(pct)
        suffix = " ..." if self._config.is_pending(self._config.control_key(self._ch, "volume")) else ""
        self._level_label.setText(f"{pct} %{suffix}")
        del blocker

    @pyqtSlot(int, int, int)
    @_slot_guard
    def handle_midi_input(self, midi_channel: int, cc: int, value: int) -> None:
        """Real-time slider sync from MidiThread.midi_cc_received.
        Learn logic lives in MainWindow.on_midi_cc_received so there is one
        central break-on-first-match gate for both volume and mute-CC learn.
        """
        if getattr(self._config, "is_remote", False):
            return
        mapped_cc = self._config.get_midi_cc(self._ch)
        mapped_channel = self._config.get_midi_channel(self._ch)
        if mapped_cc is not None and cc == mapped_cc and midi_channel == mapped_channel:
            vol = midi_cc_to_volume(value)
            self.set_volume(vol)
            self._config.set_channel_volume(self._ch, vol)

    @pyqtSlot(int)
    @_slot_guard
    def _on_slider_changed(self, value: int) -> None:
        """Apply a genuine mouse, keyboard, or accessibility slider edit."""
        vol_float = value / 100.0
        logger.debug(
            "Channel UI volume command: channel=%d source=user_interaction value=%d remote=%s",
            self._ch,
            value,
            self._config.is_remote,
        )
        if self._config.is_remote:
            self.set_volume(self._config.get_channel_volume(self._ch))
        else:
            self._level_label.setText(f"{value} %")
        self._config.set_channel_volume(self._ch, vol_float)

    @pyqtSlot(str, bool)
    def _on_pending_changed(self, control_key: str, pending: bool) -> None:
        key_for = getattr(self._config, "control_key", None)
        if not callable(key_for):
            return
        controls = {
            key_for(self._ch, "volume"): self._level_label,
            key_for(self._ch, "mute"): self._mute_btn,
            key_for(self._ch, "label"): self._ch_label,
            key_for(self._ch, "mode"): self._mode_cb,
            key_for(self._ch, "mappings"): self._add_btn,
            key_for(self._ch, "hardware"): self._add_btn,
            key_for(self._ch, "inverted"): self._invert_cb,
            key_for(self._ch, "v-sink"): self._vsink_cb,
            key_for(self._ch, "volume-midi"): getattr(self, "_learn_btn", None),
            key_for(self._ch, "mute-midi"): getattr(self, "_mute_learn_btn", None),
        }
        control = controls.get(control_key)
        if control is None:
            return
        if control is self._level_label:
            current = self._level_label.text().removesuffix(" ...")
            self._level_label.setText(f"{current} ..." if pending else current)
        else:
            control.setEnabled(not pending)
            if self._config.is_remote and not self._remote_editable:
                control.setEnabled(False)

    def set_mute_state(self, is_muted: bool) -> None:
        self._muted = is_muted
        if is_muted:
            self._mute_btn.setIcon(QIcon.fromTheme("audio-volume-muted"))
            self._slider.setEnabled(False)
        else:
            self._mute_btn.setIcon(QIcon.fromTheme("audio-volume-high"))
            self._slider.setEnabled(self._gain_control_supported and self._remote_editable)

    def set_gain_control_supported(self, supported: bool) -> None:
        """Reflect whether the effective audio backend can apply channel gain."""
        self._gain_control_supported = supported
        self._gain_unsupported_badge.setVisible(not supported)
        if not supported:
            self._slider.setEnabled(False)
            self._slider.setToolTip(
                "Volume control unavailable "
                "(no usable gain backend in this runtime)"
            )
        else:
            # Only re-enable if not currently muted; mute state takes precedence.
            if not self._muted:
                self._slider.setEnabled(self._remote_editable)
            self._slider.setToolTip("")
        self._update_minimum_height()

    def set_v_sink_supported(self, supported: bool, reason: str = "") -> None:
        """Enable V-Sink actions only when the effective backend can honor them."""
        self._v_sink_supported = supported
        saved_enabled = self._config.is_v_sink_enabled(self._ch)
        self._vsink_cb.blockSignals(True)
        self._vsink_cb.setChecked(saved_enabled)
        self._vsink_cb.blockSignals(False)
        self._vsink_cb.setEnabled(supported and self._remote_editable)
        self._vsink_cb.setText("V-Sink")
        if supported:
            self._vsink_cb.setToolTip("Route audio through a NativMix virtual sink.")
        else:
            detail = reason or "The effective routing owner does not support NativMix V-Sinks."
            self._vsink_cb.setToolTip(
                f"{detail}\nThe saved preference is preserved for a usable NativMix owner."
            )
        self._update_minimum_height()

    def set_remote_editable(self, editable: bool) -> None:
        """Gate receiver-owned edits while leaving canonical values visible."""
        if not self._config.is_remote:
            return
        self._remote_editable = editable
        for control in (
            self._mute_btn,
            self._ch_label,
            self._sep,
            self._mode_cb,
            self._add_btn,
            self._invert_cb,
        ):
            control.setEnabled(editable)
        self._slider.setEnabled(editable and self._gain_control_supported and not self._muted)
        self._vsink_cb.setEnabled(editable and self._v_sink_supported)
        if self._show_midi_bindings:
            self._learn_btn.setEnabled(editable)
            self._mute_learn_btn.setEnabled(editable)
            self._remove_midi_btn.setEnabled(editable)
        for index in range(self._app_list_layout.count()):
            item = self._app_list_layout.itemAt(index)
            row = item.widget() if item is not None else None
            if row is not None:
                row.setEnabled(editable)
        detail = "" if editable else "Receiver editing is disabled; this value is read-only."
        if detail:
            self._learn_btn.setToolTip(detail)
            self._mute_learn_btn.setToolTip(detail)
        elif self._show_midi_bindings:
            self._refresh_vol_learn_label()
            self._refresh_mute_learn_label()

    def refresh(self) -> None:
        self._refresh_app_list()
        if self._show_midi_bindings:
            self._refresh_vol_learn_label()
            self._refresh_mute_learn_label()

    def update_settings(self) -> None:
        self._invert_cb.setVisible(self._config.show_invert_option)
        self._update_minimum_height()

    def refresh_theme(self) -> None:
        """Tell the channel to redraw components for the new theme."""
        self.update_dynamic_styles()
        # _app_list_layout contains _AppRow widgets
        for i in range(self._app_list_layout.count()):
            item = self._app_list_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), _AppRow):
                item.widget().update_dynamic_styles()

    def update_dynamic_styles(self) -> None:
        """Apply dynamic stylesheets directly to the components to override stubborn Qt defaults."""
        palette = QApplication.palette()

        # Prevent KDE from fading the accent color when the window loses focus
        for role in (
            QPalette.ColorRole.Highlight, QPalette.ColorRole.HighlightedText,
            QPalette.ColorRole.WindowText, QPalette.ColorRole.Button,
        ):
            palette.setColor(QPalette.ColorGroup.Inactive, role, palette.color(QPalette.ColorGroup.Active, role))

        self._slider.setPalette(palette)

        accent_hex = palette.color(QPalette.ColorRole.Highlight).name()
        # Use Button instead of Dark because Dark is not parsed by our KDE theme parser,
        # causing it to stay stuck on the previous theme's color!
        bg_hex = palette.color(QPalette.ColorRole.Button).name()
        text_color = palette.color(QPalette.ColorRole.WindowText)
        border_hex = f"rgba({text_color.red()}, {text_color.green()}, {text_color.blue()}, 50)"

        # 1. Sliders (Dynamic Theme Variables)
        # Use theme-compliant colors to prevent reverting to default blue when inactive.
        # Make the border slightly darker than the main accent color for better contrast
        slider_border_hex = palette.color(QPalette.ColorRole.Highlight).darker(150).name()

        slider_qss = f"""
        QSlider::groove:vertical {{
            background: {bg_hex};
            border: 1px solid {border_hex};
            width: 6px;
            border-radius: 3px;
        }}
        QSlider::add-page:vertical {{
            background: {accent_hex};
            border: 1px solid {border_hex};
            border-radius: 3px;
        }}
        QSlider::sub-page:vertical {{
            background: transparent;
        }}
        QSlider::handle:vertical {{
            background: {bg_hex};
            border: 1px solid {slider_border_hex};
            height: 12px;
            margin: 0 -4px;
            border-radius: 7px;
        }}
        """
        self._slider.setStyleSheet(slider_qss)

        # Color the labels using QPalette instead of stylesheets to avoid breaking Wayland native tooltips
        pal_ch = self._ch_label.palette()
        pal_ch.setColor(QPalette.ColorRole.WindowText, palette.color(QPalette.ColorRole.Highlight))
        self._ch_label.setPalette(pal_ch)

        pal_lvl = self._level_label.palette()
        pal_lvl.setColor(QPalette.ColorRole.WindowText, palette.color(QPalette.ColorRole.Highlight))
        self._level_label.setPalette(pal_lvl)

        # 3. ToolButtons (Mute, Add) Inherit Global Hover
        # We only set specific properties here if needed.
        btn_qss = "QToolButton, QPushButton { border: none; border-radius: 4px; }"
        self._mute_btn.setStyleSheet(btn_qss)
        self._add_btn.setStyleSheet(btn_qss)

        # 4. Re-apply selection highlight using the updated accent colour.
        self.set_selected(self._selected)

    # ------------------------------------------------------------------
    # App list
    # ------------------------------------------------------------------

    def _refresh_app_list(self) -> None:
        while self._app_list_layout.count():
            item = self._app_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        unresolved = (
            self._config.get_unresolved_targets()
            if hasattr(self._config, "get_unresolved_targets")
            else set()
        )

        if self._config.get_channel_mode(self._ch) == "hardware":
            hw_id = self._config.get_hardware_id(self._ch)
            if hw_id:
                display_name = (
                    self._config.get_target_label(hw_id)
                    if hasattr(self._config, "get_target_label")
                    else hw_id.removeprefix("sink:").removeprefix("source:")
                )
                row = _AppRow(display_name, on_remove=self._remove_hw)
                if self._config.is_remote:
                    row.set_receiver_availability(self._config.is_target_available(hw_id, "hardware"))
                self._app_list_layout.addWidget(row)
        else:
            for name in self._config.get_app_names(self._ch):
                row = _AppRow(name, on_remove=lambda _=False, n=name: self._remove_app(n))
                if name.lower() not in {"system master", "other apps"}:
                    row.routing_pause_toggled.connect(self._on_app_routing_pause_toggled)
                    row.set_routing_paused(self._config.is_app_routing_paused(self._ch, name))
                if self._config.is_remote:
                    row.set_receiver_availability(self._config.is_target_available(name, "app"))
                else:
                    row.set_unresolved(name in unresolved)
                self._app_list_layout.addWidget(row)

        # Hide V-Sink for special pseudo-apps (System Master / Other Apps),
        # hardware mode, or when running on Windows (no PipeWire null-sinks).
        _SPECIAL = ("system master", "other apps")
        app_names_lower = [n.lower() for n in self._config.get_app_names(self._ch)]
        has_special = any(n in _SPECIAL for n in app_names_lower)
        is_hw = self._config.get_channel_mode(self._ch) == "hardware"
        self._vsink_cb.setVisible(not has_special and not is_hw and not is_windows())
        self._update_minimum_height()

    @pyqtSlot(str, bool)
    @_slot_guard
    def _on_app_routing_pause_toggled(self, app_name: str, paused: bool) -> None:
        self._config.set_app_routing_paused(self._ch, app_name, paused)

    def update_unresolved_state(self, unresolved_targets: set) -> None:
        """
        Update the unresolved-target indicator on each _AppRow in this channel.

        Called when the backend emits ``unresolved_targets_changed`` so that the
        UI stays in sync without requiring a full list rebuild.
        """
        for i in range(self._app_list_layout.count()):
            item = self._app_list_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), _AppRow):
                row: _AppRow = item.widget()
                row.set_unresolved(row.app_name in unresolved_targets)

    def _remove_app(self, app_name: str) -> None:
        self._config.remove_app_name(self._ch, app_name)
        self._refresh_app_list()

    def _remove_hw(self, _=False) -> None:
        self._config.clear_hardware_target(self._ch)
        self._refresh_app_list()

    # ------------------------------------------------------------------
    # Mode Switching
    # ------------------------------------------------------------------

    @pyqtSlot(bool)
    @_slot_guard
    def _on_mode_toggled(self, checked: bool) -> None:
        mode = "hardware" if checked else "app"
        self._config.change_channel_mode(self._ch, mode)
        if getattr(self._config, "is_remote", False):
            self._mode_cb.blockSignals(True)
            self._mode_cb.setChecked(self._config.get_channel_mode(self._ch) == "hardware")
            self._mode_cb.blockSignals(False)
        self._apply_mode_ui(self._config.get_channel_mode(self._ch) == "hardware")
        if not getattr(self._config, "is_remote", False):
            self._refresh_app_list()

    def _apply_mode_ui(self, is_hw: bool) -> None:
        if is_hw:
            self._add_btn.setText("+ Device")
            self._add_btn.setToolTip("Assign hardware input/output.")
        else:
            self._add_btn.setText("+ App")
            self._add_btn.setToolTip("Assign audio stream.")
        # V-Sink visibility is handled by _refresh_app_list called after this

    # ------------------------------------------------------------------
    # Stream / Hardware picker
    # ------------------------------------------------------------------

    def _open_picker(self, checked: bool = False) -> None:
        if self._config.get_channel_mode(self._ch) == "hardware":
            self._open_hw_picker()
        else:
            self._open_stream_picker()

    def _open_hw_picker(self) -> None:
        current_hw = self._config.get_hardware_id(self._ch)
        menu = QMenu(self)
        targets = self._config.get_target_inventory("hardware")
        for kind, heading in (("output", "── Outputs ──"), ("input", "── Inputs ──")):
            matching = sorted(
                (item for item in targets if item.kind == kind),
                key=lambda item: item.label.casefold(),
            )
            if not matching:
                continue
            if not menu.isEmpty():
                menu.addSeparator()
            header = menu.addAction(heading)
            header.setEnabled(False)
            for item in matching:
                label = item.label if item.available else f"{item.label} (unavailable)"
                action = menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(item.key == current_hw)
                action.setToolTip(
                    "Configured receiver target is currently unavailable."
                    if not item.available
                    else f"Receiver {kind}: {item.label}"
                )
                action.triggered.connect(lambda _=False, key=item.key: self._on_hw_picked(key))
        if not targets:
            a = menu.addAction("No hardware found")
            a.setEnabled(False)

        menu.exec(self._add_btn.mapToGlobal(self._add_btn.rect().bottomLeft()))

    def _on_hw_picked(self, hw_id: str) -> None:
        self._config.toggle_hardware_target(self._ch, hw_id)
        if not getattr(self._config, "is_remote", False):
            self._refresh_app_list()

    def _open_stream_picker(self) -> None:
        # Every logical target may be shared across channels.
        already_here = set(self._config.get_app_names(self._ch))
        menu = QMenu(self)
        targets = self._config.get_target_inventory("app")
        added_actions = 0
        for item in sorted(
            targets,
            key=lambda target: (
                0 if target.label == "System Master" else 1 if target.label == "Other Apps" else 2,
                target.label.casefold(),
            ),
        ):
            name = item.label
            label = name if item.available else f"{name} (unavailable)"
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(name in already_here)
            if name in ("System Master", "Other Apps"):
                font = action.font()
                font.setBold(True)
                action.setFont(font)

            action.triggered.connect(
                lambda _=False, key=item.key: self._on_stream_picked(key)
            )
            action.setToolTip(
                "Configured receiver target is currently unavailable; the mapping will be preserved."
                if not item.available
                else f"Receiver target: {name}"
            )
            added_actions += 1

        if added_actions == 0:
            a = menu.addAction("No available streams")
            a.setEnabled(False)

        if not getattr(self._config, "is_remote", False):
            menu.addSeparator()
            type_action = menu.addAction("✏  Enter app name…")
            type_action.triggered.connect(self._open_manual_app_input)

        menu.exec(self._add_btn.mapToGlobal(self._add_btn.rect().bottomLeft()))

    def _on_test_set_50_percent(self, checked: bool = False) -> None:
        """
        Temporary dev/test action: bypass debounce and channel-binding logic
        entirely and force-write 50% volume directly to whatever PW/PA stream
        currently matches this channel's mapped app names.  Used to quickly
        prove the backend write path (pw-cli/wpctl/pactl) is functioning
        without waiting on slider debounce or `_should_apply_volume` gating.
        """
        app_names = self._config.get_app_names(self._ch)
        if not app_names:
            logger.info("Test action: channel %d has no mapped apps — nothing to set", self._ch)
            return
        logger.info(
            "Test action: forcing 50%% volume on channel %d apps=%s (bypassing debounce/binding)",
            self._ch, app_names,
        )
        for name in app_names:
            if hasattr(self._backend, "_apply_volume_by_name"):
                self._backend._apply_volume_by_name(name, 0.5)

    def _open_manual_app_input(self, checked: bool = False) -> None:
        name, ok = QInputDialog.getText(self, "Pin App", "App name:")
        if ok and name.strip():
            self._on_stream_picked(name.strip())

    def _on_stream_picked(self, target_key: str) -> None:
        try:
            self._config.toggle_mapping(self._ch, target_key)
        except ValueError as e:
            _msg = QMessageBox(self)
            _msg.setIcon(QMessageBox.Icon.NoIcon)
            _msg.setWindowTitle("NativMix")
            _msg.setText(f"⚠  {e}")
            _msg.exec()
            # Re-open the picker so the user can choose a different app
            self._open_stream_picker()
            return

        if not getattr(self._config, "is_remote", False):
            self._refresh_app_list()

    def _on_rename(self, new_name: str) -> None:
        self._config.set_channel_label(self._ch, new_name)
        if not getattr(self._config, "is_remote", False):
            self._ch_label.setText(new_name)

    # ------------------------------------------------------------------
    # Inversion
    # ------------------------------------------------------------------

    @pyqtSlot(bool)
    @_slot_guard
    def _on_invert_toggled(self, checked: bool) -> None:
        self._config.set_inverted(self._ch, checked)
        if getattr(self._config, "is_remote", False):
            self._invert_cb.blockSignals(True)
            self._invert_cb.setChecked(self._config.get_effective_inversion(self._ch))
            self._invert_cb.blockSignals(False)
        logger.debug("Channel %d inversion: %s", self._ch, checked)

    def set_other_apps_tooltip(self, names: list[str]) -> None:
        """Dynamically update the tooltip for the 'Other Apps' label."""
        app_names = [n.lower() for n in self._config.get_app_names(self._ch)]
        if "other apps" not in app_names:
            return

        text = "Contains:\n• " + "\n• ".join(names) if names else "No other apps active"

        for i in range(self._app_list_layout.count()):
            item = self._app_list_layout.itemAt(i)
            if item and item.widget():
                widget = item.widget()
                if isinstance(widget, _AppRow) and widget.app_name.lower() == "other apps":
                    widget.set_name_tooltip(text)
                    self._slider.setToolTip(text)
                    break

    @pyqtSlot(bool)
    @_slot_guard
    def _on_vsink_toggled(self, checked: bool) -> None:
        if checked and not self._v_sink_supported:
            self.set_v_sink_supported(False, getattr(self._backend, "v_sink_capability_reason", ""))
            return
        self._config.set_v_sink_enabled(self._ch, checked)
        if getattr(self._config, "is_remote", False):
            self._vsink_cb.blockSignals(True)
            self._vsink_cb.setChecked(self._config.is_v_sink_enabled(self._ch))
            self._vsink_cb.blockSignals(False)
        logger.debug("Channel %d V-Sink enabled: %s", self._ch, checked)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """
    NativMix main mixer window.

    Pure native Qt style – no QSS, no manual palette colors.
    Responds to KDE dark/light theme switches via QApplication.paletteChanged.
    """

    profile_switch_requested = pyqtSignal(str)  # profile_id
    delete_profile_requested = pyqtSignal(str)  # profile_id
    fader_display_synced = pyqtSignal()

    def __init__(
        self, config: ConfigManager, backend: AudioBackendBase,
        arduino_thread: ArduinoThread | None = None,
        midi_thread: MidiThread | None = None,
        profile_manager: ProfileManager | None = None,
        mixer_facade: MixerFacade | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._config  = config
        self._backend = backend
        self._arduino = arduino_thread
        self._midi    = midi_thread
        self._profile_manager = profile_manager
        self._local_mixer = mixer_facade or LocalMixerFacade(
            config,
            profile_manager,
            backend,
            parent=self,
        )
        self._mixer = self._local_mixer
        self._channels: list[ChannelWidget] = []
        self._last_mode = self._config.input_mode
        self.settings = QSettings('nativmix', 'GUI')

        # Multi-select state
        self._selected_channels: set[int] = set()
        self._last_clicked_index: int = -1

        # Guard: set True while a show() is in flight to suppress spurious hide.
        self._show_requested: bool = False

        from nativmix.metadata import __app_name__, __version__
        self.setWindowTitle(f"{__app_name__} v{__version__}")
        # ── Window Flags ──
        # Tool is the correct type for accessory windows on all compositors
        # (KDE Wayland, COSMIC, X11).  Window|SkipTaskbarHint breaks mapping
        # on some Wayland compositors without a valid activation token.
        self.setWindowFlags(
            Qt.WindowType.Tool |
            Qt.WindowType.FramelessWindowHint
        )

        from nativmix.utils.paths import get_icon_path
        icon_path = get_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))
        else:
            self.setWindowIcon(QIcon.fromTheme("nativmix", QIcon.fromTheme("audio-volume-high")))


        # UI Stabilization: Fix size to prevent jumping for tiling engines
        self.setMinimumSize(400, 420)
        self.resize(400, 420)

        # Flicker Protection: Disable updates until audit is finished
        self.setUpdatesEnabled(False)

        # ARGB surface is required for border-radius to clip corners correctly.
        # Wayland always supports ARGB; alpha=255 keeps the window opaque when
        # transparency is disabled, but the compositor still sees transparent corners.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._setup_ui()

        # ── Universal Volume Sync ──
        # Delay startup sync slightly to allow background threads to connect.
        QTimer.singleShot(250, self.sync_ui_to_hardware)

    @property
    def mixer_facade(self) -> MixerFacade:
        """Return the currently displayed local or receiver mixer boundary."""
        return self._mixer

    def set_mixer_facade(self, mixer_facade: MixerFacade) -> None:
        """Atomically switch the mirrored area without changing laptop managers."""
        if mixer_facade is self._mixer:
            self._on_mixer_state_changed()
            return
        old = self._mixer
        try:
            old.state_changed.disconnect(self._on_mixer_state_changed)
        except (AttributeError, RuntimeError, TypeError):
            pass
        try:
            old.status_changed.disconnect(self._update_remote_banner)
        except (AttributeError, RuntimeError, TypeError):
            pass
        try:
            old.pending_changed.disconnect(self._on_mixer_pending_changed)
        except (AttributeError, RuntimeError, TypeError):
            pass
        self._mixer = mixer_facade
        self.settings_panel.set_mixer_facade(mixer_facade)
        mixer_facade.state_changed.connect(self._on_mixer_state_changed)
        mixer_facade.status_changed.connect(self._update_remote_banner)
        mixer_facade.pending_changed.connect(self._on_mixer_pending_changed)
        self._on_mixer_pending_changed("profiles", False)
        self._on_mixer_pending_changed("channels", False)
        self._mixer_structure_signature: object | None = None
        self._on_mixer_state_changed()

    @pyqtSlot(str, bool)
    def _on_mixer_pending_changed(self, control_key: str, pending: bool) -> None:
        editable = not self._mixer.is_remote or self._mixer.editing_allowed
        if control_key == "profiles":
            for control in (self._profile_combo, self._profile_add_btn, self._profile_delete_btn):
                control.setEnabled(editable and not pending)
        elif control_key == "channels":
            self._add_midi_btn.setEnabled(editable and not pending)
            self._bulk_delete_btn.setEnabled(editable and not pending)

    @pyqtSlot()
    def _on_mixer_state_changed(self) -> None:
        """Render one committed facade state on the Qt main thread."""
        channels = self._mixer.all_channels()
        signature = (
            self._mixer.active_profile_id,
            tuple((channel.get("channel_id"), channel["index"], channel.get("is_midi")) for channel in channels),
            tuple(self._mixer.get_channel_order()),
        )
        if signature != getattr(self, "_mixer_structure_signature", None):
            self._mixer_structure_signature = signature
            self._rebuild_channels()
        else:
            for channel in self._channels:
                channel.refresh()
                channel.set_volume(self._mixer.get_channel_volume(channel.channel_index))
                channel.set_mute_state(self._mixer.is_channel_muted(channel.channel_index))
                channel.set_gain_control_supported(self._mixer.gain_control_supported)
                channel.set_v_sink_supported(
                    self._mixer.v_sink_supported,
                    self._mixer.v_sink_capability_reason,
                )
        self._populate_profile_combo()
        editable = not self._mixer.is_remote or self._mixer.editing_allowed
        for control in (self._profile_combo, self._profile_add_btn, self._profile_delete_btn):
            control.setEnabled(editable and not self._mixer.is_pending("profiles"))
        self._add_midi_btn.setEnabled(editable and not self._mixer.is_pending("channels"))
        self._bulk_delete_btn.setEnabled(editable and not self._mixer.is_pending("channels"))
        for channel in self._channels:
            channel.set_remote_editable(editable)
        if self._mixer.active_profile_id:
            try:
                profile = self._mixer.load_profile(self._mixer.active_profile_id)
                self.settings_panel.update_profile_ui(profile, len(self._mixer.list_profiles()) > 1)
            except (KeyError, RuntimeError, ValueError):
                logger.debug("Active mixer profile disappeared during refresh", exc_info=True)
        self._update_remote_banner()
        self.refresh_layout()

    def _update_remote_banner(self, *_args: object) -> None:
        if not self._mixer.is_remote:
            self._remote_banner.setVisible(False)
            return
        status = self._mixer.sync_status
        peer = self._mixer.receiver_name or "receiver"
        text = f"Remote mixer — {peer}"
        self._remote_banner.setText(text)
        self._remote_banner.setAccessibleName(f"Remote mixer for {peer}")
        self._remote_banner.setToolTip(
            f"{self._mixer.active_profile_name or 'No profile'} · {status}\n{self._mixer.sync_detail}"
        )
        self._remote_banner.setVisible(True)

    def _setup_ui(self) -> None:
        # ── Central widget ─────────────────────────────────────────────
        central = QFrame()
        central.setObjectName("MainFrame")
        self.setCentralWidget(central)

        self._apply_transparency()
        self._root_layout = QVBoxLayout(central)
        self._root_layout.setContentsMargins(8, 8, 8, 8)
        self._root_layout.setSpacing(6)
        root = self._root_layout

        self._remote_banner = QLabel()
        self._remote_banner.setObjectName("remote_mixer_banner")
        self._remote_banner.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        banner_font = self._remote_banner.font()
        banner_font.setBold(True)
        banner_font.setPointSize(max(8, banner_font.pointSize() - 1))
        self._remote_banner.setFont(banner_font)
        self._remote_banner.setMargin(1)
        self._remote_banner.setWordWrap(False)
        self._remote_banner.setVisible(False)
        root.addWidget(self._remote_banner)

        # ── Collapsible Settings Area & Pin ────────────────────────────
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)

        self._toggle_settings_btn = QRadioButton("Settings")
        self._toggle_settings_btn.setToolTip("Show or hide the settings panel.")
        self._toggle_settings_btn.setAutoExclusive(False)
        self._toggle_settings_btn.setChecked(False)
        self._toggle_settings_btn.toggled.connect(self._on_settings_toggled)

        top_bar.addWidget(self._toggle_settings_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        # ── Profile selector ────────────────────────────────────────────
        if self._profile_manager is not None:
            self._profile_combo = QComboBox()
            self._profile_combo.setEditable(True)
            self._profile_combo.setMinimumWidth(120)
            self._profile_combo.setToolTip("Active profile — click to switch, type to rename")
            self._populate_profile_combo()

            self._profile_add_btn = QPushButton("+")
            self._profile_add_btn.setFixedSize(QSize(26, 26))
            self._profile_add_btn.setToolTip("Create or duplicate a profile")
            profile_add_menu = QMenu(self._profile_add_btn)
            profile_add_menu.addAction("Create blank profile").triggered.connect(self._on_add_profile_clicked)
            profile_add_menu.addAction("Duplicate current profile").triggered.connect(
                self._on_duplicate_profile_clicked
            )
            self._profile_add_btn.setMenu(profile_add_menu)

            self._profile_delete_btn = QPushButton("-")
            self._profile_delete_btn.setFixedSize(QSize(26, 26))
            self._profile_delete_btn.setToolTip("Delete current profile")
            self._profile_delete_btn.clicked.connect(self._on_delete_profile_clicked)

            top_bar.addWidget(self._profile_combo, alignment=Qt.AlignmentFlag.AlignLeft)
            top_bar.addWidget(self._profile_add_btn, alignment=Qt.AlignmentFlag.AlignLeft)
            top_bar.addWidget(self._profile_delete_btn, alignment=Qt.AlignmentFlag.AlignLeft)

            # Debounce rename: only save after 500 ms of no typing
            self._profile_rename_timer = QTimer(self)
            self._profile_rename_timer.setSingleShot(True)
            self._profile_rename_timer.setInterval(500)
            self._profile_rename_timer.timeout.connect(self._apply_profile_rename)

            self._profile_combo.currentIndexChanged.connect(self._on_profile_selected)
            self._profile_combo.editTextChanged.connect(
                lambda _: self._profile_rename_timer.start()
            )

            self._profile_manager.profile_list_changed.connect(self._populate_profile_combo)
            self._profile_manager.profile_changed.connect(self._on_profile_changed_externally)

        top_bar.addStretch()

        self._pin_btn = QRadioButton("Don't Close")
        self._pin_btn.setToolTip("Keep the window open instead of hiding to tray on close.")
        self._pin_btn.setAutoExclusive(False)
        self._pin_btn.setChecked(self._config.stay_open)
        self._pin_btn.toggled.connect(self._on_pin_toggled)

        self._compact_btn = QRadioButton("Compact")
        self._compact_btn.setToolTip("Hide app assignments and controls — show faders only.")
        self._compact_btn.setAutoExclusive(False)
        self._compact_btn.setChecked(self._config.compact_mode)
        self._compact_btn.toggled.connect(self._on_compact_toggled)

        top_bar.addWidget(self._compact_btn, alignment=Qt.AlignmentFlag.AlignRight)
        top_bar.addWidget(self._pin_btn, alignment=Qt.AlignmentFlag.AlignRight)

        root.addLayout(top_bar)

        self.settings_panel = SettingsPanel(
            self._config,
            profile_manager=self._profile_manager,
            mixer_facade=self._mixer,
        )
        self._settings_scroll = QScrollArea()
        self._settings_scroll.setWidgetResizable(True)
        self._settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._settings_scroll.setWidget(self.settings_panel)
        self._settings_scroll.setVisible(False)
        root.addWidget(self._settings_scroll)
        self._update_checker = UpdateChecker(self._config, parent=self)
        self.settings_panel.update_checks_changed.connect(self._on_update_checks_changed)
        self._update_checker.release_available.connect(self._on_update_available)

        # ── Scrollable channel area ────────────────────────────────────
        self._channel_scroll = QScrollArea()
        self._channel_scroll.setWidgetResizable(True)
        self._channel_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._channel_scroll.viewport().setAutoFillBackground(False)
        self._channel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._channel_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._channel_container = QWidget()
        self._ch_layout = QHBoxLayout(self._channel_container)
        self._ch_layout.setContentsMargins(0, 0, 0, 0)
        self._ch_layout.setSpacing(1)
        self._ch_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._ch_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        self._channel_scroll.setWidget(self._channel_container)
        root.addWidget(self._channel_scroll)
        self._drag_channel_index: int | None = None
        self._drag_global_pos: QPoint | None = None
        self._drag_autoscroll_timer = QTimer(self)
        self._drag_autoscroll_timer.setInterval(35)
        self._drag_autoscroll_timer.timeout.connect(self._autoscroll_channel_drag)

        # ── Add MIDI Channel Button ──
        self._add_midi_btn = QPushButton("+ Add MIDI Channel")
        self._add_midi_btn.clicked.connect(self._on_add_midi_clicked)
        # Visible only in hybrid/midi_only modes. Set visibility initially:
        _midi_mode = self._config.input_mode in ("hybrid", "midi_only")
        _compact = self._config.compact_mode
        self._add_midi_btn.setVisible(_midi_mode and not _compact)

        # ── Edit MIDI Channel Toggle Button ──
        self._edit_midi_btn = QPushButton("✏ Edit MIDI Channel")
        self._edit_midi_btn.setCheckable(True)
        self._edit_midi_btn.setVisible(_midi_mode and not _compact)
        self._edit_midi_btn.toggled.connect(self._on_edit_midi_toggled)

        # ── Bulk-action buttons (shown when strips are selected) ────────
        self._bulk_delete_btn = QPushButton("Delete Selected")
        self._bulk_delete_btn.setVisible(False)
        self._bulk_delete_btn.setToolTip("Delete all selected MIDI channels")
        self._bulk_delete_btn.clicked.connect(self._on_bulk_delete)

        self._bulk_learn_btn = QPushButton("MIDI Learn Selected")
        self._bulk_learn_btn.setVisible(False)
        self._bulk_learn_btn.setToolTip("Start MIDI CC learn on all selected MIDI channels")
        self._bulk_learn_btn.clicked.connect(self._on_bulk_midi_learn)

        # ── Size Grip (for frameless resizing) ─────────────────────────
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(self._add_midi_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        bottom_layout.addWidget(self._edit_midi_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        bottom_layout.addWidget(self._bulk_delete_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        bottom_layout.addWidget(self._bulk_learn_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        bottom_layout.addStretch()
        self._size_grip = QSizeGrip(self)
        bottom_layout.addWidget(self._size_grip, alignment=Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        root.addLayout(bottom_layout)

        # ── Build initial channels ─────────────────────────────────────
        self._rebuild_channels()
        self.refresh_layout()

        # ── Restore geometry ───────────────────────────────────────────
        geom = self.settings.value('geometry')
        self._has_saved_geometry = bool(geom)
        # Debounce geometry writes: moveEvent/resizeEvent fire on every pixel.
        # The timer is restarted on each call; the actual write happens once,
        # 500 ms after the last movement/resize ends.
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(500)
        self._geometry_save_timer.timeout.connect(self._flush_geometry)
        if geom:
            self.restoreGeometry(geom)
            # Guard: if the restored position is off every screen (e.g. after a
            # resolution change or panel resize) move the window to the primary
            # screen's available area so it stays visible.
            win_rect = self.frameGeometry()
            on_screen = any(
                s.availableGeometry().intersects(win_rect)
                for s in QApplication.screens()
            )
            if not on_screen:
                logger.debug("Restored geometry is off-screen – resetting to primary screen")
                self.settings.remove('geometry')
                primary = QApplication.primaryScreen()
                if primary:
                    ag = primary.availableGeometry()
                    self.move(ag.x(), ag.y())

        # ── Restore GUI state ──────────────────────────────────────────
        if self.settings.value('settings_open', False, type=bool):
            self._toggle_settings_btn.setChecked(True)
        if self.settings.value('edit_midi_active', False, type=bool):
            self._edit_midi_btn.setChecked(True)

        # ── Signal connections ─────────────────────────────────────────
        self._config.mapping_changed.connect(self._on_mapping_changed)
        self._config.routing_pause_changed.connect(self._on_routing_pause_changed)
        self._config.settings_changed.connect(self._apply_transparency)
        self._config.settings_changed.connect(self._on_settings_updated)
        self._backend.other_apps_changed.connect(self._on_other_apps_changed)
        if hasattr(self._backend, "unresolved_targets_changed"):
            self._backend.unresolved_targets_changed.connect(self._on_unresolved_targets_changed)
        if hasattr(self._backend, "status_changed"):
            # Use QueuedConnection to guarantee GUI-thread delivery: status_changed
            # may be emitted from a background thread (e.g. _PipeWirePollerThread in
            # PW-only / Flatpak mode), and direct cross-thread calls to UI methods
            # are unsafe and can cause the window to remain hidden on startup.
            self._backend.status_changed.connect(
                self._on_audio_status_changed,
                Qt.ConnectionType.QueuedConnection,
            )
        if hasattr(self._backend, "capability_changed"):
            self._backend.capability_changed.connect(
                self._on_capability_changed,
                Qt.ConnectionType.QueuedConnection,
            )
        if hasattr(self._backend, "routing_owner_status_changed"):
            self._backend.routing_owner_status_changed.connect(
                self.settings_panel.set_routing_owner_status,
                Qt.ConnectionType.QueuedConnection,
            )

        # Qt emits paletteChanged when the system theme switches – no CSS needed
        QApplication.instance().paletteChanged.connect(self._on_palette_changed)

        self.settings_panel.panic_triggered.connect(self._on_panic_triggered)
        self.settings_panel.master_refresh_requested.connect(self._on_master_refresh)
        self.settings_panel.master_output_changed.connect(self._on_master_changed)
        if self._midi:
            self.settings_panel.midi_panic_triggered.connect(self._midi.restart_midi)
        # ── Initial Population ──
        self._on_master_refresh()

    @pyqtSlot(bool)
    @_slot_guard
    def _on_update_checks_changed(self, enabled: bool) -> None:
        if enabled:
            self._update_checker.check_now()
        else:
            self._update_checker.cancel()

    def check_for_updates_at_startup(self) -> None:
        """Start the opted-in check after startup coordination has completed."""
        self._update_checker.check_at_startup()

    @pyqtSlot(str, str)
    @_slot_guard
    def _on_update_available(self, installed_version: str, available_version: str) -> None:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("NativMix update available")
        dialog.setText(f"NativMix {available_version} is available.")
        dialog.setInformativeText(
            f"Installed version: {installed_version}\n"
            f"Available version: {available_version}\n\n"
            "NativMix will only open the release page; it will not download or run anything."
        )
        view_release = dialog.addButton("View release", QMessageBox.ButtonRole.ActionRole)
        ignore_release = dialog.addButton("Ignore this version", QMessageBox.ButtonRole.DestructiveRole)
        dialog.addButton("Remind me later", QMessageBox.ButtonRole.RejectRole)
        dialog.exec()

        clicked = dialog.clickedButton()
        if clicked is view_release:
            QDesktopServices.openUrl(QUrl(RELEASE_PAGE_URL))
        elif clicked is ignore_release:
            self._update_checker.ignore_version(available_version)

    # ------------------------------------------------------------------
    # Channel management
    # ------------------------------------------------------------------

    def _rebuild_channels(self) -> None:
        # Layout Batching: Disable layout updates during population
        self._ch_layout.setEnabled(False)
        try:
            while self._ch_layout.count():
                item = self._ch_layout.takeAt(0)
                widget = item.widget()
                if widget:
                    if isinstance(widget, ChannelWidget) and widget.is_midi_channel and self._midi:
                        try:
                            self._midi.midi_cc_received.disconnect(widget.handle_midi_input)
                        except RuntimeError:
                            pass
                    widget.deleteLater()
            self._channels.clear()
            # Channel indices change on rebuild; old selection is no longer valid.
            self._selected_channels.clear()
            self._last_clicked_index = -1

            widgets_by_id: dict[int, ChannelWidget] = {}
            for ch_dict in self._mixer.all_channels():
                i = ch_dict["index"]
                is_midi = ch_dict.get("is_midi", False)
                # In USB mode MIDI widgets are purged by refresh_layout anyway;
                # skip creating them to avoid the wasted create-then-destroy cycle.
                if is_midi and self._mixer.input_mode == "usb":
                    continue
                w = ChannelWidget(i, self._mixer, self._mixer, is_midi=is_midi)
                widgets_by_id[i] = w
                # Ensure MIDI-relevant signals are connected even after rebuild
                if w.is_midi_channel and self._midi:
                    self._midi.midi_cc_received.connect(w.handle_midi_input)
                # Apply current edit mode so buttons show/hide correctly —
                # kept outside the self._midi guard so it fires on every rebuild.
                if w.is_midi_channel and hasattr(self, '_edit_midi_btn'):
                    w.set_edit_mode(self._edit_midi_btn.isChecked())

                # Apply compact mode
                if hasattr(self, '_compact_btn'):
                    w.set_compact_mode(self._compact_btn.isChecked())

                # Wire multi-select: Ctrl/Shift-click on the channel label
                w.strip_clicked.connect(self._on_strip_clicked)
                w._sep.drag_started.connect(self._on_channel_drag_started)
                w._sep.drag_moved.connect(self._on_channel_drag_moved)
                w._sep.drag_finished.connect(self._on_channel_drag_finished)
                w._sep.move_requested.connect(self._move_channel_by_step)

                # Apply the effective gain capability so newly created widgets
                # reflect the probe result even if the signal fired before rebuild.
                if hasattr(self._mixer, "gain_control_supported"):
                    w.set_gain_control_supported(self._mixer.gain_control_supported)
                backend_v_sink_supported = getattr(self._mixer, "v_sink_supported", None)
                if isinstance(backend_v_sink_supported, bool):
                    w.set_v_sink_supported(
                        backend_v_sink_supported,
                        getattr(self._mixer, "v_sink_capability_reason", ""),
                    )

            order = list(widgets_by_id)
            if self._mixer is not None:
                order = self._mixer.get_channel_order()
            for channel_id in order:
                widget = widgets_by_id.get(channel_id)
                if widget is not None:
                    self._ch_layout.addWidget(widget)
            self._channels = [widgets_by_id[channel_id] for channel_id in sorted(widgets_by_id)]

            self._ch_layout.addStretch()
        finally:
            self._ch_layout.setEnabled(True)
            self._ch_layout.update()
            # Hide bulk buttons since the selection was cleared.
            if hasattr(self, '_bulk_delete_btn'):
                self._update_selection_ui()

    def _visual_channel_order(self) -> list[int]:
        order: list[int] = []
        for position in range(self._ch_layout.count()):
            widget = self._ch_layout.itemAt(position).widget()
            if isinstance(widget, ChannelWidget):
                order.append(widget.channel_index)
        return order

    def _apply_visual_channel_order(self, order: list[int]) -> None:
        widgets = {widget.channel_index: widget for widget in self._channels}
        while self._ch_layout.count():
            self._ch_layout.takeAt(0)
        for channel_id in order:
            widget = widgets.get(channel_id)
            if widget is not None:
                self._ch_layout.addWidget(widget)
        self._ch_layout.addStretch()
        self._ch_layout.invalidate()

    @pyqtSlot(int)
    @_slot_guard
    def _on_channel_drag_started(self, channel_index: int) -> None:
        self._drag_channel_index = channel_index
        self._drag_global_pos = None
        self._drag_autoscroll_timer.start()

    @pyqtSlot(int, object)
    @_slot_guard
    def _on_channel_drag_moved(self, channel_index: int, global_pos: QPoint) -> None:
        if channel_index != self._drag_channel_index:
            return
        self._drag_global_pos = global_pos
        self._move_dragged_channel(global_pos)

    @pyqtSlot(int, object)
    @_slot_guard
    def _on_channel_drag_finished(self, channel_index: int, global_pos: QPoint) -> None:
        if channel_index != self._drag_channel_index:
            return
        self._move_dragged_channel(global_pos)
        self._drag_autoscroll_timer.stop()
        self._drag_channel_index = None
        self._drag_global_pos = None
        if self._mixer is not None:
            self._mixer.set_channel_order(self._visual_channel_order())
            if self._mixer.is_remote:
                self._apply_visual_channel_order(self._mixer.get_channel_order())

    def _move_dragged_channel(self, global_pos: QPoint) -> None:
        source = self._drag_channel_index
        if source is None:
            return
        order = self._visual_channel_order()
        if source not in order:
            return
        local_x = self._channel_container.mapFromGlobal(global_pos).x()
        target_slot = len(order)
        for slot, channel_id in enumerate(order):
            widget = next((item for item in self._channels if item.channel_index == channel_id), None)
            if widget is not None and local_x < widget.geometry().center().x():
                target_slot = slot
                break
        source_slot = order.index(source)
        order.pop(source_slot)
        if target_slot > source_slot:
            target_slot -= 1
        if target_slot == source_slot:
            return
        order.insert(max(0, min(target_slot, len(order))), source)
        self._apply_visual_channel_order(order)

    def _autoscroll_channel_drag(self) -> None:
        if self._drag_global_pos is None:
            return
        viewport = self._channel_scroll.viewport()
        viewport_pos = viewport.mapFromGlobal(self._drag_global_pos)
        scroll_bar = self._channel_scroll.horizontalScrollBar()
        margin = 32
        if viewport_pos.x() < margin:
            scroll_bar.setValue(scroll_bar.value() - 18)
        elif viewport_pos.x() > viewport.width() - margin:
            scroll_bar.setValue(scroll_bar.value() + 18)
        self._move_dragged_channel(self._drag_global_pos)

    @pyqtSlot(int, int)
    @_slot_guard
    def _move_channel_by_step(self, channel_index: int, direction: int) -> None:
        order = self._visual_channel_order()
        if channel_index not in order:
            return
        old_slot = order.index(channel_index)
        new_slot = max(0, min(len(order) - 1, old_slot + direction))
        if new_slot == old_slot:
            return
        order.pop(old_slot)
        order.insert(new_slot, channel_index)
        self._apply_visual_channel_order(order)
        if self._mixer is not None:
            self._mixer.set_channel_order(order)
            if self._mixer.is_remote:
                self._apply_visual_channel_order(self._mixer.get_channel_order())

    def finalize_ui(self) -> None:
        """Called once hardware/audio audit is complete to enable rendering."""
        if not self.updatesEnabled():
            logger.debug("MainWindow: Hardware audit complete. Enabling UI updates.")
            self.setUpdatesEnabled(True)
            self.update()

    def set_show_requested(self, value: bool) -> None:
        """Set the show-in-flight guard flag (used by tray and IPC show handlers)."""
        self._show_requested = value

    def set_force_quit(self) -> None:
        """Mark the window for a real quit so closeEvent does not intercept it."""
        self._force_quit = True

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @pyqtSlot(list)
    @_slot_guard
    def on_volumes_changed(self, volumes: list[float]) -> None:
        if self._mixer.is_remote:
            return
        for i, vol in enumerate(volumes):
            if i < len(self._channels):
                displayed_volume = (
                    self._config.get_channel_volume(i)
                    if self._config.midi_fader_feedback
                    else vol
                )
                self._channels[i].set_volume(displayed_volume)

    def sync_sliders_from_config(self) -> None:
        """Refresh on-screen fader positions from persisted profile/config volumes."""
        for i in range(self._mixer.num_channels):
            if i >= len(self._channels):
                break
            self._channels[i].set_volume(self._mixer.get_channel_volume(i))
        logger.debug("Slider positions synced from config/profile")
        self.fader_display_synced.emit()

    @pyqtSlot(int, float)
    @_slot_guard
    def on_channel_volume_changed(self, channel_index: int, volume: float) -> None:
        if self._mixer.is_remote:
            return
        self._config.set_channel_volume(channel_index, volume)
        if 0 <= channel_index < len(self._channels):
            self._channels[channel_index].set_volume(volume)

    @pyqtSlot(bool)
    @_slot_guard
    def on_midi_connection_changed(self, connected: bool) -> None:
        """Reset Learn mode for all channels if connection is lost."""
        if not connected:
            logger.debug("MainWindow: MIDI connection lost, resetting Learn state.")
            for widget in self._channels:
                widget.cancel_learn()

    @pyqtSlot(int, int, int)
    @_slot_guard
    def on_midi_cc_received(self, midi_channel: int, control_number: int, value: int) -> None:
        """
        Central Learn handshake for both volume-CC and mute-CC.
        Iterates all channels and acts on the first one that is in learn mode.
        A single break ensures one CC event never assigns to multiple channels.
        Mute-CC learn only captures on value==127 (button press) so fader
        movements cannot accidentally complete the learn.
        """
        for widget in self._channels:
            if not widget.isVisible():
                continue
            if widget.is_waiting_for_volume_learn():
                self._mixer.set_midi_cc(
                    widget.channel_index,
                    control_number,
                    midi_channel=midi_channel,
                )
                widget.update_midi_cc(control_number, midi_channel)
                logger.debug(
                    "Volume Learn: M%d/CC%d → channel %d",
                    midi_channel + 1,
                    control_number,
                    widget.channel_index,
                )
                break
            if widget.is_waiting_for_mute_learn() and value == 127:
                self._mixer.set_midi_mute_cc(
                    widget.channel_index,
                    control_number,
                    midi_channel=midi_channel,
                )
                widget.update_midi_mute_cc(control_number, midi_channel)
                logger.debug(
                    "Mute-CC Learn: M%d/CC%d → channel %d",
                    midi_channel + 1,
                    control_number,
                    widget.channel_index,
                )
                break

    @pyqtSlot(int)
    @_slot_guard
    def on_channel_count_changed(self, n: int) -> None:
        if n == self._config.hw_channel_count:
            return
        logger.debug("Channel count changed to %d – rebuilding GUI", n)
        self._config.num_channels = n
        self._config.save()
        self._rebuild_channels()
        self.refresh_layout()

    @pyqtSlot(int, list)
    @_slot_guard
    def _on_mapping_changed(self, channel_index: int, _names: list[str]) -> None:
        """
        Refresh ALL channels when a mapping changes, so the + App menus
        immediately reflect the new exclusivity rules.
        """
        for ch in self._channels:
            ch.refresh()

    @pyqtSlot(str, bool)
    @_slot_guard
    def _on_routing_pause_changed(self, app_name: str, _paused: bool) -> None:
        """Refresh every shared row for an app whose routing pause changed."""
        needle = app_name.lower()
        for channel in self._channels:
            if any(name.lower() == needle for name in self._config.get_app_names(channel.channel_index)):
                channel.refresh()

    @pyqtSlot(int, bool)
    @_slot_guard
    def on_mute_state_changed(self, channel_index: int, is_muted: bool) -> None:
        if self._mixer.is_remote:
            return
        if 0 <= channel_index < len(self._channels):
            self._channels[channel_index].set_mute_state(is_muted)

    def open_settings(self) -> None:
        """Open the settings panel (called from tray icon)."""
        self._toggle_settings_btn.setChecked(True)

    @pyqtSlot(bool)
    @_slot_guard
    def _on_settings_toggled(self, checked: bool) -> None:
        self._settings_scroll.setVisible(checked)
        self.settings.setValue('settings_open', checked)

    @pyqtSlot(bool)
    @_slot_guard
    def _on_pin_toggled(self, checked: bool) -> None:
        self._config.stay_open = checked
        self._config.save()
        logger.debug("Stay Open (Pin) toggled: %s", checked)

    @pyqtSlot(bool)
    @_slot_guard
    def _on_compact_toggled(self, checked: bool) -> None:
        if checked:
            # Remember current height before hiding content
            self._pre_compact_height = self.height()
        self._config.compact_mode = checked
        for w in self._channels:
            w.set_compact_mode(checked)
        if hasattr(self, '_add_midi_btn'):
            _midi_mode = self._config.input_mode in ("hybrid", "midi_only")
            self._add_midi_btn.setVisible(_midi_mode and not checked)
            self._edit_midi_btn.setVisible(_midi_mode and not checked)
        if checked:
            # Tighten margins and spacing in compact mode; hide grip to save space
            self._root_layout.setContentsMargins(8, 8, 8, 2)
            self._root_layout.setSpacing(4)
            self._size_grip.setVisible(False)
            # Allow window to shrink below normal minimum temporarily
            self.setMinimumHeight(0)
            # Shrink to fit; then lock minimum to compact height so user can't go smaller
            def _do_compact_resize():
                QApplication.processEvents()
                m = self._root_layout.contentsMargins()
                sp = self._root_layout.spacing()
                top_h = self._toggle_settings_btn.height()
                ch_h = (self._channels[0].sizeHint().height()
                        if self._channels else 200)
                h = m.top() + top_h + sp + ch_h + m.bottom()
                logger.debug("Compact resize: top=%d ch=%d → h=%d", top_h, ch_h, h)
                # setFixedHeight forces the resize even if the WM ignores resize()
                self.setFixedHeight(h)
                # Immediately release fixed constraint so user can still resize larger
                def _release_height_constraint() -> None:
                    self.setMinimumHeight(h)
                    self.setMaximumHeight(16777215)
                QTimer.singleShot(0, _release_height_constraint)
            QTimer.singleShot(0, _do_compact_resize)
        else:
            # Restore normal margins, spacing, grip and minimum height
            self._root_layout.setContentsMargins(8, 8, 8, 8)
            self._root_layout.setSpacing(6)
            self._size_grip.setVisible(True)
            self.setMinimumHeight(420)
            # Restore saved height
            saved = getattr(self, '_pre_compact_height', None)
            if saved:
                QTimer.singleShot(0, lambda: self.resize(self.width(), saved))
        logger.debug("Compact mode toggled: %s", checked)

    @_slot_guard
    def _on_palette_changed(self, _palette=None) -> None:
        """
        Called by Qt when the system theme changes (dark ↔ light or accent changes).
        Re-apply the glass look and our dynamic styling hooks.
        """
        logger.debug("System palette changed – repainting and syncing theme")

        # 2. Update window background (transparency)
        self._apply_transparency()

        # 3. Cascade redraws to all channels (Labels, etc.)
        for ch in self._channels:
            ch.refresh_theme()

        self.repaint()

    @pyqtSlot()
    @_slot_guard
    def _on_settings_updated(self) -> None:
        if self._mixer.is_remote:
            self._apply_transparency()
            self._update_remote_banner()
            return
        # 1. Rebuild channels if mode or count changed
        mode_changed = (self._last_mode != self._config.input_mode)
        # In USB mode MIDI widgets are not built, so compare against hw count only.
        expected_widgets = (
            self._config.hw_channel_count
            if self._config.input_mode == "usb"
            else self._config.num_channels
        )
        count_changed = (len(self._channels) != expected_widgets)

        expected_order = (
            self._profile_manager.get_channel_order()
            if self._profile_manager is not None
            else [widget.channel_index for widget in self._channels]
        )
        order_changed = self._visual_channel_order() != [
            channel_id
            for channel_id in expected_order
            if any(widget.channel_index == channel_id for widget in self._channels)
        ]

        if mode_changed or count_changed or order_changed:
            logger.debug("Mode or count changed (%s -> %s) – rebuilding GUI",
                        self._last_mode, self._config.input_mode)
            self._last_mode = self._config.input_mode
            self._rebuild_channels()

        # 2. Centralized UI refresh and mode-specific state
        self.refresh_layout()

        # 3. Update existing widgets
        for ch in self._channels:
            ch.update_settings()

    def refresh_layout(self) -> None:
        """
        Centralized UI refresh logic for input modes (usb, hybrid, midi_only).
        """
        if self._mixer.is_remote:
            compact = self._config.compact_mode
            for widget in self._channels:
                widget.setVisible(True)
                widget.set_compact_mode(compact)
            has_midi = any(widget.is_midi_channel for widget in self._channels)
            self._add_midi_btn.setVisible(self._mixer.supports_midi and not compact)
            self._edit_midi_btn.setVisible(has_midi and not compact)
            if self.layout():
                self.layout().activate()
            return
        mode = self._config.input_mode
        logger.debug("Centralized UI refresh for mode: %s", mode)

        # 1. Thread Management & App Cleanup
        if mode == "usb":
            # MidiThread handles USB-idle internally via set_mode() (called via
            # config.settings_changed signal in main.py).  We do NOT stop/start
            # the thread here so the ALSA virtual port stays alive across mode
            # switches and doesn't accumulate duplicate ports.

            # CLEAR app assignments from MIDI channels so they don't block apps
            self._config.clear_midi_channel_mappings()

            # FULL PURGE of MIDI widgets from memory/UI
            remaining_channels = []
            for widget in self._channels:
                if widget.is_midi_channel:
                    logger.debug("Purging MIDI widget: index=%d", widget.channel_index)
                    self._ch_layout.removeWidget(widget)
                    # Disconnect signal before deleteLater() to prevent a
                    # midi_cc_received firing on a half-destroyed widget.
                    if self._midi:
                        try:
                            self._midi.midi_cc_received.disconnect(widget.handle_midi_input)
                        except RuntimeError:
                            pass  # Already disconnected
                    widget.deleteLater()
                else:
                    remaining_channels.append(widget)
            self._channels = remaining_channels
        # Note: the MIDI thread is started by main.py *after* all signal
        # connections are wired.  Do not call self._midi.start() here — if
        # the thread were started before status_changed is connected to the
        # settings panel the initial "Connected" status would be emitted with
        # no listener and the GUI would permanently show "MIDI: Offline".

        # 2. USB specific logic
        if mode == "midi_only":
            self._config.clear_usb_channel_mappings()
            if self._arduino and self._arduino.isRunning():
                # We don't stop the arduino thread (discovery), but backend blocks it.
                pass
        elif mode in ("usb", "hybrid") and self._arduino:
            if not self._arduino.isRunning():
                try:
                    logger.debug("Attempting to restart Arduino thread for %s mode", mode)
                    self._arduino.start()
                except Exception as exc:
                    logger.error("Failed to start Arduino thread: %s", exc)

        # 3. Universal Synchronization
        # Push ANY change to Backend + UI immediately
        self.sync_ui_to_hardware()

        # 4. Visibility logic (Clean Hide/Show)
        if hasattr(self, '_add_midi_btn'):
            _midi_mode = mode in ("hybrid", "midi_only")
            _compact = self._config.compact_mode
            self._add_midi_btn.setVisible(_midi_mode and not _compact)
            self._edit_midi_btn.setVisible(_midi_mode and not _compact)

        for widget in self._channels:
            is_midi = widget.is_midi_channel
            if mode == "usb":
                # Hide MIDI, show USB
                widget.setVisible(not is_midi)
            elif mode == "midi_only":
                # Hide USB, show MIDI
                widget.setVisible(is_midi)
            else:
                # Hybrid: show all
                widget.setVisible(True)

        # 4. Layout Stabilization
        if self.layout():
            self.layout().activate()

    def sync_ui_to_hardware(self) -> None:
        """
        Pull latest volumes from Arduino and MIDI threads and push to Backend + UI.
        Crucial for startup and mode transitions to prevent jumps.
        """
        logger.debug("Universal Volume Sync triggered")
        if self._mixer.is_remote:
            for channel in self._channels:
                channel.set_volume(self._mixer.get_channel_volume(channel.channel_index))
                channel.set_mute_state(self._mixer.is_channel_muted(channel.channel_index))
            return
        mode = self._config.input_mode
        hardware_synced = False

        # 1. Arduino Sync
        # Only if we are in a mode that uses hardware
        if mode in ("usb", "hybrid") and self._arduino:
            try:
                if self._arduino.has_real_data:
                    hw_vols = self._arduino.get_last_volumes()
                    logger.debug("Syncing Arduino volumes: %s", hw_vols)
                    self.on_volumes_changed(hw_vols)
                    self._backend.apply_poti_volumes(hw_vols)
                    hardware_synced = True
                else:
                    logger.debug(
                        "Arduino sync: no real data yet – keeping profile/config volumes for UI"
                    )
            except Exception as exc:
                logger.error("Arduino sync failed: %s", exc)

        # 2. MIDI Sync
        if mode in ("hybrid", "midi_only") and self._midi:
            try:
                mapped = self._midi.get_mapped_volumes()
                if mapped:
                    logger.debug("Syncing MIDI volumes: %s", mapped)
                    self._backend.apply_midi_volumes(mapped)
                    for ch, vol in mapped:
                        self._config.set_channel_volume(ch, vol)
                        self.on_channel_volume_changed(ch, vol)
                    hardware_synced = True
            except Exception as exc:
                logger.error("MIDI sync failed: %s", exc)

        if not hardware_synced:
            self.sync_sliders_from_config()

    @pyqtSlot(bool)
    @_slot_guard
    def _on_add_midi_clicked(self, checked: bool = False) -> None:
        self._mixer.add_midi_channel()
        # The add_midi_channel method emits settings_changed, which triggers _on_settings_updated,
        # which detects the length difference and rebuilds.

    @pyqtSlot(bool)
    @_slot_guard
    def _on_edit_midi_toggled(self, checked: bool) -> None:
        for w in self._channels:
            if w.is_midi_channel:
                w.set_edit_mode(checked)
        self.settings.setValue('edit_midi_active', checked)

    # ------------------------------------------------------------------
    # Multi-select
    # ------------------------------------------------------------------

    @pyqtSlot(int, int)
    @_slot_guard
    def _on_strip_clicked(self, channel_index: int, modifiers_int: int) -> None:
        """Handle Ctrl-Click (toggle) or Shift-Click (range) on a channel label."""
        ctrl = Qt.KeyboardModifier.ControlModifier.value
        shift = Qt.KeyboardModifier.ShiftModifier.value

        if modifiers_int & shift and self._last_clicked_index >= 0:
            # Range select: find both widget positions in a single pass
            anchor_pos = target_pos = None
            for p, w in enumerate(self._channels):
                if w.channel_index == self._last_clicked_index:
                    anchor_pos = p
                elif w.channel_index == channel_index:
                    target_pos = p
                if anchor_pos is not None and target_pos is not None:
                    break
            if anchor_pos is not None and target_pos is not None:
                lo, hi = sorted((anchor_pos, target_pos))
                for pos in range(lo, hi + 1):
                    w = self._channels[pos]
                    self._selected_channels.add(w.channel_index)
                    w.set_selected(True)
        elif modifiers_int & ctrl:
            # Toggle individual strip
            widget = next(
                (w for w in self._channels if w.channel_index == channel_index),
                None,
            )
            if widget is not None:
                if channel_index in self._selected_channels:
                    self._selected_channels.discard(channel_index)
                    widget.set_selected(False)
                else:
                    self._selected_channels.add(channel_index)
                    widget.set_selected(True)
            self._last_clicked_index = channel_index

        self._update_selection_ui()

    def _select_all_channels(self) -> None:
        """Select all visible channel strips (Ctrl+A)."""
        for w in self._channels:
            self._selected_channels.add(w.channel_index)
            w.set_selected(True)
        self._update_selection_ui()

    def _clear_selection(self) -> None:
        """Deselect all channel strips."""
        self._selected_channels.clear()
        self._last_clicked_index = -1
        for w in self._channels:
            w.set_selected(False)
        self._update_selection_ui()

    def _update_selection_ui(self) -> None:
        """Show/hide and relabel bulk-action buttons based on current selection."""
        # Count selected MIDI strips once; used for both buttons.
        midi_count = sum(
            1 for w in self._channels
            if w.channel_index in self._selected_channels and w.is_midi_channel
        )
        show_delete = midi_count > 0
        show_learn = midi_count > 0 and self._midi is not None

        s = "s" if midi_count != 1 else ""
        self._bulk_delete_btn.setVisible(show_delete)
        if show_delete:
            self._bulk_delete_btn.setText(f"Delete {midi_count} MIDI channel{s}")

        self._bulk_learn_btn.setVisible(show_learn)
        if show_learn:
            self._bulk_learn_btn.setText(f"MIDI Learn {midi_count} channel{s}")

    @_slot_guard
    def _on_bulk_delete(self, checked: bool = False) -> None:
        """Delete all selected MIDI channels after confirmation."""
        midi_widgets = [
            w for w in self._channels
            if w.channel_index in self._selected_channels and w.is_midi_channel
        ]
        if not midi_widgets:
            return
        count = len(midi_widgets)
        s = "s" if count != 1 else ""
        reply = QMessageBox.question(
            self,
            "Delete MIDI Channels",
            f"Delete {count} selected MIDI channel{s}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        # Collect indices and sort descending so removing one doesn't shift others.
        indices = sorted(
            (w.channel_index for w in midi_widgets), reverse=True
        )
        self._clear_selection()
        self._mixer.remove_midi_channels(indices)
        # settings_changed → _on_settings_updated → _rebuild_channels

    @_slot_guard
    def _on_bulk_midi_learn(self, checked: bool = False) -> None:
        """Start volume MIDI-CC learn on all selected MIDI channels."""
        midi_widgets = [
            w for w in self._channels
            if w.channel_index in self._selected_channels and w.is_midi_channel
        ]
        if not midi_widgets:
            return
        # Enable edit mode so the learn buttons become visible on each strip.
        if hasattr(self, '_edit_midi_btn') and not self._edit_midi_btn.isChecked():
            self._edit_midi_btn.setChecked(True)
        self._clear_selection()
        for w in midi_widgets:
            w.start_volume_learn()

    def _populate_profile_combo(self) -> None:
        """Rebuild the profile combo from ProfileManager (blocks signals to avoid loops)."""
        if not hasattr(self, "_profile_combo") or self._profile_manager is None:
            return
        profiles = self._mixer.list_profiles()
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for p in profiles:
            self._profile_combo.addItem(p["name"], userData=p["id"])
        active_id = self._mixer.active_profile_id
        for i in range(self._profile_combo.count()):
            if self._profile_combo.itemData(i) == active_id:
                self._profile_combo.setCurrentIndex(i)
                break
        self._profile_combo.blockSignals(False)
        if hasattr(self, "_profile_delete_btn"):
            self._profile_delete_btn.setEnabled(
                len(profiles) > 1 and not self._mixer.is_pending("profiles")
            )

    @pyqtSlot(int)
    @_slot_guard
    def _on_profile_selected(self, index: int) -> None:
        if self._mixer is None or index < 0:
            return
        profile_id = self._profile_combo.itemData(index)
        if profile_id and profile_id != self._mixer.active_profile_id:
            if self._mixer.is_remote:
                self._mixer.select_profile(profile_id)
                self._on_profile_changed_externally(self._mixer.active_profile_id)
            else:
                self.profile_switch_requested.emit(profile_id)

    @pyqtSlot(str)
    @_slot_guard
    def _on_profile_changed_externally(self, profile_id: str) -> None:
        """Update combo when profile changes from IPC or MIDI (not from the combo itself)."""
        if not hasattr(self, "_profile_combo"):
            return
        self._profile_combo.blockSignals(True)
        for i in range(self._profile_combo.count()):
            if self._profile_combo.itemData(i) == profile_id:
                self._profile_combo.setCurrentIndex(i)
                break
        self._profile_combo.blockSignals(False)

    @_slot_guard
    def _apply_profile_rename(self) -> None:
        """Debounced rename: save the text currently in the combo as the active profile name."""
        if self._mixer is None or not hasattr(self, "_profile_combo"):
            return
        new_name = self._profile_combo.currentText().strip()
        active_id = self._mixer.active_profile_id
        if new_name and active_id:
            try:
                current_name = self._mixer.load_profile(active_id).get("name", "")
                if new_name != current_name:
                    self._mixer.rename_profile(active_id, new_name)
                    if self._mixer.is_remote:
                        self._populate_profile_combo()
            except Exception:
                logger.exception("Error renaming profile")

    @pyqtSlot(bool)
    @_slot_guard
    def _on_add_profile_clicked(self, checked: bool = False) -> None:
        if self._mixer is None:
            return
        names = {p["name"] for p in self._mixer.list_profiles()}
        n = len(names) + 1
        candidate = f"Profile {n}"
        while candidate in names:
            n += 1
            candidate = f"Profile {n}"
        new_id = self._mixer.create_profile(candidate, self._mixer.hw_channel_count)
        if new_id and not self._mixer.is_remote:
            self.profile_switch_requested.emit(new_id)
            # Defer focus/select until after the event loop processes the switch signal.
            QTimer.singleShot(0, self._focus_profile_name_editor)

    @pyqtSlot(bool)
    @_slot_guard
    def _on_duplicate_profile_clicked(self, checked: bool = False) -> None:
        del checked
        active_id = self._mixer.active_profile_id
        if not active_id:
            return
        names = {profile["name"] for profile in self._mixer.list_profiles()}
        base = f"{self._mixer.active_profile_name} Copy"
        candidate = base
        suffix = 2
        while candidate in names:
            candidate = f"{base} {suffix}"
            suffix += 1
        new_id = self._mixer.duplicate_profile(active_id, candidate)
        if new_id and not self._mixer.is_remote:
            self.profile_switch_requested.emit(new_id)

    @pyqtSlot(bool)
    @_slot_guard
    def _on_delete_profile_clicked(self, checked: bool = False) -> None:
        if self._mixer is None:
            return
        active_id = self._mixer.active_profile_id
        if not active_id:
            return
        if len(self._mixer.list_profiles()) <= 1:
            return
        if self._mixer.is_remote:
            profile_name = self._mixer.active_profile_name
            reply = QMessageBox.question(
                self,
                "Delete Profile",
                f"Delete receiver profile '{profile_name}' permanently?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._mixer.delete_profile(active_id)
        else:
            self.delete_profile_requested.emit(active_id)

    def _focus_profile_name_editor(self) -> None:
        """Focus the profile name field and select all text for quick rename."""
        if not hasattr(self, "_profile_combo"):
            return
        self._profile_combo.setFocus()
        line_edit = self._profile_combo.lineEdit()
        if line_edit is not None:
            line_edit.selectAll()

    def _profile_clone_source(self) -> tuple[int, list[dict]]:
        """Return ``(channel_count, channels)`` to use for cloning a new profile."""
        source_channels = self._config.all_channels()
        source_count_raw = len(source_channels)
        if self._profile_manager is None:
            return source_count_raw, source_channels
        active_id = self._profile_manager.active_profile_id
        if active_id:
            try:
                source_profile = self._profile_manager.load(active_id)
            except Exception:
                logger.exception("Could not load source profile %s for cloning", active_id)
                source_profile = None
            if source_profile is not None:
                source_channels = source_profile.get("channels", [])
                source_count_raw = source_profile.get("channel_count", len(source_channels))
        try:
            source_channel_count = max(0, int(source_count_raw))
        except (TypeError, ValueError):
            source_channel_count = max(0, len(source_channels))
        return source_channel_count, source_channels

    def _apply_transparency(self) -> None:
        """
        Applies a semi-transparent background to the main window.
        """
        transparent = bool(self._config.transparency)
        # WA_TranslucentBackground stays always-on (set at init); only alpha changes.

        sys_color = self.palette().color(QPalette.ColorRole.Window)
        if transparent:
            alpha = 200  # Transparency (semi-transparent, but readable)
        else:
            alpha = 255  # Solid (Standard System-Theme)

        rgba_string = f"rgba({sys_color.red()}, {sys_color.green()}, {sys_color.blue()}, {alpha})"
        self.setStyleSheet(f"#MainFrame {{ background-color: {rgba_string}; border-radius: 12px; }}")

        # Force a repaint to safely apply KWin compositor changes on-the-fly
        self.repaint()

    @pyqtSlot(list)
    @_slot_guard
    def _on_other_apps_changed(self, names: list[str]) -> None:
        """Dynamically updates the tooltip for the 'Other Apps' channel."""
        if self._mixer.is_remote:
            return
        for ch_widget in self._channels:
            ch_widget.set_other_apps_tooltip(names)

    @pyqtSlot(str, str)
    @_slot_guard
    def _on_audio_status_changed(self, status_type: str, message: str) -> None:
        """Forward backend audio status to the settings panel mode badge.

        This slot is always invoked on the GUI thread (connected with
        QueuedConnection) so it is safe to update UI elements directly.
        """
        logger.debug(
            "_on_audio_status_changed: status_type=%r message=%r isVisible=%s",
            status_type, message, self.isVisible(),
        )
        try:
            if hasattr(self, "settings_panel") and hasattr(self.settings_panel, "set_audio_mode"):
                self.settings_panel.set_audio_mode(status_type, message)
        except Exception:
            logger.exception(
                "_on_audio_status_changed: unhandled exception "
                "(status_type=%r message=%r)", status_type, message
            )

    @pyqtSlot(set)
    @_slot_guard
    def _on_unresolved_targets_changed(self, unresolved_targets: set) -> None:
        """Propagate unresolved-target state to all channel widgets."""
        if self._mixer.is_remote:
            return
        for ch_widget in self._channels:
            ch_widget.update_unresolved_state(unresolved_targets)

    @pyqtSlot(str, bool)
    @_slot_guard
    def _on_capability_changed(self, cap_name: str, supported: bool) -> None:
        """Propagate backend capability probe results to all channel widgets.

        Currently handles ``gain_control_supported``: when False, every channel
        strip disables its volume slider and shows a warning badge so the user
        knows that no effective gain backend is available in this runtime.
        """
        if self._mixer.is_remote:
            return
        logger.debug(
            "_on_capability_changed: cap_name=%r supported=%s", cap_name, supported
        )
        if cap_name == "gain_control_supported":
            for ch_widget in self._channels:
                ch_widget.set_gain_control_supported(supported)
        elif cap_name == "v_sink_supported":
            reason = getattr(self._backend, "v_sink_capability_reason", "")
            for ch_widget in self._channels:
                ch_widget.set_v_sink_supported(supported, reason)

    @pyqtSlot()
    @_slot_guard
    def _on_panic_triggered(self) -> None:
        """Reset all apps to default sink, destroy V-Sinks, clear mappings."""
        if self._mixer.is_remote:
            return
        reply = QMessageBox.question(
            self, "Panic Reset",
            "This will destroy all virtual cables and move all apps back to the system default output."
            "\n\nAre you sure?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        if reply == QMessageBox.StandardButton.Yes:
            # 1. Backend reset
            self._backend.panic_reset()
            # 2. Config purge
            for i in range(self._config.num_channels):
                self._config.set_app_names(i, [])
                self._config.set_v_sink_enabled(i, False)
            self._config.save()
            # 3. GUI refresh
            self._rebuild_channels()
            self._on_master_refresh()
            logger.debug("Panic Reset completed from GUI.")



    @pyqtSlot()
    @_slot_guard
    def _on_master_refresh(self) -> None:
        if self._mixer.is_remote:
            return
        """Fetch real sinks and update the settings panel dropdown."""
        sinks = self._backend.get_real_sinks()
        default = self._backend.get_default_sink_name()
        self.settings_panel.populate_master_outputs(sinks, default)

    @pyqtSlot(str)
    @_slot_guard
    def _on_master_changed(self, sink_name: str) -> None:
        if self._mixer.is_remote:
            return
        """Set the new default sink and route loopbacks."""
        self._backend.set_default_sink_and_move_loopbacks(sink_name)

    # ------------------------------------------------------------------
    # Close → conditionally hide to tray or actually close
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.settings.setValue('geometry', self.saveGeometry())

        # If the Tray Icon called "Quit NativMix", we must accept the event
        # so QApplication.quit() can actually terminate the application.
        if getattr(self, "_force_quit", False):
            logger.debug("MainWindow force-closing, stopping background threads")
            # Block signals before stop() so in-flight emissions during the
            # 2-second graceful-wait window cannot reach already-torn-down slots.
            if self._arduino:
                self._arduino.blockSignals(True)
                self._arduino.stop()
            if self._midi:
                self._midi.blockSignals(True)
                self._midi.stop()
            event.accept()
            return

        # Always accept so the Wayland compositor can proceed (e.g. system shutdown).
        # WA_DeleteOnClose is not set → Qt hides the window, app stays alive via tray.
        event.accept()
        if self._config.stay_open:
            # "Don't Close": re-show in the next event-loop tick so the window
            # stays visible for the user. During system shutdown the event loop
            # exits before the timer fires → window stays hidden → shutdown proceeds.
            QTimer.singleShot(0, self.show)
            logger.debug("Close event accepted, re-showing (Stay Open is ON)")
        else:
            logger.debug("Window closed/hidden to tray (Stay Open is OFF)")




    # ------------------------------------------------------------------
    # Drag & Auto-Hide on Focus Loss (Applet Behavior)
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        """Native Wayland Window Move. No manual coordinate math needed."""
        is_reorder_grip = self._hit_channel_reorder_grip(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and not is_reorder_grip:
            if self.windowHandle():
                self.windowHandle().startSystemMove()
        super().mousePressEvent(event)

    def _hit_channel_reorder_grip(self, position: QPoint) -> bool:
        """Return whether a window-local point belongs to a channel reorder grip."""
        widget = self.childAt(position)
        while widget is not None and widget is not self:
            if isinstance(widget, _ChannelReorderGrip):
                return True
            widget = widget.parentWidget()
        return False

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.ActivationChange:
            active = self.isActiveWindow()
            show_req = getattr(self, "_show_requested", False)
            active_widget = QApplication.activeWindow()
            logger.debug(
                "changeEvent ActivationChange: isActiveWindow=%s _show_requested=%s "
                "isVisible=%s activeWindow=%s stay_open=%s",
                active,
                show_req,
                self.isVisible(),
                type(active_widget).__name__ if active_widget else None,
                self._config.stay_open,
            )
            if not active:
                # Suppress auto-hide while a show request is in flight.
                if show_req:
                    logger.debug("changeEvent: _show_requested active – skipping auto-hide")
                    super().changeEvent(event)
                    return
                # Don't hide if a child dialog (e.g. QMessageBox) is currently active
                if active_widget is self or (active_widget is not None and active_widget.parent() is not None):
                    logger.debug("changeEvent: child dialog or self active – keeping visible")
                elif not self._config.stay_open:
                    self._save_geometry()
                    self.hide()
                    logger.debug("Window auto-hidden on focus loss")
        super().changeEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            if self._selected_channels:
                self._clear_selection()
            else:
                for ch in self._channels:
                    ch.cancel_learn()
        elif (
            event.key() == Qt.Key.Key_A
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._select_all_channels()
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._selected_channels:
                self._on_bulk_delete()
        super().keyPressEvent(event)

    def moveEvent(self, event) -> None:
        self._save_geometry()
        super().moveEvent(event)

    def resizeEvent(self, event) -> None:
        self._save_geometry()
        super().resizeEvent(event)

    def showEvent(self, event) -> None:
        g = self.geometry()
        logger.debug(
            "showEvent: geometry=(%d,%d %dx%d) isActiveWindow=%s _show_requested=%s",
            g.x(), g.y(), g.width(), g.height(),
            self.isActiveWindow(),
            getattr(self, "_show_requested", False),
        )
        super().showEvent(event)
        self.sync_sliders_from_config()
        # Dirty X11 trick for GNOME: Mutter's smart placement overrides the
        # position set by restoreGeometry(). Capture pos before the compositor
        # moves it and reapply after the placement round-trip (~80 ms).
        if self._has_saved_geometry and (_is_gnome_x11() or _is_kde_x11()):
            target = self.pos()
            QTimer.singleShot(80, lambda: self.move(target))

    def hideEvent(self, event) -> None:
        logger.debug("hideEvent fired (caller will be in traceback if needed)")
        super().hideEvent(event)

    def _save_geometry(self) -> None:
        """Schedule a debounced geometry write (500 ms after the last call)."""
        self._geometry_save_timer.start()  # restarts if already running

    def _flush_geometry(self) -> None:
        """Write the current geometry to QSettings (called by debounce timer)."""
        if self.isVisible():
            self.settings.setValue('geometry', self.saveGeometry())
            logger.debug("Window geometry saved")
