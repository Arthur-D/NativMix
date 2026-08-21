"""Opt-in GitHub release checks for GitHub-distributed NativMix builds."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from packaging.version import InvalidVersion, Version
from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from nativmix.metadata import __version__
from nativmix.utils.proc_resolver import IS_FLATPAK

logger = logging.getLogger(__name__)

RELEASE_API_URL = "https://api.github.com/repos/Arthur-D/NativMix/releases/latest"
RELEASE_PAGE_URL = "https://github.com/Arthur-D/NativMix/releases/latest"
REQUEST_TIMEOUT_MS = 10_000
USER_AGENT = f"NativMix/{__version__} (+https://github.com/Arthur-D/NativMix)"


def update_checks_supported(*, is_flatpak: bool | None = None, platform_name: str | None = None) -> bool:
    """Return whether this install is distributed through GitHub releases."""
    flatpak = IS_FLATPAK if is_flatpak is None else is_flatpak
    platform = sys.platform if platform_name is None else platform_name
    return flatpak or platform == "win32"


def newer_release_version(
    installed_version: str,
    remote_tag: str,
    *,
    draft: bool = False,
    prerelease: bool = False,
) -> str | None:
    """Return the normalized remote version only when it is a newer stable release."""
    if draft or prerelease:
        return None
    try:
        installed_text = installed_version.strip()
        remote_text = remote_tag.strip()
        if installed_text[:1] in {"v", "V"}:
            installed_text = installed_text[1:]
        if remote_text[:1] in {"v", "V"}:
            remote_text = remote_text[1:]
        installed = Version(installed_text)
        remote = Version(remote_text)
    except (AttributeError, InvalidVersion):
        return None
    if remote.is_prerelease or remote.is_devrelease or remote <= installed:
        return None
    return str(remote)


class UpdateChecker(QObject):
    """Perform at most one asynchronous release request at a time."""

    release_available = pyqtSignal(str, str)  # installed version, available version

    def __init__(
        self,
        config: Any,
        *,
        network_manager: QNetworkAccessManager | None = None,
        installed_version: str = __version__,
        supported: bool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._manager = network_manager if network_manager is not None else QNetworkAccessManager(self)
        self._installed_version = installed_version
        self._supported = update_checks_supported() if supported is None else supported
        self._reply: QNetworkReply | None = None
        self._timed_out = False
        self._startup_check_attempted = False
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._on_timeout)

    @property
    def request_in_progress(self) -> bool:
        return self._reply is not None

    def check_at_startup(self) -> bool:
        """Start the single automatic check for this process, if opted in."""
        if self._startup_check_attempted:
            return False
        self._startup_check_attempted = True
        return self.check_now()

    def check_now(self) -> bool:
        """Start an asynchronous request when policy and consent allow it."""
        if not self._supported or not self._config.check_for_updates or self._reply is not None:
            return False

        request = QNetworkRequest(QUrl(RELEASE_API_URL))
        request.setRawHeader(b"User-Agent", USER_AGENT.encode("ascii"))
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
        reply = self._manager.get(request)
        if reply is None:
            logger.warning("Could not create GitHub update request")
            return False
        self._reply = reply
        self._timed_out = False
        reply.finished.connect(lambda current=reply: self._on_finished(current))
        self._timeout_timer.start(REQUEST_TIMEOUT_MS)
        logger.debug("Checking Arthur-D/NativMix GitHub releases")
        return True

    def cancel(self) -> None:
        """Abort an active request without issuing any further network access."""
        reply = self._reply
        if reply is None:
            return
        self._timeout_timer.stop()
        self._reply = None
        reply.abort()
        reply.deleteLater()
        logger.debug("Update check cancelled")

    def ignore_version(self, version: str) -> None:
        """Persist the remote version the user chose not to see again."""
        self._config.ignored_update_version = version
        self._config.save()

    def _on_timeout(self) -> None:
        reply = self._reply
        if reply is None:
            return
        self._timed_out = True
        logger.warning("GitHub update check timed out")
        reply.abort()
        if self._reply is reply:
            self._on_finished(reply)

    def _on_finished(self, reply: QNetworkReply) -> None:
        if reply is not self._reply:
            return

        self._timeout_timer.stop()
        self._reply = None
        timed_out = self._timed_out
        self._timed_out = False
        try:
            if timed_out:
                return
            if reply.error() != QNetworkReply.NetworkError.NoError:
                logger.warning("GitHub update check failed: %s", reply.errorString())
                return

            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if not isinstance(status, int) or not 200 <= status < 300:
                logger.warning("GitHub update check returned HTTP %s", status)
                return

            try:
                payload = json.loads(bytes(reply.readAll()))
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
                logger.warning("GitHub update response was not valid JSON: %s", exc)
                return
            if not isinstance(payload, dict):
                logger.warning("GitHub update response was not a JSON object")
                return

            remote = newer_release_version(
                self._installed_version,
                payload.get("tag_name", ""),
                draft=payload.get("draft") is True,
                prerelease=payload.get("prerelease") is True,
            )
            if remote is None or remote == self._config.ignored_update_version:
                return
            if not self._config.check_for_updates:
                return
            self.release_available.emit(self._installed_version, remote)
        finally:
            reply.deleteLater()
