"""Linux MPRIS control, independent of whether a player has an audio stream."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusMessage, QDBusPendingCallWatcher, QDBusPendingReply, QDBusVariant

from nativmix.utils.config_manager import SPECIAL_APPS
from nativmix.utils.proc_resolver import resolve_app_id_name, resolve_binary_name

logger = logging.getLogger(__name__)
PREFIX = "org.mpris.MediaPlayer2."
ROOT = "org.mpris.MediaPlayer2"
PLAYER = ROOT + ".Player"
PATH = "/org/mpris/MediaPlayer2"


@dataclass(frozen=True)
class Player:
    service: str
    identity: str
    desktop_entry: str
    status: str

    def matches(self, apps: list[str]) -> bool:
        """Reuse the mixer's identity aliases, but never fuzzy-match another player."""
        binary = self.service.removeprefix(PREFIX).split(".", 1)[0]
        names = {
            self.identity, self.desktop_entry, binary,
            resolve_app_id_name(self.desktop_entry) or "",
            resolve_binary_name(self.desktop_entry) or "",
            resolve_binary_name(binary) or "",
        }
        normalized = {name.strip().casefold() for name in names if name}
        return any(app.strip().casefold() in normalized for app in apps)


def select_player(players: list[Player], apps: list[str] | None, previous: str | None) -> Player | None:
    """None means all players; an empty/missing app mapping means no candidates."""
    if apps is not None:
        apps = [app for app in apps if app.strip().casefold() not in SPECIAL_APPS]
    candidates = sorted(
        (player for player in players if apps is None or player.matches(apps)),
        key=lambda player: player.service,
    )
    playing = [player for player in candidates if player.status == "Playing"]
    pool = playing or candidates
    remembered = next((player for player in pool if player.service == previous), None)
    paused = [player for player in pool if player.status == "Paused"]
    return remembered or next(iter(paused or pool), None)


class MediaController(QObject):
    """On-demand, bounded asynchronous requests. Never launch or retry a player."""

    status_changed = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._watchers: set[QDBusPendingCallWatcher] = set()
        self._previous: dict[int, str] = {}
        self._generation = 0
        self._busy = False

    def cancel(self) -> None:
        """Invalidate pending work before profile/input changes and shutdown."""
        self._generation += 1
        self._busy = False
        for watcher in self._watchers:
            watcher.finished.disconnect()
            watcher.deleteLater()
        self._watchers.clear()

    def _call(
        self, service: str, path: str, interface: str, method: str,
        arguments: list[Any], done: Callable[[list[Any] | None], None],
    ) -> None:
        message = QDBusMessage.createMethodCall(service, path, interface, method)
        message.setArguments(arguments)
        message.setAutoStartService(False)
        watcher = QDBusPendingCallWatcher(QDBusConnection.sessionBus().asyncCall(message, 1000), self)
        self._watchers.add(watcher)
        generation = self._generation

        def finished(call: QDBusPendingCallWatcher) -> None:
            self._watchers.discard(call)
            reply = QDBusPendingReply(call).reply()
            call.deleteLater()
            if generation != self._generation:
                return
            if reply.type() == QDBusMessage.MessageType.ErrorMessage:
                logger.debug("MPRIS %s %s failed: %s", service, method, reply.errorMessage())
                done(None)
            else:
                done(reply.arguments())

        watcher.finished.connect(finished)

    def toggle(self, channel: int, apps: list[str] | None) -> None:
        if self._busy:
            self.status_changed.emit("Media control is busy; try again shortly.")
            return
        if apps == []:
            self._finish("No application is assigned to this channel.")
            return
        self._busy = True
        self._call(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames", [],
            lambda reply: self._discovered(channel, apps, reply),
        )

    def _discovered(self, channel: int, apps: list[str] | None, reply: list[Any] | None) -> None:
        if not reply or not isinstance(reply[0], (list, tuple)):
            self._finish("Media players are unavailable (session bus discovery failed).")
            return
        names = sorted({name for name in reply[0] if isinstance(name, str) and name.startswith(PREFIX)})
        if not names:
            self._finish("No matching media player is running or available.")
            return
        properties: dict[str, dict[str, Any]] = {name: {} for name in names}
        pending = {(name, prop) for name in names for prop in ("Identity", "DesktopEntry", "PlaybackStatus")}

        def received(name: str, prop: str, result: list[Any] | None) -> None:
            if result:
                value = result[0].variant() if isinstance(result[0], QDBusVariant) else result[0]
                if isinstance(value, str):
                    properties[name][prop] = value
            pending.discard((name, prop))
            if pending:
                return
            players = [
                Player(service, values.get("Identity", ""), values.get("DesktopEntry", ""), values["PlaybackStatus"])
                for service, values in properties.items()
                if values.get("PlaybackStatus") in ("Playing", "Paused", "Stopped")
            ]
            selected = select_player(players, apps, self._previous.get(channel))
            if selected is None:
                self._finish("No matching media player is running or available.")
                return

            def toggled(response: list[Any] | None) -> None:
                if response is not None:
                    self._previous[channel] = selected.service
                    self._finish(f"Play/pause sent to {selected.identity or selected.service.removeprefix(PREFIX)}.")
                else:
                    self._finish("The mapped player is unavailable or cannot accept play/pause.")

            self._call(selected.service, PATH, PLAYER, "PlayPause", [], toggled)

        for name, prop in sorted(pending):
            interface = PLAYER if prop == "PlaybackStatus" else ROOT
            self._call(name, PATH, "org.freedesktop.DBus.Properties", "Get", [interface, prop],
                       partial(received, name, prop))

    def _finish(self, message: str) -> None:
        self._busy = False
        self.status_changed.emit(message)


