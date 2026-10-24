"""High-level camera API.

Naming mirrors the vendor's own Python reference (vendor/python_reference/tl_camera.py:
TLCameraSDK / TLCamera) so that existing user code built against the official SDK ports
with an import change.

The arm/stream/stop sequence follows what ThorCam actually does on the wire (protocol.md):
configure geometry and timing while stopped, ``arm`` then ``start``, read frames, ``stop``.
"""

from __future__ import annotations

import usb.core
import usb.util

from .commands import Commands
from .frames import (
    Frame,
    FrameAlignmentError,
    decode_frame,
    frame_chunks_on_wire,
)
from .protocol import (
    KNOWN_PIDS,
    PID_ZELUX,
    VID,
    CommandError,
    parse_frame_header,
)
from .stream import DEFAULT_QUEUE_DEPTH, FrameStream, RawFrame, StreamStats
from .transport import StreamDesyncError, TsiCameraTransport


class TsiCamera:
    def __init__(
        self, transport: TsiCameraTransport, synchronous_stream: bool = False
    ) -> None:
        self.transport = transport
        # Read the image endpoint with a plain read loop instead of libusb async transfers;
        # only meant for emulated devices (see ``fake.py``), which have no libusb handle.
        self._synchronous_stream = synchronous_stream
        self.commands = Commands(transport)
        self._armed = False
        # Cached per-arm so a frame read doesn't cost a round trip per property.
        self._geometry: tuple[int, int] | None = None
        self._calibration_lines = 0
        self._clock_hz: int | None = None
        # The reader thread for the current acquisition, and the counters from the last one
        # so stream_stats stays readable after disarm().
        self._stream: FrameStream | None = None
        self._last_stats: StreamStats | None = None

    @classmethod
    def open_camera(
        cls,
        pid: int | None = None,
        serial: str | None = None,
        read_burst_bytes: int | None = None,
        find=None,
        synchronous_stream: bool = False,
    ) -> TsiCamera:
        """Open the first matching camera.

        ``read_burst_bytes`` overrides how large a single DATA_IN_EP read asks for -- None
        keeps the default (see stream.READ_BURST_BYTES). Larger bursts mean fewer syscalls
        per frame; the reader thread makes this much less critical than it was when reads
        and decoding shared a thread.

        ``find`` and ``synchronous_stream`` exist for emulated devices; see
        ``TsiCameraTransport.open`` and ``__init__``.
        """
        kwargs = (
            {} if read_burst_bytes is None else {"read_burst_bytes": read_burst_bytes}
        )
        return cls(
            TsiCameraTransport.open(pid, serial, find=find, **kwargs),
            synchronous_stream=synchronous_stream,
        )

    def close(self) -> None:
        """Stop the camera if it is running, then release the USB interface.

        The stop is best-effort and must never prevent the release: if the camera has been
        unplugged, or answers `stop` with an error, letting that propagate would leak the
        interface claim and mask whatever exception was already unwinding the `with` block.
        """
        try:
            if self._armed:
                self.disarm()
        except (usb.core.USBError, CommandError):
            self._armed = False
        finally:
            self.transport.close()

    def __enter__(self) -> TsiCamera:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- identity ----------------------------------------------------------

    @property
    def serial_number(self) -> str:
        """The camera's serial number string."""
        return self.commands.get_serial_number()

    @property
    def model(self) -> str:
        """The camera's model string, e.g. ``"CS165CU"``."""
        return self.commands.get_model()

    @property
    def is_zelux(self) -> bool:
        """True on the Zelux family (CS165MU/CU); False on the CS-series."""
        return self.transport.pid == PID_ZELUX

    @property
    def is_color(self) -> bool:
        """True on a color body (e.g. CS165CU); False on mono (CS135MU, CS165MU, ...).

        A raw frame from a color camera is still the undemosaiced Bayer mosaic -- see
        ``color_filter_phase`` and ``frames.debayer_bilinear``. Displaying it directly (as
        the live-view scripts used to) shows the mosaic's per-pixel color-filter sensitivity
        as a fine checkerboard pattern, not a sensor fault.
        """
        return self.commands.is_color()

    @property
    def color_filter_phase(self) -> str | None:
        """Which Bayer color sits at the origin pixel of the full frame, or None if
        ``is_color`` is False. See ``frames.BAYER_PATTERNS``."""
        return self.commands.get_color_filter_phase()

    @property
    def name(self) -> str:
        """The camera's user-assigned display name (set via ThorCam; read-only here)."""
        return self.commands.get_name()

    @property
    def firmware_version(self) -> str:
        """The camera's firmware version string."""
        return self.commands.get_firmware_version()

    # -- settings ----------------------------------------------------------

    @property
    def exposure_time_us(self) -> int:
        """Exposure time in microseconds."""
        return self.commands.get_exposure_us()

    @exposure_time_us.setter
    def exposure_time_us(self, value: int) -> None:
        self.commands.set_exposure_us(value)

    @property
    def exposure_time_range_us(self) -> tuple[int, int]:
        """Current (min, max) exposure time in microseconds. Moves with frame-rate control
        and ROI -- query it again after changing either rather than caching it."""
        return self.commands.limits("exp")

    @property
    def frame_time_us(self) -> int:
        """Actual frame period in microseconds (read-only; follows exposure and ROI)."""
        return self.commands.get_frame_time_us()

    @property
    def sensor_readout_time_ns(self) -> int:
        """Sensor readout time. See ``Commands.get_sensor_readout_time_ns`` for the caveat
        on its unit, which is inherited from the vendor SDK's naming and not independently
        confirmed against this firmware."""
        return self.commands.get_sensor_readout_time_ns()

    @property
    def is_frame_rate_control_enabled(self) -> bool:
        """Whether frame-rate control (``sfr``) is on. While it is off, the frame period is
        the shortest the current ROI allows; turn it on before setting
        ``frame_rate_control_period_us``."""
        return self.commands.get_frame_rate_control()[0]

    @is_frame_rate_control_enabled.setter
    def is_frame_rate_control_enabled(self, value: bool) -> None:
        self.commands.set_frame_rate_control(value)

    @property
    def frame_rate_control_period_us(self) -> int:
        """The frame period frame-rate control is holding to, in microseconds.

        Deliberately **not** named ``frame_rate_control_value`` after the vendor SDK's own
        property: that one is a *frequency* in fps (``tl_camera_get/set_frame_rate_control_
        value``), while this firmware's ``sfr`` command is a *period* in microseconds --
        aliasing the two under one name would silently swap units for anyone porting vendor
        code. Convert with ``1e6 / period_us`` if you need fps.
        """
        return self.commands.get_frame_rate_control()[1]

    @frame_rate_control_period_us.setter
    def frame_rate_control_period_us(self, value: int) -> None:
        self.commands.set_frame_period_us(value)

    @property
    def frame_rate_control_period_range_us(self) -> tuple[int, int]:
        """Current (min, max) frame period in microseconds for frame-rate control. Moves
        with the ROI."""
        return self.commands.limits("sfr")

    @property
    def gain(self) -> int:
        """Raw gain, in the camera's own units."""
        return self.commands.get_gain()

    @gain.setter
    def gain(self, value: int) -> None:
        self.commands.set_gain(value)

    @property
    def gain_range(self) -> tuple[int, int]:
        """Current (min, max) for ``gain``, in the same raw units as ``gain`` itself --
        an index range on the CS-series, 0.1 dB units on the Zelux."""
        return self.commands.limits("gain")

    @property
    def black_level(self) -> int:
        """Black-level offset, in the camera's own units. Zelux only -- raises
        ``CommandError`` on the CS-series, which has no ``blklvl``."""
        return self.commands.get_black_level()

    @black_level.setter
    def black_level(self, value: int) -> None:
        self.commands.set_black_level(value)

    @property
    def black_level_range(self) -> tuple[int, int]:
        """Current (min, max) for ``black_level``. Zelux only; see ``black_level``."""
        return self.commands.limits("blklvl")

    @property
    def bit_depth(self) -> int:
        """Bits per pixel the sensor reports (10 on both families)."""
        return self.commands.get_pixel_bit_depth()

    @property
    def frames_per_trigger_zero_for_unlimited(self) -> int:
        """Frames captured per trigger; 0 means continuous."""
        return self.commands.get_frames_per_trigger()

    @frames_per_trigger_zero_for_unlimited.setter
    def frames_per_trigger_zero_for_unlimited(self, value: int) -> None:
        self.commands.set_frames_per_trigger(value)

    @property
    def frames_per_trigger_range(self) -> tuple[int, int]:
        """Current (min, max) for ``frames_per_trigger_zero_for_unlimited``."""
        return self.commands.limits("nframes")

    @property
    def roi(self) -> tuple[int, int, int, int]:
        """ROI as (x0, y0, x1, y1) in sensor pixels, both corners inclusive."""
        return self.commands.get_roi()

    @roi.setter
    def roi(self, box: tuple[int, int, int, int]) -> None:
        self.commands.set_roi(*box)

    @property
    def roi_range(self) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
        """(min box, max box) for ``roi``, each an (x0, y0, x1, y1) inclusive box."""
        return self.commands.get_roi_range()

    @property
    def binx(self) -> int:
        """Horizontal binning factor; 1 on cameras without on-camera binning (the Zelux
        bins on the host instead). Set *before* ``roi`` -- see ``commands.set_binx``."""
        return self.commands.get_binx()

    @binx.setter
    def binx(self, value: int) -> None:
        self.commands.set_binx(value)

    @property
    def binx_range(self) -> tuple[int, int]:
        """Current (min, max) for ``binx``. (1, 1) on cameras without on-camera binning."""
        try:
            return self.commands.limits("hbin")
        except CommandError:
            return 1, 1

    @property
    def biny(self) -> int:
        """Vertical binning factor; 1 on cameras without on-camera binning. See ``binx``."""
        return self.commands.get_biny()

    @biny.setter
    def biny(self, value: int) -> None:
        self.commands.set_biny(value)

    @property
    def biny_range(self) -> tuple[int, int]:
        """Current (min, max) for ``biny``. (1, 1) on cameras without on-camera binning."""
        try:
            return self.commands.limits("vbin")
        except CommandError:
            return 1, 1

    @property
    def image_size(self) -> tuple[int, int]:
        """(width, height) of the decoded image, after ROI and binning."""
        return self.commands.get_image_size()

    @property
    def image_width_pixels(self) -> int:
        """Width of the decoded image, after ROI and binning. See ``image_size``."""
        return self.image_size[0]

    @property
    def image_height_pixels(self) -> int:
        """Height of the decoded image, after ROI and binning. See ``image_size``."""
        return self.image_size[1]

    @property
    def is_armed(self) -> bool:
        """Whether ``arm()`` has been called without a matching ``disarm()``.

        Host-side state, mirrored from the sequence this driver itself drove -- not a fresh
        query of the camera's own ``state``.
        """
        return self._armed

    # -- streaming ---------------------------------------------------------

    def arm(self, frames_to_buffer: int = DEFAULT_QUEUE_DEPTH, tap=None) -> None:
        """Arm and start the camera, and begin continuously draining the image endpoint.

        ``frames_to_buffer`` is how many whole frames the reader thread may keep queued for
        the consumer. It is a real setting here, not the no-op it was: the reader never
        blocks on the consumer, so this is the amount of slack a slow decode gets before the
        oldest queued frame is dropped (and counted in ``stream_stats``).

        The endpoint is flushed *before* ``start``: whatever the previous session left behind
        would otherwise be handed to the first read of this one as the tail of somebody
        else's frame.

        Frames-per-trigger is reset to 0 (continuous) first if a previous session left it
        set, since a non-zero value would end this acquisition after that many frames.
        """
        if self._armed or self._stream is not None:
            raise RuntimeError("camera is already armed")
        try:
            if self.commands.get_frames_per_trigger() != 0:
                self.commands.set_frames_per_trigger(0)
        except CommandError:
            pass  # not every firmware exposes nframes; nothing to reset then
        self._geometry = self.commands.get_image_size()
        self._calibration_lines = self.commands.get_calibration_lines()
        self._clock_hz = self.commands.get_timestamp_clock_hz()

        width, height = self._geometry
        expected_chunks = frame_chunks_on_wire(
            width, height, self._calibration_lines, is_zelux=self.is_zelux
        )

        self.commands.arm()
        # From here on the camera may be armed or running, so any failure must still send
        # `stop` -- close() only does that while _armed is set.
        self._armed = True
        try:
            self.transport.flush_data_endpoint()
            self.commands.start()
            stream = FrameStream(
                self.transport.dev,
                expected_chunks=expected_chunks,
                frame_count_of=_frame_count_of,
                read_burst_bytes=self.transport.read_burst_bytes,
                queue_depth=max(1, frames_to_buffer),
                synchronous=self._synchronous_stream,
                tap=tap,
            )
            stream.start()
            self._stream = stream
        except BaseException:
            try:
                self.disarm()
            except (usb.core.USBError, CommandError):
                self._armed = False
                self._geometry = None
            raise

    def issue_software_trigger(self) -> None:
        """Start an acquisition on an armed camera.

        There is no separate software-trigger command in either firmware's vocabulary --
        ``start`` is what ThorCam sends -- so arm() already starts the stream and this is a
        no-op kept for API compatibility.
        """
        if not self._armed:
            raise RuntimeError("camera is not armed")

    def disarm(self) -> None:
        """Stop the camera first, then the reader, then clear the endpoint.

        The order is load-bearing and it is the opposite of the obvious one. Stopping the
        reader first leaves the camera free-running into a FIFO nobody is draining, and a
        CS165CU answers a command sent in that state by stalling its command endpoint --
        after which the device needs a physical replug. Issuing ``stop`` while the reader is
        still draining keeps the data path moving across the transition, so the camera is
        never asked to accept a command while backed up.
        """
        stream, self._stream = self._stream, None
        try:
            # The reader is deliberately still running at this point -- see above.
            self.commands.stop()
        finally:
            if stream is not None:
                stream.stop()
                self._last_stats = stream.stats
            self._armed = False
            self._geometry = None
            try:
                self.transport.flush_data_endpoint()
            except usb.core.USBError:
                pass

    @property
    def stream_stats(self) -> StreamStats | None:
        """Counters for the current (or most recent) acquisition. See stream.StreamStats.

        Worth reading after any run: ``frames_missed`` and ``frames_truncated`` are the
        camera dropping data because the host fell behind, while
        ``frames_dropped_by_consumer`` is this process's own queue overflowing. They are
        different problems with different fixes, so they are counted separately.
        """
        if self._stream is not None:
            return self._stream.stats
        return self._last_stats

    # -- frame access ------------------------------------------------------

    def get_pending_raw_frame(self, timeout: float = 1.0) -> RawFrame | None:
        """Next whole frame off the queue, undecoded, or None if none arrived in time."""
        if self._stream is None:
            raise RuntimeError("camera is not armed")
        return self._stream.next_frame(timeout)

    def get_pending_frame_raw_bytes(self, timeout: float = 1.0) -> bytes | None:
        """Next frame's raw, undecoded wire bytes, or None if none arrived in time."""
        frame = self.get_pending_raw_frame(timeout)
        return None if frame is None else frame.data

    def get_pending_frame_or_null(
        self, order: str | None = None, timeout: float = 1.0
    ) -> Frame | None:
        """Decode the next queued frame, or None if none arrived before ``timeout``.

        Frames come from the reader thread's queue, so the time spent decoding here does not
        stall the USB endpoint -- which is the whole reason the effective frame rate used to
        collapse to a fraction of what the camera was producing.

        ``order`` overrides the pixel byte layout; leave it as the default ``None`` so
        ``decode_frame`` picks the one confirmed correct for this camera's own family (see
        ``frames.ORDER_BY_FAMILY``) rather than assuming one for every camera.
        """
        geometry = self._geometry  # read once: disarm() on another thread clears it
        raw = self.get_pending_raw_frame(timeout)
        if raw is None or geometry is None:
            return None

        width, height = geometry
        try:
            return decode_frame(
                raw.data,
                width,
                height,
                clock_frequency_hz=self._clock_hz,
                calibration_lines=self._calibration_lines,
                order=order,
            )
        except FrameAlignmentError as exc:
            # The assembler only emits frames whose chunk indices ran 0..N-1, so this should
            # not happen. If it does, it is a decoding-side disagreement about the frame's
            # shape, not a lost stream position: the next frame comes from the queue already
            # aligned, so there is nothing to resynchronise and the run continues.
            raise StreamDesyncError(str(exc)) from exc


