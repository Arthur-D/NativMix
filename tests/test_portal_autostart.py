from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import QMetaMethod, QObject, pyqtSignal
from PyQt6.QtDBus import QDBusArgument

from nativmix.utils.config_manager import ConfigManager
from nativmix.utils.portal_autostart import PortalAutostart


class FakePortal(QObject):
    finished = pyqtSignal(bool, bool, str)

    def __init__(self, accepts_request: bool = True) -> None:
        super().__init__()
        self.accepts_request = accepts_request
        self.requests: list[bool] = []

    def request(self, enabled: bool) -> bool:
        self.requests.append(enabled)
        return self.accepts_request


def _config(tmp_path) -> ConfigManager:
    return ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")


def _prime_request(portal: PortalAutostart, enabled: bool) -> None:
    portal._requested_state = enabled


def test_portal_response_callback_is_registered_as_exact_qt_slot():
    portal = PortalAutostart()

    method_index = portal.metaObject().indexOfSlot("_on_response(uint,QVariantMap)")

    assert method_index >= 0
    assert portal.metaObject().method(method_index).methodType() is QMetaMethod.MethodType.Slot


def test_portal_enable_success_is_confirmed(qtbot):
    portal = PortalAutostart()
    _prime_request(portal, True)

    with qtbot.waitSignal(portal.finished, timeout=1000) as signal:
        portal._on_response(0, {"autostart": True})

    assert signal.args == [True, True, "Autostart was confirmed by the desktop portal."]


def test_portal_disable_success_is_confirmed(qtbot):
    portal = PortalAutostart()
    _prime_request(portal, False)

    with qtbot.waitSignal(portal.finished, timeout=1000) as signal:
        portal._on_response(0, {"autostart": False})

    assert signal.args[0:2] == [False, True]


def test_portal_denial_rejects_request(qtbot):
    portal = PortalAutostart()
    _prime_request(portal, True)

    with qtbot.waitSignal(portal.finished, timeout=1000) as signal:
        portal._on_response(1, {})

    assert signal.args == [True, False, "Autostart permission was denied."]


def test_portal_unavailable_reports_error(qtbot):
    portal = PortalAutostart()
    bus = MagicMock()
    bus.isConnected.return_value = False

    with (
        patch("nativmix.utils.portal_autostart.QDBusConnection.sessionBus", return_value=bus),
        qtbot.waitSignal(portal.finished, timeout=1000) as signal,
    ):
        assert portal.request(True) is False

    assert signal.args == [True, False, "The desktop portal session bus is unavailable."]


def test_portal_connection_exception_reports_failed_result(qtbot, caplog):
    portal = PortalAutostart()
    bus = MagicMock()
    bus.isConnected.return_value = True
    bus.baseService.return_value = ":1.42"
    bus.connect.side_effect = TypeError(
        "callable must be a method of a QtCore.QObject instance decorated by QtCore.pyqtSlot"
    )

    with (
        patch("nativmix.utils.portal_autostart.QDBusConnection.sessionBus", return_value=bus),
        caplog.at_level(logging.WARNING),
        qtbot.waitSignal(portal.finished, timeout=1000) as signal,
    ):
        assert portal.request(True) is False

    assert signal.args == [True, False, "Could not subscribe to the desktop portal response."]
    assert portal.pending is False
    assert "callable must be a method of a QtCore.QObject instance decorated by QtCore.pyqtSlot" in caplog.text


def test_portal_request_uses_native_option_types():
    class CapturingMessage:
        def __init__(self) -> None:
            self.arguments = None

        def setArguments(self, arguments) -> None:
            self.arguments = arguments

    portal = PortalAutostart()
    bus = MagicMock()
    bus.isConnected.return_value = True
    bus.baseService.return_value = ":1.42"
    bus.connect.return_value = True
    message = CapturingMessage()
    watcher = MagicMock()
    watcher.finished = MagicMock()
    watcher.finished.connect = MagicMock()

    with (
        patch("nativmix.utils.portal_autostart.QDBusConnection.sessionBus", return_value=bus),
        patch("nativmix.utils.portal_autostart.QDBusMessage.createMethodCall", return_value=message),
        patch("nativmix.utils.portal_autostart.QDBusPendingCallWatcher", return_value=watcher),
    ):
        assert portal.request(True) is True

    assert message.arguments is not None
    _, options = message.arguments
    assert isinstance(options["handle_token"], str)
    assert isinstance(options["reason"], str)
    assert isinstance(options["autostart"], bool)
    assert isinstance(options["commandline"], QDBusArgument)
    portal._finish(False, "cleanup")


def test_portal_method_error_rejects_request(qtbot):
    portal = PortalAutostart()
    _prime_request(portal, True)
    watcher = MagicMock()
    reply = MagicMock()
    reply.isError.return_value = True
    reply.error.return_value.message.return_value = "portal method failed"

    with (
        patch("nativmix.utils.portal_autostart.QDBusPendingReply", return_value=reply),
        qtbot.waitSignal(portal.finished, timeout=1000) as signal,
    ):
        portal._on_method_finished(watcher)

    assert signal.args == [True, False, "portal method failed"]
    assert portal.pending is False


