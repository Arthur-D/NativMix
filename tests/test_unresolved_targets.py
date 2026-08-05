"""
Tests for unresolved-target handling introduced by the Flatpak visibility fix.

Covers:
- Bindings are NOT cleared when a target is unresolved.
- _apply_volume_by_name marks an app as unresolved when no sink-input matches.
- _apply_volume_by_name removes the unresolved mark when a match is found.
- Stale PA sink-input writes are skipped when unresolved (no volume_set_all_chans call).
- Throttled warning is emitted (not spammed) on unresolved.
- get_unresolved_targets() returns the current set.
"""
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

try:
    import pulsectl  # noqa: F401
    _PULSECTL_OK = True
except Exception:
    _PULSECTL_OK = False

_SKIP_NO_PULSECTL = pytest.mark.skipif(
    not _PULSECTL_OK, reason="pulsectl / libpulse not available"
)


def _make_si(index: int, props: dict | None = None) -> MagicMock:
    si = MagicMock()
    si.index = index
    si.proplist = props if props is not None else {}
    return si


def _make_manager(tmp_path: Path):
    """Return a PipeWireManager fully initialized for unresolved-target tests."""
    from nativmix.audio.manager import PipeWireManager
    from nativmix.utils.config_manager import ConfigManager

    cfg = ConfigManager(
        config_path=tmp_path / "config.json",
        profiles_dir=tmp_path / "profiles",
    )
    mgr = PipeWireManager.__new__(PipeWireManager)
    mgr._config = cfg
    mgr._state_lock = threading.RLock()
    mgr._poti_volumes = {}
    mgr._channel_muted = {}
    mgr._vsink_creating = set()
    mgr._pw_nodes = {}
    mgr._pw_nodes_lock = threading.Lock()
    mgr._stable_ids = {}
    mgr._unresolved_targets = set()
    mgr._unresolved_lock = threading.Lock()
    mgr.can_set_volume_pw = False
    mgr.can_set_volume = True
    mgr._flatpak_hard_guard = False  # default: not Flatpak
    # Provide a no-op emit so we don't need a real Qt signal in unit tests.
    mgr.unresolved_targets_changed = MagicMock()
    return mgr


def _make_pulse_mock(sink_inputs):
    pulse = MagicMock()
    pulse.sink_input_list.return_value = sink_inputs
    return pulse


# ---------------------------------------------------------------------------
# Unresolved target — persistence and state tracking
# ---------------------------------------------------------------------------

@_SKIP_NO_PULSECTL
class TestUnresolvedTargets:
    """Verify unresolved-target behaviour does not affect saved bindings."""

    def test_unresolved_target_does_not_clear_config_binding(self, tmp_path):
        """
        When a target app is not found in the current audio graph, the saved
        binding in config must NOT be removed.
        """
        from nativmix.utils.config_manager import ConfigManager

        cfg = ConfigManager(
            config_path=tmp_path / "config.json",
            profiles_dir=tmp_path / "profiles",
        )
        cfg.ensure_channels(1)
        cfg.set_app_names(0, ["Spotify"])
        cfg.save()

        mgr = _make_manager(tmp_path)
        mgr._config = cfg

        # Pulse returns no matching sink-inputs (app not visible in sandbox).
        pulse = _make_pulse_mock([])

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Unknown"):
            mgr._apply_volume_by_name("Spotify", 0.8, pulse=pulse)

        # Binding must still be in config.
        assert "Spotify" in cfg.get_app_names(0), (
            "Binding must not be cleared when target is unresolved"
        )

    def test_unresolved_target_added_to_unresolved_set(self, tmp_path):
        """Target is added to _unresolved_targets when no sink-input matches."""
        mgr = _make_manager(tmp_path)
        pulse = _make_pulse_mock([])

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Unknown"):
            mgr._apply_volume_by_name("Spotify", 0.5, pulse=pulse)

        assert "Spotify" in mgr._unresolved_targets

    def test_resolved_target_removed_from_unresolved_set(self, tmp_path):
        """When a previously unresolved target is found, it is removed from the set."""
        mgr = _make_manager(tmp_path)
        # Pre-mark as unresolved.
        mgr._unresolved_targets.add("Spotify")

        si = _make_si(501, {"application.name": "Spotify", "application.process.id": "0"})
        pulse = _make_pulse_mock([si])

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Spotify"):
            mgr._apply_volume_by_name("Spotify", 0.8, pulse=pulse)

        assert "Spotify" not in mgr._unresolved_targets

    def test_unresolved_signal_emitted_on_state_change(self, tmp_path):
        """unresolved_targets_changed is emitted when the set transitions."""
        mgr = _make_manager(tmp_path)
        pulse = _make_pulse_mock([])

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Unknown"):
            mgr._apply_volume_by_name("Spotify", 0.5, pulse=pulse)

        mgr.unresolved_targets_changed.emit.assert_called()
        emitted_set = mgr.unresolved_targets_changed.emit.call_args[0][0]
        assert "Spotify" in emitted_set

    def test_unresolved_no_pa_volume_write_when_no_match(self, tmp_path):
        """
        When no sink-input matches, volume_set_all_chans must NOT be called —
        there is nothing to write to.
        """
        mgr = _make_manager(tmp_path)

        # A sink-input exists but belongs to a different app.
        other_si = _make_si(999, {"application.name": "Firefox", "application.process.id": "0"})
        pulse = _make_pulse_mock([other_si])

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Firefox"):
            mgr._apply_volume_by_name("Spotify", 0.8, pulse=pulse)

        pulse.volume_set_all_chans.assert_not_called()

    def test_get_unresolved_targets_returns_snapshot(self, tmp_path):
        """get_unresolved_targets() returns an independent copy of the set."""
        mgr = _make_manager(tmp_path)
        mgr._unresolved_targets = {"Spotify", "Discord"}

        result = mgr.get_unresolved_targets()

        assert result == {"Spotify", "Discord"}
        # Mutations to the returned set must not affect internal state.
        result.add("Zoom")
        assert "Zoom" not in mgr._unresolved_targets

    def test_system_master_not_tracked_as_unresolved(self, tmp_path):
        """System Master uses a category-match path and must never be marked unresolved."""
        mgr = _make_manager(tmp_path)
        mgr.can_set_volume_pw = False
        pulse = _make_pulse_mock([])

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Unknown"):
            # System Master falls through to PA default-sink path; no match is fine.
            try:
                mgr._apply_volume_by_name("System Master", 0.8, pulse=pulse)
            except Exception:
                pass  # PA errors expected without a real session

        assert "System Master" not in mgr._unresolved_targets

    def test_unresolved_warning_throttled(self, tmp_path, caplog):
        """Only the first unresolved warning per key is emitted within the interval."""
        import logging
        from nativmix.audio import pipewire_native

        mgr = _make_manager(tmp_path)
        pulse = _make_pulse_mock([])

        # Force throttle interval to 0 for second call to ensure first is logged.
        original_last = {}
        pipewire_native._throttled_warner._last = original_last

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Unknown"):
            with caplog.at_level(logging.WARNING, logger="nativmix.audio.manager"):
                mgr._apply_volume_by_name("Spotify", 0.5, pulse=pulse)
                # Second immediate call: throttle should suppress the warning.
                mgr._apply_volume_by_name("Spotify", 0.5, pulse=pulse)

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        # At most one warning for the same key within the interval.
        unresolved_warnings = [m for m in warning_msgs if "Spotify" in m and "not found" in m]
        assert len(unresolved_warnings) <= 1, (
            f"Expected throttled (≤1) unresolved warning, got {len(unresolved_warnings)}: "
            f"{unresolved_warnings}"
        )


