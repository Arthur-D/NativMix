import pytest
from serial import SerialException

from nativmix.hardware.arduino import ArduinoThread


def test_transient_larger_channel_count_is_discarded_without_index_error():
    thread = ArduinoThread(num_channels=2)
    emitted: list[list[float]] = []
    thread.volumes_changed.connect(emitted.append)

    thread._process_line("1|2|3")
    thread._process_line("4|5|6")

    assert thread._num_channels == 2
    assert len(thread._channels) == 2
    assert emitted == []


def test_channel_count_adapts_after_three_stable_clean_frames():
    thread = ArduinoThread(num_channels=2)
    counts: list[int] = []
    thread.channel_count_changed.connect(counts.append)

    thread._process_line("1|2|3")
    thread._process_line("4|5|6")
    thread._process_line("7|8|9")

    assert thread._num_channels == 3
    assert len(thread._channels) == 3
    assert counts == [3]


def test_malformed_channel_count_candidate_does_not_reset_channels():
    thread = ArduinoThread(num_channels=2)

    thread._process_line("1|2|oops")
    thread._process_line("3|4")

    assert thread._num_channels == 2
    assert len(thread._channels) == 2


def test_prepare_for_sleep_closes_serial_and_gates_reconnect():
    thread = ArduinoThread(num_channels=2)
    serial_handle = type("_Serial", (), {"closed": False, "close": lambda self: setattr(self, "closed", True)})()
    thread._active_ser = serial_handle

    thread.prepare_for_sleep()

    assert serial_handle.closed is True
    assert thread._system_sleeping is True
    thread.resume_from_sleep()
    assert thread._system_sleeping is False


def test_read_close_race_is_normalized_during_sleep():
    thread = ArduinoThread(num_channels=2)
    thread._system_sleeping = True
    serial_handle = type("_Serial", (), {"readline": lambda self: (_ for _ in ()).throw(TypeError("fd is None"))})()

    with pytest.raises(SerialException):
        thread._read_line(serial_handle)


def test_read_type_error_remains_visible_when_running():
    thread = ArduinoThread(num_channels=2)
    thread._running = True
    serial_handle = type("_Serial", (), {"readline": lambda self: (_ for _ in ()).throw(TypeError("bad state"))})()

    with pytest.raises(TypeError):
        thread._read_line(serial_handle)
