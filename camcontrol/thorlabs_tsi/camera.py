"""High-level camera API.

Naming mirrors the vendor's own Python reference (vendor/python_reference/tl_camera.py:
TLCameraSDK / TLCamera) so that existing user code built against the official SDK ports
with an import change.

The arm/stream/stop sequence follows what ThorCam actually does on the wire (protocol.md):
configure geometry and timing while stopped, ``arm`` then ``start``, read frames, ``stop``.
"""

from __future__ import annotations

import usb.core

from .commands import Commands
from .frames import (
    Frame,
    FrameAlignmentError,
    decode_frame,
    frame_chunks_on_wire,
)
from .protocol import (
    PID_ZELUX,
    CommandError,
    parse_frame_header,
)
from .stream import DEFAULT_QUEUE_DEPTH, FrameStream, RawFrame, StreamStats
from .transport import StreamDesyncError, TsiCameraTransport


class TsiCamera:
    def __init__(self, transport: TsiCameraTransport) -> None:
        self.transport = transport
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
    ) -> TsiCamera:
        """Open the first matching camera.

        ``read_burst_bytes`` overrides how large a single DATA_IN_EP read asks for -- None
        keeps the default (see stream.READ_BURST_BYTES). Larger bursts mean fewer syscalls
        per frame; the reader thread makes this much less critical than it was when reads
        and decoding shared a thread.
        """
        kwargs = (
            {} if read_burst_bytes is None else {"read_burst_bytes": read_burst_bytes}
        )
        return cls(TsiCameraTransport.open(pid, serial, **kwargs))

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

    # -- settings ----------------------------------------------------------

    @property
    def exposure_time_us(self) -> int:
        """Exposure time in microseconds."""
        return self.commands.get_exposure_us()

    @exposure_time_us.setter
    def exposure_time_us(self, value: int) -> None:
        self.commands.set_exposure_us(value)

    @property
    def gain(self) -> int:
        """Raw gain, in the camera's own units."""
        return self.commands.get_gain()

    @gain.setter
    def gain(self, value: int) -> None:
        self.commands.set_gain(value)

    @property
    def frames_per_trigger_zero_for_unlimited(self) -> int:
        """Frames captured per trigger; 0 means continuous."""
        return self.commands.get_frames_per_trigger()

    @frames_per_trigger_zero_for_unlimited.setter
    def frames_per_trigger_zero_for_unlimited(self, value: int) -> None:
        self.commands.set_frames_per_trigger(value)

    @property
    def roi(self) -> tuple[int, int, int, int]:
        """ROI as (x0, y0, x1, y1) in sensor pixels, both corners inclusive."""
        return self.commands.get_roi()

    @roi.setter
    def roi(self, box: tuple[int, int, int, int]) -> None:
        self.commands.set_roi(*box)

    @property
    def image_size(self) -> tuple[int, int]:
        """(width, height) of the decoded image, after ROI and binning."""
        return self.commands.get_image_size()

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
        """
        self._geometry = self.commands.get_image_size()
        self._calibration_lines = self.commands.get_calibration_lines()
        self._clock_hz = self.commands.get_timestamp_clock_hz()

        width, height = self._geometry
        expected_chunks = frame_chunks_on_wire(
            width, height, self._calibration_lines, is_zelux=self.is_zelux
        )

        self.commands.arm()
        self.transport.flush_data_endpoint()
        self.commands.start()
        self._armed = True

        self._stream = FrameStream(
            self.transport.dev,
            expected_chunks=expected_chunks,
            frame_count_of=_frame_count_of,
            read_burst_bytes=self.transport.read_burst_bytes,
            queue_depth=max(1, frames_to_buffer),
            tap=tap,
        )
        self._stream.start()

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
        raw = self.get_pending_raw_frame(timeout)
        if raw is None:
            return None

        width, height = self._geometry
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


def _frame_count_of(data: bytes) -> int | None:
    header = parse_frame_header(data[:64])
    return None if header is None else header["frame_count"]
