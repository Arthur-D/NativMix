import json
from dataclasses import dataclass

import pytest
from PyQt6.QtCore import QByteArray, QObject, pyqtSignal
from PyQt6.QtNetwork import QNetworkReply, QNetworkRequest

from nativmix.utils.update_checker import (
    RELEASE_API_URL,
    REQUEST_TIMEOUT_MS,
    USER_AGENT,
    UpdateChecker,
    newer_release_version,
    update_checks_supported,
)


@dataclass
class _Config:
    check_for_updates: bool = False
    ignored_update_version: str = ""
    save_count: int = 0

    def save(self) -> None:
        self.save_count += 1


class _Reply(QObject):
    finished = pyqtSignal()

    def __init__(
        self,
        payload: object = None,
        *,
        status: int | None = 200,
        error: QNetworkReply.NetworkError = QNetworkReply.NetworkError.NoError,
    ) -> None:
        super().__init__()
        self._body = QByteArray(json.dumps(payload).encode()) if payload is not None else QByteArray()
        self._status = status
        self._error = error
        self.abort_count = 0
        self.deleted = False

    def error(self) -> QNetworkReply.NetworkError:
        return self._error

    def errorString(self) -> str:
        return "simulated network failure"

    def attribute(self, attribute: QNetworkRequest.Attribute) -> int | None:
        assert attribute == QNetworkRequest.Attribute.HttpStatusCodeAttribute
        return self._status

    def readAll(self) -> QByteArray:
        return self._body

    def abort(self) -> None:
        self.abort_count += 1

    def deleteLater(self) -> None:
        self.deleted = True


class _NetworkManager:
    def __init__(self, replies: list[_Reply]) -> None:
        self.replies = replies
        self.requests: list[QNetworkRequest] = []

    def get(self, request: QNetworkRequest) -> _Reply:
        self.requests.append(request)
        return self.replies.pop(0)


@pytest.mark.parametrize(
    ("is_flatpak", "platform_name", "expected"),
    [
        (True, "linux", True),
        (False, "win32", True),
        (False, "linux", False),
        (False, "darwin", False),
    ],
)
def test_platform_policy(is_flatpak, platform_name, expected):
    assert update_checks_supported(is_flatpak=is_flatpak, platform_name=platform_name) is expected


@pytest.mark.parametrize(
    ("installed", "remote", "kwargs", "expected"),
    [
        ("1.0.0", "v1.1.0", {}, "1.1.0"),
        ("V1.0.0", "V1.1.0", {}, "1.1.0"),
        ("1.0.0", "1.0.0", {}, None),
        ("1.1.0", "1.0.0", {}, None),
        ("broken", "1.1.0", {}, None),
        ("1.0.0", "not-a-version", {}, None),
        ("1.0.0", "1.1.0rc1", {}, None),
        ("1.1.0rc1", "1.1.0", {}, "1.1.0"),
        ("1.0.0+flatpak", "1.0.0", {}, None),
        ("1.0.0.dev1+local", "1.0.0", {}, "1.0.0"),
        ("1.1.0.dev1", "1.0.0", {}, None),
        ("1.0.0", "1.1.0", {"draft": True}, None),
        ("1.0.0", "1.1.0", {"prerelease": True}, None),
    ],
)
def test_version_comparison(installed, remote, kwargs, expected):
    assert newer_release_version(installed, remote, **kwargs) == expected


def test_default_disabled_constructs_no_request(monkeypatch):
    manager = _NetworkManager([])
    checker = UpdateChecker(_Config(), network_manager=manager, supported=True)

    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("request must not be constructed before opt-in")

    monkeypatch.setattr("nativmix.utils.update_checker.QNetworkRequest", fail_if_constructed)
    assert checker.check_at_startup() is False
    assert manager.requests == []


def test_explicit_enable_starts_async_request_with_fork_headers():
    config = _Config(check_for_updates=True)
    reply = _Reply({"tag_name": "v1.1.0", "draft": False, "prerelease": False})
    manager = _NetworkManager([reply])
    checker = UpdateChecker(config, network_manager=manager, installed_version="1.0.0", supported=True)

    assert checker.check_now() is True
    assert checker.request_in_progress is True
    request = manager.requests[0]
    assert request.url().toString() == RELEASE_API_URL
    assert bytes(request.rawHeader(b"User-Agent")).decode() == USER_AGENT
    assert bytes(request.rawHeader(b"Accept")) == b"application/vnd.github+json"
    assert REQUEST_TIMEOUT_MS == 10_000


def test_settings_enable_checks_immediately_and_disable_cancels():
    from nativmix.gui.main_window import MainWindow

    class Checker:
        def __init__(self):
            self.check_count = 0
            self.cancel_count = 0

        def check_now(self):
            self.check_count += 1

        def cancel(self):
            self.cancel_count += 1

    class Window:
        _update_checker = Checker()

    window = Window()
    MainWindow._on_update_checks_changed(window, True)
    MainWindow._on_update_checks_changed(window, False)

    assert window._update_checker.check_count == 1
    assert window._update_checker.cancel_count == 1