def discover_available_cameras(
    pid: int | None = None, find=None
) -> list[tuple[int, str, str]]:
    """List every connected TSI camera as (pid, serial_number, model).

    There is no cheaper way to get a serial number than this: nothing else in this driver
    reads the USB string descriptors, and the only place a serial is ever known is over the
    ASCII command channel -- the same ``sernum`` round trip ``TsiCameraTransport.open()``
    already does when it is asked to find one specific camera by serial. This function does
    that for every matching device instead of stopping at the first one, and releases each
    interface again before moving to the next, so nothing is left claimed on return.

    A camera already claimed by another process or session is skipped rather than raising --
    multi-camera discovery should not fail just because one candidate is busy. The same goes
    for one that refuses the identity queries (``CommandError``).

    ``find`` replaces ``usb.core.find``, e.g. to hand in emulated devices (see ``fake.py``).
    """
    find = usb.core.find if find is None else find
    found: list[tuple[int, str, str]] = []
    candidates = [
        dev
        for dev in find(find_all=True, idVendor=VID)
        if dev.idProduct == pid or (pid is None and dev.idProduct in KNOWN_PIDS)
    ]
    for dev in candidates:
        transport = None
        try:
            usb.util.claim_interface(dev, 0)
            transport = TsiCameraTransport(dev)
            serial = transport.command("sernum")
            model = transport.command("model")
            found.append((dev.idProduct, serial, model))
        except (usb.core.USBError, CommandError):
            continue
        finally:
            if transport is not None:
                transport.close()
            else:
                try:
                    usb.util.dispose_resources(dev)
                except Exception:
                    pass
    return found


def _frame_count_of(data: bytes) -> int | None:
    header = parse_frame_header(data[:64])
    return None if header is None else header["frame_count"]
