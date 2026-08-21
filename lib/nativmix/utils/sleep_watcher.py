"""System suspend notifications from systemd-logind."""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection

logger = logging.getLogger(__name__)

_LOGIN1_SERVICE = "org.freedesktop.login1"
_LOGIN1_PATH = "/org/freedesktop/login1"
_LOGIN1_INTERFACE = "org.freedesktop.login1.Manager"
_PREPARE_FOR_SLEEP = "PrepareForSleep"


class SleepWatcher(QObject):
    """Emit signals before system sleep and after resume when logind is available."""

    preparing_for_sleep = pyqtSignal()
    resumed_from_sleep = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._connected = False

    def start(self) -> None:
        """Subscribe to logind's PrepareForSleep signal."""
        if self._connected:
            return
        bus = QDBusConnection.systemBus()
        if not bus.isConnected():
            logger.warning("SleepWatcher: system D-Bus unavailable; suspend release disabled")
            return
        self._connected = bus.connect(
            _LOGIN1_SERVICE,
            _LOGIN1_PATH,
            _LOGIN1_INTERFACE,
            _PREPARE_FOR_SLEEP,
            self._on_prepare_for_sleep,
        )
        if self._connected:
            logger.info("SleepWatcher: listening for logind PrepareForSleep")
        else:
            logger.warning("SleepWatcher: failed to subscribe to logind PrepareForSleep")

    def stop(self) -> None:
        """Unsubscribe from logind."""
        if not self._connected:
            return
        QDBusConnection.systemBus().disconnect(
            _LOGIN1_SERVICE,
            _LOGIN1_PATH,
            _LOGIN1_INTERFACE,
            _PREPARE_FOR_SLEEP,
            self._on_prepare_for_sleep,
        )
        self._connected = False

    @pyqtSlot(bool)
    def _on_prepare_for_sleep(self, sleeping: bool) -> None:
        if sleeping:
            self.preparing_for_sleep.emit()
        else:
            self.resumed_from_sleep.emit()
