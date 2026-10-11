"""Horizontal A/B crossfader row.

Shown below the channel scroll area and above the MIDI/size-grip bottom bar.
The bar only reports user intent via signals — all state (position, MIDI
binding, enabled flag, A/B assignment) lives in the active profile via
ConfigManager, so this widget stays free of business logic.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from nativmix.gui.crossfader_layout import channel_eligible_for_side, format_side_label_names
from nativmix.utils.qt_utils import _slot_guard

# Slider resolution: 0 = full A, _XF_RESOLUTION = full B. Mapped to 0.0–1.0.
_XF_RESOLUTION = 1000

# Shown above the slider when no USB/MIDI channel is assigned as the control.
_PLACEHOLDER_CONTROL_NAME = "—"


class CrossfaderBar(QWidget):
    """A horizontal crossfader with a control-name label, A/B menus and MIDI Learn.

    MIDI Learn/Clear is only offered via the main right-click menu while
    ``_midi_edit_mode`` is active (gated by MainWindow's "Edit MIDI Channel"
    toggle). Right-clicking the A/B labels always opens the assignment menu,
    independent of edit mode.
    """

    position_changed = pyqtSignal(float)  # 0.0 (full A) .. 1.0 (full B)
    midi_learn_requested = pyqtSignal()
    midi_clear_requested = pyqtSignal()
    side_assignment_toggled = pyqtSignal(str, int, bool)  # side "a"|"b", channel_index, checked

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._learning = False
        self._cc: int | None = None
        self._midi_channel = 0
        self._midi_edit_mode = False
        self._control_index: int | None = None
        # Rows for the A/B assignment menus: (channel_index, display_name, current_side).
        self._side_entries: list[tuple[int, str, str]] = []
        self._ctx_menu: QMenu | None = None

        outer = QVBoxLayout(self)
        # Tight to the V-Sink row above — only horizontal padding for A/B hit targets.
        outer.setContentsMargins(4, 0, 4, 0)
        outer.setSpacing(0)

        self._control_label = QLabel(_PLACEHOLDER_CONTROL_NAME)
        self._control_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        small_font = self._control_label.font()
        small_font.setPointSize(max(7, small_font.pointSize() - 1))
        self._control_label.setFont(small_font)
        # Always visible (placeholder or name) so assigning a control channel
        # does not nudge the A/B track downward.
        outer.addWidget(self._control_label)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self._label_a = QLabel("A")
        self._label_a.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label_a.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._label_a.customContextMenuRequested.connect(
            lambda pos: self._on_side_context_menu("a", self._label_a, pos)
        )

        self._label_b = QLabel("B")
        self._label_b.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label_b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._label_b.customContextMenuRequested.connect(
            lambda pos: self._on_side_context_menu("b", self._label_b, pos)
        )

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, _XF_RESOLUTION)
        self._slider.setValue(_XF_RESOLUTION // 2)
        self._slider.valueChanged.connect(self._on_slider_changed)

        row.addWidget(self._label_a)
        row.addWidget(self._slider, stretch=1)
        row.addWidget(self._label_b)
        outer.addLayout(row)

        # Match ChannelWidget MIDI Learn control size (compact tool button).
        self._learn_btn = QToolButton()
        self._learn_btn.setText("Learn")
        self._learn_btn.setVisible(False)
        self._learn_btn.setCheckable(False)
        learn_font = self._learn_btn.font()
        if learn_font.pointSize() > 0:
            learn_font.setPointSize(max(8, learn_font.pointSize() - 2))
        else:
            learn_font.setPointSize(8)
        self._learn_btn.setFont(learn_font)
        self._learn_btn.setIconSize(QSize(16, 16))
        self._learn_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._learn_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._learn_btn.setFixedHeight(28)
        self._learn_btn.setStyleSheet(
            "QToolButton { padding-left: 8px; padding-right: 8px; padding-top: 4px; padding-bottom: 4px; }"
        )
        self._learn_btn.clicked.connect(lambda _checked=False: self.midi_learn_requested.emit())
        outer.addWidget(self._learn_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._side_names_a: list[str] = []
        self._side_names_b: list[str] = []

        # Right-click on the slider / control-name label / empty area → Learn / Clear
        # MIDI, gated by `_midi_edit_mode`. Right-click on A/B is handled above and
        # never bubbles here because the labels have their own context menu policy.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

        self._refresh_tooltip()

    # ------------------------------------------------------------------
    # Signals out
    # ------------------------------------------------------------------
    @_slot_guard
    def _on_slider_changed(self, value: int) -> None:
        self.position_changed.emit(value / float(_XF_RESOLUTION))

    def _build_main_menu(self) -> QMenu:
        """Build the Learn/Clear menu (slider / control-name / empty-area right-click)."""
        menu = QMenu(self)
        if self._midi_edit_mode:
            learn = menu.addAction("Cancel MIDI Learn" if self._learning else "Learn MIDI")
            learn.triggered.connect(lambda _checked=False: self.midi_learn_requested.emit())
            clear = menu.addAction("Clear MIDI")
            clear.triggered.connect(lambda _checked=False: self.midi_clear_requested.emit())
        else:
            hint = menu.addAction("Enable Edit MIDI Channel to Learn")
            hint.setEnabled(False)
        return menu

    @_slot_guard
    def _on_context_menu(self, pos) -> None:
        menu = self._build_main_menu()
        # Keep a reference so the non-blocking popup is not garbage-collected.
        self._ctx_menu = menu
        menu.popup(self.mapToGlobal(pos))

    def _build_side_menu(self, side: str) -> QMenu:
        """Build the checkable A/B assignment menu for *side* ("a" or "b")."""
        menu = QMenu(self)
        sides = {idx: s for idx, _name, s in self._side_entries}
        added_any = False
        for idx, name, cur_side in self._side_entries:
            if self._control_index is not None and idx == self._control_index:
                continue  # Control channel: not a valid A/B target, hidden entirely.
            eligible = channel_eligible_for_side(side, idx, control_index=self._control_index, sides=sides)
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(cur_side == side)
            act.setEnabled(eligible)
            if eligible:
                # Desired state from config at menu-build time — do not trust
                # QAction.isChecked() at trigger (racy across Qt styles).
                want_on = cur_side != side
                act.triggered.connect(
                    lambda _checked=False, i=idx, s=side, on=want_on: self.side_assignment_toggled.emit(s, i, on)
                )
            added_any = True
        if not added_any:
            empty = menu.addAction("No channels available")
            empty.setEnabled(False)
        return menu

    @_slot_guard
    def _on_side_context_menu(self, side: str, label: QLabel, pos) -> None:
        menu = self._build_side_menu(side)
        self._ctx_menu = menu
        menu.popup(label.mapToGlobal(pos))

    # ------------------------------------------------------------------
    # State sync (called by MainWindow — never re-emits position_changed)
    # ------------------------------------------------------------------
    def set_position(self, position: float) -> None:
        """Move the slider to *position* (0.0–1.0) without emitting a change."""
        value = int(round(max(0.0, min(1.0, position)) * _XF_RESOLUTION))
        self._slider.blockSignals(True)
        self._slider.setValue(value)
        self._slider.blockSignals(False)

    def set_learning(self, active: bool) -> None:
        """Toggle the visual 'waiting for MIDI' hint."""
        self._learning = bool(active)
        self._learn_btn.setText("Cancel" if self._learning else "Learn")
        self._refresh_tooltip()

    def set_midi_label(self, cc: int | None, midi_channel: int = 0) -> None:
        """Remember the current MIDI binding for the tooltip."""
        self._cc = cc
        self._midi_channel = midi_channel
        self._refresh_tooltip()

    def set_control_name(self, name: str) -> None:
        """Update the label shown above the slider.

        Empty names keep the `—` placeholder so bar height stays stable when
        a control channel is assigned or cleared.
        """
        text = (name or "").strip()
        self._control_label.setText(text if text else _PLACEHOLDER_CONTROL_NAME)
        self._control_label.setVisible(True)

    def set_control_index(self, control_index: int | None) -> None:
        """Remember which channel index is the frozen crossfader control (if any).

        Used to hide that channel from the A/B assignment menus.
        """
        self._control_index = control_index

    def set_side_menu_model(self, entries: list[tuple[int, str, str]]) -> None:
        """Set the rows used to build the A/B assignment menus.

        Each entry is ``(channel_index, display_name, current_side)`` where
        ``current_side`` is ``"none"``, ``"a"`` or ``"b"``.
        """
        self._side_entries = list(entries)

    def set_side_names(self, side_a: list[str], side_b: list[str]) -> None:
        self._side_names_a = list(side_a)
        self._side_names_b = list(side_b)
        self.refresh_side_labels()

    def refresh_side_labels(self) -> None:
        """Re-elide A/B side labels for the current bar width."""
        self._refresh_side_labels()

    def _refresh_side_labels(self) -> None:
        self._apply_side_label(self._label_a, "A", self._side_names_a)
        self._apply_side_label(self._label_b, "B", self._side_names_b)

    def _side_label_budget(self) -> int:
        """Stable pixel budget per A/B label (keeps the slider usable)."""
        bar_w = self.width()
        if bar_w <= 1:
            return 72
        # ~1/4 of the track after padding; clamp so short bars still show something.
        return max(56, min(140, (bar_w - 24) // 4))

    def _apply_side_label(self, label: QLabel, letter: str, names: list[str]) -> None:
        joined = format_side_label_names(names)
        text = joined if joined else letter
        budget = self._side_label_budget()
        # Fixed width — do NOT use setMaximumWidth alone (elide→empty→width 0 feedback).
        label.setFixedWidth(budget)
        metrics = label.fontMetrics()
        elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(8, budget - 4))
        if not elided.strip():
            elided = letter
        label.setText(elided)
        label.setToolTip(joined if joined else letter)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._refresh_side_labels()

    def set_midi_edit_mode(self, active: bool) -> None:
        """Gate Learn/Clear in the main context menu to MIDI-edit mode only.

        When edit mode turns off while a Learn is in progress, cancel it
        locally and re-emit `midi_learn_requested` (the same toggle signal the
        right-click "Cancel MIDI Learn" action uses) so MainWindow's
        `_crossfader_midi_learning` flag — which it owns — stays in sync.
        """
        active = bool(active)
        turning_off = self._midi_edit_mode and not active
        self._midi_edit_mode = active
        self._learn_btn.setVisible(active)
        if turning_off and self._learning:
            self.set_learning(False)
            self.midi_learn_requested.emit()

    def _refresh_tooltip(self) -> None:
        if self._learning:
            text = "Move a MIDI control to bind the crossfader… (right-click to cancel)"
        elif self._cc is None:
            text = "A/B crossfader — right-click to Learn/Clear a MIDI CC."
        else:
            text = f"A/B crossfader — MIDI M{self._midi_channel + 1}/CC{self._cc} (right-click to change)."
        self.setToolTip(text)
        self._slider.setToolTip(text)
