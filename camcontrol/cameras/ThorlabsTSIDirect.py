"""Direct-USB backend for Thorlabs TSI scientific cameras (Zelux, Kiralux/Quantalux CS-series).

Alternative to ``ThorlabsTLCam`` (which wraps the vendor SDK/DLL): this backend talks to the
camera directly over USB via ``camcontrol.thorlabs_tsi``, the vendored reverse-engineered
driver, so it needs no ThorCam/vendor SDK installation. It covers basic streaming only --
ROI, exposure, gain -- and does not implement binning, hardware triggering, or the vendor's
color pipeline; a color body's frames come back as the raw, undemosaiced Bayer mosaic. See
``camcontrol/thorlabs_tsi/camera.py`` and its ``protocol.md`` for what is and isn't covered.
"""

import collections
import threading

import numpy as np
import usb.core
import usb.util
from pylablib.devices.interface import camera as cam_iface
from pylablib.thread.devices.generic.camera import GenericCameraThread

from ..gui import cam_gui_parameters
from ..gui.base_cam_ctl_gui import GenericCameraSettings_GUI, GenericCameraStatus_GUI
from ..thorlabs_tsi import protocol
from ..thorlabs_tsi.camera import TsiCamera
from ..thorlabs_tsi.transport import TsiCameraTransport
from .base import ICameraDescriptor

TDeviceInfo = collections.namedtuple("TDeviceInfo", ["model", "serial_number"])


class ThorlabsTSICamera(cam_iface.IROICamera, cam_iface.IExposureCamera):
    """
    Thorlabs TSI scientific camera, accessed directly over USB (no vendor SDK/DLL).

    Args:
        serial: camera serial number; ``None`` connects to the first camera of the matching
            ``pid`` (or of any known family if ``pid`` is also ``None``)
        pid: USB product ID identifying the camera family (``protocol.PID_ZELUX`` or
            ``protocol.PID_CS_SERIES``); ``None`` matches either
    """

    def __init__(self, serial=None, pid=None):
        super().__init__()
        self.serial = serial
        self.pid = pid
        self._tsi = None
        self._sensor_size = None
        self._poll_thread = None
        self._stop_poll = threading.Event()
        self._buffer_lock = threading.RLock()
        self._frame_notifier = cam_iface.FrameNotifier()
        self._frames = []
        self._frames_max = 0
        self.open()
        self._add_info_variable("device_info", self.get_device_info)
        self._add_info_variable("gain_range", self._get_gain_range, ignore_error=Exception)
        self._add_settings_variable("gain", self.get_gain, self.set_gain)

    def _get_connection_parameters(self):
        return self.serial

    def open(self):
        if self._tsi is None:
            self._tsi = TsiCamera.open_camera(pid=self.pid, serial=self.serial)
            self.serial = self._tsi.serial_number
            self.pid = self._tsi.transport.pid

    def close(self):
        if self._tsi is not None:
            try:
                self.clear_acquisition()
            finally:
                self._tsi.close()
                self._tsi = None

    def is_opened(self):
        return self._tsi is not None

    def get_device_info(self):
        """Return tuple ``(model, serial_number)``."""
        return TDeviceInfo(self._tsi.model, self._tsi.serial_number)

    ### Exposure ###
    def get_frame_timings(self):
        exposure = self._tsi.exposure_time_us * 1e-6
        frame_period = self._tsi.commands.get_frame_time_us() * 1e-6
        return self._TAcqTimings(exposure, frame_period)

    @cam_iface.acqstopped
    def set_exposure(self, exposure):
        self._tsi.exposure_time_us = max(1, int(exposure * 1e6))
        return self.get_exposure()

    ### Gain ###
    def get_gain(self):
        """Raw gain in the camera's own units (see the camera's ``gain.table``, if any)."""
        return self._tsi.gain

    def set_gain(self, gain):
        self._tsi.gain = int(gain)
        return self.get_gain()

    def _get_gain_range(self):
        return tuple(self._tsi.commands.limits("gain"))

    ### Geometry ###
    def _get_sensor_size(self):
        if self._sensor_size is None:
            x0, y0, x1, y1 = (int(v) for v in self._tsi.commands.query("roi.max").split())
            self._sensor_size = (x1 - x0 + 1, y1 - y0 + 1)
        return self._sensor_size

    def get_detector_size(self):
        return self._get_sensor_size()

    def get_roi(self):
        x0, y0, x1, y1 = self._tsi.roi
        return x0, x1 + 1, y0, y1 + 1

    @cam_iface.acqcleared
    def set_roi(self, hstart=0, hend=None, vstart=0, vend=None):
        hlim, vlim = self.get_roi_limits()
        hstart, hend = self._truncate_roi_axis((hstart, hend), hlim)
        vstart, vend = self._truncate_roi_axis((vstart, vend), vlim)
        self._tsi.roi = (hstart, vstart, hend - 1, vend - 1)
        return self.get_roi()

    def get_roi_limits(self, hbin=1, vbin=1):
        w, h = self._get_sensor_size()
        return cam_iface.TAxisROILimit(1, w, 1, 1, 1), cam_iface.TAxisROILimit(1, h, 1, 1, 1)

    def _get_data_dimensions_rc(self):
        roi = self.get_roi()
        return roi[3] - roi[2], roi[1] - roi[0]

    ### Acquisition process controls ###
    def setup_acquisition(self, nframes=100):  # pylint: disable=arguments-differ
        super().setup_acquisition(nframes=nframes)

    def clear_acquisition(self):
        self.stop_acquisition()
        with self._buffer_lock:
            self._frames = []
            self._frames_max = 0
        super().clear_acquisition()

    def start_acquisition(self, *args, **kwargs):
        self.stop_acquisition()
        super().start_acquisition(*args, **kwargs)
        nframes = self._acq_params["nframes"]
        with self._buffer_lock:
            self._frames = []
            self._frames_max = nframes + 10
            self._frame_notifier.reset()
        self._frame_counter.reset(nframes)
        self._stop_poll.clear()
        self._tsi.arm(frames_to_buffer=min(nframes, 16))
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="thorlabs-tsi-poll", daemon=True
        )
        self._poll_thread.start()

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
            except (usb.core.USBError, RuntimeError):
                break
            if frame is None:
                continue
            with self._buffer_lock:
                self._frames.append(frame.image)
                if len(self._frames) > self._frames_max:
                    self._frames = self._frames[-self._frames_max :]
                self._frame_notifier.inc()

    def _get_acquired_frames(self):
        return self._frame_notifier.counter

    def _wait_for_next_frame(self, timeout=20.0, idx=None):
        self._frame_notifier.wait(idx=idx, timeout=timeout)

    def _read_frames(self, rng, return_info=False):
        with self._buffer_lock:
            base = self._frame_notifier.counter - len(self._frames)
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

    def connect_device(self):
        self.device = ThorlabsTSICamera(serial=self.serial, pid=self.pid)

    def setup_task(self, serial=None, pid=None, remote=None, misc=None):  # pylint: disable=arguments-differ
        self.serial = serial
        self.pid = pid
        super().setup_task(remote=remote, misc=misc)