def test_startup_check_runs_at_most_once():
    config = _Config(check_for_updates=True)
    manager = _NetworkManager([_Reply()])
    checker = UpdateChecker(config, network_manager=manager, supported=True)

    assert checker.check_at_startup() is True
    assert checker.check_at_startup() is False
    assert len(manager.requests) == 1


def test_disabled_and_unsupported_checks_do_not_request():
    manager = _NetworkManager([])
    assert UpdateChecker(_Config(), network_manager=manager, supported=True).check_now() is False
    enabled = _Config(check_for_updates=True)
    assert UpdateChecker(enabled, network_manager=manager, supported=False).check_now() is False
    assert manager.requests == []


def test_failed_request_creation_is_handled():
    class Manager:
        def get(self, request):
            return None

    checker = UpdateChecker(_Config(check_for_updates=True), network_manager=Manager(), supported=True)

    assert checker.check_now() is False
    assert checker.request_in_progress is False


def test_disable_cancels_active_request_and_prevents_future_checks():
    config = _Config(check_for_updates=True)
    reply = _Reply()
    manager = _NetworkManager([reply])
    checker = UpdateChecker(config, network_manager=manager, supported=True)
    checker.check_now()

    config.check_for_updates = False
    checker.cancel()

    assert reply.abort_count == 1
    assert reply.deleted is True
    assert checker.request_in_progress is False
    assert checker.check_now() is False
    assert len(manager.requests) == 1


def test_valid_newer_response_emits_and_cleans_up(qtbot):
    config = _Config(check_for_updates=True)
    reply = _Reply({"tag_name": "v1.2.0", "draft": False, "prerelease": False})
    checker = UpdateChecker(
        config,
        network_manager=_NetworkManager([reply]),
        installed_version="1.0.0",
        supported=True,
    )

    with qtbot.waitSignal(checker.release_available) as signal:
        checker.check_now()
        reply.finished.emit()

    assert signal.args == ["1.0.0", "1.2.0"]
    assert checker.request_in_progress is False
    assert checker._timeout_timer.isActive() is False
    assert reply.deleted is True


@pytest.mark.parametrize(
    "reply",
    [
        _Reply(status=503),
        _Reply(error=QNetworkReply.NetworkError.ConnectionRefusedError),
        _Reply(payload=["not", "an", "object"]),
    ],
)
def test_response_failures_do_not_notify(reply, qtbot):
    config = _Config(check_for_updates=True)
    checker = UpdateChecker(config, network_manager=_NetworkManager([reply]), supported=True)
    received = []
    checker.release_available.connect(lambda *args: received.append(args))

    checker.check_now()
    reply.finished.emit()

    assert received == []
    assert checker.request_in_progress is False
    assert checker._timeout_timer.isActive() is False
    assert reply.deleted is True


def test_malformed_json_does_not_notify(qtbot):
    reply = _Reply()
    reply._body = QByteArray(b"{broken")
    checker = UpdateChecker(
        _Config(check_for_updates=True),
        network_manager=_NetworkManager([reply]),
        supported=True,
    )
    received = []
    checker.release_available.connect(lambda *args: received.append(args))

    checker.check_now()
    reply.finished.emit()

    assert received == []
    assert reply.deleted is True


def test_timeout_aborts_cleans_up_and_does_not_notify():
    reply = _Reply()
    checker = UpdateChecker(
        _Config(check_for_updates=True),
        network_manager=_NetworkManager([reply]),
        supported=True,
    )
    received = []
    checker.release_available.connect(lambda *args: received.append(args))
    checker.check_now()

    checker._on_timeout()

    assert received == []
    assert reply.abort_count == 1
    assert reply.deleted is True
    assert checker.request_in_progress is False
    assert checker._timeout_timer.isActive() is False


def test_overlapping_request_is_suppressed():
    first = _Reply()
    second = _Reply()
    manager = _NetworkManager([first, second])
    checker = UpdateChecker(_Config(check_for_updates=True), network_manager=manager, supported=True)

    assert checker.check_now() is True
    assert checker.check_now() is False
    assert len(manager.requests) == 1


def test_ignored_version_is_silent_but_newer_release_notifies(qtbot):
    config = _Config(check_for_updates=True, ignored_update_version="1.1.0")
    ignored = _Reply({"tag_name": "v1.1.0"})
    newer = _Reply({"tag_name": "v1.2.0"})
    checker = UpdateChecker(
        config,
        network_manager=_NetworkManager([ignored, newer]),
        installed_version="1.0.0",
        supported=True,
    )
    received = []
    checker.release_available.connect(lambda *args: received.append(args))

    checker.check_now()
    ignored.finished.emit()
    checker.check_now()
    newer.finished.emit()

    assert received == [("1.0.0", "1.2.0")]


def test_ignore_version_persists():
    config = _Config(check_for_updates=True)
    checker = UpdateChecker(config, network_manager=_NetworkManager([]), supported=True)

    checker.ignore_version("1.2.0")

    assert config.ignored_update_version == "1.2.0"
    assert config.save_count == 1
