"""Protocol-level emulation of a Thorlabs TSI camera, for running the real driver without one.

``FakeTsiDevice`` stands in for the ``usb.core.Device`` the driver talks to. It answers the
ASCII command channel the way the firmware does (stateful settings, in-band errors, values
snapped to what the camera supports) and streams frames on the data endpoint in the exact
wire format of either family -- chunk markers, headers, footer, 10-bit packing -- so that
everything above it runs unmodified: ``TsiCameraTransport``, ``Commands``, ``FrameStream``'s
chunk assembler, ``decode_frame`` and ``TsiCamera``, up to the app's ``ThorlabsTSIDirect``
backend.

What it does *not* emulate is libusb itself: ``AsyncBulkReader`` needs a real libusb handle,
so a ``TsiCamera`` on a fake device must use ``synchronous_stream=True``.

The two families follow what protocol.md records for the Zelux (CS165MU/CU, PID 0x4002) and
the CS-series (CS135MU, PID 0x4001). Behaviour nobody has measured (exact error codes,
readout timings, limits of minor commands) is a plausible stand-in, not a claim about the
hardware. ``FakeFaults`` injects the failure modes the driver is built to survive.

Typical use::

    dev = FakeTsiDevice(family="cs", color=True)
    cam = TsiCamera.open_camera(find=FakeTsiBus([dev]).find, synchronous_stream=True)
"""

from __future__ import annotations

import array
import errno
import struct
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import usb.core

from .protocol import (
    CMD_IN_EP,
    CMD_OUT_EP,
    CS_CHUNK_MARKER_BYTES,
    CS_FOOTER_SENTINEL,
    CS_SIGNATURE,
    DATA_CHUNK_BYTES,
    DATA_IN_EP,
    FRAME_MAGIC,
    PID_CS_SERIES,
    PID_ZELUX,
    TAG_END,
    TAG_FRAME_COUNT,
    TAG_IMAGE_FORMAT,
    TAG_IMAGE_OFFSET,
    TAG_PIXEL_CLOCK_HIGH,
    TAG_PIXEL_CLOCK_LOW,
    VID,
    ZELUX_CHUNK_MARKER_BYTES,
    decode_command_frame,
    encode_command_frame,
)

PIXEL_MAX = (1 << 10) - 1


# ---------------------------------------------------------------------------
# 10-bit packing (the inverse of frames.unpack_pixels)
# ---------------------------------------------------------------------------


def pack_lsb_last(pixels: np.ndarray) -> bytes:
    """Pack (height, width) 10-bit pixels the Zelux way: four high bytes, then one byte
    holding the four 2-bit low parts (pixel 0 in the lowest bits)."""
    p = _groups(pixels)
    out = np.empty(p.shape[:2] + (5,), dtype=np.uint8)
    out[:, :, :4] = p >> 2
    out[:, :, 4] = (
        (p[:, :, 0] & 3) | (p[:, :, 1] & 3) << 2 | (p[:, :, 2] & 3) << 4 | (p[:, :, 3] & 3) << 6
    )
    return out.tobytes()


def pack_contiguous(pixels: np.ndarray) -> bytes:
    """Pack (height, width) 10-bit pixels the CS-series way: pixel k is bits [10k, 10k+10)
    of the row, least significant bit first."""
    p = _groups(pixels)
    p0, p1, p2, p3 = (p[:, :, i] for i in range(4))
    out = np.empty(p.shape[:2] + (5,), dtype=np.uint8)
    out[:, :, 0] = p0 & 0xFF
    out[:, :, 1] = (p0 >> 8) | (p1 & 0x3F) << 2
    out[:, :, 2] = (p1 >> 6) | (p2 & 0x0F) << 4
    out[:, :, 3] = (p2 >> 4) | (p3 & 0x03) << 6
    out[:, :, 4] = p3 >> 2
    return out.tobytes()


