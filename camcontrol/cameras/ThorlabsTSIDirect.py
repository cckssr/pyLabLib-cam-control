"""Direct-USB backend for Thorlabs TSI scientific cameras (Zelux, Kiralux/Quantalux CS-series).

Alternative to ``ThorlabsTLCam`` (which wraps the vendor SDK/DLL): this backend talks to the
camera directly over USB via ``camcontrol.thorlabs_tsi``, the vendored reverse-engineered
driver, so it needs no ThorCam/vendor SDK installation. It covers free-running streaming
with exposure, frame-rate control, gain (in dB), black level (Zelux), ROI, on-camera
binning (CS-series) and an optional bilinear demosaic of color bodies' Bayer mosaic; it does
not implement hardware triggering or the vendor's color pipeline. See
``camcontrol/thorlabs_tsi/camera.py`` and its ``protocol.md`` for what is and isn't covered.
"""

import collections
import functools
import math
import sys
import threading

import numpy as np
import usb.core
from pylablib.core.devio import comm_backend
from pylablib.devices.interface import camera as cam_iface
from pylablib.thread.devices.generic.camera import GenericCameraThread

from ..gui import cam_gui_parameters
from ..gui.base_cam_ctl_gui import GenericCameraSettings_GUI, GenericCameraStatus_GUI
from ..thorlabs_tsi.camera import TsiCamera, discover_available_cameras
from ..thorlabs_tsi.frames import BAYER_PATTERNS, debayer_bilinear
from ..thorlabs_tsi.protocol import PIXEL_GROUP_PIXELS, CommandError
from ..thorlabs_tsi.transport import StreamDesyncError
from .base import ICameraDescriptor

TDeviceInfo = collections.namedtuple(
    "TDeviceInfo", ["model", "serial_number", "firmware_version", "name"]
)
TSensorInfo = collections.namedtuple(
    "TSensorInfo", ["width", "height", "bit_depth", "bayer_pattern"]
)
TFrameInfo = collections.namedtuple("TFrameInfo", ["frame_index", "framestamp", "timestamp_us"])

COLOR_MODES = {"raw": "Raw (Bayer mosaic)", "rgb": "RGB (demosaiced)"}


class ThorlabsTSIError(comm_backend.DeviceError):
    """Thorlabs TSI direct-USB camera error (wraps the driver's ``CommandError``/``USBError``)"""


class ThorlabsTSITimeoutError(ThorlabsTSIError):
    """Thorlabs TSI direct-USB camera frame timeout"""


class ThorlabsTSIFrameTransferError(ThorlabsTSIError, cam_iface.DefaultFrameTransferError):
    """The image stream broke off; the acquisition loop answers this by restarting it"""


def _translate_errors(func):
    """Re-raise driver errors as :exc:`ThorlabsTSIError`.

    pylablib's device threads only handle their device's ``Error`` class gracefully (e.g. a
    failed connection becomes a "disconnected" status); anything else ends the thread.
    """

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (CommandError, usb.core.USBError) as exc:
            raise ThorlabsTSIError(f"{type(exc).__name__}: {exc}") from exc

    return wrapped


def _shifted_bayer_phase(phase, x0, y0):
    """The ``color_filter_phase`` name for an image starting at sensor pixel ``(x0, y0)``.

    The camera reports the color at the full frame's origin; an ROI starting on an odd row or
    column sees the 2x2 tile shifted by one, which demosaicing with the unshifted tile would
    turn into swapped colors.
    """
    tl, tr, bl, br = BAYER_PATTERNS[phase]
    if x0 % 2:
        tl, tr, bl, br = tr, tl, br, bl
    if y0 % 2:
        tl, tr, bl, br = bl, br, tl, tr
    names = {pattern: name for name, pattern in BAYER_PATTERNS.items()}
    return names[tl + tr + bl + br]


