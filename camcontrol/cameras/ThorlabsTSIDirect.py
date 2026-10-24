"""Direct-USB backend for Thorlabs TSI scientific cameras (Zelux, Kiralux/Quantalux CS-series).

Alternative to ``ThorlabsTLCam`` (which wraps the vendor SDK/DLL): this backend talks to the
camera directly over USB via ``camcontrol.thorlabs_tsi``, the vendored reverse-engineered
driver, so it needs no ThorCam/vendor SDK installation. It covers basic streaming only --
ROI, exposure, gain -- and does not implement binning, hardware triggering, or the vendor's
color pipeline; a color body's frames come back as the raw, undemosaiced Bayer mosaic. See
``camcontrol/thorlabs_tsi/camera.py`` and its ``protocol.md`` for what is and isn't covered.
"""

import collections
import functools
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
from ..thorlabs_tsi.protocol import CommandError
from ..thorlabs_tsi.transport import StreamDesyncError
from .base import ICameraDescriptor

TDeviceInfo = collections.namedtuple("TDeviceInfo", ["model", "serial_number"])


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


class ThorlabsTSICamera(cam_iface.IROICamera, cam_iface.IExposureCamera):
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

    def __init__(self, serial=None, pid=None, camera_factory=None):
        super().__init__()
        self.serial = serial
        self.pid = pid
        self._camera_factory = camera_factory or TsiCamera.open_camera
        self._tsi = None
        self._sensor_size = None
        self._roi_min_size = None
        self._poll_thread = None
        self._poll_error = None
        self._stop_poll = threading.Event()
        self._buffer_lock = threading.RLock()
        self._frame_notifier = cam_iface.FrameNotifier()
        self._frames = collections.deque()
        self.open()
        self._add_info_variable("device_info", self.get_device_info)
        self._add_info_variable("gain_range", self._get_gain_range, ignore_error=Exception)
        self._add_settings_variable("gain", self.get_gain, self.set_gain)
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
        except (CommandError, usb.core.USBError) as exc:
            tsi.close()  # don't leak the interface claim of a half-opened camera
            raise ThorlabsTSIError(f"could not initialize camera: {exc}") from exc
        self._tsi = tsi

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
        """Return tuple ``(model, serial_number)``."""
        return TDeviceInfo(self._tsi.model, self._tsi.serial_number)

    ### Exposure ###
    @_translate_errors
    def get_frame_timings(self):
        exposure = self._tsi.exposure_time_us * 1e-6
        frame_period = self._tsi.commands.get_frame_time_us() * 1e-6
        return self._TAcqTimings(exposure, frame_period)

    @_translate_errors
    @cam_iface.acqstopped
    def set_exposure(self, exposure):
        self._tsi.exposure_time_us = max(1, int(exposure * 1e6))
        return self.get_exposure()

    ### Gain ###
    @_translate_errors
    def get_gain(self):
        """Raw gain in the camera's own units (see the camera's ``gain.table``, if any)."""
        return self._tsi.gain

    @_translate_errors
    @cam_iface.acqstopped
    def set_gain(self, gain):
        self._tsi.gain = int(gain)
        return self.get_gain()

    @_translate_errors
    def _get_gain_range(self):
        return tuple(self._tsi.commands.limits("gain"))

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
        return x0, x1 + 1, y0, y1 + 1

    @_translate_errors
    @cam_iface.acqcleared
    def set_roi(self, hstart=0, hend=None, vstart=0, vend=None):
        hlim, vlim = self.get_roi_limits()
        hstart, hend = self._truncate_roi_axis((hstart, hend), hlim)
        vstart, vend = self._truncate_roi_axis((vstart, vend), vlim)
        self._tsi.roi = (hstart, vstart, hend - 1, vend - 1)
        return self.get_roi()

    @_translate_errors
    def get_roi_limits(self, hbin=1, vbin=1):
        """ROI limits; the camera may still snap a requested ROI further (see ``set_roi``).

        The width step is 16 on the CS-series (its documented quantisation, matching
        ``roi.min``'s 16-pixel minimum); on the Zelux it is the 4-pixel group the 10-bit
        unpacker needs, since that family's own quantisation is not characterised.
        """
        w, h = self._get_sensor_size()
        min_w, min_h = self._roi_min_size
        wstep = 4 if self._tsi.is_zelux else 16
        min_w = max(wstep, -(-min_w // wstep) * wstep)
        return (
            cam_iface.TAxisROILimit(min_w, w, 1, wstep, 1),
            cam_iface.TAxisROILimit(min_h, h, 1, 1, 1),
        )

    def _get_data_dimensions_rc(self):
        roi = self.get_roi()
        return roi[3] - roi[2], roi[1] - roi[0]

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
        self._tsi.arm(frames_to_buffer=min(nframes, 16))
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="thorlabs-tsi-poll", daemon=True
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

    def _poll_loop(self):
        """Drain decoded frames off the camera's own reader thread into our ring buffer.

        ``TsiCamera`` already runs a dedicated USB reader thread and queues whole decoded
        frames (see ``thorlabs_tsi.stream.FrameStream``); this thread only needs to pull them
        off with ``get_pending_frame_or_null`` and publish them the way pylablib expects
        (mirroring the vendor-SDK backend's callback-driven ring buffer in
        ``pylablib.devices.Thorlabs.TLCamera.ThorlabsTLCamera.RingBuffer``, adapted to a pull
        loop since this driver has no native callback).
        """
        while not self._stop_poll.is_set():
            try:
                frame = self._tsi.get_pending_frame_or_null(timeout=0.2)
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
            if frame is None:
                continue
            with self._buffer_lock:
                self._frames.append(frame.image)
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
            frames = [self._frames[n - base] for n in range(*rng)]
        return frames, None

    def _zero_frame(self, n):
        rows, cols = self._get_data_dimensions_rc()
        return np.zeros((n, rows, cols), dtype=self._default_image_dtype)


class GainParameter(cam_gui_parameters.IntGUIParameter):
    """ThorlabsTSIDirect gain parameter; receives its value range from the camera."""

    def __init__(self, settings):
        super().__init__(settings, "gain", "Gain (raw)")

    def setup(self, parameters, full_info):
        super().setup(parameters, full_info)
        if "gain_range" in full_info:
            self.base.w[self.gui_name].set_limiter(full_info["gain_range"])


class Settings_GUI(GenericCameraSettings_GUI):
    def get_basic_parameters(self, name):
        if name == "gain":
            return GainParameter(self)
        if name == "trigger_mode":
            return None  # only free-running acquisition is implemented
        return super().get_basic_parameters(name)

    def setup_settings_tables(self):
        super().setup_settings_tables()
        self.add_builtin_parameter("gain", "advanced")


class ThorlabsTSICameraThread(GenericCameraThread):
    """Device thread for a direct-USB Thorlabs TSI camera."""

    parameter_variables = GenericCameraThread.parameter_variables | {
        "exposure",
        "frame_period",
        "detector_size",
        "buffer_size",
        "acq_status",
        "roi_limits",
        "roi",
        "gain",
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
        return GenericCameraStatus_GUI(parent, cam_desc=self)
