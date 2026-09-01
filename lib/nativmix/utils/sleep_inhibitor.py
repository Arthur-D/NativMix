"""Suspend-only inhibition while trusted-LAN remote control is enabled."""

from __future__ import annotations

import ctypes
import logging
import secrets
import sys
from collections.abc import Callable
from typing import cast

from PyQt6.QtCore import QMetaType, QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import (
    QDBusArgument,
    QDBusConnection,
    QDBusMessage,
    QDBusPendingCallWatcher,
    QDBusPendingReply,
)

logger = logging.getLogger(__name__)

_PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_INHIBIT_INTERFACE = "org.freedesktop.portal.Inhibit"
_REQUEST_INTERFACE = "org.freedesktop.portal.Request"
_REQUEST_TIMEOUT_MS = 30_000

# XDG portal Inhibit flags: logout=1, user-switch=2, suspend=4, idle=8.
SUSPEND_INHIBIT_FLAG = 4

_ES_SYSTEM_REQUIRED = 0x00000001
_ES_CONTINUOUS = 0x80000000


def _dbus_uint32(value: int) -> QDBusArgument:
    type_id = cast(int, QMetaType.Type.UInt.value)
    argument: QDBusArgument = QDBusArgument(value, type_id)
    return argument


class PortalSuspendBackend(QObject):
    """Own one XDG Desktop Portal suspend inhibitor request."""

    state_changed = pyqtSignal(int, str, str)  # generation, state, detail
    portal_available = pyqtSignal()

    def __init__(self, parent: QObject | None = None, timeout_ms: int = _REQUEST_TIMEOUT_MS) -> None:
        super().__init__(parent)
        self._bus: QDBusConnection | None = None
        self._watchers: dict[QDBusPendingCallWatcher, int] = {}
        self._request_paths: dict[int, str] = {}
        self._generation: int | None = None
        self._owner_connected = False
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.setInterval(timeout_ms)
        self._timeout.timeout.connect(self._on_timeout)

    def acquire(self, generation: int) -> bool:
        """Submit an asynchronous suspend-only portal request."""
        if self._generation is not None:
            return False
        self._generation = generation
        try:
            bus = QDBusConnection.sessionBus()
            self._bus = bus
            if not bus.isConnected():
                self._finish("unavailable", "Desktop portal session bus is unavailable.")
                return False
            self._connect_owner_changes()
            token = f"nativmix_{secrets.token_hex(16)}"
            sender = bus.baseService().lstrip(":").replace(".", "_")
            self._request_paths[generation] = f"{_PORTAL_PATH}/request/{sender}/{token}"

            message = QDBusMessage.createMethodCall(
                _PORTAL_SERVICE,
                _PORTAL_PATH,
                _INHIBIT_INTERFACE,
                "Inhibit",
            )
            message.setArguments(
                [
                    "",
                    _dbus_uint32(SUSPEND_INHIBIT_FLAG),
                    {
                        "handle_token": token,
                        "reason": "Keep trusted-LAN remote controller processing available.",
                    },
                ]
            )
            pending_call = bus.asyncCall(message)
            watcher = QDBusPendingCallWatcher(pending_call, self)
            self._watchers[watcher] = generation
            watcher.finished.connect(self._on_method_finished)
            self._timeout.start()
        except (RuntimeError, TypeError):
            logger.exception("Could not set up the desktop portal suspend inhibitor")
            self._finish("unavailable", "Could not set up the desktop portal request.")
            return False
        logger.info("Requested portal suspend inhibitor")
        return True

    def release(self) -> None:
        """Close the request object, releasing or cancelling the inhibitor."""
        self._timeout.stop()
        generation = self._generation
        if generation is not None:
            self._close_request(self._request_paths.pop(generation, None))
        self._generation = None

    def cleanup(self) -> None:
        self.release()
        for watcher in self._watchers:
            watcher.deleteLater()
        self._watchers.clear()
        self._request_paths.clear()
        if self._owner_connected and self._bus is not None:
            self._bus.disconnect(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameOwnerChanged",
                "sss",
                self._on_name_owner_changed,
            )
        self._owner_connected = False
        self._bus = None

    def _connect_owner_changes(self) -> None:
        if self._owner_connected or self._bus is None:
            return
        self._owner_connected = self._bus.connect(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameOwnerChanged",
            "sss",
            self._on_name_owner_changed,
        )

    def _close_request(self, path: str | None) -> None:
        if path is None or self._bus is None or not self._bus.isConnected():
            return
        message = QDBusMessage.createMethodCall(
            _PORTAL_SERVICE,
            path,
            _REQUEST_INTERFACE,
            "Close",
        )
        self._bus.asyncCall(message)

    def _on_method_finished(self, watcher: QDBusPendingCallWatcher) -> None:
        generation = self._watchers.pop(watcher, None)
        if generation is None:
            watcher.deleteLater()
            return
        reply = QDBusPendingReply(watcher)
        if reply.isError():
            if generation == self._generation:
                error = reply.error()
                detail = error.message() or "Portal inhibit request failed."
                error_text = f"{error.name()} {detail}".lower()
                permission_error = any(word in error_text for word in ("denied", "notallowed", "access"))
                state = "denied" if permission_error else "unavailable"
                self._finish(state, detail)
            self._request_paths.pop(generation, None)
            watcher.deleteLater()
            return
        returned = reply.value()
        returned_path = returned.path() if hasattr(returned, "path") else str(returned)
        predicted_path = self._request_paths.pop(generation, None)
        request_path = returned_path or predicted_path
        if generation != self._generation:
            self._close_request(request_path)
            watcher.deleteLater()
            return
        if not request_path:
            self._finish("unavailable", "Desktop portal returned no inhibitor handle.")
            watcher.deleteLater()
            return
        self._request_paths[generation] = request_path
        self._timeout.stop()
        logger.info("Portal suspend inhibitor active")
        self.state_changed.emit(
            generation,
            "active",
            "System sleep is inhibited; display idle remains enabled.",
        )
        watcher.deleteLater()

    @pyqtSlot(str, str, str)
    def _on_name_owner_changed(self, name: str, old_owner: str, new_owner: str) -> None:
        if name != _PORTAL_SERVICE:
            return
        if old_owner and old_owner != new_owner and self._generation is not None:
            generation = self._generation
            self.release()
            self.state_changed.emit(generation, "unavailable", "Desktop portal restarted; inhibition was lost.")
        if new_owner and old_owner != new_owner:
            self.portal_available.emit()

    def _on_timeout(self) -> None:
        self._finish("unavailable", "Desktop portal did not answer the inhibit request.")

    def _finish(self, state: str, detail: str) -> None:
        generation = self._generation
        self.release()
        if generation is not None:
            logger.warning("Suspend inhibition failed: %s", detail)
            self.state_changed.emit(generation, state, detail)