class ThorlabsTSICamera(cam_iface.IBinROICamera, cam_iface.IExposureCamera):
    """
    Thorlabs TSI scientific camera, accessed directly over USB (no vendor SDK/DLL).

    Args:
        serial: camera serial number; ``None`` connects to the first camera of the matching
            ``pid`` (or of any known family if ``pid`` is also ``None``)
        pid: USB product ID identifying the camera family (``protocol.PID_ZELUX`` or
            ``protocol.PID_CS_SERIES``); ``None`` matches either
        camera_factory: callable ``(pid=, serial=)`` returning an opened
            :class:`~camcontrol.thorlabs_tsi.camera.TsiCamera`; defaults to
            ``TsiCamera.open_camera`` (real USB). Used to substitute an emulated device.
    """

    Error = ThorlabsTSIError
    TimeoutError = ThorlabsTSITimeoutError
    FrameTransferError = ThorlabsTSIFrameTransferError
    _TFrameInfo = TFrameInfo
    _frameinfo_fields = TFrameInfo._fields

    def __init__(self, serial=None, pid=None, camera_factory=None):
        super().__init__()
        self.serial = serial
        self.pid = pid
        self._camera_factory = camera_factory or TsiCamera.open_camera
        self._tsi = None
        self._sensor_size = None
        self._roi_min_size = None
        self._color_mode = "raw"
        self._poll_thread = None
        self._poll_error = None
        self._stop_poll = threading.Event()
        self._buffer_lock = threading.RLock()
        self._frame_notifier = cam_iface.FrameNotifier()
        self._frames = collections.deque()
        self.open()
        self._add_info_variable("device_info", self.get_device_info)
        self._add_info_variable("sensor_info", self.get_sensor_info)
        self._add_info_variable("gain_range", self.get_gain_range)
        self._add_status_variable("exposure_range", self.get_exposure_range)
        self._add_settings_variable("frame_period", self.get_frame_period, self.set_frame_period)
        self._add_settings_variable("gain_db", self.get_gain_db, self.set_gain_db)
        if self._caps["black_level"]:
            self._add_info_variable("black_level_range", self.get_black_level_range)
            self._add_settings_variable("black_level", self.get_black_level, self.set_black_level)
        if self._caps["bayer_phase"] is not None:
            self._add_settings_variable("color_mode", self.get_color_mode, self.set_color_mode)
        self._add_status_variable("stream_stats", self._get_stream_stats, ignore_error=Exception)

    def _get_connection_parameters(self):
        return self.serial

    def open(self):
        if self._tsi is not None:
            return
        try:
            tsi = self._camera_factory(pid=self.pid, serial=self.serial)
        except (CommandError, usb.core.USBError, RuntimeError) as exc:
            # RuntimeError: no matching camera on the bus / no camera with that serial
            raise ThorlabsTSIError(f"could not open camera: {exc}") from exc
        try:
            self.serial = tsi.serial_number
            self.pid = tsi.transport.pid
            # ThorCam's own connect sequence starts with an unconditional `stop`
            # (protocol.md's "Init/configure/stream sequence"). Without it, a camera left
            # "running" by an earlier crashed/killed session/process answers the first
            # roi/exposure change with "Camera Running" and connect throws --
            # acquisition_in_progress() only tracks this object's own poll thread, so it has
            # no way to know the hardware was already running before this object existed.
            try:
                tsi.commands.stop()
            except CommandError:
                pass
            self._caps = self._probe_capabilities(tsi)
        except (CommandError, usb.core.USBError) as exc:
            tsi.close()  # don't leak the interface claim of a half-opened camera
            raise ThorlabsTSIError(f"could not initialize camera: {exc}") from exc
        self._tsi = tsi

    @staticmethod
    def _probe_capabilities(tsi):
        """Which optional features this camera has (they differ by family and model)"""
        try:
            supported = set(tsi.commands.supported())
        except CommandError:
            supported = None
        gain_table = tsi.commands.get_gain_table()
        return {
            # the firmware's own command list is authoritative; the family is the fallback
            "black_level": "blklvl" in supported if supported else tsi.is_zelux,
            "max_bin": (tsi.binx_range[1], tsi.biny_range[1]),
            "bayer_phase": tsi.color_filter_phase if tsi.is_color else None,
            "gain_table": gain_table,
        }

    def close(self):
        if self._tsi is not None:
            try:
                self.clear_acquisition()
            except ThorlabsTSIError as exc:
                # e.g. the camera was unplugged: it can't be stopped, but must still be released
                print(f"ThorlabsTSIDirect: error stopping camera on close: {exc}", file=sys.stderr)
            finally:
                self._tsi.close()
                self._tsi = None

    def is_opened(self):
        return self._tsi is not None

    @_translate_errors
    def get_device_info(self):
        """Return tuple ``(model, serial_number, firmware_version, name)``."""
        return TDeviceInfo(
            self._tsi.model,
            self._tsi.serial_number,
            self._query_optional(lambda: self._tsi.firmware_version),
            self._query_optional(lambda: self._tsi.name),
        )

    @_translate_errors
    def get_sensor_info(self):
        """Return tuple ``(width, height, bit_depth, bayer_pattern)``; ``bayer_pattern`` is
        the 2x2 tile at the sensor origin (e.g. ``"RGGB"``) or ``None`` on a mono camera."""
        width, height = self._get_sensor_size()
        phase = self._caps["bayer_phase"]
        return TSensorInfo(
            width,
            height,
            self._query_optional(lambda: self._tsi.bit_depth),
            None if phase is None else BAYER_PATTERNS.get(phase, phase),
        )

    @staticmethod
    def _query_optional(getter):
        try:
            return getter()
        except CommandError:
            return None

    ### Exposure and frame rate ###
    @_translate_errors
    def get_frame_timings(self):
        exposure = self._tsi.exposure_time_us * 1e-6
        frame_period = self._tsi.frame_time_us * 1e-6
        return self._TAcqTimings(exposure, frame_period)

    @_translate_errors
    @cam_iface.acqstopped
    def set_exposure(self, exposure):
        lo, hi = self._tsi.exposure_time_range_us
        self._tsi.exposure_time_us = int(np.clip(round(exposure * 1e6), lo, hi))
        return self.get_exposure()

    @_translate_errors
    def get_exposure_range(self):
        """Current ``(min, max)`` exposure in seconds; moves with ROI and frame-rate control"""
        lo, hi = self._tsi.exposure_time_range_us
        return lo * 1e-6, hi * 1e-6

    @_translate_errors
    @cam_iface.acqstopped
    def set_frame_period(self, frame_period):
        """Set the frame period via the camera's frame-rate control.

        ``None`` or 0 (or less) switches frame-rate control off, so the camera runs as fast as
        the exposure and ROI allow. Larger values are clamped to the current allowed range.
        """
        if frame_period is None or frame_period <= 0:
            self._tsi.is_frame_rate_control_enabled = False
        else:
            self._tsi.is_frame_rate_control_enabled = True
            lo, hi = self._tsi.frame_rate_control_period_range_us
            self._tsi.frame_rate_control_period_us = int(np.clip(round(frame_period * 1e6), lo, hi))
        return self.get_frame_period()

    ### Gain and black level ###
    @_translate_errors
    def get_gain_db(self):
        """Gain in dB"""
        return self._tsi.commands.get_gain_db()

    @_translate_errors
    @cam_iface.acqstopped
    def set_gain_db(self, gain_db):
        """Set the gain in dB; the CS-series only has a few discrete gains, so the nearest
        one is chosen there"""
        lo, hi = self._tsi.gain_range
        table = self._caps["gain_table"]
        if table:
            indices = range(max(lo, 0), min(hi, len(table) - 1) + 1)
            raw = min(indices, key=lambda i: abs(table[i] / 10 - gain_db))
        else:
            raw = int(np.clip(round(gain_db * 10), lo, hi))
        self._tsi.gain = raw
        return self.get_gain_db()

    @_translate_errors
    def get_gain_range(self):
        """``(min, max)`` gain in dB"""
        lo, hi = self._tsi.gain_range
        table = self._caps["gain_table"]
        if table:
            steps = table[max(lo, 0) : hi + 1]
            return min(steps) / 10, max(steps) / 10
        return lo / 10, hi / 10

    @_translate_errors
    def get_black_level(self):
        """Black level offset, in the camera's own units (Zelux only)"""
        return self._tsi.black_level

    @_translate_errors
    @cam_iface.acqstopped
    def set_black_level(self, level):
        lo, hi = self._tsi.black_level_range
        self._tsi.black_level = int(np.clip(level, lo, hi))
        return self.get_black_level()

    @_translate_errors
    def get_black_level_range(self):
        return tuple(self._tsi.black_level_range)

    ### Color ###
    def get_color_mode(self):
        """``"raw"`` (the sensor's Bayer mosaic) or ``"rgb"`` (bilinear demosaic); color
        bodies only"""
        return self._color_mode

    @cam_iface.acqcleared
    def set_color_mode(self, mode):
        if mode not in COLOR_MODES:
            raise ValueError(f"unknown color mode {mode!r}; expected one of {list(COLOR_MODES)}")
        if self._caps["bayer_phase"] is not None:
            self._color_mode = mode
        return self._color_mode

    def _debayer_phase(self):
        """The Bayer phase to demosaic frames with, or ``None`` to keep them raw"""
        phase = self._caps["bayer_phase"]
        if phase is None or self._color_mode != "rgb":
            return None
        hstart, _, vstart, _, hbin, vbin = self.get_roi()
        if hbin > 1 or vbin > 1:
            return None  # binning mixes the mosaic's colors; there is nothing to demosaic
        return _shifted_bayer_phase(phase, hstart, vstart)

    def _get_stream_stats(self):
        """Counters from the underlying USB reader thread (frames missed/truncated/dropped,
        read errors, resync events) -- surfaced as a status variable so a stalled or silently
        dying stream is visible instead of just showing no frames with no explanation."""
        stats = self._tsi.stream_stats
        return None if stats is None else stats.as_dict()

    ### Geometry ###
    def _get_sensor_size(self):
        """Full sensor size, derived from ``roi.max``.

        ``roi.max`` is *not* "the largest legal ROI box" -- its four fields are each the
        independent maximum for that positional argument (e.g. x0's max is constrained by
        the minimum ROI width, so it is nowhere near 0; see protocol.md's roi.min/.max
        discussion). Only x1/y1 -- the maximum right/bottom edge -- describe the sensor's own
        extent; x0/y0 from the same reply must not be combined with them.
        """
        if self._sensor_size is None:
            (mx0, my0, mx1, my1), (_x0_max, _y0_max, x1, y1) = self._tsi.roi_range
            self._sensor_size = (x1 + 1, y1 + 1)
            # roi.min's x1/y1 are the smallest right/bottom edge at x0=y0=0, i.e. the
            # minimum ROI size (e.g. "0 0 15 1" -> 16x2 on a CS135MU)
            self._roi_min_size = (max(1, mx1 - mx0 + 1), max(1, my1 - my0 + 1))
        return self._sensor_size

    @_translate_errors
    def get_detector_size(self):
        return self._get_sensor_size()

    @_translate_errors
    def get_roi(self):
        x0, y0, x1, y1 = self._tsi.roi
        return x0, x1 + 1, y0, y1 + 1, self._tsi.binx, self._tsi.biny

    @_translate_errors
    @cam_iface.acqcleared
    def set_roi(self, hstart=0, hend=None, vstart=0, vend=None, hbin=1, vbin=1):
        hlim, vlim = self.get_roi_limits(hbin, vbin)
        hstart, hend, hbin = self._truncate_roi_axis((hstart, hend, hbin), hlim)
        vstart, vend, vbin = self._truncate_roi_axis((vstart, vend, vbin), vlim)
        if hlim.maxbin > 1 or vlim.maxbin > 1:
            # binning first: the camera trims the ROI to a whole number of super-pixels
            self._tsi.binx, self._tsi.biny = hbin, vbin
        self._tsi.roi = (hstart, vstart, hend - 1, vend - 1)
        width = self._tsi.image_width_pixels
        if width % PIXEL_GROUP_PIXELS:
            raise ThorlabsTSIError(
                f"the camera chose an image width of {width} pixels, which is not a multiple of "
                f"{PIXEL_GROUP_PIXELS}; frames with such widths can't be decoded"
            )
        return self.get_roi()

    @_translate_errors
    def get_roi_limits(self, hbin=1, vbin=1):
        """ROI limits; the camera may still snap a requested ROI further (see ``set_roi``).

        The width step is 16 on the CS-series (its documented quantisation, matching
        ``roi.min``'s 16-pixel minimum); on the Zelux it is the 4-pixel group the 10-bit
        unpacker needs, since that family's own quantisation is not characterised. With
        binning, the step also keeps the *binned* width a multiple of that group.
        """
        w, h = self._get_sensor_size()
        min_w, min_h = self._roi_min_size
        max_hbin, max_vbin = self._caps["max_bin"]
        hbin = max(1, min(hbin, max_hbin))
        vbin = max(1, min(vbin, max_vbin))
        wstep = math.lcm(4 if self._tsi.is_zelux else 16, PIXEL_GROUP_PIXELS * hbin)
        min_w = max(wstep, -(-min_w // wstep) * wstep)
        min_h = max(vbin, -(-min_h // vbin) * vbin)
        return (
            cam_iface.TAxisROILimit(min_w, w, 1, wstep, max_hbin),
            cam_iface.TAxisROILimit(min_h, h, 1, vbin, max_vbin),
        )

    def _get_data_dimensions_rc(self):
        hstart, hend, vstart, vend, hbin, vbin = self.get_roi()
        return (vend - vstart) // vbin, (hend - hstart) // hbin

    ### Acquisition process controls ###
    def setup_acquisition(self, nframes=100):  # pylint: disable=arguments-differ
        super().setup_acquisition(nframes=nframes)

    def clear_acquisition(self):
        self.stop_acquisition()
        with self._buffer_lock:
            self._frames = collections.deque()
        super().clear_acquisition()

    @_translate_errors
    def start_acquisition(self, *args, **kwargs):
        self.stop_acquisition()
        super().start_acquisition(*args, **kwargs)
        nframes = self._acq_params["nframes"]
        with self._buffer_lock:
            # pylablib only ever asks for the last `nframes`, but more can arrive between it
            # checking the range and reading it; the margin keeps those reads valid
            self._frames = collections.deque(maxlen=nframes + max(16, nframes // 4))
            self._frame_notifier.reset()
        self._frame_counter.reset(nframes)
        self._poll_error = None
        self._stop_poll.clear()
        debayer_phase = self._debayer_phase()
        self._tsi.arm(frames_to_buffer=min(nframes, 16))
        self._poll_thread = threading.Thread(
            target=self._poll_loop, args=(debayer_phase,), name="thorlabs-tsi-poll", daemon=True
        )
        self._poll_thread.start()

    @_translate_errors
    def stop_acquisition(self):
        if self.acquisition_in_progress():
            self._stop_poll.set()
            thread, self._poll_thread = self._poll_thread, None
            thread.join(5.0)
            self._tsi.disarm()

    def acquisition_in_progress(self):
        return self._poll_thread is not None

    def _poll_loop(self, debayer_phase=None):
        """Drain decoded frames off the camera's own reader thread into our ring buffer.

        ``TsiCamera`` already runs a dedicated USB reader thread and queues whole decoded
        frames (see ``thorlabs_tsi.stream.FrameStream``); this thread only needs to pull them
        off with ``get_pending_frame_or_null`` and publish them the way pylablib expects
        (mirroring the vendor-SDK backend's callback-driven ring buffer in
        ``pylablib.devices.Thorlabs.TLCamera.ThorlabsTLCamera.RingBuffer``, adapted to a pull
        loop since this driver has no native callback). Demosaicing, if enabled, happens here
        too, off the camera thread.
        """
        while not self._stop_poll.is_set():
            try:
                frame = self._tsi.get_pending_frame_or_null(timeout=0.2)
                if frame is None:
                    continue
                image = frame.image
                if debayer_phase is not None:
                    image = debayer_bilinear(image, debayer_phase)
            except StreamDesyncError as exc:
                # By design recoverable: the assembler only ever emits chunk-aligned frames,
                # so the *next* one already starts realigned -- see get_pending_frame_or_null's
                # docstring. Treating this as fatal would silently end streaming on the first
                # transient desync, which looks identical to "no camera output" from the GUI.
                print(f"ThorlabsTSIDirect: frame desync, continuing: {exc}", file=sys.stderr)
                continue
            except usb.core.USBError as exc:
                # The stream itself broke off; restarting the acquisition may recover it
                print(f"ThorlabsTSIDirect: USB error, stopping stream: {exc}", file=sys.stderr)
                self._poll_error = ThorlabsTSIFrameTransferError(f"USB stream error: {exc}")
                break
            except Exception as exc:
                # Anything else (e.g. a frame that fails to decode) won't fix itself on a
                # restart; it must still reach the camera thread instead of ending this thread
                # silently with acquisition_in_progress() stuck at True and no frames
                print(f"ThorlabsTSIDirect: stream failed: {exc!r}", file=sys.stderr)
                self._poll_error = ThorlabsTSIError(f"stream failed: {type(exc).__name__}: {exc}")
                break
            stamp = None if frame.timestamp_ns is None else int(frame.timestamp_ns // 1000)
            with self._buffer_lock:
                self._frames.append((image, frame.frame_count, stamp))
                self._frame_notifier.inc()

    def _check_poll_error(self):
        """Re-raise (once) the error that ended the poll thread"""
        err, self._poll_error = self._poll_error, None
        if err is not None:
            raise err

    def _get_acquired_frames(self):
        return self._frame_notifier.counter

    def _wait_for_next_frame(self, timeout=20.0, idx=None):
        self._check_poll_error()
        self._frame_notifier.wait(idx=idx, timeout=timeout)

    def _read_frames(self, rng, return_info=False):
        with self._buffer_lock:
            base = self._frame_notifier.counter - len(self._frames)
            if rng[0] < base:
                # Overwritten since pylablib checked the range; returning other frames in
                # their place would silently mislabel them
                raise ThorlabsTSIFrameTransferError(
                    f"frames {rng[0]}..{base - 1} were overwritten before they could be read"
                )
            entries = [self._frames[n - base] for n in range(*rng)]
        frames = [image for image, _, _ in entries]
        infos = None
        if return_info:
            infos = [
                TFrameInfo(n, -1 if count is None else count, -1 if stamp is None else stamp)
                for n, (_, count, stamp) in zip(range(*rng), entries, strict=True)
            ]
        # frames carry a trailing color dimension when demosaiced
        return frames, infos, (frames[0].ndim - 2 if frames else 0)

    def _zero_frame(self, n):
        rows, cols = self._get_data_dimensions_rc()
        channels = (3,) if self._debayer_phase() is not None else ()
        return np.zeros((n, rows, cols) + channels, dtype=self._default_image_dtype)


class ExposureParameter(cam_gui_parameters.FloatGUIParameter):
    """Exposure parameter whose limits come from the camera"""

    def __init__(self, settings):
        super().__init__(
            settings,
            "exposure",
            "Exposure (ms)",
            limit=(0, None),
            fmt=".3f",
            default=10,
            factor=1e3,
        )

    def setup(self, parameters, full_info):
        super().setup(parameters, full_info)
        if "exposure_range" in full_info:
            lo, hi = full_info["exposure_range"]
            self.base.w[self.gui_name].set_limiter((lo * self.factor, hi * self.factor))


class GainParameter(cam_gui_parameters.FloatGUIParameter):
    """Gain (dB) parameter whose limits come from the camera"""

    def __init__(self, settings):
        super().__init__(settings, "gain_db", "Gain (dB)", fmt=".1f")

    def setup(self, parameters, full_info):
        super().setup(parameters, full_info)
        if "gain_range" in full_info:
            self.base.w[self.gui_name].set_limiter(full_info["gain_range"])


class BlackLevelParameter(cam_gui_parameters.IntGUIParameter):
    """Black level parameter whose limits come from the camera (Zelux only)"""

    def __init__(self, settings):
        super().__init__(settings, "black_level", "Black level")

    def setup(self, parameters, full_info):
        super().setup(parameters, full_info)
        if "black_level_range" in full_info:
            self.base.w[self.gui_name].set_limiter(full_info["black_level_range"])


class Settings_GUI(GenericCameraSettings_GUI):
    _frame_period_kind = "value"
    _bin_kind = "both"

    def get_basic_parameters(self, name):
        if name == "exposure":
            return ExposureParameter(self)
        if name == "trigger_mode":
            return None  # only free-running acquisition is implemented
        return super().get_basic_parameters(name)

    def setup_settings_tables(self):
        super().setup_settings_tables()
        self.add_parameter(GainParameter(self), "common")
        self.add_parameter(BlackLevelParameter(self), "advanced")
        self.add_parameter(
            cam_gui_parameters.EnumGUIParameter(self, "color_mode", "Color output", COLOR_MODES),
            "advanced",
        )


class Status_GUI(GenericCameraStatus_GUI):
    _stream_counters = {
        "frames_missed": "Frames missed (camera)",
        "frames_truncated": "Frames truncated",
        "frames_dropped_by_consumer": "Frames dropped (host)",
        "read_errors": "USB read errors",
    }

    def setup_status_table(self):
        self.add_text_label("model", label="Model:")
        self.add_text_label("serial_number", label="Serial number:")
        self.add_text_label("firmware_version", label="Firmware:")
        self.add_text_label("sensor", label="Sensor:")
        for name, label in self._stream_counters.items():
            self.add_num_label(f"stream/{name}", formatter="int", label=f"{label}:")

    def show_parameters(self, params):
        super().show_parameters(params)
        if "device_info" in params:
            info = params["device_info"]
            self.v["model"] = info.model
            self.v["serial_number"] = info.serial_number
            self.v["firmware_version"] = info.firmware_version or "?"
        if "sensor_info" in params:
            info = params["sensor_info"]
            desc = f"{info.width} x {info.height}, {info.bit_depth or '?'} bit"
            self.v["sensor"] = desc + (
                f", {info.bayer_pattern} color" if info.bayer_pattern else ", mono"
            )
        stats = params.get("stream_stats")
        for name in self._stream_counters:
            value = stats.get(name, 0) if stats else 0
            self.v[f"stream/{name}"] = value
            self.w[f"stream/{name}"].setStyleSheet("color: red; font-weight: bold" if value else "")


class ThorlabsTSICameraThread(GenericCameraThread):
    """Device thread for a direct-USB Thorlabs TSI camera."""

    parameter_variables = GenericCameraThread.parameter_variables | {
        "exposure",
        "exposure_range",
        "frame_period",
        "detector_size",
        "buffer_size",
        "acq_status",
        "roi_limits",
        "roi",
        "gain_db",
        "gain_range",
        "black_level",
        "black_level_range",
        "color_mode",
        "device_info",
        "sensor_info",
        "stream_stats",
    }

    def make_device(self):
        """Create the (opened) camera object; overridden to substitute an emulated device"""
        return ThorlabsTSICamera(serial=self.serial, pid=self.pid)

    def connect_device(self):
        try:
            self.device = self.make_device()
        except (OSError, ThorlabsTSIError) as exc:
            # Reported as "disconnected" (and retried by the thread) instead of ending it
            print(f"ThorlabsTSIDirect: connection failed: {exc}", file=sys.stderr)
            self.device = None
            raise self.ConnectionFailError from exc

    def setup_task(self, serial=None, pid=None, remote=None, misc=None):  # pylint: disable=arguments-differ
        self.serial = serial
        self.pid = pid
        super().setup_task(remote=remote, misc=misc)


class ThorlabsTSIDirectCameraDescriptor(ICameraDescriptor):
    _cam_kind = "ThorlabsTSIDirect"

    @classmethod
    def iterate_cameras(cls, verbose=False):
        if verbose:
            print("Searching for Thorlabs TSI cameras (direct USB)")
        try:
            found = discover_available_cameras()
        except (usb.core.USBError, CommandError) as exc:
            if verbose:
                print(f"Error searching for Thorlabs TSI cameras (direct USB): {exc}\n")
            return
        except usb.core.NoBackendError:
            if verbose:
                print(
                    "Error accessing USB: no libusb backend found; required for direct "
                    "USB access to Thorlabs TSI cameras\n"
                )
            if verbose == "full":
                cls.print_error()
            return
        if not found:
            if verbose:
                print("Found no Thorlabs TSI cameras (direct USB)\n")
            return
        if verbose:
            print(
                "Found {} Thorlabs TSI camera{} (direct USB)".format(
                    len(found), "s" if len(found) > 1 else ""
                )
            )
        for pid, serial, model in found:
            if verbose:
                print(f"Found Thorlabs TSI camera serial={serial} model={model}")
            yield (pid, serial, model), (pid, serial, model)

    @classmethod
    def generate_description(cls, idx, cam=None, info=None):
        pid, serial, model = info
        cam_desc = cls.build_cam_desc(params={"serial": serial, "pid": pid})
        cam_desc["display_name"] = f"{model} {serial} (direct USB)"
        cam_name = f"thorlabs_tsi_direct_{idx}"
        return cam_name, cam_desc

    def get_kind_name(self):
        return "Thorlabs Scientific Camera (direct USB)"

    def make_thread(self, name):
        return ThorlabsTSICameraThread(name=name, kwargs=self.settings["params"].as_dict())

    def make_gui_control(self, parent):
        return Settings_GUI(parent, cam_desc=self)

    def make_gui_status(self, parent):
        return Status_GUI(parent, cam_desc=self)
