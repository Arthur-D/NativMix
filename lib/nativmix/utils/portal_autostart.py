"""Asynchronous Flatpak autostart requests via the XDG Background portal."""

from __future__ import annotations

import logging
import secrets

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtDBus import (
    QDBusConnection,
    QDBusMessage,
    QDBusPendingCallWatcher,
    QDBusPendingReply,
    QDBusVariant,
)

logger = logging.getLogger(__name__)

_PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_BACKGROUND_INTERFACE = "org.freedesktop.portal.Background"
_REQUEST_INTERFACE = "org.freedesktop.portal.Request"
_REQUEST_TIMEOUT_MS = 30_000


class PortalAutostart(QObject):
    """Submit and track one Background.RequestBackground request at a time."""

    finished = pyqtSignal(bool, bool, str)  # requested state, success, detail

    def __init__(self, parent: QObject | None = None, timeout_ms: int = _REQUEST_TIMEOUT_MS) -> None:
        super().__init__(parent)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.setInterval(timeout_ms)
        self._timeout.timeout.connect(self._on_timeout)
        self._bus: QDBusConnection | None = None
        self._watcher: QDBusPendingCallWatcher | None = None
        self._request_path: str | None = None
        self._requested_state: bool | None = None

    @property
    def pending(self) -> bool:
        return self._requested_state is not None

    def request(self, enabled: bool) -> bool:
        """Start a non-blocking portal request, returning whether it was submitted."""
        if self.pending:
            logger.warning("Portal autostart request ignored while another request is pending")
            return False

        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            self.finished.emit(enabled, False, "The desktop portal session bus is unavailable.")
            return False

        token = f"nativmix_{secrets.token_hex(16)}"
        sender = bus.baseService().lstrip(":").replace(".", "_")
        request_path = f"{_PORTAL_PATH}/request/{sender}/{token}"
        self._bus = bus
        self._request_path = request_path
        self._requested_state = enabled

        if not self._connect_response(request_path):
            self._finish(False, "Could not subscribe to the desktop portal response.")
            return False

        options = {
            "handle_token": QDBusVariant(token),
            "reason": QDBusVariant("Start NativMix automatically to restore hardware mixer control."),
            "autostart": QDBusVariant(enabled),
            "commandline": QDBusVariant(["nativmix", "--hidden"]),
        }
        message = QDBusMessage.createMethodCall(
            _PORTAL_SERVICE,
            _PORTAL_PATH,
            _BACKGROUND_INTERFACE,
            "RequestBackground",
        )
        message.setArguments(["", options])
        pending_call = bus.asyncCall(message)
        self._watcher = QDBusPendingCallWatcher(pending_call, self)
        self._watcher.finished.connect(self._on_method_finished)
        self._timeout.start()
        logger.debug("Portal autostart request submitted (enabled=%s, path=%s)", enabled, request_path)
        return True

    def _connect_response(self, path: str) -> bool:
        if self._bus is None:
            return False
        return self._bus.connect(
            _PORTAL_SERVICE,
            path,
            _REQUEST_INTERFACE,
            "Response",
            self._on_response,
        )

    def _disconnect_response(self) -> None:
        if self._bus is not None and self._request_path is not None:
            self._bus.disconnect(
                _PORTAL_SERVICE,
                self._request_path,
                _REQUEST_INTERFACE,
                "Response",
                self._on_response,
            )

    def _on_method_finished(self, watcher: QDBusPendingCallWatcher) -> None:
        reply = QDBusPendingReply(watcher)
        if reply.isError():
            self._finish(False, reply.error().message() or "The portal request could not be started.")
            return

        returned = reply.value()
        returned_path = returned.path() if hasattr(returned, "path") else str(returned)
        if returned_path and returned_path != self._request_path:
            self._disconnect_response()
            self._request_path = returned_path
            if not self._connect_response(returned_path):
                self._finish(False, "Could not subscribe to the returned desktop portal request.")
                return
        watcher.deleteLater()
        self._watcher = None

    def _on_response(self, response: int, results: dict) -> None:
        enabled = bool(self._requested_state)
        if response != 0:
            detail = "Autostart permission was denied." if response == 1 else "The portal request failed."
            self._finish(False, detail)
            return
        result = results.get("autostart", False)
        confirmed = bool(result.variant() if hasattr(result, "variant") else result)
        if confirmed != enabled:
            self._finish(False, "The desktop portal did not confirm the requested autostart state.")
            return
        self._finish(True, "Autostart was confirmed by the desktop portal.")

    def _on_timeout(self) -> None:
        if self._request_path is not None and self._bus is not None:
            message = QDBusMessage.createMethodCall(
                _PORTAL_SERVICE,
                self._request_path,
                _REQUEST_INTERFACE,
                "Close",
            )
            self._bus.asyncCall(message)
        self._finish(False, "The desktop portal did not respond in time.")

    def _finish(self, success: bool, detail: str) -> None:
        if self._requested_state is None:
            return
        requested = self._requested_state
        self._timeout.stop()
        self._disconnect_response()
        if self._watcher is not None:
            self._watcher.deleteLater()
        self._watcher = None
        self._request_path = None
        self._requested_state = None
        self._bus = None
        if success:
            logger.info("Portal autostart state confirmed: %s", requested)
        else:
            logger.warning("Portal autostart request failed (enabled=%s): %s", requested, detail)
        self.finished.emit(requested, success, detail)