class WindowsSuspendBackend(QObject):
    """Use SetThreadExecutionState to inhibit Windows system sleep only."""

    state_changed = pyqtSignal(int, str, str)
    portal_available = pyqtSignal()

    def __init__(
        self,
        parent: QObject | None = None,
        execution_state: Callable[[int], int] | None = None,
    ) -> None:
        super().__init__(parent)
        self._execution_state = execution_state
        self._active = False

    def _call(self, flags: int) -> int:
        if self._execution_state is not None:
            return self._execution_state(flags)
        return int(ctypes.windll.kernel32.SetThreadExecutionState(flags))  # type: ignore[attr-defined]

    def acquire(self, generation: int) -> bool:
        try:
            result = self._call(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        except (AttributeError, OSError):
            logger.exception("Windows suspend inhibition is unavailable")
            self.state_changed.emit(generation, "unavailable", "Windows execution-state API is unavailable.")
            return False
        if not result:
            self.state_changed.emit(generation, "unavailable", "Windows rejected the system-sleep inhibitor.")
            return False
        self._active = True
        self.state_changed.emit(generation, "active", "System sleep is inhibited; display idle remains enabled.")
        return True

    def release(self) -> None:
        if not self._active:
            return
        self._active = False
        try:
            if not self._call(_ES_CONTINUOUS):
                logger.warning("Windows did not confirm suspend inhibitor release")
        except (AttributeError, OSError):
            logger.exception("Could not release Windows suspend inhibitor")

    def cleanup(self) -> None:
        self.release()


class RemoteSleepInhibitor(QObject):
    """Reconcile remote-mode intent with a platform suspend inhibitor."""

    status_changed = pyqtSignal(str, str)

    def __init__(self, parent: QObject | None = None, backend: QObject | None = None) -> None:
        super().__init__(parent)
        if backend is None:
            backend = WindowsSuspendBackend(self) if sys.platform == "win32" else PortalSuspendBackend(self)
        self._backend = backend
        self._backend.state_changed.connect(self._on_backend_state_changed)  # type: ignore[attr-defined]
        self._backend.portal_available.connect(self._on_backend_available)  # type: ignore[attr-defined]
        self._generation = 0
        self._desired = False
        self._request_pending = False
        self._state = "off"
        self._detail = "Remote controller is off."

    def configure(self, role: str, enabled: bool, subsystem_running: bool = True) -> None:
        desired = role in ("send", "receive") and enabled and subsystem_running
        if desired == self._desired:
            return
        self._desired = desired
        self._generation += 1
        self._request_pending = False
        if not desired:
            self._backend.release()  # type: ignore[attr-defined]
            self._publish("off", "System sleep prevention is off.")
            return
        self._acquire()

    def refresh(self) -> None:
        """Re-establish inhibition after a portal restart or unexpected resume."""
        if not self._desired:
            return
        self._generation += 1
        self._backend.release()  # type: ignore[attr-defined]
        self._request_pending = False
        self._state = "off"
        self._acquire()

    def cleanup(self) -> None:
        self._desired = False
        self._generation += 1
        self._request_pending = False
        self._backend.cleanup()  # type: ignore[attr-defined]
        self._publish("off", "System sleep prevention is off.")

    def _acquire(self) -> None:
        if not self._desired or self._request_pending or self._state == "active":
            return
        self._request_pending = True
        self._publish("acquiring", "Requesting permission to prevent system sleep...")
        if not self._backend.acquire(self._generation):  # type: ignore[attr-defined]
            self._request_pending = False

    @pyqtSlot(int, str, str)
    def _on_backend_state_changed(self, generation: int, state: str, detail: str) -> None:
        if generation != self._generation or not self._desired:
            if state == "active":
                self._backend.release()  # type: ignore[attr-defined]
            return
        self._request_pending = False
        self._publish(state, detail)

    @pyqtSlot()
    def _on_backend_available(self) -> None:
        if self._desired and self._state == "unavailable":
            self._generation += 1
            self._acquire()

    def _publish(self, state: str, detail: str) -> None:
        if (state, detail) == (self._state, self._detail):
            return
        self._state = state
        self._detail = detail
        self.status_changed.emit(state, detail)
