from __future__ import annotations

from unittest.mock import MagicMock, patch

from PyQt6.QtCore import QMetaType, QObject, pyqtSignal

from nativmix.utils.sleep_inhibitor import (
    SUSPEND_INHIBIT_FLAG,
    PortalSuspendBackend,
    RemoteSleepInhibitor,
    WindowsSuspendBackend,
)


class FakeBackend(QObject):
    state_changed = pyqtSignal(int, str, str)
    portal_available = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.acquisitions: list[int] = []
        self.releases = 0
        self.cleanups = 0

    def acquire(self, generation: int) -> bool:
        self.acquisitions.append(generation)
        return True

    def release(self) -> None:
        self.releases += 1

    def cleanup(self) -> None:
        self.cleanups += 1


def test_send_receive_acquire_waiting_duplicates_and_off_release(qtbot) -> None:
    backend = FakeBackend()
    inhibitor = RemoteSleepInhibitor(backend=backend)

    inhibitor.configure("send", True)
    inhibitor.configure("send", True)
    inhibitor.configure("receive", True)
    assert len(backend.acquisitions) == 1

    generation = backend.acquisitions[0]
    backend.state_changed.emit(generation, "active", "active while waiting")
    inhibitor.configure("receive", True)
    assert len(backend.acquisitions) == 1

    inhibitor.configure("off", True)
    assert backend.releases == 1


def test_setting_toggle_and_subsystem_stop_release() -> None:
    backend = FakeBackend()
    inhibitor = RemoteSleepInhibitor(backend=backend)

    inhibitor.configure("receive", False)
    assert backend.acquisitions == []
    inhibitor.configure("receive", True)
    assert len(backend.acquisitions) == 1
    inhibitor.configure("receive", True, subsystem_running=False)
    assert backend.releases == 1


def test_delayed_grant_after_off_is_released() -> None:
    backend = FakeBackend()
    inhibitor = RemoteSleepInhibitor(backend=backend)
    inhibitor.configure("send", True)
    stale_generation = backend.acquisitions[0]
    inhibitor.configure("off", True)

    backend.state_changed.emit(stale_generation, "active", "late")

    assert backend.releases == 2


def test_denial_has_no_retry_storm_and_owner_return_reacquires() -> None:
    backend = FakeBackend()
    inhibitor = RemoteSleepInhibitor(backend=backend)
    inhibitor.configure("receive", True)
    generation = backend.acquisitions[0]

    backend.state_changed.emit(generation, "unavailable", "portal lost")
    assert len(backend.acquisitions) == 1
    backend.portal_available.emit()
    assert len(backend.acquisitions) == 2


def test_cleanup_releases_backend() -> None:
    backend = FakeBackend()
    inhibitor = RemoteSleepInhibitor(backend=backend)
    inhibitor.configure("send", True)
    inhibitor.cleanup()
    assert backend.cleanups == 1


def test_resume_refresh_releases_and_reacquires() -> None:
    backend = FakeBackend()
    inhibitor = RemoteSleepInhibitor(backend=backend)
    inhibitor.configure("send", True)
    backend.state_changed.emit(backend.acquisitions[0], "active", "active")

    inhibitor.refresh()

    assert backend.releases == 1
    assert len(backend.acquisitions) == 2


