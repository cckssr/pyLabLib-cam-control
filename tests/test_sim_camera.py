"""The `simulated` camera backend (camcontrol.cameras.sim)."""

import time

import numpy as np
import pytest

from camcontrol.cameras.sim import PATTERNS, SimulatedCamera


@pytest.fixture
def cam():
    cam = SimulatedCamera(size=(320, 200))
    yield cam
    cam.close()


@pytest.mark.parametrize("pattern", list(PATTERNS))
def test_non_square_frames_match_reported_dimensions(cam, pattern):
    cam.set_pattern(pattern)
    assert cam.get_data_dimensions() == (200, 320)
    frame = cam.render_frame(3)
    assert frame.shape == (200, 320)
    assert frame.dtype == np.dtype("<u2")


def test_roi_and_binning(cam):
    cam.set_roi(10, 110, 20, 81, hbin=2, vbin=4)
    hstart, hend, vstart, vend, hbin, vbin = cam.get_roi()
    assert (hbin, vbin) == (2, 4)
    assert (vend - vstart) % vbin == 0
    assert cam.render_frame(0).shape == cam.get_data_dimensions() == (15, 50)


def test_signal_scales_with_exposure_and_binning(cam):
    cam.set_pattern("flat")
    cam.set_noise(False)
    cam.set_exposure(5e-3)
    dim = cam.render_frame(0).mean()
    cam.set_exposure(10e-3)
    bright = cam.render_frame(0).mean()
    offset = (2**12 - 1) // 40
    assert bright - offset == pytest.approx(2 * (dim - offset), rel=0.01)
    cam.set_roi(hbin=2, vbin=2)
    assert cam.render_frame(0).mean() - offset == pytest.approx(4 * (bright - offset), rel=0.01)


def test_saturates_at_bit_depth():
    cam = SimulatedCamera(size=(64, 64), bit_depth=8)
    cam.set_exposure(1.0)
    assert cam.render_frame(0).max() == 255


def test_frames_are_deterministic_per_index(cam):
    assert np.array_equal(cam.render_frame(5), cam.render_frame(5))
    assert not np.array_equal(cam.render_frame(5), cam.render_frame(6))


def test_frame_period_and_trigger_mode(cam):
    cam.set_exposure(1e-3)
    assert cam.get_frame_period() == pytest.approx(1e-3 + 200 * 2e-6)
    cam.set_frame_period(0.05)
    assert cam.get_frame_period() == pytest.approx(0.05)
    cam.set_frame_period(0)
    cam.set_trigger_mode("ext")
    assert cam.get_frame_period() == pytest.approx(0.1)  # 10 Hz simulated trigger


def test_acquisition_with_frame_info(cam):
    cam.set_exposure(1e-3)
    cam.set_frame_info_format("namedtuple")
    cam.start_acquisition(nframes=50)
    time.sleep(0.05)
    cam.wait_for_frame(timeout=2)
    frames, infos = cam.read_multiple_images(return_info=True)
    cam.stop_acquisition()
    assert frames and len(frames) == len(infos)
    assert [i.frame_index for i in infos] == list(
        range(infos[0].frame_index, infos[0].frame_index + len(infos))
    )
