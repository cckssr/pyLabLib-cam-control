"""Thorlabs TSI command channel (transport + Commands) against the fake device."""

import pytest
import usb.util

from camcontrol.thorlabs_tsi.camera import TsiCamera, discover_available_cameras
from camcontrol.thorlabs_tsi.commands import Commands
from camcontrol.thorlabs_tsi.fake import FakeFaults, FakeTsiBus, FakeTsiDevice
from camcontrol.thorlabs_tsi.protocol import CommandError
from camcontrol.thorlabs_tsi.transport import TsiCameraTransport


def _commands(dev):
    usb.util.claim_interface(dev, 0)
    return Commands(TsiCameraTransport(dev))


def test_identity_and_limits():
    cmd = _commands(FakeTsiDevice(family="cs", serial="12345"))
    assert cmd.get_serial_number() == "12345"
    assert cmd.get_model() == "CS135MU"
    assert cmd.get_roi_range() == ((0, 0, 15, 1), (1264, 1022, 1279, 1023))
    assert cmd.get_gain_table() == [-75, -56, 0, 53]
    cmd.set_gain(3)
    assert cmd.get_gain_db() == pytest.approx(5.3)
    assert "hbin" in cmd.supported()


def test_zelux_has_no_binning_or_gain_table():
    cmd = _commands(FakeTsiDevice(family="zelux"))
    assert cmd.get_binx() == 1
    assert cmd.get_gain_table() is None
    cmd.set_gain(120)
    assert cmd.get_gain_db() == pytest.approx(12.0)
    assert cmd.get_calibration_lines() == 6


def test_roi_snaps_and_is_refused_while_running():
    cmd = _commands(FakeTsiDevice(family="cs"))
    assert cmd.set_roi(3, 5, 100, 50) == (3, 5, 98, 50)  # width quantised to 96
    cmd.arm()
    with pytest.raises(CommandError, match="Camera Running"):
        cmd.set_roi(0, 0, 63, 63)
    cmd.stop()
    cmd.set_roi(0, 0, 63, 63)


def test_frame_rate_control():
    cmd = _commands(FakeTsiDevice(family="zelux"))
    with pytest.raises(CommandError, match="Command not enabled"):
        cmd.set_frame_period_us(200_000)
    cmd.set_frame_rate_control(True)
    assert cmd.set_frame_period_us(200_000) == 200_000
    assert cmd.get_frame_rate_control() == (True, 200_000)


def test_injected_command_error():
    dev = FakeTsiDevice(faults=FakeFaults(command_errors={"gain": "Command not enabled"}))
    cmd = _commands(dev)
    with pytest.raises(CommandError, match="Command not enabled"):
        cmd.get_gain()


def test_late_reply_does_not_answer_the_next_command():
    dev = FakeTsiDevice(serial="777", faults=FakeFaults(late_replies=frozenset({"model"})))
    cmd = _commands(dev)
    with pytest.raises(Exception, match="timed out"):
        cmd.get_model()
    # the late "model" reply is now queued; it must be discarded, not returned for sernum
    assert cmd.get_serial_number() == "777"


def test_discovery_skips_busy_and_refusing_devices():
    ok = FakeTsiDevice(serial="1")
    busy = FakeTsiDevice(serial="2")
    usb.util.claim_interface(busy, 0)
    refusing = FakeTsiDevice(
        serial="3", faults=FakeFaults(command_errors={"sernum": "Invalid command"})
    )
    bus = FakeTsiBus([ok, busy, refusing])
    assert discover_available_cameras(find=bus.find) == [(ok.idProduct, "1", "CS165MU")]
    assert not ok.claimed and not refusing.claimed


def test_open_by_serial_releases_other_cameras():
    a, b = FakeTsiDevice(serial="A"), FakeTsiDevice(family="cs", serial="B")
    cam = TsiCamera.open_camera(serial="B", find=FakeTsiBus([a, b]).find, synchronous_stream=True)
    assert cam.model == "CS135MU"
    assert b.claimed and not a.claimed
    cam.close()
    assert not b.claimed