def test_portal_timeout_reverts_pending_request(qtbot):
    portal = PortalAutostart()
    _prime_request(portal, True)

    with qtbot.waitSignal(portal.finished, timeout=1000) as signal:
        portal._on_timeout()

    assert signal.args == [True, False, "The desktop portal did not respond in time."]
    assert portal.pending is False


def test_flatpak_panel_waits_for_portal_confirmation(tmp_path, qtbot):
    from nativmix.gui.settings_panel import SettingsPanel

    config = _config(tmp_path)
    portal = FakePortal()
    with (
        patch("nativmix.gui.settings_panel.IS_FLATPAK", True),
        patch("nativmix.gui.settings_panel.is_windows", return_value=False),
        patch("nativmix.gui.settings_panel._systemd_unit_available") as systemd_probe,
    ):
        panel = SettingsPanel(config, autostart_portal=portal)

    panel._autostart_btn.click()
    assert portal.requests == [True]
    assert panel._autostart_btn.isEnabled() is False
    assert panel._autostart_btn.isChecked() is False
    assert "PENDING" in panel._autostart_btn.text()
    systemd_probe.assert_not_called()

    portal.finished.emit(True, True, "confirmed")
    assert panel._autostart_btn.isEnabled() is True
    assert panel._autostart_btn.isChecked() is True
    assert config.portal_autostart_enabled is True
    assert _config(tmp_path).portal_autostart_enabled is True
    panel.close()


def test_flatpak_panel_reverts_after_denial(tmp_path, qtbot):
    from nativmix.gui.settings_panel import SettingsPanel

    config = _config(tmp_path)
    portal = FakePortal()
    with (
        patch("nativmix.gui.settings_panel.IS_FLATPAK", True),
        patch("nativmix.gui.settings_panel.is_windows", return_value=False),
    ):
        panel = SettingsPanel(config, autostart_portal=portal)

    panel._autostart_btn.click()
    portal.finished.emit(True, False, "denied")
    assert panel._autostart_btn.isEnabled() is True
    assert panel._autostart_btn.isChecked() is False
    assert config.portal_autostart_enabled is False
    panel.close()


def test_flatpak_panel_reverts_after_portal_connection_exception(tmp_path, qtbot):
    from nativmix.gui.settings_panel import SettingsPanel

    config = _config(tmp_path)
    portal = PortalAutostart()
    bus = MagicMock()
    bus.isConnected.return_value = True
    bus.baseService.return_value = ":1.42"
    bus.connect.side_effect = TypeError(
        "callable must be a method of a QtCore.QObject instance decorated by QtCore.pyqtSlot"
    )
    with (
        patch("nativmix.gui.settings_panel.IS_FLATPAK", True),
        patch("nativmix.gui.settings_panel.is_windows", return_value=False),
    ):
        panel = SettingsPanel(config, autostart_portal=portal)

    with patch("nativmix.utils.portal_autostart.QDBusConnection.sessionBus", return_value=bus):
        panel._autostart_btn.click()

    assert portal.pending is False
    assert panel._autostart_btn.isEnabled() is True
    assert panel._autostart_btn.isChecked() is False
    assert "OFF" in panel._autostart_btn.text()
    panel.close()


def test_flatpak_panel_confirms_disable_only_after_response(tmp_path, qtbot):
    from nativmix.gui.settings_panel import SettingsPanel

    config = _config(tmp_path)
    config.portal_autostart_enabled = True
    config.save()
    portal = FakePortal()
    with (
        patch("nativmix.gui.settings_panel.IS_FLATPAK", True),
        patch("nativmix.gui.settings_panel.is_windows", return_value=False),
    ):
        panel = SettingsPanel(config, autostart_portal=portal)

    panel._autostart_btn.click()
    assert portal.requests == [False]
    assert panel._autostart_btn.isChecked() is True
    assert "PENDING" in panel._autostart_btn.text()

    portal.finished.emit(False, True, "confirmed")
    assert panel._autostart_btn.isChecked() is False
    assert config.portal_autostart_enabled is False
    panel.close()


def test_native_linux_still_selects_systemd_path(tmp_path, qtbot):
    from nativmix.gui.settings_panel import SettingsPanel

    config = _config(tmp_path)
    with (
        patch("nativmix.gui.settings_panel.IS_FLATPAK", False),
        patch("nativmix.gui.settings_panel.is_windows", return_value=False),
        patch("nativmix.gui.settings_panel._systemd_unit_available", return_value=True),
        patch("nativmix.gui.settings_panel._is_service_enabled", return_value=False),
    ):
        panel = SettingsPanel(config)

    assert panel._use_portal_autostart is False
    assert panel._use_systemd is True
    assert "(systemd)" in panel._autostart_btn.text()
    panel.close()


def test_native_linux_without_systemd_still_selects_xdg_path(tmp_path, qtbot):
    from nativmix.gui.settings_panel import SettingsPanel

    config = _config(tmp_path)
    with (
        patch("nativmix.gui.settings_panel.IS_FLATPAK", False),
        patch("nativmix.gui.settings_panel.is_windows", return_value=False),
        patch("nativmix.gui.settings_panel._systemd_unit_available", return_value=False),
        patch("nativmix.gui.settings_panel._is_autostart_enabled", return_value=False),
    ):
        panel = SettingsPanel(config)

    assert panel._use_portal_autostart is False
    assert panel._use_systemd is False
    assert "XDG" in panel._autostart_btn.toolTip()
    panel.close()
