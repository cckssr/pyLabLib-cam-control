"""ThorlabsTSIDirect's pylablib camera adapter over the fake device (no hardware)."""

import functools
import time

import numpy as np
import pytest

from camcontrol.cameras.ThorlabsTSIDirect import (
    ThorlabsTSICamera,
    ThorlabsTSIError,
    ThorlabsTSIFrameTransferError,
    _shifted_bayer_phase,
)
from camcontrol.thorlabs_tsi.camera import TsiCamera
from camcontrol.thorlabs_tsi.fake import FakeFaults, FakeTsiBus, FakeTsiDevice


def _open(dev):
    factory = functools.partial(
        TsiCamera.open_camera, find=FakeTsiBus([dev]).find, synchronous_stream=True
    )
    return ThorlabsTSICamera(camera_factory=factory)


@pytest.fixture
def cam_and_dev(request):
    dev = FakeTsiDevice(**getattr(request, "param", {}))
    cam = _open(dev)
    yield cam, dev
    cam.close()


def _acquire(cam, n, timeout=10.0):
    cam.start_acquisition(nframes=100)
    got = []
    deadline = time.monotonic() + timeout
    while len(got) < n and time.monotonic() < deadline:
        cam.wait_for_frame(timeout=1.0)
        got += cam.read_multiple_images()
    return got


@pytest.mark.parametrize("cam_and_dev", [{"family": "zelux"}, {"family": "cs"}], indirect=True)
def test_geometry_and_settings(cam_and_dev):
    cam, dev = cam_and_dev
    assert cam.get_detector_size() == dev.sensor
    hlim, vlim = cam.get_roi_limits()
    assert (hlim.min, vlim.min) == ((80, 4) if dev.family == "zelux" else (16, 2))
    cam.set_roi(10, 300, 20, 220)
    hstart, hend, vstart, vend, hbin, vbin = cam.get_roi()
    assert (hend - hstart) % hlim.sstep == 0
    assert cam._get_data_dimensions_rc() == (vend - vstart, hend - hstart)
    cam.set_exposure(5e-3)
    assert cam.get_exposure() == pytest.approx(5e-3)


def test_open_failure_is_a_device_error():
    with pytest.raises(ThorlabsTSIError, match="could not open"):
        ThorlabsTSICamera(
            camera_factory=functools.partial(TsiCamera.open_camera, find=FakeTsiBus([]).find)
        )


@pytest.mark.parametrize(
    "cam_and_dev", [{"family": "zelux"}, {"family": "cs", "color": True}], indirect=True
)
def test_acquire_stop_restart(cam_and_dev):
    cam, dev = cam_and_dev
    cam.set_roi(0, 256, 0, 128)
    cam.set_exposure(2e-3)
    frames = _acquire(cam, 5)
    assert len(frames) >= 5
    assert frames[0].shape == (128, 256)
    counts = [FakeTsiDevice.frame_count_from_image(f) for f in frames]
    assert counts == sorted(counts)
    cam.stop_acquisition()
    assert not cam.acquisition_in_progress()
    assert dev.state == "stopped"
    assert len(_acquire(cam, 3)) >= 3  # restartable
    cam.stop_acquisition()


def test_stream_faults_are_survived(cam_and_dev):
    cam, dev = cam_and_dev
    dev.faults = FakeFaults(drop_chunk_every=3, junk_every=4, trailer_bytes=300, stall_every=7)
    cam.set_roi(0, 512, 0, 256)  # several chunks per frame, so there is one to drop
    cam.set_exposure(1e-3)
    frames = _acquire(cam, 10)
    assert len(frames) >= 10
    stats = cam._get_stream_stats()
    assert stats["frames_truncated"] > 0 and stats["read_errors"] > 0
    cam.stop_acquisition()


def test_unplug_surfaces_as_error(cam_and_dev):
    cam, dev = cam_and_dev
    cam.set_roi(0, 256, 0, 64)
    cam.set_exposure(1e-3)
    assert _acquire(cam, 2)
    dev.unplug()
    with pytest.raises(ThorlabsTSIError):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            cam.wait_for_frame(timeout=0.5)
            cam.read_multiple_images()
    with pytest.raises(ThorlabsTSIError):
        cam.stop_acquisition()  # can't send stop to an unplugged camera...
    cam.close()  # ...but closing still releases it
    assert not cam.is_opened()


def test_overwritten_frames_are_not_returned(cam_and_dev):
    cam, dev = cam_and_dev
    cam.set_roi(0, 256, 0, 64)
    cam.set_exposure(1e-3)
    _acquire(cam, 1)
    with cam._buffer_lock:
        newest = cam._frame_notifier.counter
        oldest = newest - len(cam._frames)
        if oldest > 0:
            with pytest.raises(ThorlabsTSIFrameTransferError):
                cam._read_frames((oldest - 1, newest))
        frames, *_ = cam._read_frames((newest - 1, newest))
    assert isinstance(frames[0], np.ndarray)
    cam.stop_acquisition()


