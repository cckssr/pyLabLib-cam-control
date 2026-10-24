"""Simulated camera: a hardware-free camera with a simple but physically shaped image model.

Frames are rendered on demand from the acquisition clock: a test scene (moving spot,
gradient, scrolling checkerboard or flat field) is turned into photo-electrons by the
exposure, gets shot and read noise, and is digitized with a gain, an offset and the chosen
bit depth (saturating at full scale). Binning sums charge like a real sensor. Rendering is
deterministic per frame index, so re-reading a frame returns identical data.
"""

import collections
import time

import numpy as np
from pylablib.devices.interface import camera
from pylablib.thread.devices.generic.camera import GenericCameraThread

from ..gui import cam_gui_parameters
from ..gui.base_cam_ctl_gui import GenericCameraSettings_GUI, GenericCameraStatus_GUI
from .base import ICameraDescriptor

TDeviceInfo = collections.namedtuple("TDeviceInfo", ["kind"])
TFrameInfo = collections.namedtuple("TFrameInfo", ["frame_index", "timestamp_us"])

PATTERNS = {
    "gaussian": "Moving spot",
    "gradient": "Gradient",
    "checkerboard": "Moving checkerboard",
    "flat": "Flat field",
}


class SimulatedCamera(camera.IBinROICamera, camera.IExposureCamera):
    """
    Simulated camera.

    Args:
        size: full "sensor" size ``(width, height)``
        pattern: test scene; one of :data:`PATTERNS`
        bit_depth: digitizer bit depth (pixel values saturate at ``2**bit_depth-1``)
        ext_trigger_rate: rate (Hz) of the simulated external trigger in ``"ext"`` trigger mode
        seed: noise seed
    """

    _TFrameInfo = TFrameInfo
    _frameinfo_fields = TFrameInfo._fields
    _min_exposure = 1e-5
    _max_bin = 16
    _full_well = 20000  # electrons
    _peak_flux = 4e5  # electrons per second at the brightest point of a scene
    _read_noise = 5.0  # electrons RMS
    _row_time = 2e-6  # readout time per (binned) row, s
    _noise_bank_size = 16

    def __init__(
        self, size=(1024, 1024), pattern="gaussian", bit_depth=12, ext_trigger_rate=10.0, seed=0
    ):
        super().__init__()
        self._size = tuple(int(s) for s in size)
        self._roi = (0, self._size[0], 0, self._size[1], 1, 1)
        self._exposure = 0.01
        self._frame_period = 0.0  # requested; 0 means "as fast as possible"
        self._gain_db = 0.0
        self._pattern = "gaussian"
        self.set_pattern(pattern)
        self._noise = True
        self._trigger_mode = "int"
        self._bit_depth = int(bit_depth)
        self._ext_trigger_rate = float(ext_trigger_rate)
        self._seed = seed
        self._opened = False
        self._acquisition_started = None
        self._cache = {}
        self.open()
        self._add_info_variable("device_info", self.get_device_info)
        self._add_settings_variable("frame_period", self.get_frame_period, self.set_frame_period)
        self._add_settings_variable("gain", self.get_gain, self.set_gain)
        self._add_settings_variable("pattern", self.get_pattern, self.set_pattern)
        self._add_settings_variable("noise", self.get_noise, self.set_noise)
        self._add_settings_variable("trigger_mode", self.get_trigger_mode, self.set_trigger_mode)
        self._add_info_variable("bit_depth", lambda: self._bit_depth)

    def open(self):
        self._opened = True

    def close(self):
        if self._opened:
            self.clear_acquisition()
        self._opened = False

    def is_opened(self):
        return self._opened

    def _get_connection_parameters(self):
        return (self._size,)

    def get_device_info(self):
        return TDeviceInfo("simulated")

    ### Timing ###
    def _min_frame_period(self):
        rows = (self._roi[3] - self._roi[2]) // self._roi[5]
        return self._exposure + rows * self._row_time

    def get_frame_timings(self):
        period = max(self._frame_period, self._min_frame_period())
        if self._trigger_mode == "ext":
            period = max(period, 1 / self._ext_trigger_rate)
        return self._TAcqTimings(self._exposure, period)

    @camera.acqstopped
    def set_exposure(self, exposure):
        self._exposure = max(float(exposure), self._min_exposure)
        return self.get_exposure()

    @camera.acqstopped
    def set_frame_period(self, frame_period):
        """Set the frame period; 0 (or anything below the minimum) runs as fast as possible"""
        self._frame_period = max(float(frame_period or 0), 0.0)
        return self.get_frame_period()

    ### Image formation settings ###
    def get_gain(self):
        """Gain in dB"""
        return self._gain_db

    def set_gain(self, gain):
        self._gain_db = float(np.clip(gain, 0, 36))
        return self._gain_db

    def get_pattern(self):
        return self._pattern

    def set_pattern(self, pattern):
        if pattern not in PATTERNS:
            raise ValueError(f"unknown pattern {pattern!r}; expected one of {list(PATTERNS)}")
        self._pattern = pattern
        return self._pattern

    def get_noise(self):
        """Whether shot and read noise are added"""
        return self._noise

    def set_noise(self, enabled):
        self._noise = bool(enabled)
        return self._noise

    def get_trigger_mode(self):
        """``"int"`` (free-running) or ``"ext"`` (a simulated external trigger at a fixed rate)"""
        return self._trigger_mode

    @camera.acqstopped
    def set_trigger_mode(self, mode):
        if mode not in ("int", "ext"):
            raise ValueError(f"unknown trigger mode {mode!r}")
        self._trigger_mode = mode
        return self._trigger_mode

    ### Acquisition process controls ###
    def setup_acquisition(self, nframes=100):  # pylint: disable=arguments-differ
        super().setup_acquisition(nframes=nframes)

    def clear_acquisition(self):
        self.stop_acquisition()
        super().clear_acquisition()

    def start_acquisition(self, *args, **kwargs):
        self.stop_acquisition()
        super().start_acquisition(*args, **kwargs)
        self._acquisition_started = time.monotonic()
        self._acquisition_period = self.get_frame_period()
        self._frame_counter.reset(self._acq_params["nframes"])

    def stop_acquisition(self):
        if self.acquisition_in_progress():
            self._frame_counter.update_acquired_frames(self._get_acquired_frames())
            self._acquisition_started = None

    def acquisition_in_progress(self):
        return self._acquisition_started is not None

    def get_frames_status(self):
        if self.acquisition_in_progress():
            self._frame_counter.update_acquired_frames(self._get_acquired_frames())
        return self._TFramesStatus(*self._frame_counter.get_frames_status())

    def _get_acquired_frames(self):
        if self._acquisition_started is None:
            return None
        elapsed = time.monotonic() - self._acquisition_started
        return int(elapsed // self._acquisition_period)

    ### Image settings and transfer controls ###
    def get_detector_size(self):
        return self._size

    def get_roi(self):
        return self._roi

    @camera.acqcleared
    def set_roi(self, hstart=0, hend=None, vstart=0, vend=None, hbin=1, vbin=1):
        hlim, vlim = self.get_roi_limits(hbin, vbin)
        hstart, hend, hbin = self._truncate_roi_axis((hstart, hend, hbin), hlim)
        vstart, vend, vbin = self._truncate_roi_axis((vstart, vend, vbin), vlim)
        # a partial super-pixel at the far edge is dropped, as on a real sensor
        hend -= (hend - hstart) % hbin
        vend -= (vend - vstart) % vbin
        self._roi = (hstart, hend, vstart, vend, hbin, vbin)
        return self.get_roi()

    def get_roi_limits(self, hbin=1, vbin=1):
        wdet, hdet = self.get_detector_size()
        hlim = camera.TAxisROILimit(1, wdet, 1, 1, self._max_bin)
        vlim = camera.TAxisROILimit(1, hdet, 1, 1, self._max_bin)
        return hlim, vlim

    def _get_data_dimensions_rc(self):
        hstart, hend, vstart, vend, hbin, vbin = self.get_roi()
        return (vend - vstart) // vbin, (hend - hstart) // hbin

    ### Frame rendering ###
    _support_chunks = True

    def _geometry(self):
        """Pixel-centre coordinates and a bank of unit normal noise frames for the current
        ROI and binning, cached until either changes"""
        if self._cache.get("roi") != self._roi:
            hstart, _, vstart, _, hbin, vbin = self._roi
            rows, cols = self._get_data_dimensions_rc()
            xs = (hstart + (np.arange(cols) + 0.5) * hbin).astype(np.float32)
            ys = (vstart + (np.arange(rows) + 0.5) * vbin).astype(np.float32)
            rng = np.random.default_rng(self._seed)
            bank = rng.standard_normal((self._noise_bank_size, rows, cols), dtype=np.float32)
            self._cache = {"roi": self._roi, "value": (xs, ys, bank)}
        return self._cache["value"]

    def _scene(self, idx, xs, ys):
        """Relative brightness (0..1) of the test scene at frame ``idx``"""
        w, h = self._size
        if self._pattern == "gaussian":
            phase = idx * 0.04
            cx, cy = w * (0.5 + 0.3 * np.cos(phase)), h * (0.5 + 0.3 * np.sin(2 * phase))
            sigma = min(w, h) / 15
            # separable Gaussian: an outer product of 1D profiles is far cheaper than a 2D exp
            spot = np.outer(
                np.exp(-((ys - cy) ** 2) / (2 * sigma**2)),
                np.exp(-((xs - cx) ** 2) / (2 * sigma**2)),
            )
            return spot * np.float32(0.95) + np.float32(0.05)
        if self._pattern == "gradient":
            return (xs[None, :] / w + ys[:, None] / h) * np.float32(0.5)
        if self._pattern == "checkerboard":
            col = ((xs + 2 * idx) // 64 % 2).astype(bool)
            row = (ys // 64 % 2).astype(bool)
            return np.where(row[:, None] ^ col[None, :], np.float32(0.8), np.float32(0.2))
        return np.full((len(ys), len(xs)), 0.5, dtype=np.float32)

    def render_frame(self, idx):
        """Render frame number ``idx`` of the current acquisition settings"""
        xs, ys, bank = self._geometry()
        hbin, vbin = self._roi[4:]
        electrons = self._scene(idx, xs, ys) * np.float32(
            self._peak_flux * self._exposure * hbin * vbin
        )
        if self._noise:
            n = len(bank)
            shot = bank[idx % n] * np.sqrt(electrons)
            electrons = electrons + shot + bank[(idx * 7 + 3) % n] * np.float32(self._read_noise)
        max_count = 2**self._bit_depth - 1
        counts_per_electron = max_count / self._full_well * 10 ** (self._gain_db / 20)
        counts = electrons * np.float32(counts_per_electron) + np.float32(max_count // 40)
        return np.clip(counts, 0, max_count).astype(self._default_image_dtype)

    def _read_frames(self, rng, return_info=False):
        frames = [self.render_frame(i) for i in range(*rng)]
        infos = None
        if return_info:
            period_us = self._acquisition_period * 1e6
            infos = [TFrameInfo(i, int(i * period_us)) for i in range(*rng)]
        return frames, infos


class SimulatedCameraSettings_GUI(GenericCameraSettings_GUI):
    _frame_period_kind = "value"
    _bin_kind = "both"
    _trigger_modes = {"int": "Internal", "ext": "External (simulated)"}

    def setup_settings_tables(self):
        super().setup_settings_tables()
        self.add_parameter(
            cam_gui_parameters.FloatGUIParameter(
                self, "gain", "Gain (dB)", limit=(0, 36), fmt=".1f", default=0
            ),
            "common",
        )
        self.add_parameter(
            cam_gui_parameters.EnumGUIParameter(self, "pattern", "Test pattern", PATTERNS),
            "advanced",
        )
        self.add_parameter(
            cam_gui_parameters.BoolGUIParameter(self, "noise", "Shot and read noise", default=True),
            "advanced",
        )


class SimulatedCameraThread(GenericCameraThread):
    """Device thread for a simulated camera"""

    parameter_variables = GenericCameraThread.parameter_variables | {
        "exposure",
        "frame_period",
        "detector_size",
        "buffer_size",
        "acq_status",
        "roi_limits",
        "roi",
        "gain",
        "pattern",
        "noise",
        "trigger_mode",
    }

    def connect_device(self):
        self.device = SimulatedCamera(
            size=self.cam_size,
            pattern=self.pattern,
            bit_depth=self.bit_depth,
            ext_trigger_rate=self.ext_trigger_rate,
        )

    def setup_task(  # pylint: disable=arguments-differ
        self,
        size=(1024, 1024),
        pattern="gaussian",
        bit_depth=12,
        ext_trigger_rate=10.0,
        remote=None,
        misc=None,
    ):
        self.cam_size = size
        self.pattern = pattern
        self.bit_depth = bit_depth
        self.ext_trigger_rate = ext_trigger_rate
        super().setup_task(remote=remote, misc=misc)


class SimulatedCameraDescriptor(ICameraDescriptor):
    _cam_kind = "simulated"

    @classmethod
    def iterate_cameras(cls, verbose=False):
        yield from []  # do not discover by default

    def get_kind_name(self):
        return "Simulated camera"

    def make_thread(self, name):
        return SimulatedCameraThread(name=name, kwargs=self.settings["params"].as_dict())

    def make_gui_control(self, parent):
        return SimulatedCameraSettings_GUI(parent, cam_desc=self)

    def make_gui_status(self, parent):
        return GenericCameraStatus_GUI(parent, cam_desc=self)