def _groups(pixels: np.ndarray) -> np.ndarray:
    height, width = pixels.shape
    if width % 4:
        raise ValueError(f"width {width} is not a multiple of 4")
    return (np.asarray(pixels, dtype=np.uint16) & PIXEL_MAX).reshape(height, width // 4, 4)


# ---------------------------------------------------------------------------
# Camera models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FamilySpec:
    """What differs between the two firmware families (and between sensor sizes)."""

    pid: int
    model: str
    sensor: tuple[int, int]  # (width, height)
    min_roi: tuple[int, int]  # (width, height)
    width_step: int
    clock_hz: int
    calibration_lines: int  # 0: the camera answers num_calibration_lines with an error
    gain_table: tuple[int, ...] | None  # 0.1 dB per step; None: gain is directly 0.1 dB
    gain_max: int
    max_bin: int  # 1: no hbin/vbin commands
    has_black_level: bool
    row_time_us: float
    errors: dict[str, int] = field(default_factory=dict)


_ZELUX_ERRORS = {"Invalid command": 1, "Camera Running": 22, "Command not enabled": 18}
_CS_ERRORS = {"Invalid command": 1, "Camera Running": 25, "Command not enabled": 21}

FAMILIES = {
    "zelux": FamilySpec(
        pid=PID_ZELUX,
        model="CS165MU",
        sensor=(1440, 1080),
        min_roi=(80, 4),
        width_step=4,
        clock_hz=1000,
        calibration_lines=6,
        gain_table=None,
        gain_max=480,
        max_bin=1,
        has_black_level=True,
        row_time_us=26.6,
        errors=_ZELUX_ERRORS,
    ),
    "cs": FamilySpec(
        pid=PID_CS_SERIES,
        model="CS135MU",
        sensor=(1280, 1024),
        min_roi=(16, 2),
        width_step=16,
        clock_hz=72_000_000,
        calibration_lines=0,
        gain_table=(-75, -56, 0, 53),
        gain_max=3,
        max_bin=16,
        has_black_level=False,
        row_time_us=10.0,
        errors=_CS_ERRORS,
    ),
}


@dataclass
class FakeFaults:
    """Failure modes to inject. ``every`` counts are in emitted frames; 0 disables."""

    #: drop one chunk from the middle of every Nth frame (the frame arrives incomplete)
    drop_chunk_every: int = 0
    #: cut every Nth frame off after half its chunks (what a camera does when not drained)
    truncate_every: int = 0
    #: send this many bytes of non-chunk junk before every Nth frame (needs a realign)
    junk_every: int = 0
    junk_bytes: int = 5000
    #: skip a frame-counter value every Nth frame (a frame the camera never sent)
    counter_gap_every: int = 0
    #: the data endpoint stalls (EPIPE) on every Nth frame, until clear_halt
    stall_every: int = 0
    #: bytes of trailing non-chunk data after every frame
    trailer_bytes: int = 0
    #: command name -> error text the camera answers that command with
    command_errors: dict[str, str] = field(default_factory=dict)
    #: command names whose reply only arrives after the host's read has timed out
    late_replies: frozenset[str] = frozenset()
    #: every endpoint fails with ENODEV after this many frames (the camera was unplugged)
    disconnect_after_frames: int = 0

    @classmethod
    def from_dict(cls, value: dict | None) -> FakeFaults:
        """Build from a plain dict (e.g. a settings-file section)."""
        value = dict(value or {})
        if "late_replies" in value:
            value["late_replies"] = frozenset(value["late_replies"])
        return cls(**value)


class _Ctx:
    """Stand-in for the pyusb device context that ``usb.util`` calls into."""

    def __init__(self, dev: FakeTsiDevice) -> None:
        self._dev = dev

    def managed_claim_interface(self, device, interface) -> None:
        self._dev._check_connected()
        with self._dev._lock:
            if self._dev.claimed:
                raise usb.core.USBError("Resource busy", errno=errno.EBUSY)
            self._dev.claimed = True

    def managed_release_interface(self, device, interface) -> None:
        with self._dev._lock:
            self._dev.claimed = False

    def dispose(self, device) -> None:
        with self._dev._lock:
            self._dev.claimed = False


class _CameraError(Exception):
    """An in-band error reply: ``error <code>,<text>``."""


class FakeTsiDevice:
    """An emulated Thorlabs TSI camera behind the pyusb ``Device`` interface the driver uses.

    Args:
        family: ``"zelux"`` or ``"cs"``
        serial: serial number string
        model: model string; defaults to the family's (with a ``C`` instead of ``M`` for
            ``color=True``)
        sensor: sensor (width, height); defaults to the family's
        color: Bayer color sensor (``color_filter_phase`` "red", i.e. an RGGB tile)
        faults: failure injection, see :class:`FakeFaults`
        command_wait_s: upper bound on how long a command-channel read waits before timing
            out (the real timeout is seconds; nothing is gained by waiting that long here)
        seed: noise seed
    """

    def __init__(
        self,
        family: str = "zelux",
        serial: str = "00001",
        model: str | None = None,
        sensor: tuple[int, int] | None = None,
        color: bool = False,
        faults: FakeFaults | None = None,
        command_wait_s: float = 0.05,
        seed: int = 0,
    ) -> None:
        if family not in FAMILIES:
            raise ValueError(f"unknown family {family!r}; expected one of {list(FAMILIES)}")
        self.family = family
        self.spec = FAMILIES[family]
        self.idVendor = VID
        self.idProduct = self.spec.pid
        self.serial = serial
        self.model = model or (self.spec.model.replace("MU", "CU") if color else self.spec.model)
        self.sensor = tuple(sensor or self.spec.sensor)
        self.color = color
        self.faults = faults or FakeFaults()
        self.command_wait_s = command_wait_s
        self._rng_seed = seed
        self._ctx = _Ctx(self)
        self._lock = threading.RLock()
        self.claimed = False
        self.connected = True
        self.commands_received: list[str] = []
        # command channel
        self._replies: list[tuple[bytes, bool]] = []  # (frame, delay the first read)
        # settings
        width, height = self.sensor
        self.state = "stopped"
        self.exposure_us = 10_000
        self.gain = 0
        self.black_level = 12 if self.spec.has_black_level else 0
        self.sfr_on = False
        self.sfr_period_us = 100_000
        self.nframes = 0
        self.hbin = self.vbin = 1
        self.roi = (0, 0, width - 1, height - 1)
        # streaming
        self._data = bytearray()
        self._data_stalled = False
        self._t_start = 0.0
        self._next_due = 0.0
        self._frame_counter = 0
        self.frames_emitted = 0
        self._emitted_this_run = 0
        self._grid_cache: dict = {}

    # -- pyusb Device interface ------------------------------------------------

    def write(self, endpoint: int, data, timeout: int | None = None) -> int:
        self._check_connected()
        if endpoint != CMD_OUT_EP:
            raise usb.core.USBError("Pipe error", errno=errno.EPIPE)
        command = decode_command_frame(bytes(data))
        with self._lock:
            self.commands_received.append(command)
            try:
                reply = self._handle(command)
            except _CameraError as err:
                reply = str(err)
            name = command.split(" ")[0].split(".")[0]
            self._replies.append((encode_command_frame(reply), name in self.faults.late_replies))
        return len(data)

    def read(self, endpoint: int, size_or_buffer, timeout: int | None = None):
        self._check_connected()
        timeout_s = (timeout or 1000) / 1000
        if endpoint == CMD_IN_EP:
            return self._read_reply(size_or_buffer, timeout_s)
        if endpoint == DATA_IN_EP:
            return self._read_data(size_or_buffer, timeout_s)
        raise usb.core.USBError("Pipe error", errno=errno.EPIPE)

    def clear_halt(self, endpoint: int) -> None:
        self._check_connected()
        if endpoint == DATA_IN_EP:
            with self._lock:
                self._data_stalled = False

    def set_configuration(self, configuration=None) -> None:
        self._check_connected()

    def reset(self) -> None:
        self._check_connected()

    # -- command channel ---------------------------------------------------------

    def _read_reply(self, size_or_buffer, timeout_s: float):
        with self._lock:
            if self._replies:
                frame, delayed = self._replies[0]
                if delayed:
                    self._replies[0] = (frame, False)
                else:
                    self._replies.pop(0)
                    return self._deliver(frame, size_or_buffer)
        time.sleep(min(timeout_s, self.command_wait_s))
        raise usb.core.USBTimeoutError("Operation timed out", errno=errno.ETIMEDOUT)

    def _error(self, text: str) -> _CameraError:
        return _CameraError(f"error {self.spec.errors.get(text, 99)},{text}")

    def _handle(self, command: str) -> str:
        name, _, arg = command.partition(" ")
        base, _, suffix = name.partition(".")
        if base in self.faults.command_errors:
            raise self._error(self.faults.command_errors[base])
        handler = getattr(self, f"_cmd_{base}", None)
        if handler is None:
            raise self._error("Invalid command")
        result = handler(suffix, arg.strip())
        return str(result)

    def _require_stopped(self) -> None:
        if self.state != "stopped":
            raise self._error("Camera Running")

    def _int_setting(self, suffix, arg, lo, hi, get, set_, live=True):
        if suffix == "min":
            return lo
        if suffix == "max":
            return hi
        if suffix:
            raise self._error("Invalid command")
        if arg:
            if not live:
                self._require_stopped()
            set_(int(np.clip(int(arg), lo, hi)))
        return get()

    def _readonly(self, suffix, arg, value):
        if suffix or arg:
            raise self._error("Invalid command")
        return value

    def _cmd_discover(self, suffix, arg):
        names = [n[len("_cmd_") :] for n in dir(self) if n.startswith("_cmd_")]
        if not self.spec.has_black_level:
            names.remove("blklvl")
        if self.spec.max_bin == 1:
            names = [n for n in names if n not in ("hbin", "vbin")]
        return " ".join(sorted(names))

    def _cmd_sernum(self, suffix, arg):
        return self._readonly(suffix, arg, self.serial)

    def _cmd_model(self, suffix, arg):
        return self._readonly(suffix, arg, self.model)

    def _cmd_name(self, suffix, arg):
        return self._readonly(suffix, arg, f"Fake {self.model}")

    def _cmd_ver(self, suffix, arg):
        return self._readonly(suffix, arg, "fake-1.0.0")

    def _cmd_state(self, suffix, arg):
        return self._readonly(suffix, arg, self.state)

    def _cmd_color(self, suffix, arg):
        return self._readonly(suffix, arg, "bayer" if self.color else "none")

    def _cmd_color_filter_phase(self, suffix, arg):
        if not self.color:
            raise self._error("Invalid command")
        return self._readonly(suffix, arg, "red")

    def _cmd_pixel_bit_depth(self, suffix, arg):
        return self._readonly(suffix, arg, 10)

    def _cmd_pixel_clock_count_base(self, suffix, arg):
        return self._readonly(suffix, arg, self.spec.clock_hz)

    def _cmd_num_calibration_lines(self, suffix, arg):
        if not self.spec.calibration_lines:
            raise self._error("Invalid command")
        return self._readonly(suffix, arg, self.spec.calibration_lines)

    def _cmd_image_width(self, suffix, arg):
        return self._readonly(suffix, arg, self.image_size[0])

    def _cmd_image_height(self, suffix, arg):
        return self._readonly(suffix, arg, self.image_size[1])

    def _cmd_sensor_readout_time(self, suffix, arg):
        return self._readonly(suffix, arg, int(self.readout_us * 1000))

    def _cmd_frametime(self, suffix, arg):
        return self._readonly(suffix, arg, int(self.frame_period_us))

    def _cmd_exp(self, suffix, arg):
        def set_(v):
            self.exposure_us = v

        return self._int_setting(suffix, arg, 40, 20_000_000, lambda: self.exposure_us, set_)

    def _cmd_gain(self, suffix, arg):
        if suffix == "table":
            if self.spec.gain_table is None:
                raise self._error("Invalid command")
            return " ".join(str(v) for v in self.spec.gain_table)

        def set_(v):
            self.gain = v

        return self._int_setting(suffix, arg, 0, self.spec.gain_max, lambda: self.gain, set_)

    def _cmd_blklvl(self, suffix, arg):
        if not self.spec.has_black_level:
            raise self._error("Invalid command")

        def set_(v):
            self.black_level = v

        return self._int_setting(suffix, arg, 0, 100, lambda: self.black_level, set_)

    def _cmd_nframes(self, suffix, arg):
        def set_(v):
            self.nframes = v

        return self._int_setting(suffix, arg, 0, 1_000_000, lambda: self.nframes, set_, live=False)

    def _cmd_sfr(self, suffix, arg):
        natural = int(self.natural_period_us)
        if suffix == "min":
            return natural
        if suffix == "max":
            return 10_000_000
        if suffix:
            raise self._error("Invalid command")
        if arg in ("on", "off"):
            self.sfr_on = arg == "on"
        elif arg:
            if not self.sfr_on:
                raise self._error("Command not enabled")
            self.sfr_period_us = int(np.clip(int(arg), natural, 10_000_000))
            return self.sfr_period_us
        return f"{'on' if self.sfr_on else 'off'} {int(self.frame_period_us)}"

    def _bin_command(self, suffix, arg, axis):
        if self.spec.max_bin == 1:
            raise self._error("Invalid command")

        def get():
            return self.hbin if axis == "h" else self.vbin

        def set_(v):
            if axis == "h":
                self.hbin = v
            else:
                self.vbin = v
            self.roi = self._snap_roi(*self.roi)

        return self._int_setting(suffix, arg, 1, self.spec.max_bin, get, set_, live=False)

    def _cmd_hbin(self, suffix, arg):
        return self._bin_command(suffix, arg, "h")

    def _cmd_vbin(self, suffix, arg):
        return self._bin_command(suffix, arg, "v")

    def _cmd_roi(self, suffix, arg):
        width, height = self.sensor
        min_w, min_h = self.spec.min_roi
        if suffix == "min":
            return f"0 0 {min_w - 1} {min_h - 1}"
        if suffix == "max":
            # each field is the independent maximum of that argument (see protocol.md)
            return f"{width - min_w} {height - min_h} {width - 1} {height - 1}"
        if suffix:
            raise self._error("Invalid command")
        if arg:
            self._require_stopped()
            x0, y0, x1, y1 = (int(v) for v in arg.split())
            self.roi = self._snap_roi(x0, y0, x1, y1)
        return " ".join(str(v) for v in self.roi)

    def _snap_roi(self, x0, y0, x1, y1):
        """Clamp and quantise an inclusive ROI box the way the camera does."""
        width, height = self.sensor
        min_w, min_h = self.spec.min_roi
        x0, x1 = sorted((int(np.clip(x0, 0, width - 1)), int(np.clip(x1, 0, width - 1))))
        y0, y1 = sorted((int(np.clip(y0, 0, height - 1)), int(np.clip(y1, 0, height - 1))))
        wstep = self.spec.width_step * self.hbin
        w = max(min_w, x1 - x0 + 1)
        w = max(wstep, w // wstep * wstep)
        h = max(min_h, y1 - y0 + 1)
        h = max(self.vbin, h // self.vbin * self.vbin)
        x0 = min(x0, width - w)
        y0 = min(y0, height - h)
        return x0, y0, x0 + w - 1, y0 + h - 1

    def _cmd_arm(self, suffix, arg):
        self._readonly(suffix, arg, None)
        if self.state != "stopped":
            raise self._error("Camera Running")
        self.state = "armed"
        return "armed"

    def _cmd_disarm(self, suffix, arg):
        self._readonly(suffix, arg, None)
        self.state = "stopped"
        return "disarmed"

    def _cmd_start(self, suffix, arg):
        self._readonly(suffix, arg, None)
        if self.state != "armed":
            raise self._error("Command not enabled")
        self.state = "running"
        self._t_start = time.monotonic()
        self._next_due = self._t_start + self.frame_period_us * 1e-6
        self._emitted_this_run = 0
        return "running"

    def _cmd_stop(self, suffix, arg):
        self._readonly(suffix, arg, None)
        self.state = "stopped"
        return "stopped"

    # -- derived geometry and timing ---------------------------------------------

    @property
    def roi_size(self) -> tuple[int, int]:
        x0, y0, x1, y1 = self.roi
        return x1 - x0 + 1, y1 - y0 + 1

    @property
    def image_size(self) -> tuple[int, int]:
        """(width, height) of the frames sent, after binning."""
        w, h = self.roi_size
        return w // self.hbin, h // self.vbin

    @property
    def readout_us(self) -> float:
        return self.roi_size[1] * self.spec.row_time_us

    @property
    def natural_period_us(self) -> float:
        """Shortest frame period at the current settings."""
        return max(self.exposure_us, self.readout_us) + 50

    @property
    def frame_period_us(self) -> float:
        natural = self.natural_period_us
        return max(natural, self.sfr_period_us) if self.sfr_on else natural

    # -- data endpoint -------------------------------------------------------------

    def _read_data(self, size_or_buffer, timeout_s: float):
        deadline = time.monotonic() + timeout_s
        while True:
            with self._lock:
                if self._data_stalled:
                    raise usb.core.USBError("Pipe error", errno=errno.EPIPE)
                self._produce_due_frames()
                self._check_connected()
                if self._data_stalled:
                    raise usb.core.USBError("Pipe error", errno=errno.EPIPE)
                if self._data:
                    size = (
                        size_or_buffer if isinstance(size_or_buffer, int) else len(size_or_buffer)
                    )
                    chunk = bytes(self._data[:size])
                    del self._data[:size]
                    return self._deliver(chunk, size_or_buffer)
                next_due = self._next_due if self.state == "running" else None
            now = time.monotonic()
            if now >= deadline:
                raise usb.core.USBTimeoutError("Operation timed out", errno=errno.ETIMEDOUT)
            wake = deadline if next_due is None else min(deadline, max(next_due, now + 1e-4))
            time.sleep(max(0.0, wake - now))

    @staticmethod
    def _deliver(data: bytes, size_or_buffer):
        """Return ``data`` the way pyusb's ``read`` does for an int size or a buffer."""
        if isinstance(size_or_buffer, int):
            return array.array("B", data[:size_or_buffer])
        n = min(len(data), len(size_or_buffer))
        memoryview(size_or_buffer).cast("B")[:n] = data[:n]
        return n

    def _produce_due_frames(self) -> None:
        """Append every frame that is due by now to the data buffer.

        Like the camera's own FIFO, only ~100 ms worth of frames are held for a host that
        isn't reading: older frames are dropped and their counter values skipped, which the
        host sees as frames missed.
        """
        if self.state != "running":
            return
        now = time.monotonic()
        period = self.frame_period_us * 1e-6
        due_times = []
        while self._next_due <= now:
            due_times.append(self._next_due)
            self._next_due += period
        if self.nframes:
            due_times = due_times[: max(0, self.nframes - self._emitted_this_run)]
        if not due_times:
            return
        max_backlog = max(4, int(0.1 / period))
        if len(due_times) > max_backlog:
            self._frame_counter += len(due_times) - max_backlog
            due_times = due_times[-max_backlog:]
        for due in due_times:
            self._emit_frame(due)
            if self._data_stalled:
                break
        if self.nframes and self._emitted_this_run >= self.nframes:
            self.state = "armed"  # this trigger's frames are done

    def _emit_frame(self, due: float) -> None:
        faults = self.faults
        n = self.frames_emitted + 1  # 1-based for the `every` counts

        def hit(every):
            return every and n % every == 0

        if faults.disconnect_after_frames and self.frames_emitted >= faults.disconnect_after_frames:
            self.connected = False
            return
        if hit(faults.counter_gap_every):
            self._frame_counter += 1
        frame_count = self._frame_counter
        self._frame_counter += 1
        self.frames_emitted += 1
        self._emitted_this_run += 1
        image = self.render(frame_count)
        timestamp = int((due - self._t_start) * self.spec.clock_hz)
        chunks = self.wire_chunks(image, frame_count, timestamp)
        if hit(faults.drop_chunk_every) and len(chunks) > 2:
            del chunks[len(chunks) // 2]
        if hit(faults.truncate_every) and len(chunks) > 1:
            chunks = chunks[: max(1, len(chunks) // 2)]
        if hit(faults.junk_every):
            self._data += bytes(range(256)) * (faults.junk_bytes // 256) + bytes(
                faults.junk_bytes % 256
            )
        for chunk in chunks:
            self._data += chunk
        if faults.trailer_bytes:
            self._data += bytes(faults.trailer_bytes)
        if hit(faults.stall_every):
            self._data_stalled = True

    # -- frame content -------------------------------------------------------------

    def render(self, frame_count: int) -> np.ndarray:
        """The (height, width) 10-bit image for frame ``frame_count`` at current settings.

        A moving Gaussian spot over a diagonal gradient, scaled by exposure and gain, plus
        noise. Row 0 starts with the frame counter's low 32 bits, one bit per 4-pixel block
        (bright = 1), so tests can check which frame they got.
        """
        width, height = self.image_size
        xs, ys, background, spot_tint, noise_bank = self._scene_cache()
        sw, sh = self.sensor
        phase = frame_count * 0.05
        cx = sw * (0.5 + 0.3 * np.cos(phase))
        cy = sh * (0.5 + 0.3 * np.sin(phase))
        sigma = min(sw, sh) / 12
        # separable Gaussian: an outer product of two 1D profiles is far cheaper than a 2D exp
        spot = np.outer(
            np.exp(-((ys - cy) ** 2) / (2 * sigma**2)), np.exp(-((xs - cx) ** 2) / (2 * sigma**2))
        )
        scene = spot * spot_tint + background
        scale = 600 * (self.exposure_us / 10_000) * 10 ** (self._gain_db() / 20)
        signal = scene * np.float32(scale) + np.float32(self.black_level)
        signal += noise_bank[frame_count % len(noise_bank)]
        image = np.clip(signal, 0, PIXEL_MAX).astype(np.uint16)
        bits = min(32, width // 4)
        for bit in range(bits):
            image[0, bit * 4 : bit * 4 + 4] = PIXEL_MAX if frame_count >> bit & 1 else 0
        return image

    def _scene_cache(self):
        """Per-geometry precomputation for :meth:`render`: pixel centre coordinates, the
        static background, the spot's color tint and a bank of noise frames."""
        x0, y0, _, _ = self.roi
        width, height = self.image_size
        key = (x0, y0, width, height, self.hbin, self.vbin, self.color)
        if self._grid_cache.get("key") != key:
            xs = (x0 + (np.arange(width) + 0.5) * self.hbin).astype(np.float32)
            ys = (y0 + (np.arange(height) + 0.5) * self.vbin).astype(np.float32)
            sw, sh = self.sensor
            background = 0.1 * (xs[None, :] / sw + ys[:, None] / sh)
            spot_tint = np.full((height, width), 0.8, dtype=np.float32)
            if self.color:
                # an RGGB mosaic: tint the spot red and the background blue, so a demosaic
                # has something to show
                red = (np.arange(height) % 2 == 0)[:, None] & (np.arange(width) % 2 == 0)
                blue = (np.arange(height) % 2 == 1)[:, None] & (np.arange(width) % 2 == 1)
                spot_tint *= np.where(red, 1.0, np.where(blue, 0.2, 0.4)).astype(np.float32)
                background = background * np.where(blue, 1.0, np.where(red, 0.2, 0.5))
            rng = np.random.default_rng(self._rng_seed)
            noise_bank = rng.normal(0, 2, size=(8, height, width)).astype(np.float32)
            self._grid_cache = {
                "key": key,
                "value": (xs, ys, background.astype(np.float32), spot_tint, noise_bank),
            }
        return self._grid_cache["value"]

    def _gain_db(self) -> float:
        table = self.spec.gain_table
        return (table[self.gain] if table else self.gain) / 10

    @staticmethod
    def frame_count_from_image(image: np.ndarray) -> int:
        """Decode the frame counter :meth:`render` stamps into row 0."""
        bits = min(32, image.shape[1] // 4)
        return sum(1 << b for b in range(bits) if image[0, b * 4] > PIXEL_MAX // 2)

    def wire_chunks(self, image: np.ndarray, frame_count: int, timestamp: int) -> list[bytes]:
        """``image`` as the list of 16 KiB chunks this family sends on DATA_IN_EP."""
        height, width = image.shape
        if self.spec.calibration_lines:
            calib = np.full((self.spec.calibration_lines, width), self.black_level, np.uint16)
            pixels = np.concatenate([calib, image])
        else:
            pixels = image
        if self.family == "zelux":
            header = FRAME_MAGIC + struct.pack("<II", frame_count, timestamp & 0xFFFFFFFF)
            header += bytes(4)
            content = header + pack_lsb_last(pixels)

            def marker(k):
                return header[:2] + bytes([k % 256]) + header[3:]

            marker_bytes = ZELUX_CHUNK_MARKER_BYTES
            padding = b"\x00"
        else:
            tags = [
                (TAG_FRAME_COUNT, frame_count),
                (TAG_PIXEL_CLOCK_HIGH, timestamp >> 32),
                (TAG_PIXEL_CLOCK_LOW, timestamp & 0xFFFFFFFF),
                (TAG_IMAGE_FORMAT, 0),
                (TAG_IMAGE_OFFSET, 0),
                (TAG_END, 0),
            ]
            header = FRAME_MAGIC + CS_SIGNATURE + bytes(4)
            header += b"".join(tag + struct.pack("<I", value) for tag, value in tags)
            footer = CS_FOOTER_SENTINEL + bytes(8) + struct.pack("<II", height, width)
            footer += CS_FOOTER_SENTINEL
            content = header + pack_contiguous(pixels) + footer

            def marker(k):
                return FRAME_MAGIC[:2] + bytes([k % 256, 0])

            marker_bytes = CS_CHUNK_MARKER_BYTES
            padding = b"\x00\x00\xff\xff"
        chunks = [content[:DATA_CHUNK_BYTES]]
        pos = DATA_CHUNK_BYTES
        payload = DATA_CHUNK_BYTES - marker_bytes
        while pos < len(content):
            chunks.append(marker(len(chunks)) + content[pos : pos + payload])
            pos += payload
        last = chunks[-1]
        if len(last) < DATA_CHUNK_BYTES:
            fill = DATA_CHUNK_BYTES - len(last)
            chunks[-1] = last + (padding * (fill // len(padding) + 1))[:fill]
        return chunks

    # -- connection ----------------------------------------------------------------

    def _check_connected(self) -> None:
        if not self.connected:
            raise usb.core.USBError(
                "No such device (it may have been disconnected)", errno=errno.ENODEV
            )

    def unplug(self) -> None:
        """Simulate pulling the cable: every further operation fails with ENODEV."""
        with self._lock:
            self.connected = False

    def __repr__(self) -> str:
        return f"FakeTsiDevice({self.family!r}, model={self.model!r}, serial={self.serial!r})"


class FakeTsiBus:
    """A set of fake devices, with a ``find`` that mimics ``usb.core.find`` for them."""

    def __init__(self, devices: list[FakeTsiDevice]) -> None:
        self.devices = list(devices)

    def find(self, find_all: bool = False, idVendor: int | None = None, **kwargs):
        found = [
            dev
            for dev in self.devices
            if dev.connected and (idVendor is None or dev.idVendor == idVendor)
        ]
        if find_all:
            return iter(found)
        return found[0] if found else None