def _find_usb_devices():
    """Every connected USB device matching a known Thorlabs TSI PID."""
    return [
        dev
        for dev in usb.core.find(find_all=True, idVendor=protocol.VID)
        if dev.idProduct in protocol.KNOWN_PIDS
    ]


class ThorlabsTSIDirectCameraDescriptor(ICameraDescriptor):
    _cam_kind = "ThorlabsTSIDirect"

    @classmethod
    def iterate_cameras(cls, verbose=False):
        if verbose:
            print("Searching for Thorlabs TSI cameras (direct USB)")
        try:
            devices = _find_usb_devices()
        except usb.core.NoBackendError:
            if verbose:
                print(
                    "Error accessing USB: no libusb backend found; required for direct "
                    "USB access to Thorlabs TSI cameras\n"
                )
            if verbose == "full":
                cls.print_error()
            return
        if not devices:
            if verbose:
                print("Found no Thorlabs TSI cameras (direct USB)\n")
            return
        if verbose:
            print(
                "Found {} Thorlabs TSI camera{} (direct USB)".format(
                    len(devices), "s" if len(devices) > 1 else ""
                )
            )
        for dev in devices:
            transport = None
            try:
                transport = TsiCameraTransport(dev)
                try:
                    usb.util.claim_interface(dev, 0)
                except usb.core.USBError:
                    dev.set_configuration()
                    usb.util.claim_interface(dev, 0)
                serial = transport.command("sernum")
                if verbose:
                    print(f"Found Thorlabs TSI camera serial={serial}")
                yield transport, (dev.idProduct, serial)
            except usb.core.USBError:
                if verbose == "full":
                    cls.print_error()
            finally:
                if transport is not None:
                    transport.close()

    @classmethod
    def generate_description(cls, idx, cam=None, info=None):
        pid, serial = info
        cam_desc = cls.build_cam_desc(params={"serial": serial, "pid": pid})
        cam_desc["display_name"] = f"Thorlabs TSI {serial} (direct USB)"
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
