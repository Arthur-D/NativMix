"""Shared MIDI Learn button for channel playback and global active media."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMenu, QSizePolicy, QToolButton, QWidget

from nativmix.utils.qt_utils import _slot_guard


class MediaBindingButton(QToolButton):
    def __init__(self, config: Any, channel: int = -1, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._channel = channel
        self.setCheckable(True)
        self.setIcon(QIcon.fromTheme("media-playback-start"))
        self.setIconSize(QSize(14, 14))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(24)
        self.setStyleSheet("QToolButton { padding: 1px 2px; }")
        self._menu = QMenu(self)
        self.setMenu(self._menu)
        self._menu.aboutToShow.connect(self._rebuild_menu)
        self.toggled.connect(self.refresh)
        self.refresh()

    def refresh(self, checked: bool = False) -> None:
        binding = self._config.get_media_binding(self._channel)
        cc = binding["cc"]
        label = "—" if cc is None else f"{binding['midi_channel'] + 1}:{cc}"
        self.setText("Cancel" if self.isChecked() else label)
        target = "Active media" if self._channel == -1 else "Channel media"
        self.setAccessibleName(f"{target} play/pause MIDI Learn ({label})")
        self.setToolTip(
            f"{target} play/pause — {binding['mode']} button.\n"
            "Click to learn a CC; the arrow offers MIDI channel, button mode, and Clear.\n"
            + ("Controls a playing player, or resumes the last selected player."
               if self._channel == -1 else
               "Uses this channel's assigned applications. Unavailable apps are ignored; never launches them.")
        )

    def cancel_learn(self) -> None:
        self.setChecked(False)

    def learn(self, midi_channel: int, cc: int, value: int) -> bool:
        if not self.isChecked() or not self.isEnabled():
            return False
        binding = self._config.get_media_binding(self._channel)
        if binding["mode"] == "momentary" and value < 64:
            return False
        self.setChecked(False)
        self._config.set_media_binding(self._channel, cc, midi_channel, binding["mode"])
        self.refresh()
        return True

    @_slot_guard
    def _change(self, **changes: Any) -> None:
        self.cancel_learn()
        binding = self._config.get_media_binding(self._channel)
        binding.update(changes)
        self._config.set_media_binding(self._channel, **binding)
        self.refresh()

    def _rebuild_menu(self) -> None:
        self._menu.clear()
        binding = self._config.get_media_binding(self._channel)
        for mode, label in (("momentary", "Momentary (press/release)"), ("toggle", "Toggle (alternating values)")):
            action = self._menu.addAction(label)
            assert action is not None
            action.setCheckable(True)
            action.setChecked(binding["mode"] == mode)
            action.triggered.connect(lambda checked=False, mode=mode: self._change(mode=mode))
        channels = self._menu.addMenu("MIDI channel")
        assert channels is not None
        for channel in range(16):
            action = channels.addAction(str(channel + 1))
            assert action is not None
            action.setCheckable(True)
            action.setChecked(binding["midi_channel"] == channel)
            action.triggered.connect(lambda checked=False, channel=channel: self._change(midi_channel=channel))
        self._menu.addSeparator()
        clear = self._menu.addAction("Clear")
        assert clear is not None
        clear.triggered.connect(lambda checked=False: self._change(cc=None))