# ---------------------------------------------------------------------------
# Flatpak hard guard regression test
# ---------------------------------------------------------------------------

@_SKIP_NO_PULSECTL
class TestFlatpakHardGuard:
    """
    Regression test: when _flatpak_hard_guard is True and an app name cannot
    be resolved to a live PW node, NO PA command (volume_set_all_chans) must
    be executed — the binding is kept but the write is suppressed entirely.
    """

    def test_flatpak_unresolved_no_pa_write_attempted(self, tmp_path):
        """
        Flatpak mode + unresolved app name → volume_set_all_chans never called.

        A sink-input that matches by application.name but has no associated
        PW node (pw_node is None) must be skipped when the Flatpak hard guard
        is active.  The binding must remain intact and no PA write must occur.
        """
        mgr = _make_manager(tmp_path)
        mgr._flatpak_hard_guard = True  # simulate Flatpak environment

        # A sink-input matches by application.name but there is no PW node
        # for it (pw_nodes snapshot is empty → pw_node will be None).
        si = _make_si(42, {"application.name": "Spotify", "application.process.id": "0"})
        pulse = _make_pulse_mock([si])

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Spotify"):
            mgr._apply_volume_by_name("Spotify", 0.7, pulse=pulse)

        # No PA write must have been attempted.
        pulse.volume_set_all_chans.assert_not_called()

    def test_flatpak_unresolved_binding_preserved(self, tmp_path):
        """
        When the hard guard suppresses a write, the saved config binding
        must not be cleared.
        """
        from nativmix.utils.config_manager import ConfigManager

        cfg = ConfigManager(
            config_path=tmp_path / "config.json",
            profiles_dir=tmp_path / "profiles",
        )
        cfg.ensure_channels(1)
        cfg.set_app_names(0, ["Spotify"])
        cfg.save()

        mgr = _make_manager(tmp_path)
        mgr._config = cfg
        mgr._flatpak_hard_guard = True

        pulse = _make_pulse_mock([])

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Unknown"):
            mgr._apply_volume_by_name("Spotify", 0.7, pulse=pulse)

        assert "Spotify" in cfg.get_app_names(0), (
            "Binding must not be cleared when Flatpak hard guard suppresses write"
        )

    def test_flatpak_guard_off_allows_pa_write(self, tmp_path):
        """
        With _flatpak_hard_guard=False (host mode), a matched sink-input with
        no PW node still receives the PA volume write.
        """
        mgr = _make_manager(tmp_path)
        mgr._flatpak_hard_guard = False  # host mode

        si = _make_si(99, {"application.name": "Spotify", "application.process.id": "0"})
        pulse = _make_pulse_mock([si])

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Spotify"):
            mgr._apply_volume_by_name("Spotify", 0.5, pulse=pulse)

        pulse.volume_set_all_chans.assert_called_once_with(si, 0.5)

    def test_flatpak_unresolved_sandbox_warning_emitted(self, tmp_path, caplog):
        """
        When the hard guard skips a write, a rate-limited 'unresolved in sandbox'
        WARNING must be logged.
        """
        import logging
        from nativmix.audio import pipewire_native

        mgr = _make_manager(tmp_path)
        mgr._flatpak_hard_guard = True
        # Clear throttle state so the warning fires immediately.
        pipewire_native._throttled_warner._last = {}

        si = _make_si(77, {"application.name": "Discord", "application.process.id": "0"})
        pulse = _make_pulse_mock([si])

        with patch("nativmix.audio.manager.resolve_app_name", return_value="Discord"):
            with caplog.at_level(logging.WARNING, logger="nativmix.audio.manager"):
                mgr._apply_volume_by_name("Discord", 0.4, pulse=pulse)

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("unresolved in sandbox" in m for m in warning_msgs), (
            f"Expected 'unresolved in sandbox' warning, got: {warning_msgs}"
        )

