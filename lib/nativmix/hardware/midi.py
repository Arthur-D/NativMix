"""
MIDI hardware backend for NativMix.

Handles MIDI input devices (via mido/rtmidi) and maps Control Change (CC)
messages to volume levels. Supports a "Learn" mode for interactive setup.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import mido
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

class MidiThread(QThread):
    """
    Background thread that listens for MIDI CC messages from a specific device.

    Signals
    -------
    midi_volumes_changed(list[tuple[int, float]])
        Emitted when mapped MIDI CC values change.
        List of (channel_index, volume_0_to_1).
    midi_cc_received(int, int)
        Emitted for the "Learn" handshake: (control_number, value).
    connection_changed(bool)
        Emitted when the device is opened (True) or closed/missing (False).
    """

    midi_volumes_changed = pyqtSignal(list)  # list[tuple[int, float]]
    midi_cc_received = pyqtSignal(int, int)
    connection_changed = pyqtSignal(bool)

    def __init__(self, device_name: str = "", parent=None) -> None:
        super().__init__(parent)
        self.daemon = True
        self._device_name: str = device_name
        self._running: bool = False
        self._cc_map: dict[int, int] = {}  # cc_number -> channel_index
        self._last_values: dict[int, int] = {} # cc_number -> last_seen_value (0-127)

    def set_device(self, name: str) -> None:
        """Update the target MIDI device. Reconnects on the next loop cycle."""
        if self._device_name != name:
            logger.info("MIDI Port change requested: %s", name)
            self._device_name = name

    def update_mappings(self, mappings: dict[int, int]) -> None:
        """
        Update the CC -> Channel mappings.
        Args:
            mappings: dict where key is CC number, value is channel index.
        """
        self._cc_map = mappings
        logger.debug("MIDI CC mappings updated: %s", self._cc_map)

    def get_mapped_volumes(self) -> list[tuple[int, float]]:
        """Return a list of (channel_index, volume) for all current mappings."""
        results = []
        for cc, ch_idx in self._cc_map.items():
            if cc in self._last_values:
                val = self._last_values[cc]
                results.append((ch_idx, val / 127.0))
        return results

    def stop(self) -> None:
        """Signal the thread to exit and wait for it to finish."""
        self._running = False
        self.wait()

    def run(self) -> None:
        """Main loop: open port -> listen -> reconnect on error."""
        self._running = True
        logger.info("MidiThread started")

        while self._running:
            if not self._device_name:
                self.connection_changed.emit(False)
                self._sleep_checked(1.0)
                continue

            try:
                # Use a local copy of the device name for this connection session
                target_name = self._device_name
                
                with mido.open_input(target_name) as inport:
                    logger.info("MIDI connected: %s", target_name)
                    self.connection_changed.emit(True)
                    
                    # Process messages as they arrive (non-blocking)
                    while self._running:
                        # Check if device name was changed in settings
                        if self._device_name != target_name:
                            break
                            
                        msg = inport.poll()
                        if msg is None:
                            time.sleep(0.01)
                            continue
                        
                        if msg.type == 'control_change':
                            cc = msg.control
                            val = msg.value
                            self._last_values[cc] = val
                            
                            # 1. Always emit for Learn handshake
                            self.midi_cc_received.emit(cc, val)
                            
                            # 2. Check if mapped to a fader
                            if cc in self._cc_map:
                                ch_idx = self._cc_map[cc]
                                # Convert 0-127 to 0.0-1.0
                                vol = val / 127.0
                                self.midi_volumes_changed.emit([(ch_idx, vol)])
                                
            except (IOError, EOFError, RuntimeError) as exc:
                logger.warning("MIDI Error on %s: %s", self._device_name, exc)
                self.connection_changed.emit(False)
                self._sleep_checked(2.0)
            except Exception as exc:
                logger.exception("Unexpected MIDI error")
                self._sleep_checked(5.0)

        logger.info("MidiThread stopped")

    def _sleep_checked(self, seconds: float) -> None:
        """Sleep while checking for thread stop request."""
        end_time = time.time() + seconds
        while self._running and time.time() < end_time:
            time.sleep(0.1)