@pytest.mark.parametrize(
    ("cam_and_dev", "requested", "applied"),
    [
        ({"family": "zelux"}, 12.34, 12.3),
        ({"family": "cs"}, 4.0, 5.3),
        ({"family": "cs"}, -7.0, -7.5),
    ],
    indirect=["cam_and_dev"],
)
def test_gain_in_db(cam_and_dev, requested, applied):
    cam, dev = cam_and_dev
    assert cam.set_gain_db(requested) == pytest.approx(applied)
    lo, hi = cam.get_gain_range()
    assert lo <= applied <= hi


def test_frame_period_uses_frame_rate_control(cam_and_dev):
    cam, dev = cam_and_dev
    cam.set_roi(0, 256, 0, 64)
    cam.set_exposure(1e-3)
    natural = cam.get_frame_period()
    assert cam.set_frame_period(0.2) == pytest.approx(0.2)
    assert dev.sfr_on
    assert cam.set_frame_period(0) == pytest.approx(natural)
    assert not dev.sfr_on


@pytest.mark.parametrize("cam_and_dev", [{"family": "zelux"}, {"family": "cs"}], indirect=True)
def test_black_level_only_where_supported(cam_and_dev):
    cam, dev = cam_and_dev
    settings = cam.get_settings()
    assert ("black_level" in settings) == (dev.family == "zelux")
    if dev.family == "zelux":
        assert cam.set_black_level(30) == 30


@pytest.mark.parametrize("cam_and_dev", [{"family": "cs"}], indirect=True)
def test_hardware_binning(cam_and_dev):
    cam, dev = cam_and_dev
    hlim, vlim = cam.get_roi_limits()
    assert hlim.maxbin == vlim.maxbin == 16
    cam.set_roi(0, 1280, 0, 1024, hbin=3, vbin=5)
    hstart, hend, vstart, vend, hbin, vbin = cam.get_roi()
    assert (hbin, vbin) == (3, 5) == (dev.hbin, dev.vbin)
    rows, cols = cam.get_data_dimensions()
    assert cols % 4 == 0  # decodable
    assert (rows, cols) == (dev.image_size[1], dev.image_size[0])
    cam.set_exposure(1e-3)
    frames = _acquire(cam, 2)
    assert frames[0].shape == (rows, cols)
    cam.stop_acquisition()


def test_zelux_has_no_binning(cam_and_dev):
    cam, dev = cam_and_dev
    assert cam.get_roi_limits()[0].maxbin == 1
    cam.set_roi(0, 256, 0, 64, hbin=2, vbin=2)
    assert cam.get_roi()[4:] == (1, 1)


def test_frame_info(cam_and_dev):
    cam, dev = cam_and_dev
    cam.set_roi(0, 256, 0, 64)
    cam.set_exposure(1e-3)
    cam.set_frame_info_format("namedtuple")
    cam.start_acquisition(nframes=100)
    cam.wait_for_frame(nframes=3, timeout=5)
    frames, infos = cam.read_multiple_images(return_info=True)
    cam.stop_acquisition()
    for frame, info in zip(frames, infos, strict=True):
        assert info.framestamp == FakeTsiDevice.frame_count_from_image(frame)
        assert info.timestamp_us >= 0


@pytest.mark.parametrize("cam_and_dev", [{"family": "cs", "color": True}], indirect=True)
def test_debayered_rgb_frames(cam_and_dev):
    cam, dev = cam_and_dev
    assert cam.get_sensor_info().bayer_pattern == "RGGB"
    cam.set_roi(0, 512, 0, 256)
    cam.set_exposure(5e-3)
    cam.set_color_mode("rgb")
    frames = _acquire(cam, 2)
    cam.stop_acquisition()
    assert frames[0].shape == (256, 512, 3)
    assert cam.get_data_dimensions() == (256, 512)
    cam.set_color_mode("raw")
    assert _acquire(cam, 1)[0].shape == (256, 512)
    cam.stop_acquisition()


def test_color_mode_only_on_color_cameras(cam_and_dev):
    cam, _ = cam_and_dev
    assert "color_mode" not in cam.get_settings()


def test_bayer_phase_follows_roi_origin():
    assert _shifted_bayer_phase("red", 0, 0) == "red"
    assert _shifted_bayer_phase("red", 1, 0) == "green_left_of_red"
    # RGGB one row down reads GBRG; one row and one column down, BGGR
    assert _shifted_bayer_phase("red", 0, 1) == "green_left_of_blue"
    assert _shifted_bayer_phase("red", 3, 5) == "blue"
