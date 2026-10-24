"""ThorlabsTSIDirect's pylablib camera adapter over the fake device (no hardware)."""

import functools
import time

import numpy as np
import pytest

from camcontrol.cameras.ThorlabsTSIDirect import (
    ThorlabsTSICamera,
    ThorlabsTSIError,
    ThorlabsTSIFrameTransferError,
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
    hstart, hend, vstart, vend = cam.get_roi()
    assert (hend - hstart) % hlim.sstep == 0
    assert cam._get_data_dimensions_rc() == (vend - vstart, hend - hstart)
    cam.set_exposure(5e-3)
    assert cam.get_exposure() == pytest.approx(5e-3)
    cam.set_gain(2)
    assert cam.get_gain() == 2


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
        frames, _ = cam._read_frames((newest - 1, newest))
    assert isinstance(frames[0], np.ndarray)
    cam.stop_acquisition()