class MediaControlRouter(QObject):
    """Keep receiver/local authority and all Qt/player decisions on the owning thread."""

    def __init__(self, config: Any, midi: Any, window: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config, self._midi, self._window = config, midi, window
        self.controller = MediaController(self)
        self.controller.status_changed.connect(window.settings_panel.media_status.setText)
        midi.media_toggle_requested.connect(self._toggle)
        midi.connection_changed.connect(self._connection_changed)
        midi.remote_sync_session_changed.connect(self._session_changed)
        config.settings_changed.connect(self.configure)
        config.mapping_changed.connect(self.controller.cancel)
        self.configure()

    @pyqtSlot()
    def configure(self) -> None:
        self.controller.cancel()
        self._midi.update_media_mappings(self._config.get_all_media_mappings())

    @pyqtSlot(bool)
    def _connection_changed(self, connected: bool) -> None:
        if not connected:
            self.controller.cancel()

    @pyqtSlot(object)
    def _session_changed(self, session: object) -> None:
        self.controller.cancel()

    @pyqtSlot(int, int)
    def _toggle(self, generation: int, channel: int) -> None:
        if (generation != self._midi._current_midi_cc_generation()
                or self._config.remote_midi_role == "send" or self._config.input_mode == "usb"):
            return
        active = self._window.settings_panel.active_media_button
        if (active is not None and active.isChecked()) or any(
            widget.is_waiting_for_midi() for widget in self._window._channels
        ):
            return
        apps = None if channel == -1 else self._config.get_app_names(channel)
        if channel != -1 and self._config.get_channel_mode(channel) == "hardware":
            apps = []
        self.controller.toggle(channel, apps)

    def close(self) -> None:
        self._midi.media_toggle_requested.disconnect(self._toggle)
        self._midi.connection_changed.disconnect(self._connection_changed)
        self._midi.remote_sync_session_changed.disconnect(self._session_changed)
        self._config.settings_changed.disconnect(self.configure)
        self._config.mapping_changed.disconnect(self.controller.cancel)
        self.controller.cancel()