def test_portal_requests_only_suspend_flag_and_empty_parent() -> None:
    class CapturingMessage:
        def __init__(self) -> None:
            self.arguments = None

        def setArguments(self, arguments) -> None:
            self.arguments = arguments

    portal = PortalSuspendBackend()
    bus = MagicMock()
    bus.isConnected.return_value = True
    bus.baseService.return_value = ":1.42"
    bus.connect.return_value = True
    message = CapturingMessage()
    watcher = MagicMock()
    watcher.finished.connect = MagicMock()
    unsigned_flag = object()

    with (
        patch("nativmix.utils.sleep_inhibitor.QDBusConnection.sessionBus", return_value=bus),
        patch("nativmix.utils.sleep_inhibitor.QDBusMessage.createMethodCall", return_value=message),
        patch("nativmix.utils.sleep_inhibitor.QDBusPendingCallWatcher", return_value=watcher),
        patch("nativmix.utils.sleep_inhibitor._dbus_uint32", return_value=unsigned_flag) as encode_uint32,
    ):
        assert portal.acquire(1)

    assert message.arguments is not None
    parent, flags, options = message.arguments
    assert parent == ""
    assert flags is unsigned_flag
    encode_uint32.assert_called_once_with(SUSPEND_INHIBIT_FLAG)
    assert SUSPEND_INHIBIT_FLAG == 4
    assert set(options) == {"handle_token", "reason"}
    portal.cleanup()


def test_portal_flag_is_encoded_as_dbus_uint32() -> None:
    with patch("nativmix.utils.sleep_inhibitor.QDBusArgument") as argument:
        from nativmix.utils.sleep_inhibitor import _dbus_uint32

        _dbus_uint32(4)

    argument.assert_called_once_with(4, int(QMetaType.Type.UInt.value))


def test_portal_denial_and_owner_loss_are_explicit(qtbot) -> None:
    portal = PortalSuspendBackend()
    watcher = MagicMock()
    portal._generation = 4
    portal._watchers[watcher] = 4
    reply = MagicMock()
    reply.isError.return_value = True
    reply.error.return_value.name.return_value = "org.freedesktop.DBus.Error.AccessDenied"
    reply.error.return_value.message.return_value = "Permission denied"

    with (
        patch("nativmix.utils.sleep_inhibitor.QDBusPendingReply", return_value=reply),
        qtbot.waitSignal(portal.state_changed, timeout=1000) as denied,
    ):
        portal._on_method_finished(watcher)
    assert denied.args[0:2] == [4, "denied"]

    portal._generation = 5
    with qtbot.waitSignal(portal.state_changed, timeout=1000) as lost:
        portal._on_name_owner_changed("org.freedesktop.portal.Desktop", ":1.2", "")
    assert lost.args[0:2] == [5, "unavailable"]


def test_portal_closes_handle_returned_after_release() -> None:
    portal = PortalSuspendBackend()
    bus = MagicMock()
    bus.isConnected.return_value = True
    portal._bus = bus
    portal._generation = 7
    portal._request_paths[7] = "/predicted"
    watcher = MagicMock()
    portal._watchers[watcher] = 7
    portal.release()

    reply = MagicMock()
    reply.isError.return_value = False
    reply.value.return_value.path.return_value = "/returned"
    close_message = MagicMock()
    with (
        patch("nativmix.utils.sleep_inhibitor.QDBusPendingReply", return_value=reply),
        patch("nativmix.utils.sleep_inhibitor.QDBusMessage.createMethodCall", return_value=close_message) as create,
    ):
        portal._on_method_finished(watcher)

    assert create.call_args.args == (
        "org.freedesktop.portal.Desktop",
        "/returned",
        "org.freedesktop.portal.Request",
        "Close",
    )


def test_portal_direct_owner_replacement_reports_loss_and_availability(qtbot) -> None:
    portal = PortalSuspendBackend()
    portal._generation = 6

    with (
        qtbot.waitSignal(portal.state_changed, timeout=1000) as lost,
        qtbot.waitSignal(portal.portal_available, timeout=1000),
    ):
        portal._on_name_owner_changed("org.freedesktop.portal.Desktop", ":1.2", ":1.3")

    assert lost.args[0:2] == [6, "unavailable"]


def test_windows_uses_system_required_without_display_required() -> None:
    calls: list[int] = []
    backend = WindowsSuspendBackend(execution_state=lambda flags: calls.append(flags) or 1)
    backend.acquire(1)
    backend.release()

    assert calls == [0x80000001, 0x80000000]
