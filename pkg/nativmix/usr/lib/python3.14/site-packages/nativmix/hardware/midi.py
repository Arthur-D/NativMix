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

    def __init__(self, device_name: str = "", input_mode: str = "hybrid", parent=None) -> None:
        super().__init__(parent)
        self.daemon = True
        self._device_name: str = device_name
        self._input_mode: str = input_mode  # "usb", "hybrid", "midi_only"
        self._running: bool = False
        self._panic_flag: bool = False
        self._cc_map: dict[int, int] = {}  # cc_number -> channel_index
        self._last_values: dict[int, int] = {} # cc_number -> last_seen_value (0-127)

    def set_device(self, name: str) -> None:
        """Update the target MIDI device. Reconnects on the next loop cycle."""
        if self._device_name != name:
            logger.info("MIDI Port change requested: %s", name)
            self._device_name = name
            self._panic_flag = True

    def set_mode(self, mode: str) -> None:
        """Update the input mode (to know if MIDI is allowed)."""
        if self._input_mode != mode:
            logger.debug("MIDI Mode changed: %s -> %s", self._input_mode, mode)
            self._input_mode = mode
            self._panic_flag = True

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
        """Gracefully stop the thread loop."""
        self._running = False
        # Give the loop one more slice to check _running
        self.wait(2000)
        # Only terminate if it's really stuck (finally blocks might not run!)
        if self.isRunning():
            logger.warning("MidiThread: Force-terminating (graceful stop took too long)")
            self.terminate()
            self.wait()

    def trigger_panic(self) -> None:
        """Force-restart the MIDI subsystem to clear zombie ports."""
        logger.debug("MIDI PANIC TRIGGERED: Resetting MIDI subsystem...")
        self._panic_flag = True

    def run(self) -> None:
        """Main loop: open port -> listen -> reconnect on error."""
        self._running = True
        self._panic_flag = False
        logger.info("MidiThread started. (Mode: %s, Device: %s)", self._input_mode, self._device_name)

        try:
            import rtmidi
        except ImportError:
            logger.error("CRITICAL: rtmidi not found! MIDI will not work.")
            return

        while self._running:
            if self._panic_flag:
                self._panic_flag = False
                logger.debug("MidiThread: Internally restarting due to flag.")

            # Is MIDI even enabled?
            if self._input_mode == "usb":
                logger.debug("MidiThread: Idle (USB Only mode)")
                self.connection_changed.emit(False)
                # Wait for setting changes
                while self._running and not self._panic_flag and self._input_mode == "usb":
                    time.sleep(0.5)
                continue

            # We use local references for resources to ensure they are cleaned up in each cycle
            virtual_client = None
            
            try:
                target_device = self._device_name if self._device_name else "VIRTUAL_PORT"
                
                if target_device == "VIRTUAL_PORT":
                    logger.info("MidiThread: Opening Virtual Port 'NativMix:Input'...")
                    
                    try:
                        virtual_client = rtmidi.MidiIn(rtmidi.API_LINUX_ALSA, name="NativMix")
                        virtual_client.open_virtual_port("Input")
                        self.connection_changed.emit(True)
                    except Exception as e:
                        logger.warning("MidiThread: Could not open virtual port: %s", e)
                        self.connection_changed.emit(False)
                        self._sleep_checked(5.0)
                        continue

                    while self._running and not self._panic_flag:
                        if self._input_mode == "usb" or (self._device_name != "" and self._device_name != "VIRTUAL_PORT"):
                            break
                        
                        msg_data = virtual_client.get_message()
                        if msg_data:
                            msg, _ = msg_data
                            if len(msg) >= 3 and (msg[0] & 0xF0) == 0xB0:
                                self._handle_cc(msg[1], msg[2])
                        
                        time.sleep(0.01)
                    
                    virtual_client.close_port()
                    virtual_client = None
                    logger.info("MidiThread: Virtual Port closed.")

                else:
                    # Physical Device Mode
                    logger.info("MidiThread: Connecting to physical device: %s", target_device)
                    names = mido.get_input_names()
                    target_name = None
                    for name in names:
                        if target_device in name:
                            target_name = name
                            break
                    
                    if not target_name:
                        self.connection_changed.emit(False)
                        self._sleep_checked(5.0)
                        continue
                        
                    with mido.open_input(target_name) as inport:
                        logger.info("MidiThread: Connected to %s", target_name)
                        self.connection_changed.emit(True)
                        while self._running and not self._panic_flag:
                            if self._input_mode == "usb" or self._device_name != target_device:
                                break
                            msg = inport.receive(timeout=0.1)
                            if msg and msg.type == 'control_change':
                                self._handle_cc(msg.control, msg.value)

            except (IOError, EOFError, RuntimeError) as exc:
                logger.warning("MIDI Error: %s", exc)
                self.connection_changed.emit(False)
                self._sleep_checked(2.0)
                logger.exception("Unexpected MIDI error")
                self._sleep_checked(5.0)
            finally:
                if virtual_client:
                    try:
                        virtual_client.close_port()
                    except:
                        pass
                    logger.info("Cleanup: Virtual MIDI Port closed.")

        logger.info("MidiThread stopped")

    def _handle_cc(self, cc: int, val: int) -> None:
        """Process a single MIDI Control Change message."""
        self._last_values[cc] = val
        
        # 1. Always emit for Learn handshake
        self.midi_cc_received.emit(cc, val)
        
        # 2. Check if mapped to a fader
        if cc in self._cc_map:
            ch_idx = self._cc_map[cc]
            # Convert 0-127 to 0.0-1.0
            vol = val / 127.0
            self.midi_volumes_changed.emit([(ch_idx, vol)])

    def _sleep_checked(self, seconds: float) -> None:
        """Sleep while checking for thread stop request."""
        end_time = time.time() + seconds
        while self._running and time.time() < end_time:
            time.sleep(0.1)
